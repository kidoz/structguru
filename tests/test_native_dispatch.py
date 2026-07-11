"""Concurrency regressions for callable-sink queue generations."""

from __future__ import annotations

import threading

from structguru._native_dispatch import _DispatchChannel, _Record, _Sink


def test_close_drains_a_producer_reserved_before_retirement() -> None:
    """A producer acknowledged by the old generation cannot become orphaned."""
    received: list[str] = []
    channel = _DispatchChannel(maxsize=1)
    assert channel.reserve()

    closer = threading.Thread(target=channel.close, kwargs={"drain": True})
    closer.start()
    with channel._condition:
        assert channel._condition.wait_for(lambda: not channel._accepting, timeout=1)

    accepted = channel.put_reserved(
        _Record("accepted before close", (_Sink(1, received.append, 0),)),
        overflow="block",
    )
    closer.join(timeout=2)

    assert accepted
    assert not closer.is_alive()
    assert received == ["accepted before close"]
    assert channel.queue.unfinished_tasks == 0
