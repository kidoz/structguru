use std::collections::VecDeque;
use std::sync::{Mutex, MutexGuard};

/// Snapshot of queue activity counters.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct QueueMetrics {
    pub enqueued: u64,
    pub dropped: u64,
    pub dequeued: u64,
}

struct QueueInner<T> {
    items: VecDeque<T>,
    metrics: QueueMetrics,
}

/// Nonblocking FIFO queue with Python `Queue(maxsize=0)` compatible capacity.
///
/// `maxsize == 0` means unbounded. Positive sizes drop new items when full and
/// increment the drop counter, which is the behavior we want for future
/// best-effort logging sinks.
pub struct BoundedQueue<T> {
    maxsize: usize,
    inner: Mutex<QueueInner<T>>,
}

impl<T> BoundedQueue<T> {
    pub fn new(maxsize: usize) -> Self {
        Self {
            maxsize,
            inner: Mutex::new(QueueInner {
                items: VecDeque::new(),
                metrics: QueueMetrics::default(),
            }),
        }
    }

    pub fn maxsize(&self) -> usize {
        self.maxsize
    }

    pub fn try_enqueue(&self, item: T) -> Result<(), T> {
        let mut inner = self.lock_inner();
        if self.maxsize > 0 && inner.items.len() >= self.maxsize {
            inner.metrics.dropped += 1;
            return Err(item);
        }

        inner.items.push_back(item);
        inner.metrics.enqueued += 1;
        Ok(())
    }

    pub fn try_dequeue(&self) -> Option<T> {
        let mut inner = self.lock_inner();
        let item = inner.items.pop_front();
        if item.is_some() {
            inner.metrics.dequeued += 1;
        }
        item
    }

    pub fn len(&self) -> usize {
        self.lock_inner().items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn metrics(&self) -> QueueMetrics {
        self.lock_inner().metrics
    }

    fn lock_inner(&self) -> MutexGuard<'_, QueueInner<T>> {
        self.inner
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unbounded_queue_preserves_fifo_order() {
        let queue = BoundedQueue::new(0);

        assert_eq!(queue.maxsize(), 0);
        assert!(queue.is_empty());
        assert_eq!(queue.try_dequeue(), None);

        assert_eq!(queue.try_enqueue("first"), Ok(()));
        assert_eq!(queue.try_enqueue("second"), Ok(()));

        assert_eq!(queue.len(), 2);
        assert_eq!(queue.try_dequeue(), Some("first"));
        assert_eq!(queue.try_dequeue(), Some("second"));
        assert_eq!(queue.try_dequeue(), None);
        assert!(queue.is_empty());
    }

    #[test]
    fn bounded_queue_drops_new_items_when_full() {
        let queue = BoundedQueue::new(2);

        assert_eq!(queue.try_enqueue("first"), Ok(()));
        assert_eq!(queue.try_enqueue("second"), Ok(()));
        assert_eq!(queue.try_enqueue("third"), Err("third"));

        assert_eq!(queue.len(), 2);
        assert_eq!(queue.try_dequeue(), Some("first"));
        assert_eq!(queue.try_dequeue(), Some("second"));
    }

    #[test]
    fn metrics_track_enqueue_drop_and_dequeue_counts() {
        let queue = BoundedQueue::new(1);

        assert_eq!(queue.try_enqueue("kept"), Ok(()));
        assert_eq!(queue.try_enqueue("dropped"), Err("dropped"));
        assert_eq!(queue.try_dequeue(), Some("kept"));
        assert_eq!(queue.try_dequeue(), None);

        assert_eq!(
            queue.metrics(),
            QueueMetrics {
                enqueued: 1,
                dropped: 1,
                dequeued: 1,
            },
        );
    }
}
