//! Scoped admission leases for native and Python-assisted producer paths.
//!
//! No Python references cross a wait. A barrier snapshots exact lease IDs,
//! excludes the calling thread's reentrant work, and ignores later producers.

use pyo3::prelude::*;
use std::cell::RefCell;
use std::sync::atomic::{AtomicPtr, AtomicU32, Ordering};
use std::sync::{Arc, Condvar, Mutex};
use std::thread::ThreadId;
use std::time::{Duration, Instant};

#[derive(Default)]
struct State {
    next: u64,
    active: Vec<(u64, ThreadId)>,
}

struct SharedState {
    state: Mutex<State>,
    changed: Condvar,
}

impl SharedState {
    fn new(state: State) -> Self {
        Self {
            state: Mutex::new(state),
            changed: Condvar::new(),
        }
    }
}

thread_local! {
    static ACTIVE: RefCell<Vec<u64>> = const { RefCell::new(Vec::new()) };
}

pub(crate) struct Submissions {
    pid: AtomicU32,
    state: AtomicPtr<SharedState>,
}

impl Default for Submissions {
    fn default() -> Self {
        Self {
            pid: AtomicU32::new(std::process::id()),
            state: AtomicPtr::new(
                Arc::into_raw(Arc::new(SharedState::new(State::default()))).cast_mut(),
            ),
        }
    }
}

impl Drop for Submissions {
    fn drop(&mut self) {
        let pointer = self.state.load(Ordering::Acquire);
        if !pointer.is_null() {
            // SAFETY: the atomic pointer owns one Arc reference for the tracker.
            drop(unsafe { Arc::from_raw(pointer) });
        }
    }
}

pub(crate) struct Submission {
    tracker: Arc<Submissions>,
    id: Option<u64>,
}

impl Submissions {
    fn shared(&self) -> &SharedState {
        let pointer = self.state.load(Ordering::Acquire);
        // SAFETY: construction installs a non-null Arc pointer. Fork replacement
        // leaks the inherited pointer in the child, and normal Drop runs only
        // after every Submission and dispatcher owning this tracker is gone.
        unsafe { &*pointer }
    }

    pub(crate) fn begin(self: &Arc<Self>) -> Submission {
        let id = if self.pid.load(Ordering::Acquire) == std::process::id() {
            let mut state = self
                .shared()
                .state
                .lock()
                .unwrap_or_else(|p| p.into_inner());
            let id = state.next;
            state.next += 1;
            state.active.push((id, std::thread::current().id()));
            ACTIVE.with(|active| active.borrow_mut().push(id));
            Some(id)
        } else {
            None
        };
        Submission {
            tracker: Arc::clone(self),
            id,
        }
    }

    pub(crate) fn barrier(&self) -> u64 {
        if self.pid.load(Ordering::Acquire) != std::process::id() {
            return 0;
        }
        self.shared()
            .state
            .lock()
            .unwrap_or_else(|p| p.into_inner())
            .next
    }

    pub(crate) fn wait(&self, target: u64, timeout: Duration) -> usize {
        if self.pid.load(Ordering::Acquire) != std::process::id() {
            return 0;
        }
        let shared = self.shared();
        let own = std::thread::current().id();
        let mut state = shared.state.lock().unwrap_or_else(|p| p.into_inner());
        let pending = |s: &State| {
            s.active
                .iter()
                .filter(|(id, owner)| *id < target && *owner != own)
                .count()
        };
        let mut previous = pending(&state);
        let mut progress = Instant::now();
        while previous > 0 {
            let left = timeout.saturating_sub(progress.elapsed());
            if left.is_zero() {
                return previous;
            }
            state = shared
                .changed
                .wait_timeout(state, left)
                .unwrap_or_else(|p| p.into_inner())
                .0;
            let remaining = pending(&state);
            if remaining < previous {
                progress = Instant::now();
            }
            previous = remaining;
        }
        0
    }

    pub(crate) fn fork_child(&self) {
        let surviving = ACTIVE.with(|active| active.borrow().clone());
        let owner = std::thread::current().id();
        let next = surviving.iter().max().map_or(0, |id| id + 1);
        let replacement = SharedState::new(State {
            next,
            active: surviving.into_iter().map(|id| (id, owner)).collect(),
        });
        let replacement = Arc::into_raw(Arc::new(replacement)).cast_mut();
        let previous = self.state.swap(replacement, Ordering::AcqRel);
        self.pid.store(std::process::id(), Ordering::Release);
        // The inherited mutex and condvar may belong to a vanished thread. Leak
        // only the child copy; the parent's pointer remains owned normally.
        let _ = previous;
    }

    fn end(&self, id: u64) {
        ACTIVE.with(|active| active.borrow_mut().retain(|active_id| *active_id != id));
        if self.pid.load(Ordering::Acquire) != std::process::id() {
            return;
        }
        let shared = self.shared();
        let mut state = shared.state.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(index) = state.active.iter().position(|(lease, _)| *lease == id) {
            state.active.swap_remove(index);
            shared.changed.notify_all();
        }
    }
}

impl Drop for Submission {
    fn drop(&mut self) {
        if let Some(id) = self.id {
            self.tracker.end(id);
        }
    }
}

#[pyclass(name = "_SubmissionLease")]
pub(crate) struct SubmissionLease {
    submission: Option<Submission>,
}

impl SubmissionLease {
    pub(crate) fn new(submission: Submission) -> Self {
        Self {
            submission: Some(submission),
        }
    }
}

#[pymethods]
impl SubmissionLease {
    fn close(&mut self) {
        self.submission.take();
    }
}
