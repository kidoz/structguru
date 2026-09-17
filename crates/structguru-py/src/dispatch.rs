//! Bounded background dispatch for Python callable logging sinks.
//!
//! Port of the former `structguru._native_dispatch` module. The sink registry,
//! the per-generation queue and its worker thread, delivery accounting, and
//! deferred finalizers all live here; Python keeps only the callback-scope
//! helper names. The structure deliberately mirrors the Python original — one
//! registry lock, one lock per queue generation, the same producer leases and
//! generation rules — so the concurrency regressions pinned by
//! `tests/test_native_dispatch.py` keep their meaning.
//!
//! Callbacks are `Py<PyAny>` handles owned by this module for the lifetime of a
//! registration. The worker thread attaches to the interpreter for each record
//! it delivers; every wait a producer or lifecycle caller performs happens with
//! the interpreter detached so the worker can always make progress.

use std::cell::Cell;
use std::collections::{BTreeMap, HashMap, VecDeque};
use std::ffi::CString;
use std::sync::atomic::{AtomicBool, AtomicPtr, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError, Weak};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use pyo3::exceptions::PyUserWarning;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

thread_local! {
    /// Nesting depth of sink callbacks on this thread: the dispatch worker for
    /// its whole lifetime, raw stdlib deliveries (`structguru.core`) per call.
    static CALLBACK_DEPTH: Cell<u32> = const { Cell::new(0) };
}

/// True while the current thread is inside a sink callback.
#[pyfunction]
pub fn in_callback() -> bool {
    CALLBACK_DEPTH.with(|depth| depth.get() > 0)
}

/// Mark the current thread as inside a sink callback until `exit_callback`.
#[pyfunction]
pub fn enter_callback() {
    CALLBACK_DEPTH.with(|depth| depth.set(depth.get() + 1));
}

/// Undo one `enter_callback`.
#[pyfunction]
pub fn exit_callback() {
    CALLBACK_DEPTH.with(|depth| depth.set(depth.get().saturating_sub(1)));
}

/// One registered sink. Configured sinks carry non-positive tokens (`-index`),
/// runtime sinks positive ones; only runtime sinks take part in delivery
/// accounting, since only they can be removed one at a time.
struct Sink {
    token: i64,
    callback: Py<PyAny>,
    min_level: i64,
    level_callback: Option<Py<PyAny>>,
}

impl Sink {
    fn clone_ref(&self, py: Python<'_>) -> Sink {
        Sink {
            token: self.token,
            callback: self.callback.clone_ref(py),
            min_level: self.min_level,
            level_callback: self.level_callback.as_ref().map(|cb| cb.clone_ref(py)),
        }
    }
}

/// A rendered line with the sinks selected for it at enqueue time.
struct Record {
    line: String,
    sinks: Arc<[Sink]>,
    level: i64,
}

#[derive(Default)]
struct ChannelState {
    queue: VecDeque<Record>,
    unfinished_tasks: usize,
    accepting: bool,
    /// Producers that reserved delivery but have not yet finished inserting.
    producers: usize,
    /// Producers waiting for a free slot in block mode (observability only).
    blocked_producers: usize,
    /// Leases handed out by `reserve` and leases released, in total.
    leases_issued: u64,
    leases_settled: u64,
    /// Records appended and records whose delivery finished, in total.
    accepted: u64,
    finished: u64,
    /// Flushers currently waiting; they need a wake-up on every step.
    flush_waiters: usize,
    /// Set when a flush gave up on a worker that stopped making progress.
    abandoned: bool,
}

/// One queue generation with producer-aware shutdown semantics.
///
/// One lock guards the queue, producer leases, and the accepting flag.
/// Producers reserve delivery while holding the registry lock, then wait for
/// queue space outside it. Retirement drains all outstanding reservations.
///
/// Fork: a child never touches an inherited generation. `fork_child` builds a
/// fresh dispatcher, and the vanished worker's `Arc` clone is never released,
/// so the inherited channel cannot drop and its stale thread handle is neither
/// joined nor detached (glibc may reuse that thread identity for a live thread).
struct Channel {
    maxsize: usize,
    state: Mutex<ChannelState>,
    /// State changes producers, flushers, and lifecycle callers wait on:
    /// retirement, a freed slot, the last lease released, the last task done.
    condition: Condvar,
    /// Only the worker waits here, so a producer's notify can never wake a
    /// flusher instead of the worker.
    not_empty: Condvar,
    thread: Mutex<Option<JoinHandle<()>>>,
}

/// Records the worker takes from the queue per lock acquisition.
const DELIVERY_BATCH: usize = 64;

/// Seconds from Python into a stall bound; `None` waits without limit.
fn stall_duration(seconds: Option<f64>) -> Option<Duration> {
    seconds.map(|s| Duration::try_from_secs_f64(s.max(0.0)).unwrap_or(Duration::MAX))
}

/// Result of offering a record to a generation without waiting.
enum Offer {
    Accepted,
    /// Full in drop mode; the lease has been released.
    Full,
    /// Full in block mode; the lease is still held and the record returned.
    WouldBlock(Record),
}

impl Channel {
    fn start(maxsize: usize, dispatcher: Weak<DispatcherInner>) -> Arc<Channel> {
        let channel = Arc::new(Channel {
            maxsize,
            state: Mutex::new(ChannelState {
                accepting: true,
                ..ChannelState::default()
            }),
            condition: Condvar::new(),
            not_empty: Condvar::new(),
            thread: Mutex::new(None),
        });
        let worker = Arc::clone(&channel);
        let handle = std::thread::Builder::new()
            .name("structguru-callable-sinks".to_owned())
            .spawn(move || worker.run(dispatcher))
            .expect("spawn callable sink dispatch worker");
        *channel.lock_thread() = Some(handle);
        channel
    }

    fn lock(&self) -> MutexGuard<'_, ChannelState> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn lock_thread(&self) -> MutexGuard<'_, Option<JoinHandle<()>>> {
        self.thread.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn is_full(&self, state: &ChannelState) -> bool {
        self.maxsize > 0 && state.queue.len() >= self.maxsize
    }

    fn append(&self, state: &mut ChannelState, record: Record) {
        state.queue.push_back(record);
        state.unfinished_tasks += 1;
        state.accepted += 1;
        self.not_empty.notify_one();
    }

    fn release_lease(&self, state: &mut ChannelState) {
        state.producers -= 1;
        state.leases_settled += 1;
        if state.producers == 0 || state.flush_waiters > 0 {
            self.condition.notify_all();
        }
        if state.producers == 0 {
            self.not_empty.notify_all();
        }
    }

    fn is_abandoned(&self) -> bool {
        self.lock().abandoned
    }

    /// Wait until `done(state)`, or give up once `progress(state)` has not
    /// advanced for `stall`. Returns the guard and whether `done` was reached.
    fn wait_until<'a>(
        &'a self,
        mut state: MutexGuard<'a, ChannelState>,
        stall: Option<Duration>,
        done: impl Fn(&ChannelState) -> bool,
        progress: impl Fn(&ChannelState) -> u64,
    ) -> (MutexGuard<'a, ChannelState>, bool) {
        let mut last = progress(&state);
        let mut deadline = stall.map(|limit| Instant::now() + limit);
        while !done(&state) {
            match (stall, deadline) {
                (Some(limit), Some(at)) => {
                    let now = Instant::now();
                    if now >= at {
                        return (state, false);
                    }
                    let (next, _) = self
                        .condition
                        .wait_timeout(state, at - now)
                        .unwrap_or_else(PoisonError::into_inner);
                    state = next;
                    let current = progress(&state);
                    if current != last {
                        last = current;
                        deadline = Some(Instant::now() + limit);
                    }
                }
                _ => {
                    state = self
                        .condition
                        .wait(state)
                        .unwrap_or_else(PoisonError::into_inner);
                }
            }
        }
        (state, true)
    }

    /// Reserve one producer before a lifecycle transition can close the queue.
    fn reserve(&self) -> bool {
        let mut state = self.lock();
        if !state.accepting {
            return false;
        }
        state.producers += 1;
        state.leases_issued += 1;
        true
    }

    /// Insert a reserved record if there is room, releasing the lease unless
    /// the caller must go on to wait (`Offer::WouldBlock`).
    fn offer(&self, record: Record, blocking: bool) -> Offer {
        let mut state = self.lock();
        if self.is_full(&state) {
            if !blocking {
                self.release_lease(&mut state);
                return Offer::Full;
            }
            return Offer::WouldBlock(record);
        }
        self.append(&mut state, record);
        self.release_lease(&mut state);
        Offer::Accepted
    }

    /// Wait for a slot, insert, and release the lease. Callers detach from the
    /// interpreter first: the worker needs it to free a slot. Returns `false`
    /// when the generation was abandoned while waiting: the record is dropped.
    fn put_blocking(&self, record: Record) -> bool {
        let mut state = self.lock();
        state.blocked_producers += 1;
        while self.is_full(&state) && !state.abandoned {
            state = self
                .condition
                .wait(state)
                .unwrap_or_else(PoisonError::into_inner);
        }
        state.blocked_producers -= 1;
        if state.abandoned {
            self.release_lease(&mut state);
            return false;
        }
        self.append(&mut state, record);
        self.release_lease(&mut state);
        true
    }

    /// Wait for producers that reserved delivery before this call, then for
    /// every record accepted by then. Records other threads enqueue afterwards
    /// are not waited for, so a flush under sustained load is bounded by the
    /// queue instead of waiting for a moment when it happens to be empty.
    ///
    /// With `stall`, a worker that makes no progress for that long (a sink that
    /// never returns) is abandoned: the generation stops accepting, blocked
    /// producers drop their records, and the number of accepted records left
    /// undelivered is returned. Returns at once inside a sink callback: a
    /// callback cannot wait for its own worker. Callers detach from the
    /// interpreter first.
    fn flush(&self, stall: Option<Duration>) -> usize {
        if in_callback() {
            return 0;
        }
        let (retired, abandoned) = {
            let mut state = self.lock();
            if state.abandoned {
                return 0;
            }
            state.flush_waiters += 1;
            let lease_target = state.leases_issued;
            let (next, mut drained) = self.wait_until(
                state,
                stall,
                |state| state.leases_settled >= lease_target,
                |state| state.leases_settled,
            );
            state = next;
            if drained {
                let accepted_target = state.accepted;
                let (next, finished) = self.wait_until(
                    state,
                    stall,
                    |state| state.finished >= accepted_target,
                    |state| state.finished,
                );
                state = next;
                drained = finished;
            }
            state.flush_waiters -= 1;
            if drained {
                (!state.accepting, 0)
            } else {
                state.abandoned = true;
                state.accepting = false;
                let abandoned = state.unfinished_tasks;
                self.condition.notify_all();
                self.not_empty.notify_all();
                (false, abandoned)
            }
        };
        if retired {
            self.join();
        }
        abandoned
    }

    /// Reject new producers without waiting for callbacks or queue space.
    fn retire(&self) {
        let mut state = self.lock();
        state.accepting = false;
        self.condition.notify_all();
        self.not_empty.notify_all();
    }

    /// Reject new producers and stop after every accepted producer finishes.
    /// Both `drain` values wait behind every accepted record, as the Python
    /// dispatcher always did; see `flush` for `stall` and the return value.
    fn close(&self, drain: bool, stall: Option<Duration>) -> usize {
        let _ = drain;
        self.retire();
        if in_callback() {
            return 0;
        }
        self.flush(stall)
    }

    fn join(&self) {
        if self.is_abandoned() {
            // The worker is stuck in a sink; leave the handle to detach on drop.
            return;
        }
        let handle = self.lock_thread().take();
        if let Some(handle) = handle {
            let _ = handle.join();
        }
    }

    fn is_alive(&self) -> bool {
        self.lock_thread()
            .as_ref()
            .is_some_and(|handle| !handle.is_finished())
    }

    /// Block until this generation is retired or `timeout` elapses.
    fn wait_retired(&self, timeout: Duration) -> bool {
        let state = self.lock();
        let (state, _) = self
            .condition
            .wait_timeout_while(state, timeout, |state| state.accepting)
            .unwrap_or_else(PoisonError::into_inner);
        !state.accepting
    }

    fn run(self: Arc<Self>, dispatcher: Weak<DispatcherInner>) {
        // Every callback this thread runs is nested.
        CALLBACK_DEPTH.with(|depth| depth.set(1));
        let mut batch: Vec<Record> = Vec::with_capacity(DELIVERY_BATCH);
        loop {
            {
                let mut state = self.lock();
                loop {
                    if !state.queue.is_empty() {
                        break;
                    }
                    // A leased producer may still append after retirement, so
                    // the worker only stops once the queue is empty and no
                    // lease is held.
                    if !state.accepting && state.producers == 0 {
                        return;
                    }
                    state = self
                        .not_empty
                        .wait(state)
                        .unwrap_or_else(PoisonError::into_inner);
                }
            }
            // Stay attached while work remains, as a Python worker thread holds
            // the interpreter between switch intervals. Attaching per record
            // would hand the interpreter back and forth with a producer blocked
            // on a full queue for every slot freed. Callbacks run Python code,
            // so the interpreter's own switching still applies inside them.
            Python::attach(|py| {
                loop {
                    {
                        let mut state = self.lock();
                        let count = state.queue.len().min(DELIVERY_BATCH);
                        if count == 0 {
                            break;
                        }
                        batch.extend(state.queue.drain(..count));
                        if state.producers > 0 {
                            self.condition.notify_all(); // producers may wait for these slots
                        }
                    }
                    for record in batch.drain(..) {
                        self.deliver(py, &dispatcher, &record);
                        drop(record);
                        let mut state = self.lock();
                        state.unfinished_tasks -= 1;
                        state.finished += 1;
                        if state.unfinished_tasks == 0 || state.flush_waiters > 0 {
                            self.condition.notify_all();
                        }
                    }
                }
            });
        }
    }

    fn deliver(&self, py: Python<'_>, dispatcher: &Weak<DispatcherInner>, record: &Record) {
        let line = PyString::new(py, &record.line);
        for sink in record.sinks.iter() {
            // Worker callbacks cannot interrupt the caller: every failure,
            // `BaseException` subclasses included, is dropped.
            let _ = match &sink.level_callback {
                Some(callback) => callback.bind(py).call1((&line, record.level)),
                None => sink.callback.bind(py).call1((&line,)),
            };
        }
        // Account for the delivery before the record counts as done, so a
        // flush() observes every consequence of delivery.
        if let Some(dispatcher) = dispatcher.upgrade() {
            dispatcher.settle(py, &record.sinks);
        }
    }
}

/// Python handle to one queue generation, for lifecycle tests and metrics.
#[pyclass(name = "DispatchChannel", frozen)]
pub struct DispatchChannel {
    inner: Arc<Channel>,
}

#[pymethods]
impl DispatchChannel {
    /// Whether the generation still accepts new producers.
    #[getter]
    fn accepting(&self) -> bool {
        self.inner.lock().accepting
    }

    /// Records queued and not yet taken by the worker.
    #[getter]
    fn qsize(&self) -> usize {
        self.inner.lock().queue.len()
    }

    /// Records accepted whose delivery has not completed.
    #[getter]
    fn unfinished_tasks(&self) -> usize {
        self.inner.lock().unfinished_tasks
    }

    /// Producers currently waiting for a free slot in block mode.
    #[getter]
    fn blocked_producers(&self) -> usize {
        self.inner.lock().blocked_producers
    }

    /// Whether the worker thread is still running.
    fn is_alive(&self) -> bool {
        self.inner.is_alive()
    }

    /// Wait up to `timeout` seconds for the generation to be retired.
    fn wait_retired(&self, py: Python<'_>, timeout: f64) -> bool {
        let timeout = Duration::try_from_secs_f64(timeout.max(0.0)).unwrap_or(Duration::MAX);
        py.detach(|| self.inner.wait_retired(timeout))
    }

    /// Reject new producers without waiting.
    fn retire(&self) {
        self.inner.retire();
    }

    /// Wait for producers and queued work; no-op inside a sink callback.
    fn flush(&self, py: Python<'_>) {
        py.detach(|| {
            self.inner.flush(None);
        });
    }

    /// Retire, optionally drain, and join the worker; idempotent.
    #[pyo3(signature = (*, drain))]
    fn close(&self, py: Python<'_>, drain: bool) {
        py.detach(|| {
            self.inner.close(drain, None);
        });
    }
}

/// Registry and accounting guarded by the dispatcher lock.
struct Registry {
    configured: Vec<Sink>,
    /// Token order is insertion order: tokens only ever increase.
    runtime: BTreeMap<i64, Sink>,
    next_token: i64,
    maxsize: usize,
    channel: Option<Py<DispatchChannel>>,
    /// Retired generations remain visible until drained, so concurrent
    /// remove/flush/shutdown cannot overlook callbacks still using a sink.
    channels: Vec<Py<DispatchChannel>>,
    /// Per-level eligibility cache, invalidated on every registration change.
    eligible: HashMap<i64, Arc<[Sink]>>,
    /// Deliveries that selected a runtime sink and have not finished, and the
    /// finalizers waiting for that count to reach zero (see `remove`).
    outstanding: HashMap<i64, usize>,
    finalizers: HashMap<i64, Py<PyAny>>,
}

impl Registry {
    fn has_sinks(&self) -> bool {
        !self.configured.is_empty() || !self.runtime.is_empty()
    }

    fn live_channels(&self) -> Vec<Arc<Channel>> {
        self.channels
            .iter()
            .map(|channel| Arc::clone(&channel.get().inner))
            .collect()
    }
}

/// Lock-free copy of the registration state for a forked child.
///
/// The parent may fork while another thread holds the registry lock; the
/// child can never acquire that lock. Every registration change republishes
/// this snapshot, and `fork_child` rebuilds a dispatcher from it instead.
struct Snapshot {
    configured: Vec<Sink>,
    runtime: Vec<Sink>,
    next_token: i64,
    maxsize: usize,
    finalizers: Vec<Py<PyAny>>,
}

struct DispatcherInner {
    registry: Mutex<Registry>,
    /// Set while a generation is active; read lock-free on the hot path.
    active: AtomicBool,
    dropped: AtomicU64,
    snapshot: AtomicPtr<Snapshot>,
}

impl DispatcherInner {
    fn lock_registry(&self) -> MutexGuard<'_, Registry> {
        self.registry.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Replace the fork snapshot with the registry's current registrations.
    fn publish(&self, py: Python<'_>, registry: &Registry) {
        let snapshot = Box::new(Snapshot {
            configured: registry
                .configured
                .iter()
                .map(|s| s.clone_ref(py))
                .collect(),
            runtime: registry.runtime.values().map(|s| s.clone_ref(py)).collect(),
            next_token: registry.next_token,
            maxsize: registry.maxsize,
            finalizers: registry
                .finalizers
                .values()
                .map(|f| f.clone_ref(py))
                .collect(),
        });
        let previous = self
            .snapshot
            .swap(Box::into_raw(snapshot), Ordering::AcqRel);
        if !previous.is_null() {
            // SAFETY: only `publish` stores into `snapshot`, always a pointer
            // from `Box::into_raw`, and the swap made this one unreachable.
            drop(unsafe { Box::from_raw(previous) });
        }
    }

    fn start_channel(self: &Arc<Self>, py: Python<'_>, registry: &mut Registry) -> PyResult<()> {
        let channel = Channel::start(registry.maxsize, Arc::downgrade(self));
        let handle = Py::new(py, DispatchChannel { inner: channel })?;
        registry.channel = Some(handle.clone_ref(py));
        registry.channels.push(handle);
        self.active.store(true, Ordering::Release);
        Ok(())
    }

    /// Retire the active generation; caller must hold the registry lock.
    fn retire_active(&self, registry: &mut Registry) {
        if let Some(channel) = registry.channel.take() {
            self.active.store(false, Ordering::Release);
            channel.get().inner.retire();
        }
    }

    /// Wait outside state locks, except when invoked from a sink callback.
    ///
    /// A callback cannot wait for its own worker, nor for a worker that may be
    /// waiting on it: a stdlib handler on the raw root-logger path runs its
    /// `emit()` under the handler lock, and a queued native delivery to that
    /// same handler blocks on that lock on the worker thread. Both cases are
    /// marked by the shared callback scope. An external lifecycle call will
    /// still find and drain every retired generation.
    ///
    /// Returns the number of accepted records abandoned because a worker made
    /// no progress for `stall` (see `Channel::flush`).
    fn drain(&self, py: Python<'_>, channels: Vec<Arc<Channel>>, stall: Option<Duration>) -> usize {
        let mut abandoned = 0;
        if !in_callback() {
            abandoned = py.detach(|| channels.iter().map(|channel| channel.flush(stall)).sum());
        }
        let mut registry = self.lock_registry();
        if registry
            .channel
            .as_ref()
            .is_some_and(|channel| channel.get().inner.is_abandoned())
        {
            // Nothing can be delivered through a stuck worker: stop offering
            // records to it until the next configure().
            self.retire_active(&mut registry);
        }
        registry.channels.retain(|channel| {
            let inner = &channel.get().inner;
            inner.is_alive() && !inner.is_abandoned()
        });
        abandoned
    }

    /// Account for one finished (or dropped) delivery to each of `sinks`.
    fn settle(&self, py: Python<'_>, sinks: &[Sink]) {
        let mut due: Vec<Py<PyAny>> = Vec::new();
        {
            let mut registry = self.lock_registry();
            for sink in sinks {
                if sink.token <= 0 {
                    continue;
                }
                if let Some(remaining) = registry.outstanding.get_mut(&sink.token)
                    && *remaining > 1
                {
                    *remaining -= 1;
                    continue;
                }
                registry.outstanding.remove(&sink.token);
                if let Some(finalizer) = registry.finalizers.remove(&sink.token) {
                    due.push(finalizer);
                }
            }
            if !due.is_empty() {
                self.publish(py, &registry);
            }
        }
        for finalizer in due {
            // Releasing a sink must never break logging.
            let _ = finalizer.bind(py).call0();
        }
    }

    fn note_drop(&self, py: Python<'_>) -> PyResult<()> {
        let dropped = self.dropped.fetch_add(1, Ordering::AcqRel) + 1;
        if dropped == 1 || dropped.is_multiple_of(1000) {
            let message = CString::new(format!(
                "structguru callable sinks dropped {dropped} delivery record(s): queue full"
            ))
            .expect("warning text has no NUL");
            // Level 2 attributes the warning to the facade's `_log`, as the
            // Python dispatcher's `stacklevel=4` did.
            PyErr::warn(py, &py.get_type::<PyUserWarning>(), &message, 2)?;
        }
        Ok(())
    }
}

impl Drop for DispatcherInner {
    fn drop(&mut self) {
        let snapshot = self.snapshot.swap(std::ptr::null_mut(), Ordering::AcqRel);
        if !snapshot.is_null() {
            // SAFETY: see `publish`; nothing else can observe the pointer now.
            drop(unsafe { Box::from_raw(snapshot) });
        }
    }
}

/// Own callable-sink registration, queueing, lifecycle, and metrics.
///
/// `dict` lets tests monkeypatch lifecycle methods on the instance; `frozen`
/// keeps every method available without a borrow check, since all state is
/// behind its own synchronization.
#[pyclass(name = "CallableDispatcher", frozen, dict)]
pub struct CallableDispatcher {
    inner: Arc<DispatcherInner>,
}

impl CallableDispatcher {
    fn from_parts(
        py: Python<'_>,
        configured: Vec<Sink>,
        runtime: Vec<Sink>,
        next_token: i64,
        maxsize: usize,
        dropped: u64,
    ) -> Self {
        let registry = Registry {
            configured,
            runtime: runtime.into_iter().map(|sink| (sink.token, sink)).collect(),
            next_token,
            maxsize,
            channel: None,
            channels: Vec::new(),
            eligible: HashMap::new(),
            outstanding: HashMap::new(),
            finalizers: HashMap::new(),
        };
        let inner = Arc::new(DispatcherInner {
            registry: Mutex::new(registry),
            active: AtomicBool::new(false),
            dropped: AtomicU64::new(dropped),
            snapshot: AtomicPtr::new(std::ptr::null_mut()),
        });
        inner.publish(py, &inner.lock_registry());
        Self { inner }
    }
}

#[pymethods]
impl CallableDispatcher {
    #[new]
    fn new(py: Python<'_>) -> Self {
        Self::from_parts(py, Vec::new(), Vec::new(), 1, 1024, 0)
    }

    /// Register a runtime sink, starting dispatch when logging is enabled.
    #[pyo3(signature = (callback, min_level=0, *, enabled, level_callback=None))]
    fn add(
        &self,
        py: Python<'_>,
        callback: Py<PyAny>,
        min_level: i64,
        enabled: bool,
        level_callback: Option<Py<PyAny>>,
    ) -> PyResult<i64> {
        let mut registry = self.inner.lock_registry();
        let token = registry.next_token;
        registry.next_token += 1;
        registry.runtime.insert(
            token,
            Sink {
                token,
                callback,
                min_level,
                level_callback,
            },
        );
        registry.eligible.clear();
        if enabled && registry.channel.is_none() {
            self.inner.start_channel(py, &mut registry)?;
        }
        self.inner.publish(py, &registry);
        Ok(token)
    }

    /// Remove one sink, then drain every record that captured it.
    ///
    /// `finalizer` releases the sink's resources (closing a handler) and runs
    /// once no delivery references the sink: after the drain when called
    /// outside a callback, otherwise on the worker right after the last
    /// delivery that captured the sink, since a callback cannot wait for its
    /// own worker. Closing at once would leave those deliveries writing to a
    /// closed handler.
    #[pyo3(signature = (token, *, finalizer=None))]
    fn remove(&self, py: Python<'_>, token: i64, finalizer: Option<Py<PyAny>>) -> PyResult<bool> {
        let (removed, channels, finalizer) = {
            let mut registry = self.inner.lock_registry();
            let removed = registry.runtime.remove(&token).is_some();
            registry.eligible.clear();
            if !registry.has_sinks() {
                self.inner.retire_active(&mut registry);
            }
            let channels = registry.live_channels();
            let mut finalizer = finalizer;
            let outstanding = registry.outstanding.get(&token).copied().unwrap_or(0);
            if in_callback()
                && outstanding > 0
                && let Some(deferred) = finalizer.take()
            {
                registry.finalizers.insert(token, deferred);
            }
            self.inner.publish(py, &registry);
            (removed, channels, finalizer)
        };
        self.inner.drain(py, channels, None);
        if let Some(finalizer) = finalizer {
            finalizer.bind(py).call0()?;
        }
        Ok(removed)
    }

    /// Atomically activate a replacement queue and drain its predecessor.
    #[pyo3(signature = (callbacks, *, maxsize))]
    fn configure(&self, py: Python<'_>, callbacks: Vec<Py<PyAny>>, maxsize: usize) -> PyResult<()> {
        let configured: Vec<Sink> = callbacks
            .into_iter()
            .enumerate()
            .map(|(index, callback)| Sink {
                token: -(index as i64 + 1),
                callback,
                min_level: 0,
                level_callback: None,
            })
            .collect();
        let channels = {
            let mut registry = self.inner.lock_registry();
            self.inner.retire_active(&mut registry);
            let channels = registry.live_channels();
            registry.configured = configured;
            registry.eligible.clear();
            registry.maxsize = maxsize;
            self.inner.dropped.store(0, Ordering::Release);
            if registry.has_sinks() {
                self.inner.start_channel(py, &mut registry)?;
            }
            self.inner.publish(py, &registry);
            channels
        };
        self.inner.drain(py, channels, None);
        Ok(())
    }

    /// Stop dispatch and remove configured sinks while preserving runtime registrations.
    fn disable(&self, py: Python<'_>) {
        let channels = {
            let mut registry = self.inner.lock_registry();
            self.inner.retire_active(&mut registry);
            registry.configured.clear();
            registry.eligible.clear();
            self.inner.publish(py, &registry);
            registry.live_channels()
        };
        self.inner.drain(py, channels, None);
    }

    /// Stop the active dispatch queue while preserving registrations.
    ///
    /// `drain` is accepted for API symmetry; both values join behind every
    /// accepted queue entry, as the Python dispatcher always did.
    ///
    /// `stall_timeout` bounds the wait for a sink that stops returning; the
    /// number of accepted records left undelivered is returned (zero when
    /// every delivery completed).
    #[pyo3(signature = (*, drain, stall_timeout=None))]
    fn stop(&self, py: Python<'_>, drain: bool, stall_timeout: Option<f64>) -> usize {
        let _ = drain;
        let channels = {
            let mut registry = self.inner.lock_registry();
            self.inner.retire_active(&mut registry);
            registry.live_channels()
        };
        self.inner
            .drain(py, channels, stall_duration(stall_timeout))
    }

    /// Block until all queued deliveries have completed; see `stop` for
    /// `stall_timeout` and the return value.
    #[pyo3(signature = (stall_timeout=None))]
    fn flush(&self, py: Python<'_>, stall_timeout: Option<f64>) -> usize {
        let channels = self.inner.lock_registry().live_channels();
        self.inner
            .drain(py, channels, stall_duration(stall_timeout))
    }

    /// True when no callable sink can receive a line.
    ///
    /// Read without the lock: a stale `true` (a sink being added concurrently)
    /// only means this record predates the sink, exactly as if it had been
    /// logged one call sooner.
    fn idle(&self) -> bool {
        !self.inner.active.load(Ordering::Acquire)
    }

    /// Queue a record, reserving its sinks before removal can drain them.
    ///
    /// Logs emitted inside a sink callback, on the dispatch worker or during a
    /// raw stdlib delivery, bypass callable delivery. They still reach the
    /// native writer, but cannot recursively feed or block their own worker.
    #[pyo3(signature = (line, level, *, overflow))]
    fn enqueue(&self, py: Python<'_>, line: String, level: i64, overflow: &str) -> PyResult<bool> {
        if in_callback() {
            return Ok(true);
        }
        let blocking = overflow == "block";
        let (channel, sinks) = {
            let mut registry = self.inner.lock_registry();
            let Some(channel) = registry.channel.as_ref() else {
                return Ok(true);
            };
            let channel = Arc::clone(&channel.get().inner);
            let sinks = match registry.eligible.get(&level) {
                Some(sinks) => Arc::clone(sinks),
                None => {
                    let sinks: Arc<[Sink]> = registry
                        .configured
                        .iter()
                        .chain(registry.runtime.values())
                        .filter(|sink| level >= sink.min_level)
                        .map(|sink| sink.clone_ref(py))
                        .collect();
                    registry.eligible.insert(level, Arc::clone(&sinks));
                    sinks
                }
            };
            if sinks.is_empty() {
                return Ok(true);
            }
            if !channel.reserve() {
                return Ok(false);
            }
            for sink in sinks.iter() {
                if sink.token > 0 {
                    *registry.outstanding.entry(sink.token).or_insert(0) += 1;
                }
            }
            (channel, sinks)
        };
        // The lease covers the gap between selecting sinks and queue insertion.
        // Removal sees this producer even before its record enters the queue.
        let record = Record {
            line,
            sinks: Arc::clone(&sinks),
            level,
        };
        let accepted = match channel.offer(record, blocking) {
            Offer::Accepted => true,
            Offer::Full => false,
            Offer::WouldBlock(record) => {
                // Only a full queue pays for detaching: the worker needs the
                // interpreter to free a slot.
                py.detach(|| channel.put_blocking(record))
            }
        };
        if !accepted {
            self.inner.settle(py, &sinks);
            self.inner.note_drop(py)?;
        }
        Ok(accepted)
    }

    /// Return callable dispatch queue and drop metrics.
    fn metrics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let registry = self.inner.lock_registry();
        let depth = registry
            .channel
            .as_ref()
            .map_or(0, |channel| channel.get().inner.lock().queue.len());
        let result = PyDict::new(py);
        result.set_item(
            "callable_dropped",
            self.inner.dropped.load(Ordering::Acquire),
        )?;
        result.set_item("callable_depth", depth)?;
        result.set_item("callable_maxsize", registry.maxsize)?;
        Ok(result)
    }

    /// Reset the drop counter for isolated tests.
    fn reset_drop_count(&self) {
        self.inner.dropped.store(0, Ordering::Release);
    }

    /// Build the dispatcher a forked child continues with.
    ///
    /// Worker threads do not survive fork and a lock held by a vanished thread
    /// can never be acquired, so nothing inherited is touched: registrations
    /// come from the lock-free snapshot, the drop count carries over, queued
    /// records are not delivered in the child, and finalizers that removal
    /// deferred run now since nothing waits on the parent's counts. The caller
    /// replaces its reference; the inherited dispatcher is never used again.
    #[pyo3(signature = (*, enabled))]
    fn fork_child(&self, py: Python<'_>, enabled: bool) -> PyResult<Self> {
        // The surviving thread may have forked from inside a callback; the
        // child does not continue that delivery.
        CALLBACK_DEPTH.with(|depth| depth.set(0));
        // SAFETY: `publish` runs at construction, so the pointer is never null,
        // and the child has no other thread that could replace it.
        let snapshot = unsafe { &*self.inner.snapshot.load(Ordering::Acquire) };
        let child = Self::from_parts(
            py,
            snapshot
                .configured
                .iter()
                .map(|s| s.clone_ref(py))
                .collect(),
            snapshot.runtime.iter().map(|s| s.clone_ref(py)).collect(),
            snapshot.next_token,
            snapshot.maxsize,
            self.inner.dropped.load(Ordering::Acquire),
        );
        {
            let mut registry = child.inner.lock_registry();
            if enabled && registry.has_sinks() {
                child.inner.start_channel(py, &mut registry)?;
            }
        }
        for finalizer in &snapshot.finalizers {
            // Releasing a sink must never break logging.
            let _ = finalizer.bind(py).call0();
        }
        Ok(child)
    }

    /// The active queue generation, or `None` while dispatch is stopped.
    #[getter]
    fn _channel(&self, py: Python<'_>) -> Option<Py<DispatchChannel>> {
        self.inner
            .lock_registry()
            .channel
            .as_ref()
            .map(|channel| channel.clone_ref(py))
    }
}
