//! Scoped admission leases for the fused native producer path.
//!
//! No Python references cross a wait. A barrier snapshots exact lease IDs,
//! excludes the calling thread's reentrant work, and ignores later producers.

use std::sync::{Condvar, Mutex};
use std::thread::ThreadId;
use std::time::{Duration, Instant};

#[derive(Default)]
struct State {
    next: u64,
    active: Vec<(u64, ThreadId)>,
}

pub(crate) struct Submissions {
    pid: u32,
    state: Mutex<State>,
    changed: Condvar,
}

impl Default for Submissions {
    fn default() -> Self {
        Self {
            pid: std::process::id(),
            state: Mutex::default(),
            changed: Condvar::new(),
        }
    }
}

pub(crate) struct Submission<'a> {
    tracker: &'a Submissions,
    id: Option<u64>,
}

impl Submissions {
    pub(crate) fn begin(&self) -> Submission<'_> {
        let id = if self.pid == std::process::id() {
            let mut state = self.state.lock().unwrap_or_else(|p| p.into_inner());
            let id = state.next;
            state.next += 1;
            state.active.push((id, std::thread::current().id()));
            Some(id)
        } else {
            None
        };
        Submission { tracker: self, id }
    }

    pub(crate) fn barrier(&self) -> u64 {
        if self.pid != std::process::id() {
            return 0;
        }
        self.state.lock().unwrap_or_else(|p| p.into_inner()).next
    }

    pub(crate) fn wait(&self, target: u64, timeout: Duration) -> usize {
        if self.pid != std::process::id() {
            return 0;
        }
        let own = std::thread::current().id();
        let mut state = self.state.lock().unwrap_or_else(|p| p.into_inner());
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
            state = self
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
}

impl Drop for Submission<'_> {
    fn drop(&mut self) {
        if self.tracker.pid != std::process::id() {
            return;
        }
        if let Some(id) = self.id {
            let mut state = self.tracker.state.lock().unwrap_or_else(|p| p.into_inner());
            if let Some(index) = state.active.iter().position(|(lease, _)| *lease == id) {
                state.active.swap_remove(index);
                self.tracker.changed.notify_all();
            }
        }
    }
}
