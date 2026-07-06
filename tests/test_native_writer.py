from __future__ import annotations

import structguru._rust as rust


def test_native_string_writer_drains_messages_in_order() -> None:
    writer = rust._NativeStringWriter(0)

    assert writer.maxsize == 0
    assert writer.try_enqueue("first")
    assert writer.try_enqueue("second")
    writer.flush()

    assert writer.messages() == ["first", "second"]
    assert writer.metrics() == {
        "enqueued": 2,
        "dropped": 0,
        "dequeued": 2,
        "written": 2,
        "sink_errors": 0,
        "depth": 0,
        "maxsize": 0,
        "in_flight": 0,
        "closed": False,
        "worker_done": False,
        "paused": False,
    }
    writer.close()


def test_native_string_writer_bounded_drop_metrics() -> None:
    writer = rust._NativeStringWriter(1, paused=True)

    assert writer.try_enqueue("kept")
    assert not writer.try_enqueue("dropped")
    assert writer.metrics()["dropped"] == 1
    assert writer.metrics()["depth"] == 1

    writer.resume()
    writer.flush()

    assert writer.messages() == ["kept"]
    assert writer.metrics()["written"] == 1
    writer.close()


def test_native_string_writer_close_is_idempotent_and_rejects_new_messages() -> None:
    writer = rust._NativeStringWriter(0)

    assert writer.try_enqueue("before close")
    writer.close()
    writer.close()

    assert not writer.try_enqueue("after close")
    assert writer.messages() == ["before close"]
    assert writer.metrics()["closed"] is True
    assert writer.metrics()["worker_done"] is True


def test_native_string_writer_counts_sink_errors_without_stopping() -> None:
    writer = rust._NativeStringWriter(0, fail_after=1)

    assert writer.try_enqueue("first")
    assert writer.try_enqueue("second")
    assert writer.try_enqueue("third")
    writer.flush()

    assert writer.messages() == ["first"]
    assert writer.metrics()["enqueued"] == 3
    assert writer.metrics()["dequeued"] == 3
    assert writer.metrics()["written"] == 1
    assert writer.metrics()["sink_errors"] == 2
    writer.close()
