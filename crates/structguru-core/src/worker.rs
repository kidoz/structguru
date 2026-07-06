use std::collections::VecDeque;
use std::fmt;
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::thread::{self, JoinHandle};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SinkError {
    message: String,
}

impl SinkError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl fmt::Display for SinkError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

impl std::error::Error for SinkError {}

pub trait StringSink: Send {
    fn write(&mut self, message: String) -> Result<(), SinkError>;

    fn flush(&mut self) -> Result<(), SinkError> {
        Ok(())
    }
}

#[derive(Clone, Default)]
pub struct MemorySinkHandle {
    messages: Arc<Mutex<Vec<String>>>,
}

impl MemorySinkHandle {
    pub fn messages(&self) -> Vec<String> {
        self.messages
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .clone()
    }
}

pub struct MemorySink {
    handle: MemorySinkHandle,
}

impl MemorySink {
    pub fn new() -> (Self, MemorySinkHandle) {
        let handle = MemorySinkHandle::default();
        (
            Self {
                handle: handle.clone(),
            },
            handle,
        )
    }
}

impl StringSink for MemorySink {
    fn write(&mut self, message: String) -> Result<(), SinkError> {
        self.handle
            .messages
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push(message);
        Ok(())
    }
}

pub struct FailingSink {
    handle: MemorySinkHandle,
    fail_after: usize,
    attempts: usize,
}

impl FailingSink {
    pub fn new(fail_after: usize) -> (Self, MemorySinkHandle) {
        let handle = MemorySinkHandle::default();
        (
            Self {
                handle: handle.clone(),
                fail_after,
                attempts: 0,
            },
            handle,
        )
    }
}

impl StringSink for FailingSink {
    fn write(&mut self, message: String) -> Result<(), SinkError> {
        if self.attempts >= self.fail_after {
            self.attempts += 1;
            return Err(SinkError::new("test sink write failed"));
        }

        self.attempts += 1;
        self.handle
            .messages
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .push(message);
        Ok(())
    }
}

/// Point-in-time view of native writer state.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct WorkerMetrics {
    pub enqueued: u64,
    pub dropped: u64,
    pub dequeued: u64,
    pub written: u64,
    pub sink_errors: u64,
    pub depth: usize,
    pub maxsize: usize,
    pub in_flight: usize,
    pub closed: bool,
    pub worker_done: bool,
    pub paused: bool,
}

#[derive(Default)]
struct WorkerCounters {
    enqueued: u64,
    dropped: u64,
    dequeued: u64,
    written: u64,
    sink_errors: u64,
}

struct WorkerState {
    queue: VecDeque<String>,
    counters: WorkerCounters,
    in_flight: usize,
    closed: bool,
    worker_done: bool,
    paused: bool,
}

struct WorkerShared {
    maxsize: usize,
    state: Mutex<WorkerState>,
    available: Condvar,
    drained: Condvar,
}

/// Private native writer skeleton for already-rendered string records.
///
/// This intentionally writes into an in-memory sink for now. It proves the
/// transport semantics we need before real stdout/file sinks are attached:
/// bounded nonblocking enqueue, background drain, flush, close, and metrics.
pub struct StringWriter {
    shared: Arc<WorkerShared>,
    worker: Mutex<Option<JoinHandle<()>>>,
    memory_handle: MemorySinkHandle,
}

impl StringWriter {
    pub fn new(maxsize: usize) -> Self {
        Self::with_paused(maxsize, false)
    }

    pub fn new_paused(maxsize: usize) -> Self {
        Self::with_paused(maxsize, true)
    }

    pub fn new_failing(maxsize: usize, fail_after: usize, paused: bool) -> Self {
        let (sink, handle) = FailingSink::new(fail_after);
        Self::with_sink(maxsize, paused, Box::new(sink), handle)
    }

    fn with_paused(maxsize: usize, paused: bool) -> Self {
        let (sink, handle) = MemorySink::new();
        Self::with_sink(maxsize, paused, Box::new(sink), handle)
    }

    fn with_sink(
        maxsize: usize,
        paused: bool,
        sink: Box<dyn StringSink>,
        memory_handle: MemorySinkHandle,
    ) -> Self {
        let shared = Arc::new(WorkerShared {
            maxsize,
            state: Mutex::new(WorkerState {
                queue: VecDeque::new(),
                counters: WorkerCounters::default(),
                in_flight: 0,
                closed: false,
                worker_done: false,
                paused,
            }),
            available: Condvar::new(),
            drained: Condvar::new(),
        });
        let worker_shared = Arc::clone(&shared);
        let worker = thread::spawn(move || worker_loop(worker_shared, sink));

        Self {
            shared,
            worker: Mutex::new(Some(worker)),
            memory_handle,
        }
    }

    pub fn maxsize(&self) -> usize {
        self.shared.maxsize
    }

    pub fn try_enqueue(&self, message: String) -> Result<(), String> {
        let mut state = self.lock_state();
        if state.closed {
            return Err(message);
        }
        if self.shared.maxsize > 0 && state.queue.len() >= self.shared.maxsize {
            state.counters.dropped += 1;
            return Err(message);
        }

        state.queue.push_back(message);
        state.counters.enqueued += 1;
        self.shared.available.notify_one();
        Ok(())
    }

    pub fn flush(&self) {
        let mut state = self.lock_state();
        while !state.queue.is_empty() || state.in_flight > 0 {
            state = self
                .shared
                .drained
                .wait(state)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
        }
    }

    pub fn close(&self) {
        {
            let mut state = self.lock_state();
            state.closed = true;
            self.shared.available.notify_all();
        }

        let worker = self
            .worker
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take();
        if let Some(worker) = worker {
            let _ = worker.join();
        }
    }

    pub fn resume(&self) {
        let mut state = self.lock_state();
        state.paused = false;
        self.shared.available.notify_all();
    }

    pub fn messages(&self) -> Vec<String> {
        self.memory_handle.messages()
    }

    pub fn metrics(&self) -> WorkerMetrics {
        let state = self.lock_state();
        WorkerMetrics {
            enqueued: state.counters.enqueued,
            dropped: state.counters.dropped,
            dequeued: state.counters.dequeued,
            written: state.counters.written,
            sink_errors: state.counters.sink_errors,
            depth: state.queue.len(),
            maxsize: self.shared.maxsize,
            in_flight: state.in_flight,
            closed: state.closed,
            worker_done: state.worker_done,
            paused: state.paused,
        }
    }

    fn lock_state(&self) -> MutexGuard<'_, WorkerState> {
        self.shared
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

impl Drop for StringWriter {
    fn drop(&mut self) {
        self.close();
    }
}

fn worker_loop(shared: Arc<WorkerShared>, mut sink: Box<dyn StringSink>) {
    loop {
        let message = {
            let mut state = shared
                .state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            loop {
                while state.paused && !state.closed {
                    state = shared
                        .available
                        .wait(state)
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                }
                if let Some(message) = state.queue.pop_front() {
                    state.counters.dequeued += 1;
                    state.in_flight += 1;
                    break message;
                }
                if state.closed {
                    drop(state);
                    let flush_result = sink.flush();
                    let mut state = shared
                        .state
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner());
                    if flush_result.is_err() {
                        state.counters.sink_errors += 1;
                    }
                    state.worker_done = true;
                    shared.drained.notify_all();
                    return;
                }
                state = shared
                    .available
                    .wait(state)
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
            }
        };

        let result = sink.write(message);
        let mut state = shared
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        match result {
            Ok(()) => {
                state.counters.written += 1;
            }
            Err(_) => {
                state.counters.sink_errors += 1;
            }
        }
        state.in_flight -= 1;
        let should_flush = state.queue.is_empty() && state.in_flight == 0;
        drop(state);

        if should_flush {
            let flush_result = sink.flush();
            let mut state = shared
                .state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if flush_result.is_err() {
                state.counters.sink_errors += 1;
            }
            shared.drained.notify_all();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn writer_drains_messages_in_order() {
        let writer = StringWriter::new(0);

        assert_eq!(writer.try_enqueue("first".to_owned()), Ok(()));
        assert_eq!(writer.try_enqueue("second".to_owned()), Ok(()));
        writer.flush();

        assert_eq!(writer.messages(), vec!["first", "second"]);
        let metrics = writer.metrics();
        assert_eq!(metrics.enqueued, 2);
        assert_eq!(metrics.dequeued, 2);
        assert_eq!(metrics.written, 2);
        assert_eq!(metrics.sink_errors, 0);
        assert_eq!(metrics.depth, 0);
    }

    #[test]
    fn bounded_writer_drops_when_full() {
        let writer = StringWriter::new_paused(1);

        assert_eq!(writer.try_enqueue("kept".to_owned()), Ok(()));
        assert_eq!(
            writer.try_enqueue("dropped".to_owned()),
            Err("dropped".to_owned())
        );

        let metrics = writer.metrics();
        assert_eq!(metrics.enqueued, 1);
        assert_eq!(metrics.dropped, 1);
        assert_eq!(metrics.depth, 1);

        writer.close();
        assert_eq!(writer.messages(), vec!["kept"]);
    }

    #[test]
    fn close_is_idempotent_and_rejects_new_messages() {
        let writer = StringWriter::new(0);

        assert_eq!(writer.try_enqueue("before close".to_owned()), Ok(()));
        writer.close();
        writer.close();

        assert_eq!(
            writer.try_enqueue("after close".to_owned()),
            Err("after close".to_owned()),
        );
        assert_eq!(writer.messages(), vec!["before close"]);
        let metrics = writer.metrics();
        assert!(metrics.closed);
        assert!(metrics.worker_done);
    }

    #[test]
    fn sink_errors_are_counted_without_stopping_worker() {
        let writer = StringWriter::new_failing(0, 1, false);

        assert_eq!(writer.try_enqueue("first".to_owned()), Ok(()));
        assert_eq!(writer.try_enqueue("second".to_owned()), Ok(()));
        assert_eq!(writer.try_enqueue("third".to_owned()), Ok(()));
        writer.flush();

        assert_eq!(writer.messages(), vec!["first"]);
        let metrics = writer.metrics();
        assert_eq!(metrics.enqueued, 3);
        assert_eq!(metrics.dequeued, 3);
        assert_eq!(metrics.written, 1);
        assert_eq!(metrics.sink_errors, 2);
        assert_eq!(metrics.depth, 0);
    }
}
