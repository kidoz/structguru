from __future__ import annotations

import structguru._rust as rust


def test_native_string_queue_unbounded_fifo_order() -> None:
    queue = rust._NativeStringQueue(0)

    assert queue.maxsize == 0
    assert queue.depth() == 0
    assert queue.try_dequeue() is None

    assert queue.try_enqueue("first")
    assert queue.try_enqueue("second")

    assert queue.depth() == 2
    assert queue.try_dequeue() == "first"
    assert queue.try_dequeue() == "second"
    assert queue.try_dequeue() is None
    assert queue.metrics() == {
        "enqueued": 2,
        "dropped": 0,
        "dequeued": 2,
        "depth": 0,
        "maxsize": 0,
    }


def test_native_string_queue_bounded_drop_metrics() -> None:
    queue = rust._NativeStringQueue(2)

    assert queue.maxsize == 2
    assert queue.try_enqueue("first")
    assert queue.try_enqueue("second")
    assert not queue.try_enqueue("third")

    assert queue.depth() == 2
    assert queue.metrics() == {
        "enqueued": 2,
        "dropped": 1,
        "dequeued": 0,
        "depth": 2,
        "maxsize": 2,
    }
    assert queue.try_dequeue() == "first"
    assert queue.try_dequeue() == "second"


def test_native_string_queue_accepts_enqueue_after_dequeue() -> None:
    queue = rust._NativeStringQueue(1)

    assert queue.try_enqueue("first")
    assert not queue.try_enqueue("dropped")
    assert queue.try_dequeue() == "first"
    assert queue.try_enqueue("second")

    assert queue.try_dequeue() == "second"
    assert queue.metrics() == {
        "enqueued": 2,
        "dropped": 1,
        "dequeued": 2,
        "depth": 0,
        "maxsize": 1,
    }
