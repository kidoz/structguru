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
//! registration. The worker thread attaches to the interpreter for each batch
//! it delivers; every wait a producer or lifecycle caller performs happens with
//! the interpreter detached so the worker can always make progress. Generations
//! deliver one at a time, in the order they started (see `Sequencer`).

use std::cell::Cell;
use std::collections::{BTreeMap, BTreeSet, HashMap, VecDeque};
use std::ffi::CString;
use std::ops::{Deref, DerefMut};
use std::sync::atomic::{AtomicBool, AtomicPtr, AtomicU8, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError, Weak};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use pyo3::exceptions::{PyRuntimeError, PyUserWarning};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyString};

/// Issue a `UserWarning` that can never interrupt the caller.
///
/// A diagnostic about logging must not make logging fail: when a warnings
/// filter turns it into an exception (`-W error`), the exception goes to
/// `sys.unraisablehook`, as CPython reports errors it cannot raise.
fn warn(py: Python<'_>, message: &str, stacklevel: i32) {
    let message = CString::new(message.replace('\0', "\\0")).expect("NUL bytes are escaped");
    if let Err(error) = PyErr::warn(py, &py.get_type::<PyUserWarning>(), &message, stacklevel) {
        error.write_unraisable(py, None);
    }
}

/// Report `error` through `sys.unraisablehook` instead of raising it.
///
/// For Python diagnostics on the logging path that must not raise; see `warn`.
#[pyfunction]
pub fn report_unraisable(py: Python<'_>, error: Bound<'_, pyo3::exceptions::PyBaseException>) {
    PyErr::from_value(error.into_any()).write_unraisable(py, None);
}

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
    accepted_id: u64,
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
    /// Exact producer leases still between reservation and queue insertion.
    next_lease: u64,
    open_leases: BTreeSet<u64>,
    /// Records appended and records whose delivery finished, in total.
    accepted: u64,
    finished: u64,
    /// Undelivered record IDs and the producer leases that inserted them.
    accepted_leases: BTreeMap<u64, u64>,
    /// Flushers currently waiting; they need a wake-up on every step.
    flush_waiters: usize,
    /// Set when a flush gave up on a worker that stopped making progress.
    abandoned: bool,
}

/// Delivery order across the queue generations of one dispatcher.
///
/// Every generation takes a ticket when it starts, and its worker delivers
/// only while that ticket is the oldest one still out. So at most one worker
/// invokes sinks at a time, and a replacement generation never overtakes the
/// backlog of its predecessors, although producers fill its queue at once. A
/// ticket comes back when its worker exits or its generation is abandoned,
/// so a stuck sink holds later generations back only until a flush gives up.
#[derive(Default)]
struct Sequencer {
    tickets: Mutex<Tickets>,
    turn: Condvar,
}

#[derive(Default)]
struct Tickets {
    next: u64,
    out: BTreeSet<u64>,
}

impl Sequencer {
    fn lock(&self) -> MutexGuard<'_, Tickets> {
        self.tickets.lock().unwrap_or_else(PoisonError::into_inner)
    }

    fn take(&self) -> u64 {
        let mut tickets = self.lock();
        let ticket = tickets.next;
        tickets.next += 1;
        tickets.out.insert(ticket);
        ticket
    }

    /// Wait until every earlier ticket is back, or `ticket` itself is.
    fn wait_turn(&self, ticket: u64) {
        let tickets = self.lock();
        let _tickets = self
            .turn
            .wait_while(tickets, |tickets| {
                tickets.out.contains(&ticket) && tickets.out.first() != Some(&ticket)
            })
            .unwrap_or_else(PoisonError::into_inner);
    }

    /// Return `ticket`; idempotent.
    fn give_back(&self, ticket: u64) {
        if self.lock().out.remove(&ticket) {
            self.turn.notify_all();
        }
    }
}

/// Returns a generation's ticket however its worker stops, a panic included.
struct Turn<'a>(&'a Channel);

impl Drop for Turn<'_> {
    fn drop(&mut self) {
        self.0.sequencer.give_back(self.0.ticket);
    }
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
    /// This generation's place in the dispatcher's delivery order.
    sequencer: Arc<Sequencer>,
    ticket: u64,
    /// Mirrors `ChannelState::abandoned` for the per-sink check on delivery.
    halted: AtomicBool,
}

/// Records the worker takes from the queue per lock acquisition.
const DELIVERY_BATCH: usize = 64;

/// Distinct level numbers whose eligible sinks are cached per registration.
const MAX_CACHED_LEVELS: usize = 64;

/// Seconds from Python into a stall bound; `None` waits without limit.
pub(crate) fn stall_duration(seconds: Option<f64>) -> Option<Duration> {
    seconds.map(|s| Duration::try_from_secs_f64(s.max(0.0)).unwrap_or(Duration::MAX))
}

/// Result of offering a record to a generation.
enum Offer {
    Accepted,
    /// Full in drop mode; the lease has been released.
    Full,
    /// Full in block mode; the lease is still held and the record returned.
    WouldBlock(Record),
    /// A flush abandoned the generation; the lease has been released and the
    /// record counts toward the abandoned total that flush reported.
    Abandoned,
}

impl Channel {
    fn start(
        maxsize: usize,
        dispatcher: Weak<DispatcherInner>,
        sequencer: Arc<Sequencer>,
        fail_spawn: bool,
    ) -> PyResult<Arc<Channel>> {
        let ticket = sequencer.take();
        let channel = Arc::new(Channel {
            maxsize,
            state: Mutex::new(ChannelState {
                accepting: true,
                ..ChannelState::default()
            }),
            condition: Condvar::new(),
            not_empty: Condvar::new(),
            thread: Mutex::new(None),
            sequencer,
            ticket,
            halted: AtomicBool::new(false),
        });
        let worker = Arc::clone(&channel);
        let spawned = if fail_spawn {
            Err(std::io::Error::other(
                "injected callable worker spawn failure",
            ))
        } else {
            std::thread::Builder::new()
                .name("structguru-callable-sinks".to_owned())
                .spawn(move || worker.run(dispatcher))
        };
        let handle = match spawned {
            Ok(handle) => handle,
            Err(error) => {
                channel.sequencer.give_back(ticket);
                return Err(PyRuntimeError::new_err(format!(
                    "unable to spawn callable sink dispatch worker: {error}"
                )));
            }
        };
        *channel.lock_thread() = Some(handle);
        Ok(channel)
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

    fn append(&self, state: &mut ChannelState, lease_id: u64, mut record: Record) {
        record.accepted_id = state.accepted;
        state.accepted_leases.insert(record.accepted_id, lease_id);
        state.queue.push_back(record);
        state.unfinished_tasks += 1;
        state.accepted += 1;
        self.not_empty.notify_one();
    }

    fn release_lease(&self, state: &mut ChannelState, lease_id: u64) {
        state.producers -= 1;
        let removed = state.open_leases.remove(&lease_id);
        debug_assert!(removed);
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
        // A limit beyond the clock's range (an infinite timeout) is no limit.
        let deadline_after = |now: Instant| stall.and_then(|limit| now.checked_add(limit));
        let mut last = progress(&state);
        let mut deadline = deadline_after(Instant::now());
        while !done(&state) {
            match deadline {
                Some(at) => {
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
                        deadline = deadline_after(Instant::now());
                    }
                }
                None => {
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
    fn reserve(&self) -> Option<u64> {
        let mut state = self.lock();
        if !state.accepting {
            return None;
        }
        let lease_id = state.next_lease;
        state.next_lease += 1;
        state.producers += 1;
        state.open_leases.insert(lease_id);
        Some(lease_id)
    }

    /// Insert a reserved record if there is room, releasing the lease unless
    /// the caller must go on to wait (`Offer::WouldBlock`).
    fn offer(&self, lease_id: u64, record: Record, blocking: bool) -> Offer {
        let mut state = self.lock();
        if state.abandoned {
            // No worker will take it; the abandoning flush counted this lease.
            self.release_lease(&mut state, lease_id);
            return Offer::Abandoned;
        }
        if self.is_full(&state) {
            if !blocking {
                self.release_lease(&mut state, lease_id);
                return Offer::Full;
            }
            return Offer::WouldBlock(record);
        }
        self.append(&mut state, lease_id, record);
        self.release_lease(&mut state, lease_id);
        Offer::Accepted
    }

    /// Wait for a slot, insert, and release the lease. Callers detach from the
    /// interpreter first: the worker needs it to free a slot. Returns
    /// `Offer::Abandoned` when the generation was abandoned while waiting.
    fn put_blocking(&self, lease_id: u64, record: Record) -> Offer {
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
            self.release_lease(&mut state, lease_id);
            return Offer::Abandoned;
        }
        self.append(&mut state, lease_id, record);
        self.release_lease(&mut state, lease_id);
        Offer::Accepted
    }

    /// Wait for producers that reserved delivery before this call, then for
    /// every record accepted by then. Records other threads enqueue afterwards
    /// are not waited for, so a flush under sustained load is bounded by the
    /// queue instead of waiting for a moment when it happens to be empty.
    ///
    /// With `stall`, a worker that makes no progress for that long (a sink that
    /// never returns) is abandoned: the generation stops accepting, producers
    /// holding a lease give up their records, later generations may deliver,
    /// and the number of records lost is returned: accepted records left
    /// undelivered plus the records of those producers. Returns at once
    /// inside a sink callback: a callback cannot wait for its own worker.
    /// Callers detach from the interpreter first.
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
            let lease_target = state.next_lease;
            let accepted_target = state.accepted;
            // A finished delivery is progress too: a producer blocked on a full
            // queue settles its lease only once the worker's batch is done.
            // Later leases neither satisfy this flush nor extend its deadline.
            // Another flusher abandoning the generation ends the wait as well.
            let (next, mut drained) = self.wait_until(
                state,
                stall,
                |state| {
                    state.abandoned
                        || state
                            .open_leases
                            .iter()
                            .all(|lease_id| *lease_id >= lease_target)
                },
                |state| {
                    state
                        .open_leases
                        .iter()
                        .take_while(|lease_id| **lease_id < lease_target)
                        .count() as u64
                        + state.finished
                },
            );
            state = next;
            if drained && !state.abandoned {
                let accepted_target = state
                    .accepted_leases
                    .iter()
                    .filter(|(_, lease_id)| **lease_id < lease_target)
                    .map(|(accepted_id, _)| *accepted_id + 1)
                    .fold(accepted_target, u64::max);
                let (next, finished) = self.wait_until(
                    state,
                    stall,
                    |state| state.abandoned || state.finished >= accepted_target,
                    |state| state.finished,
                );
                state = next;
                drained = finished;
            }
            state.flush_waiters -= 1;
            if state.abandoned {
                // The flusher that abandoned the generation reported its
                // records; they must not be counted twice.
                return 0;
            }
            if drained {
                (!state.accepting, None)
            } else {
                state.abandoned = true;
                state.accepting = false;
                self.halted.store(true, Ordering::Release);
                // Every producer still holding a lease finds the generation
                // abandoned and gives up its record.
                let abandoned = state.unfinished_tasks + state.producers;
                self.condition.notify_all();
                self.not_empty.notify_all();
                (false, Some(abandoned))
            }
        };
        if retired {
            self.join();
        }
        let Some(abandoned) = abandoned else {
            return 0;
        };
        // The stuck worker never delivers again; later generations may.
        self.sequencer.give_back(self.ticket);
        abandoned
    }

    /// Queued records of an abandoned generation, which no worker will take.
    fn take_orphans(&self) -> Vec<Record> {
        let mut state = self.lock();
        if !state.abandoned {
            return Vec::new();
        }
        state.queue.drain(..).collect()
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
        let _turn = Turn(&self);
        // Every callback this thread runs is nested.
        CALLBACK_DEPTH.with(|depth| depth.set(1));
        let mut batch: Vec<Record> = Vec::with_capacity(DELIVERY_BATCH);
        let mut has_turn = false;
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
                    // A flush gave up on this generation. Whatever the stuck
                    // callback was doing, stop here rather than attaching to
                    // the interpreter again: by the time it returns the
                    // interpreter may be finalizing.
                    if state.abandoned {
                        return;
                    }
                    state = self
                        .not_empty
                        .wait(state)
                        .unwrap_or_else(PoisonError::into_inner);
                }
            }
            if !has_turn {
                // Records wait here, still accepted, until every earlier
                // generation has finished or been abandoned.
                self.sequencer.wait_turn(self.ticket);
                has_turn = true;
            }
            // Stay attached while work remains, as a Python worker thread holds
            // the interpreter between switch intervals. Attaching per record
            // would hand the interpreter back and forth with a producer blocked
            // on a full queue for every slot freed. Callbacks run Python code,
            // so the interpreter's own switching still applies inside them.
            if self.is_abandoned() {
                return;
            }
            Python::attach(|py| {
                loop {
                    {
                        let mut state = self.lock();
                        let count = state.queue.len().min(DELIVERY_BATCH);
                        if count == 0 || state.abandoned {
                            break;
                        }
                        batch.extend(state.queue.drain(..count));
                        if state.producers > 0 {
                            self.condition.notify_all(); // producers may wait for these slots
                        }
                    }
                    let mut records = batch.drain(..);
                    while let Some(record) = records.next() {
                        self.deliver(py, &dispatcher, &record);
                        let accepted_id = record.accepted_id;
                        drop(record);
                        let mut state = self.lock();
                        state.accepted_leases.remove(&accepted_id);
                        state.unfinished_tasks -= 1;
                        state.finished += 1;
                        if state.unfinished_tasks == 0 || state.flush_waiters > 0 {
                            self.condition.notify_all();
                        }
                        // The delivery that just finished may be the one a
                        // flush gave up on; stop before starting another.
                        if state.abandoned {
                            drop(state);
                            // Taken from the queue but never delivered: their
                            // sinks must not wait on them (see `remove`).
                            if let Some(dispatcher) = dispatcher.upgrade() {
                                for record in records.by_ref() {
                                    dispatcher.settle(py, &record.sinks);
                                }
                            }
                            break;
                        }
                    }
                }
            });
        }
    }

    fn deliver(&self, py: Python<'_>, dispatcher: &Weak<DispatcherInner>, record: &Record) {
        let dispatcher = dispatcher.upgrade();
        let line = PyString::new(py, &record.line);
        for sink in record.sinks.iter() {
            // Once a flush gives up on this generation, its worker delivers
            // nothing more, not even the rest of a record it has started.
            if self.halted.load(Ordering::Acquire) {
                break;
            }
            // Worker callbacks cannot interrupt the caller: every failure,
            // `BaseException` subclasses included, is counted and reported.
            let delivered = match &sink.level_callback {
                Some(callback) => callback.bind(py).call1((&line, record.level)),
                None => sink.callback.bind(py).call1((&line,)),
            };
            if let Err(error) = delivered
                && let Some(dispatcher) = &dispatcher
            {
                dispatcher.note_error(py, &error);
            }
        }
        // Account for the delivery before the record counts as done, so a
        // flush() observes every consequence of delivery.
        if let Some(dispatcher) = dispatcher {
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

    /// Hold a producer lease for deterministic lifecycle regression tests.
    fn reserve_for_test(&self) -> PyResult<u64> {
        self.inner
            .reserve()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err("channel is retired"))
    }

    /// Release a lease returned by `reserve_for_test` without queueing a record.
    fn release_lease_for_test(&self, lease_id: u64) {
        let mut state = self.inner.lock();
        self.inner.release_lease(&mut state, lease_id);
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
    retired_snapshots: Vec<Arc<Snapshot>>,
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
    /// Why no generation runs even though sinks are registered; while
    /// halted, registering a sink starts no generation.
    halt: Halt,
}

/// Why dispatch is halted, ordered by what it takes to resume.
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
enum Halt {
    Running,
    /// `stop`, `disable`, or `sync(None)`: logging is off or shutting down.
    /// `configure`, or `sync` with the callbacks of an enabled runtime,
    /// resumes, so a stale disable that lost a race with `configure` cannot
    /// leave an enabled runtime without callable dispatch.
    Lifecycle,
    /// The active generation was abandoned because a sink made no progress;
    /// only `configure` resumes.
    Abandoned,
}

impl Registry {
    /// Halt dispatch for `reason`, keeping a stronger reason already set.
    fn halt(&mut self, reason: Halt) {
        self.halt = self.halt.max(reason);
    }

    fn has_sinks(&self) -> bool {
        !self.configured.is_empty() || !self.runtime.is_empty()
    }

    fn set_configured(&mut self, callbacks: Vec<Py<PyAny>>) {
        self.configured = callbacks
            .into_iter()
            .enumerate()
            .map(|(index, callback)| Sink {
                token: -(index as i64 + 1),
                callback,
                min_level: 0,
                level_callback: None,
            })
            .collect();
        self.eligible.clear();
    }

    fn is_configured_with(&self, callbacks: &[Py<PyAny>]) -> bool {
        self.configured.len() == callbacks.len()
            && self
                .configured
                .iter()
                .zip(callbacks)
                .all(|(sink, callback)| sink.callback.is(callback))
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

/// Keep retired Python references alive until the registry is unlocked.
///
/// Removing a sink or clearing its eligibility cache is safe while its fork
/// snapshot still owns the callback. Replacing that snapshot must defer the
/// final decref too: Python destructors may reenter any dispatcher method.
struct RegistryGuard<'a> {
    registry: Option<MutexGuard<'a, Registry>>,
    retired: Vec<Arc<Snapshot>>,
}

impl Deref for RegistryGuard<'_> {
    type Target = Registry;

    fn deref(&self) -> &Registry {
        self.registry.as_deref().expect("registry guard is held")
    }
}

impl DerefMut for RegistryGuard<'_> {
    fn deref_mut(&mut self) -> &mut Registry {
        self.registry
            .as_deref_mut()
            .expect("registry guard is held")
    }
}

impl Drop for RegistryGuard<'_> {
    fn drop(&mut self) {
        // Fields drop after this method returns, with no registry lock held.
        drop(self.registry.take());
    }
}

struct DispatcherInner {
    fork_finalizers: Mutex<Vec<Py<PyAny>>>,
    fail_spawn: AtomicBool,
    replacement_gate: AtomicU8,
    submissions: Arc<crate::submission::Submissions>,
    registry: Mutex<Registry>,
    /// Set while a generation is active; read lock-free on the hot path.
    active: AtomicBool,
    dropped: AtomicU64,
    /// Sink calls that raised.
    errors: AtomicU64,
    sequencer: Arc<Sequencer>,
    snapshot: AtomicPtr<Snapshot>,
    snapshot_readers: AtomicUsize,
}

impl DispatcherInner {
    fn lock_registry(&self) -> RegistryGuard<'_> {
        RegistryGuard {
            registry: Some(self.registry.lock().unwrap_or_else(PoisonError::into_inner)),
            retired: Vec::new(),
        }
    }

    /// Replace the fork snapshot with the registry's current registrations.
    fn publish(&self, py: Python<'_>, registry: &mut RegistryGuard<'_>) {
        let snapshot = Arc::new(Snapshot {
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
            .swap(Arc::into_raw(snapshot).cast_mut(), Ordering::SeqCst);
        if !previous.is_null() {
            // SAFETY: publication owns one strong reference from into_raw.
            // Defer its release until all readers have acquired their own Arc.
            registry
                .retired_snapshots
                .push(unsafe { Arc::from_raw(previous) });
        }
        // SeqCst orders each reader's entry before its pointer load and this
        // check after the swap. A reader can therefore never see a reclaimed
        // pointer. Drop Python references only after releasing the registry.
        if self.snapshot_readers.load(Ordering::SeqCst) == 0 {
            let retired = std::mem::take(&mut registry.retired_snapshots);
            registry.retired.extend(retired);
        }
    }

    fn prepare_channel(
        self: &Arc<Self>,
        py: Python<'_>,
        maxsize: usize,
    ) -> PyResult<Py<DispatchChannel>> {
        let channel = Channel::start(
            maxsize,
            Arc::downgrade(self),
            Arc::clone(&self.sequencer),
            self.fail_spawn.swap(false, Ordering::AcqRel),
        )?;
        match Py::new(
            py,
            DispatchChannel {
                inner: Arc::clone(&channel),
            },
        ) {
            Ok(handle) => Ok(handle),
            Err(error) => {
                // An unpublished successor must not wait on the active
                // generation's ticket after allocation fails.
                channel.retire();
                channel.sequencer.give_back(channel.ticket);
                Err(error)
            }
        }
    }

    fn install_channel(
        &self,
        py: Python<'_>,
        registry: &mut Registry,
        handle: Py<DispatchChannel>,
    ) {
        registry.channel = Some(handle.clone_ref(py));
        registry.channels.push(handle);
        self.active.store(true, Ordering::Release);
    }

    fn start_channel(self: &Arc<Self>, py: Python<'_>, registry: &mut Registry) -> PyResult<()> {
        let handle = self.prepare_channel(py, registry.maxsize)?;
        self.install_channel(py, registry, handle);
        Ok(())
    }

    fn retire_for_replacement(&self, py: Python<'_>, registry: &mut Registry) {
        if let Some(channel) = registry.channel.take() {
            channel.get().inner.retire();
        }
        // Test-only widening of the lock-free observer window. No Python is
        // called while holding the registry; release uses only this atomic.
        if self
            .replacement_gate
            .compare_exchange(1, 2, Ordering::AcqRel, Ordering::Acquire)
            .is_ok()
        {
            py.detach(|| {
                let deadline = Instant::now();
                while self.replacement_gate.load(Ordering::Acquire) == 2
                    && deadline.elapsed() < Duration::from_secs(5)
                {
                    std::thread::park_timeout(Duration::from_millis(1));
                }
            });
            self.replacement_gate.store(0, Ordering::Release);
        }
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
    /// Returns the number of records abandoned because a worker made no
    /// progress for `stall` (see `Channel::flush`). Channels are flushed in
    /// the order they started, which is also their delivery order.
    fn drain(&self, py: Python<'_>, channels: Vec<Arc<Channel>>, stall: Option<Duration>) -> usize {
        let mut abandoned = 0;
        if !in_callback() {
            abandoned = py.detach(|| channels.iter().map(|channel| channel.flush(stall)).sum());
            for channel in &channels {
                // Release the sinks of records no worker will take, so that
                // removing one of them need not wait for them.
                for record in channel.take_orphans() {
                    self.settle(py, &record.sinks);
                }
            }
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
            registry.halt(Halt::Abandoned);
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
                self.publish(py, &mut registry);
            }
        }
        for finalizer in due {
            // Releasing a sink must never break logging.
            let _ = finalizer.bind(py).call0();
        }
    }

    fn note_drop(&self, py: Python<'_>) {
        let dropped = self.dropped.fetch_add(1, Ordering::AcqRel) + 1;
        if dropped == 1 || dropped.is_multiple_of(1000) {
            // Level 2 attributes the warning to the facade's `_log`, as the
            // Python dispatcher's `stacklevel=4` did.
            warn(
                py,
                &format!(
                    "structguru callable sinks dropped {dropped} delivery record(s): queue full"
                ),
                2,
            );
        }
    }

    /// Count a sink call that raised and report it at a bounded rate, like a
    /// failing stream sink. Runs on the worker, inside the callback scope, so
    /// a warning routed into logging cannot reach callable sinks again.
    fn note_error(&self, py: Python<'_>, error: &PyErr) {
        let errors = self.errors.fetch_add(1, Ordering::AcqRel) + 1;
        if errors == 1 || errors.is_multiple_of(1000) {
            let kind = error
                .get_type(py)
                .name()
                .map_or_else(|_| "BaseException".to_owned(), |name| name.to_string());
            // Exception text can contain credentials and arbitrary __str__
            // code. Diagnostics bypass record redaction: report only the type.
            warn(
                py,
                &format!("structguru callable sink raised {errors} time(s), latest {kind}"),
                1,
            );
        }
    }
}

impl Drop for DispatcherInner {
    fn drop(&mut self) {
        let snapshot = self.snapshot.swap(std::ptr::null_mut(), Ordering::AcqRel);
        if !snapshot.is_null() {
            // SAFETY: see `publish`; nothing else can observe the pointer now.
            drop(unsafe { Arc::from_raw(snapshot) });
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
        counters: (u64, u64),
        submissions: Arc<crate::submission::Submissions>,
    ) -> Self {
        let registry = Registry {
            retired_snapshots: Vec::new(),
            configured,
            runtime: runtime.into_iter().map(|sink| (sink.token, sink)).collect(),
            next_token,
            maxsize,
            channel: None,
            channels: Vec::new(),
            eligible: HashMap::new(),
            outstanding: HashMap::new(),
            finalizers: HashMap::new(),
            halt: Halt::Running,
        };
        let (dropped, errors) = counters;
        let inner = Arc::new(DispatcherInner {
            fork_finalizers: Mutex::new(Vec::new()),
            fail_spawn: AtomicBool::new(false),
            replacement_gate: AtomicU8::new(0),
            submissions,
            registry: Mutex::new(registry),
            active: AtomicBool::new(false),
            dropped: AtomicU64::new(dropped),
            errors: AtomicU64::new(errors),
            sequencer: Arc::default(),
            snapshot: AtomicPtr::new(std::ptr::null_mut()),
            snapshot_readers: AtomicUsize::new(0),
        });
        inner.publish(py, &mut inner.lock_registry());
        Self { inner }
    }

    pub(crate) fn begin_submission(&self) -> crate::submission::Submission {
        self.inner.submissions.begin()
    }
}

#[pymethods]
impl CallableDispatcher {
    fn _begin_submission(&self) -> crate::submission::SubmissionLease {
        crate::submission::SubmissionLease::new(self.begin_submission())
    }

    fn _submission_barrier(&self) -> u64 {
        self.inner.submissions.barrier()
    }

    fn _fail_next_spawn_for_test(&self) {
        self.inner.fail_spawn.store(true, Ordering::Release);
    }

    fn _pause_next_replacement_for_test(&self) {
        self.inner.replacement_gate.store(1, Ordering::Release);
    }

    fn _replacement_waiting_for_test(&self) -> bool {
        self.inner.replacement_gate.load(Ordering::Acquire) == 2
    }

    fn _release_replacement_for_test(&self) {
        self.inner.replacement_gate.store(0, Ordering::Release);
    }

    fn _wait_submissions(&self, py: Python<'_>, target: u64, timeout: f64) -> usize {
        let timeout = Duration::try_from_secs_f64(timeout.max(0.0)).unwrap_or(Duration::MAX);
        py.detach(|| self.inner.submissions.wait(target, timeout))
    }

    #[new]
    fn new(py: Python<'_>) -> Self {
        Self::from_parts(py, Vec::new(), Vec::new(), 1, 1024, (0, 0), Arc::default())
    }

    /// Register a runtime sink, starting dispatch when logging is enabled.
    ///
    /// Never replaces or waits for a generation. While dispatch is halted
    /// (see `stop`), the sink waits for the next `configure` (or `sync` with
    /// callbacks).
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
        let prepared = if enabled && registry.channel.is_none() && registry.halt == Halt::Running {
            Some(self.inner.prepare_channel(py, registry.maxsize)?)
        } else {
            None
        };
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
        if let Some(channel) = prepared {
            self.inner.install_channel(py, &mut registry, channel);
        }
        self.inner.publish(py, &mut registry);
        Ok(token)
    }

    /// Remove one sink, then drain every record that captured it.
    ///
    /// `finalizer` releases the sink's resources (closing a handler) and runs
    /// once no delivery references the sink: right after the drain, or, when a
    /// delivery is still running, on the thread that finishes the last one.
    /// That happens inside a sink callback, which cannot wait for its own
    /// worker, and when the drain abandoned a sink that stopped returning.
    /// Closing at once would leave those deliveries writing to a closed
    /// handler.
    ///
    /// Returns whether `token` was registered and, as `stop` does, the number
    /// of records abandoned because a sink made no progress for
    /// `stall_timeout`.
    #[pyo3(signature = (token, *, finalizer=None, stall_timeout=None))]
    fn remove(
        &self,
        py: Python<'_>,
        token: i64,
        finalizer: Option<Py<PyAny>>,
        stall_timeout: Option<f64>,
    ) -> PyResult<(bool, usize)> {
        let (removed, channels) = {
            let mut registry = self.inner.lock_registry();
            let removed = registry.runtime.remove(&token).is_some();
            registry.eligible.clear();
            if !registry.has_sinks() {
                self.inner.retire_active(&mut registry);
            }
            self.inner.publish(py, &mut registry);
            (removed, registry.live_channels())
        };
        let abandoned = self
            .inner
            .drain(py, channels, stall_duration(stall_timeout));
        if let Some(finalizer) = finalizer {
            let due = {
                let mut registry = self.inner.lock_registry();
                if registry.outstanding.get(&token).copied().unwrap_or(0) > 0 {
                    // `settle` runs it after the last delivery.
                    registry.finalizers.insert(token, finalizer);
                    self.inner.publish(py, &mut registry);
                    None
                } else {
                    Some(finalizer)
                }
            };
            if let Some(finalizer) = due {
                // Immediate and deferred finalization have the same containment.
                let _ = finalizer.bind(py).call0();
            }
        }
        Ok((removed, abandoned))
    }

    /// Replace the configured sinks and start a new queue generation.
    ///
    /// The new generation accepts records at once but delivers only after
    /// every earlier one has finished or been abandoned; this call then drains
    /// those predecessors. Drop and error counters restart, and a halted
    /// dispatcher resumes. See `stop` for `stall_timeout` and the return value.
    #[pyo3(signature = (callbacks, *, maxsize, stall_timeout=None))]
    fn configure(
        &self,
        py: Python<'_>,
        callbacks: Vec<Py<PyAny>>,
        maxsize: usize,
        stall_timeout: Option<f64>,
    ) -> PyResult<usize> {
        let channels = {
            let mut registry = self.inner.lock_registry();
            let prepared = if !callbacks.is_empty() || !registry.runtime.is_empty() {
                Some(self.inner.prepare_channel(py, maxsize)?)
            } else {
                None
            };
            self.inner.retire_for_replacement(py, &mut registry);
            let channels = registry.live_channels();
            registry.set_configured(callbacks);
            registry.maxsize = maxsize;
            registry.halt = Halt::Running;
            self.inner.dropped.store(0, Ordering::Release);
            self.inner.errors.store(0, Ordering::Release);
            if let Some(channel) = prepared {
                self.inner.install_channel(py, &mut registry, channel);
            } else {
                self.inner.active.store(false, Ordering::Release);
            }
            self.inner.publish(py, &mut registry);
            channels
        };
        Ok(self
            .inner
            .drain(py, channels, stall_duration(stall_timeout)))
    }

    /// Converge on a configuration without disturbing a matching one.
    ///
    /// `callbacks=None` means logging is disabled. For a registration that
    /// raced a configure() or shutdown(): when the dispatcher already matches,
    /// nothing is replaced, restarted, or waited for. A mismatch is applied as
    /// `configure` or `disable` would, except that nothing is drained (a new
    /// generation still delivers after its predecessors) and counters keep
    /// counting. Callbacks mean logging is enabled, so they resume dispatch
    /// halted by `stop`, `disable`, or `sync(None)`; after an abandoned
    /// generation only `configure` resumes it.
    #[pyo3(signature = (callbacks, *, maxsize))]
    fn sync(
        &self,
        py: Python<'_>,
        callbacks: Option<Vec<Py<PyAny>>>,
        maxsize: usize,
    ) -> PyResult<()> {
        let mut registry = self.inner.lock_registry();
        let Some(callbacks) = callbacks else {
            if registry.channel.is_some()
                || !registry.configured.is_empty()
                || registry.halt == Halt::Running
            {
                self.inner.retire_active(&mut registry);
                registry.configured.clear();
                registry.eligible.clear();
                registry.halt(Halt::Lifecycle);
                self.inner.publish(py, &mut registry);
            }
            return Ok(());
        };
        let changed = registry.maxsize != maxsize || !registry.is_configured_with(&callbacks);
        let prepared = if (changed || registry.channel.is_none())
            && (!callbacks.is_empty() || !registry.runtime.is_empty())
            && registry.halt != Halt::Abandoned
        {
            Some(self.inner.prepare_channel(py, maxsize)?)
        } else {
            None
        };
        if changed {
            self.inner.retire_for_replacement(py, &mut registry);
            registry.set_configured(callbacks);
            registry.maxsize = maxsize;
            self.inner.publish(py, &mut registry);
        }
        if registry.halt == Halt::Lifecycle {
            registry.halt = Halt::Running;
        }
        if let Some(channel) = prepared {
            self.inner.install_channel(py, &mut registry, channel);
        } else if registry.channel.is_none() {
            self.inner.active.store(false, Ordering::Release);
        }
        Ok(())
    }

    /// Stop dispatch and remove configured sinks while preserving runtime
    /// registrations; dispatch stays halted until the next `configure` (or
    /// `sync` with callbacks). See `stop` for `stall_timeout` and the return
    /// value.
    #[pyo3(signature = (*, stall_timeout=None))]
    fn disable(&self, py: Python<'_>, stall_timeout: Option<f64>) -> usize {
        let channels = {
            let mut registry = self.inner.lock_registry();
            self.inner.retire_active(&mut registry);
            registry.configured.clear();
            registry.eligible.clear();
            registry.halt(Halt::Lifecycle);
            self.inner.publish(py, &mut registry);
            registry.live_channels()
        };
        self.inner
            .drain(py, channels, stall_duration(stall_timeout))
    }

    /// Stop the active dispatch queue while preserving registrations; dispatch
    /// stays halted until the next `configure` (or `sync` with callbacks).
    ///
    /// `drain` is accepted for API symmetry; both values join behind every
    /// accepted queue entry, as the Python dispatcher always did.
    ///
    /// `stall_timeout` bounds the wait for a sink that stops returning; the
    /// number of records abandoned is returned (zero when every delivery
    /// completed).
    #[pyo3(signature = (*, drain, stall_timeout=None))]
    fn stop(&self, py: Python<'_>, drain: bool, stall_timeout: Option<f64>) -> usize {
        let _ = drain;
        let channels = {
            let mut registry = self.inner.lock_registry();
            self.inner.retire_active(&mut registry);
            registry.halt(Halt::Lifecycle);
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
    pub(crate) fn idle(&self) -> bool {
        !self.inner.active.load(Ordering::Acquire)
    }

    /// Queue a record, reserving its sinks before removal can drain them.
    ///
    /// Logs emitted inside a sink callback, on the dispatch worker or during a
    /// raw stdlib delivery, bypass callable delivery. They still reach the
    /// native writer, but cannot recursively feed or block their own worker.
    #[pyo3(signature = (line, level, *, overflow))]
    pub(crate) fn enqueue(
        &self,
        py: Python<'_>,
        line: String,
        level: i64,
        overflow: &str,
    ) -> PyResult<bool> {
        if in_callback() {
            return Ok(true);
        }
        let blocking = overflow == "block";
        let (channel, lease_id, sinks) = {
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
                    // A bridged stdlib record may carry any level number.
                    if registry.eligible.len() < MAX_CACHED_LEVELS {
                        registry.eligible.insert(level, Arc::clone(&sinks));
                    }
                    sinks
                }
            };
            if sinks.is_empty() {
                return Ok(true);
            }
            let Some(lease_id) = channel.reserve() else {
                return Ok(false);
            };
            for sink in sinks.iter() {
                if sink.token > 0 {
                    *registry.outstanding.entry(sink.token).or_insert(0) += 1;
                }
            }
            (channel, lease_id, sinks)
        };
        // The lease covers the gap between selecting sinks and queue insertion.
        // Removal sees this producer even before its record enters the queue.
        let record = Record {
            line,
            sinks: Arc::clone(&sinks),
            level,
            accepted_id: 0,
        };
        let offered = match channel.offer(lease_id, record, blocking) {
            // Only a full queue pays for detaching: the worker needs the
            // interpreter to free a slot.
            Offer::WouldBlock(record) => py.detach(|| channel.put_blocking(lease_id, record)),
            offered => offered,
        };
        match offered {
            Offer::Accepted => return Ok(true),
            Offer::Full => {
                self.inner.settle(py, &sinks);
                self.inner.note_drop(py);
            }
            // Counted by the flush that abandoned the generation, not as a
            // queue-full drop.
            Offer::Abandoned | Offer::WouldBlock(_) => self.inner.settle(py, &sinks),
        }
        Ok(false)
    }

    /// Return callable dispatch queue, drop, and error metrics.
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
        result.set_item("callable_errors", self.inner.errors.load(Ordering::Acquire))?;
        result.set_item("callable_depth", depth)?;
        result.set_item("callable_maxsize", registry.maxsize)?;
        Ok(result)
    }

    /// Reset the drop and error counters for isolated tests.
    fn reset_drop_count(&self) {
        self.inner.dropped.store(0, Ordering::Release);
        self.inner.errors.store(0, Ordering::Release);
    }

    /// Build the dispatcher a forked child continues with.
    ///
    /// Worker threads do not survive fork and a lock held by a vanished thread
    /// can never be acquired, so nothing inherited is touched: registrations
    /// come from the lock-free snapshot, drop and error counts carry over, queued
    /// records are not delivered in the child, and finalizers that removal
    /// deferred run now since nothing waits on the parent's counts. The caller
    /// replaces its reference; the inherited dispatcher is never used again.
    #[pyo3(signature = (*, enabled, defer_finalizers=false))]
    fn fork_child(&self, py: Python<'_>, enabled: bool, defer_finalizers: bool) -> PyResult<Self> {
        // The surviving thread may have forked from inside a callback; the
        // child does not continue that delivery.
        CALLBACK_DEPTH.with(|depth| depth.set(0));
        // Announce before loading the pointer so publication defers releasing
        // its Arc until this reader has incremented the strong count. This
        // never acquires a mutex, including when another thread held the
        // registry at fork. An inherited reader count may defer reclamation
        // in the old dispatcher, which the child replaces rather than reuses.
        self.inner.snapshot_readers.fetch_add(1, Ordering::SeqCst);
        let pointer = self.inner.snapshot.load(Ordering::SeqCst);
        // SAFETY: construction publishes a non-null Arc, and the reader count
        // protects its lifetime until this reader owns a strong reference.
        let snapshot = unsafe {
            Arc::increment_strong_count(pointer);
            Arc::from_raw(pointer)
        };
        self.inner.snapshot_readers.fetch_sub(1, Ordering::SeqCst);
        self.inner.submissions.fork_child();
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
            (
                self.inner.dropped.load(Ordering::Acquire),
                self.inner.errors.load(Ordering::Acquire),
            ),
            Arc::clone(&self.inner.submissions),
        );
        {
            let mut registry = child.inner.lock_registry();
            if enabled && registry.has_sinks() {
                child.inner.start_channel(py, &mut registry)?;
            }
        }
        *child
            .inner
            .fork_finalizers
            .lock()
            .unwrap_or_else(PoisonError::into_inner) = snapshot
            .finalizers
            .iter()
            .map(|finalizer| finalizer.clone_ref(py))
            .collect();
        if !defer_finalizers {
            child._finish_fork(py);
        }
        Ok(child)
    }

    /// Finish child cleanup only after the runtime publishes this dispatcher.
    /// A finalizer can log or configure; it must never reenter inherited locks.
    fn _finish_fork(&self, py: Python<'_>) {
        let callbacks = std::mem::take(
            &mut *self
                .inner
                .fork_finalizers
                .lock()
                .unwrap_or_else(PoisonError::into_inner),
        );
        for finalizer in callbacks {
            let _ = finalizer.bind(py).call0();
        }
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
