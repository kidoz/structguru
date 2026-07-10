use std::collections::VecDeque;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{BufWriter, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
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

#[derive(Default)]
pub struct NullSink;

impl StringSink for NullSink {
    fn write(&mut self, _message: String) -> Result<(), SinkError> {
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

pub struct WriteSink<W> {
    writer: W,
}

impl<W> WriteSink<W> {
    pub fn new(writer: W) -> Self {
        Self { writer }
    }

    pub fn into_inner(self) -> W {
        self.writer
    }
}

impl<W> StringSink for WriteSink<W>
where
    W: Write + Send,
{
    fn write(&mut self, message: String) -> Result<(), SinkError> {
        self.writer
            .write_all(message.as_bytes())
            .map_err(|err| SinkError::new(err.to_string()))
    }

    fn flush(&mut self) -> Result<(), SinkError> {
        self.writer
            .flush()
            .map_err(|err| SinkError::new(err.to_string()))
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

/// Rotating file sink mirroring CPython's `logging.handlers.RotatingFileHandler`.
///
/// Writes to `path`; when the cumulative written bytes reach `max_bytes`, the
/// file is rotated: the active file becomes `path.1`, `.1 → .2`, …, and the
/// oldest backup beyond `backup_count` is deleted. A fresh active file is then
/// opened. `max_bytes == 0` disables rotation. The size check happens before
/// each write, matching CPython's `RotatingFileHandler` behavior.
///
/// Like CPython, there is no cross-process lock — concurrent writers sharing one
/// path will corrupt rotation. Within a single writer thread this is sound.
pub struct RotatingFileSink {
    file: Option<BufWriter<File>>,
    path: PathBuf,
    max_bytes: usize,
    backup_count: usize,
    bytes_written: usize,
}

impl RotatingFileSink {
    /// Open `path` for append (created if missing). Parent directories are
    /// created if they do not exist (a small convenience over CPython).
    pub fn new(
        path: impl Into<PathBuf>,
        max_bytes: usize,
        backup_count: usize,
    ) -> Result<Self, SinkError> {
        let path = path.into();
        if let Some(parent) = path.parent()
            && !parent.as_os_str().is_empty()
        {
            fs::create_dir_all(parent).map_err(|err| SinkError::new(err.to_string()))?;
        }
        let file = open_append(&path)?;
        let bytes_written = file
            .get_ref()
            .metadata()
            .map_err(|err| SinkError::new(err.to_string()))?
            .len() as usize;
        Ok(Self {
            file: Some(file),
            path,
            max_bytes,
            backup_count,
            bytes_written,
        })
    }

    fn rotate(&mut self) -> Result<(), SinkError> {
        // Flush and drop the actual active handle before renaming. Windows does
        // not permit renaming a file while this process still has it open.
        if let Some(mut file) = self.file.take() {
            file.flush()
                .map_err(|err| SinkError::new(err.to_string()))?;
            drop(file);
        }

        if self.backup_count > 0 {
            // Delete the oldest backup if it exists.
            let oldest = backup_path(&self.path, self.backup_count);
            if oldest.exists() {
                fs::remove_file(&oldest).map_err(|err| SinkError::new(err.to_string()))?;
            }
            // Shift .i → .(i+1) for i in (backup_count-1 .. 1).
            for i in (1..self.backup_count).rev() {
                let from = backup_path(&self.path, i);
                let to = backup_path(&self.path, i + 1);
                if from.exists() {
                    fs::rename(&from, &to).map_err(|err| SinkError::new(err.to_string()))?;
                }
            }
            // Active → .1.
            let first = backup_path(&self.path, 1);
            fs::rename(&self.path, &first).map_err(|err| SinkError::new(err.to_string()))?;
        }

        // Open a fresh active file.
        self.file = Some(open_append(&self.path)?);
        self.bytes_written = 0;
        Ok(())
    }
}

impl StringSink for RotatingFileSink {
    fn write(&mut self, message: String) -> Result<(), SinkError> {
        let bytes = message.as_bytes();
        // Match RotatingFileHandler: when the active file is non-empty, roll
        // before writing the record that would reach or cross the threshold.
        if self.max_bytes > 0
            && self.backup_count > 0
            && self.bytes_written > 0
            && self.bytes_written.saturating_add(bytes.len()) >= self.max_bytes
        {
            self.rotate()?;
        }
        self.file
            .as_mut()
            .expect("rotating file sink always owns an active file")
            .write_all(bytes)
            .map_err(|err| SinkError::new(err.to_string()))?;
        self.bytes_written += bytes.len();
        Ok(())
    }

    fn flush(&mut self) -> Result<(), SinkError> {
        self.file
            .as_mut()
            .expect("rotating file sink always owns an active file")
            .flush()
            .map_err(|err| SinkError::new(err.to_string()))
    }
}

fn open_append(path: &Path) -> Result<BufWriter<File>, SinkError> {
    let file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|err| SinkError::new(err.to_string()))?;
    Ok(BufWriter::new(file))
}

fn backup_path(base: &Path, index: usize) -> PathBuf {
    let mut name = base
        .file_name()
        .map(|n| n.to_os_string())
        .unwrap_or_default();
    name.push(format!(".{index}"));
    base.with_file_name(name)
}

/// Fan-out sink: writes every message to all child sinks.
///
/// One child failing does not stop the others; `write` returns `Err` only when
/// every child fails (so the worker continues draining and counts partial
/// failures via `sink_errors`).
pub struct MultiSink {
    sinks: Vec<Box<dyn StringSink>>,
}

impl MultiSink {
    pub fn new(sinks: Vec<Box<dyn StringSink>>) -> Self {
        Self { sinks }
    }
}

impl StringSink for MultiSink {
    fn write(&mut self, message: String) -> Result<(), SinkError> {
        let mut last_err: Option<SinkError> = None;
        let mut ok_count = 0;
        for sink in &mut self.sinks {
            // Clone for all but the last sink to avoid an owned copy per child.
            match sink.write(message.clone()) {
                Ok(()) => ok_count += 1,
                Err(err) => last_err = Some(err),
            }
        }
        if ok_count == 0 {
            Err(last_err.unwrap_or_else(|| SinkError::new("multi-sink has no children")))
        } else {
            Ok(())
        }
    }

    fn flush(&mut self) -> Result<(), SinkError> {
        let mut last_err: Option<SinkError> = None;
        for sink in &mut self.sinks {
            if let Err(err) = sink.flush() {
                last_err = Some(err);
            }
        }
        // flush is best-effort; report the last error but don't suppress others
        last_err.map(Err).unwrap_or(Ok(()))
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
    space: Condvar,
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
    abandoned: AtomicBool,
}

impl StringWriter {
    pub fn new(maxsize: usize) -> Self {
        Self::with_paused(maxsize, false)
    }

    pub fn new_paused(maxsize: usize) -> Self {
        Self::with_paused(maxsize, true)
    }

    /// Writer that drains to the process's stdout (line-buffered by the OS).
    pub fn new_stdout(maxsize: usize) -> Self {
        Self::with_sink(
            maxsize,
            false,
            Box::new(WriteSink::new(std::io::stdout())),
            MemorySinkHandle::default(),
        )
    }

    pub fn new_null(maxsize: usize) -> Self {
        Self::with_sink(
            maxsize,
            false,
            Box::new(NullSink),
            MemorySinkHandle::default(),
        )
    }

    pub fn new_failing(maxsize: usize, fail_after: usize, paused: bool) -> Self {
        let (sink, handle) = FailingSink::new(fail_after);
        Self::with_sink(maxsize, paused, Box::new(sink), handle)
    }

    /// Writer that drains to a rotating file (append mode, size-based rotation).
    pub fn new_file(
        maxsize: usize,
        path: impl Into<PathBuf>,
        max_bytes: usize,
        backup_count: usize,
    ) -> Result<Self, SinkError> {
        let sink = RotatingFileSink::new(path, max_bytes, backup_count)?;
        Ok(Self::with_sink(
            maxsize,
            false,
            Box::new(sink),
            MemorySinkHandle::default(),
        ))
    }

    /// Writer that fans out to multiple sinks.
    pub fn new_multi(maxsize: usize, sinks: Vec<Box<dyn StringSink>>) -> Self {
        Self::with_sink(
            maxsize,
            false,
            Box::new(MultiSink::new(sinks)),
            MemorySinkHandle::default(),
        )
    }

    /// Writer wrapping a caller-built sink (for PyO3-layer composition).
    pub fn with_boxed_sink(maxsize: usize, sink: Box<dyn StringSink>) -> Self {
        Self::with_sink(maxsize, false, sink, MemorySinkHandle::default())
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
            space: Condvar::new(),
        });
        let worker_shared = Arc::clone(&shared);
        let worker = thread::spawn(move || worker_loop(worker_shared, sink));

        Self {
            shared,
            worker: Mutex::new(Some(worker)),
            memory_handle,
            abandoned: AtomicBool::new(false),
        }
    }

    /// Neutralize this writer after a `fork()`: the background thread does not
    /// exist in the child process, so `close`/`Drop` must **not** try to join
    /// it (that would hang). After this, `close`/`Drop` are no-ops and the
    /// (detached) `JoinHandle` is simply dropped. The caller replaces the writer
    /// with a fresh one in the child.
    pub fn abandon(&self) {
        self.abandoned.store(true, Ordering::Release);
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

    /// Enqueue, blocking the caller until space is available (backpressure).
    ///
    /// Returns `Err` only if the writer is closed. Callers should release the
    /// GIL around this so a full queue does not freeze other Python threads.
    pub fn enqueue_blocking(&self, message: String) -> Result<(), String> {
        let mut state = self.lock_state();
        loop {
            if state.closed {
                return Err(message);
            }
            if self.shared.maxsize == 0 || state.queue.len() < self.shared.maxsize {
                state.queue.push_back(message);
                state.counters.enqueued += 1;
                self.shared.available.notify_one();
                return Ok(());
            }
            state = self
                .shared
                .space
                .wait(state)
                .unwrap_or_else(|poisoned| poisoned.into_inner());
        }
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
        // Abandoned (post-fork) writers have no live worker thread to drain or
        // join, and their inherited state mutex may be permanently locked — never
        // touch it. Dropping the detached JoinHandle afterwards does not join.
        if self.abandoned.load(Ordering::Acquire) {
            return;
        }
        {
            let mut state = self.lock_state();
            state.closed = true;
            self.shared.available.notify_all();
            self.shared.space.notify_all(); // wake blocked producers so they see closed
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
                    shared.space.notify_one(); // a slot freed — wake a blocked producer
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
    use std::io::{Error, ErrorKind, Result as IoResult};

    #[derive(Clone, Default)]
    struct SharedWriter {
        contents: Arc<Mutex<Vec<u8>>>,
        fail_writes_after: Option<usize>,
        writes: Arc<Mutex<usize>>,
    }

    impl SharedWriter {
        fn contents(&self) -> Vec<u8> {
            self.contents
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .clone()
        }

        fn fail_writes_after(mut self, fail_writes_after: usize) -> Self {
            self.fail_writes_after = Some(fail_writes_after);
            self
        }
    }

    impl Write for SharedWriter {
        fn write(&mut self, bytes: &[u8]) -> IoResult<usize> {
            let mut writes = self
                .writes
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            if self.fail_writes_after.is_some_and(|limit| *writes >= limit) {
                *writes += 1;
                return Err(Error::new(ErrorKind::BrokenPipe, "test writer failed"));
            }

            *writes += 1;
            self.contents
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .extend_from_slice(bytes);
            Ok(bytes.len())
        }

        fn flush(&mut self) -> IoResult<()> {
            Ok(())
        }
    }

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
    fn blocking_enqueue_applies_backpressure_without_dropping() {
        let writer = StringWriter::new(2); // small bounded queue
        for i in 0..50 {
            writer.enqueue_blocking(format!("m{i}")).unwrap();
        }
        writer.flush();
        let metrics = writer.metrics();
        assert_eq!(metrics.enqueued, 50);
        assert_eq!(metrics.dropped, 0);
        assert_eq!(metrics.written, 50);
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
    fn abandoned_writer_close_is_a_noop() {
        let writer = StringWriter::new(0);
        writer.try_enqueue("x".to_owned()).unwrap();
        writer.abandon();
        // close() must not touch the state mutex or join the worker thread.
        writer.close();
        let metrics = writer.metrics();
        assert!(!metrics.closed, "abandoned close must not mark closed");
        // Drop at end of scope must also be a no-op (detaches, never joins).
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

    #[test]
    fn write_sink_writes_exact_message_bytes() {
        let output = SharedWriter::default();
        let writer = StringWriter::with_sink(
            0,
            false,
            Box::new(WriteSink::new(output.clone())),
            MemorySinkHandle::default(),
        );

        assert_eq!(writer.try_enqueue("first\n".to_owned()), Ok(()));
        assert_eq!(writer.try_enqueue("second".to_owned()), Ok(()));
        writer.flush();

        assert_eq!(output.contents(), b"first\nsecond");
        let metrics = writer.metrics();
        assert_eq!(metrics.enqueued, 2);
        assert_eq!(metrics.dequeued, 2);
        assert_eq!(metrics.written, 2);
        assert_eq!(metrics.sink_errors, 0);
    }

    #[test]
    fn write_sink_errors_are_counted_without_stopping_worker() {
        let output = SharedWriter::default().fail_writes_after(1);
        let writer = StringWriter::with_sink(
            0,
            false,
            Box::new(WriteSink::new(output.clone())),
            MemorySinkHandle::default(),
        );

        assert_eq!(writer.try_enqueue("first".to_owned()), Ok(()));
        assert_eq!(writer.try_enqueue("second".to_owned()), Ok(()));
        assert_eq!(writer.try_enqueue("third".to_owned()), Ok(()));
        writer.flush();

        assert_eq!(output.contents(), b"first");
        let metrics = writer.metrics();
        assert_eq!(metrics.enqueued, 3);
        assert_eq!(metrics.dequeued, 3);
        assert_eq!(metrics.written, 1);
        assert_eq!(metrics.sink_errors, 2);
        assert_eq!(metrics.depth, 0);
    }

    // -- rotating file sink --------------------------------------------------

    fn temp_log_path(name: &str) -> PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!("structguru-test-{}-{name}", std::process::id()));
        // Clean any leftovers from a prior run.
        for i in 0..=6 {
            let _ = std::fs::remove_file(backup_path(&p, i));
        }
        p
    }

    fn cleanup(path: &Path) {
        for i in 0..=6 {
            let _ = std::fs::remove_file(backup_path(path, i));
        }
    }

    #[test]
    fn rotating_file_writes_and_rotates_at_threshold() {
        let path = temp_log_path("rotate-basic");
        // Each write is 10 bytes; rotate after 25.
        let mut sink = RotatingFileSink::new(&path, 25, 3).unwrap();
        sink.write("0123456789".to_owned()).unwrap(); // 10 bytes, no rotate
        sink.flush().unwrap();
        assert!(!backup_path(&path, 1).exists());
        sink.write("0123456789".to_owned()).unwrap(); // 20 bytes, no rotate
        sink.write("0123456789".to_owned()).unwrap(); // 30 bytes → rotate
        sink.flush().unwrap();
        assert!(
            backup_path(&path, 1).exists(),
            ".1 should exist after rotation"
        );
        cleanup(&path);
    }

    #[test]
    fn rotating_file_shifts_backups_and_drops_oldest() {
        let path = temp_log_path("rotate-shift");
        let mut sink = RotatingFileSink::new(&path, 10, 2).unwrap();
        // Each write is 10 bytes → rotate every write.
        for _ in 0..4 {
            sink.write("0123456789".to_owned()).unwrap();
        }
        sink.flush().unwrap();
        assert!(backup_path(&path, 1).exists(), ".1 exists");
        assert!(backup_path(&path, 2).exists(), ".2 exists");
        assert!(
            !backup_path(&path, 3).exists(),
            ".3 dropped (backup_count=2)"
        );
        cleanup(&path);
    }

    #[test]
    fn rotating_file_max_bytes_zero_never_rotates() {
        let path = temp_log_path("rotate-none");
        let mut sink = RotatingFileSink::new(&path, 0, 5).unwrap();
        for _ in 0..20 {
            sink.write("data-line\n".to_owned()).unwrap();
        }
        sink.flush().unwrap();
        assert!(
            !backup_path(&path, 1).exists(),
            "no rotation when max_bytes=0"
        );
        cleanup(&path);
    }

    #[test]
    fn rotating_file_reopens_after_rotation() {
        let path = temp_log_path("rotate-reopen");
        let mut sink = RotatingFileSink::new(&path, 10, 3).unwrap();
        sink.write("0123456789".to_owned()).unwrap(); // rotate
        sink.write("after".to_owned()).unwrap(); // lands in fresh active file
        sink.flush().unwrap();

        let active = std::fs::read_to_string(&path).unwrap();
        assert!(
            active.contains("after"),
            "post-rotation write in active file"
        );
        assert!(
            !active.contains("0123456789"),
            "rotated content moved to .1"
        );
        cleanup(&path);
    }

    #[test]
    fn rotating_file_accounts_for_existing_bytes() {
        let path = temp_log_path("rotate-existing");
        std::fs::write(&path, "existing").unwrap();
        let mut sink = RotatingFileSink::new(&path, 10, 2).unwrap();
        sink.write("new".to_owned()).unwrap();
        sink.flush().unwrap();

        assert_eq!(
            std::fs::read_to_string(backup_path(&path, 1)).unwrap(),
            "existing"
        );
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "new");
        cleanup(&path);
    }

    #[cfg(windows)]
    #[test]
    fn rotating_file_closes_active_handle_before_windows_rename() {
        let path = temp_log_path("rotate-windows-handle");
        let mut sink = RotatingFileSink::new(&path, 8, 1).unwrap();
        sink.write("first".to_owned()).unwrap();
        sink.write("second".to_owned()).unwrap();
        sink.flush().unwrap();

        assert_eq!(
            std::fs::read_to_string(backup_path(&path, 1)).unwrap(),
            "first"
        );
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "second");
        cleanup(&path);
    }

    // -- multi sink ----------------------------------------------------------

    #[test]
    fn multi_sink_fans_out_to_all_sinks() {
        let (a, ha) = MemorySink::new();
        let (b, hb) = MemorySink::new();
        let mut multi = MultiSink::new(vec![Box::new(a), Box::new(b)]);
        multi.write("hello".to_owned()).unwrap();

        assert_eq!(ha.messages(), vec!["hello".to_owned()]);
        assert_eq!(hb.messages(), vec!["hello".to_owned()]);
    }

    #[test]
    fn multi_sink_continues_if_one_sink_fails() {
        let (failing, _hf) = FailingSink::new(0); // fails immediately
        let (mem, hm) = MemorySink::new();
        let mut multi = MultiSink::new(vec![Box::new(failing), Box::new(mem)]);
        let result = multi.write("survives".to_owned()).unwrap();

        assert_eq!(result, ());
        assert_eq!(hm.messages(), vec!["survives".to_owned()]);
    }

    #[test]
    fn multi_sink_errors_only_when_all_fail() {
        let (a, _) = FailingSink::new(0);
        let (b, _) = FailingSink::new(0);
        let mut multi = MultiSink::new(vec![Box::new(a), Box::new(b)]);
        let result = multi.write("nothing".to_owned());
        assert!(result.is_err(), "Err when every child fails");
    }
}
