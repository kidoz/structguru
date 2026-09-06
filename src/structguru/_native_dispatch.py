"""Bounded background dispatch for Python callable logging sinks."""

from __future__ import annotations

import itertools
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import NamedTuple

# Threads currently inside a sink callback: dispatch workers for their whole
# lifetime, raw stdlib deliveries (``structguru.core``) for each callback.
_callback_state = threading.local()


def in_callback() -> bool:
    """True while the current thread is inside a sink callback."""
    return getattr(_callback_state, "depth", 0) > 0


@contextmanager
def callback_scope() -> Iterator[None]:
    """Mark the current thread as inside a sink callback for the block."""
    depth = getattr(_callback_state, "depth", 0)
    _callback_state.depth = depth + 1
    try:
        yield
    finally:
        _callback_state.depth = depth


@dataclass(frozen=True)
class _Sink:
    token: int
    callback: Callable[[str], None]
    min_level: int
    level_callback: Callable[[str, int], None] | None = None


class _Record(NamedTuple):
    line: str
    sinks: tuple[_Sink, ...]
    level: int = 20


class _WorkQueue:
    """The deque and counter ``queue.Queue`` used to own, guarded by the channel lock."""

    __slots__ = ("items", "unfinished_tasks")

    def __init__(self) -> None:
        self.items: deque[_Record] = deque()
        self.unfinished_tasks = 0

    def qsize(self) -> int:
        return len(self.items)


class _DispatchChannel:
    """One queue generation with producer-aware shutdown semantics.

    One lock guards the queue, producer leases, and accepting flag. Producers
    reserve delivery while holding the sink registry lock, then wait for queue
    space outside that lock. Retirement drains all outstanding reservations.
    """

    def __init__(self, maxsize: int) -> None:
        self.queue = _WorkQueue()
        self.maxsize = maxsize
        lock = threading.Lock()
        # State changes producers, flushers, and lifecycle callers wait on:
        # retirement, a freed slot, the last lease released, the last task done.
        self._condition = threading.Condition(lock)
        # Only the worker waits here, so a producer's notify() can never wake a
        # flusher instead of the worker.
        self._not_empty = threading.Condition(lock)
        self._accepting = True
        self._producers = 0
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _full(self) -> bool:
        return 0 < self.maxsize <= len(self.queue.items)

    def _append(self, record: _Record) -> None:
        self.queue.items.append(record)
        self.queue.unfinished_tasks += 1
        self._not_empty.notify()

    def _release_lease(self) -> None:
        self._producers -= 1
        if self._producers == 0:
            self._condition.notify_all()
            self._not_empty.notify_all()

    def reserve(self) -> bool:
        """Reserve one producer before a lifecycle transition can close the queue."""
        with self._condition:
            if not self._accepting:
                return False
            self._producers += 1
            return True

    def put_reserved(self, record: _Record, *, overflow: str) -> bool:
        """Insert a previously reserved record and release its producer lease."""
        with self._condition:
            try:
                if self._full():
                    if overflow != "block":
                        return False
                    while self._full():
                        self._condition.wait()
                self._append(record)
                return True
            finally:
                self._release_lease()

    def flush(self) -> None:
        """Wait for producers already using this generation and all queued work."""
        if threading.current_thread() is self.thread:
            return
        with self._condition:
            self._condition.wait_for(
                lambda: self._producers == 0 and self.queue.unfinished_tasks == 0
            )
            retired = not self._accepting
        if retired:
            self.thread.join()

    def retire(self) -> None:
        """Reject new producers without waiting for callbacks or queue space."""
        with self._condition:
            self._accepting = False
            self._condition.notify_all()
            self._not_empty.notify_all()

    def close(self, *, drain: bool) -> None:
        """Reject new producers and stop after every accepted producer finishes."""
        self.retire()
        if threading.current_thread() is self.thread:
            return
        if drain:
            self.flush()
        self.thread.join()

    def _loop(self) -> None:
        _callback_state.depth = 1  # every callback this thread runs is nested
        items = self.queue.items
        while True:
            with self._not_empty:
                # A leased producer may still append after retirement, so the
                # worker only stops once the queue is empty and no lease is held.
                self._not_empty.wait_for(
                    lambda: bool(items) or (not self._accepting and self._producers == 0)
                )
                if not items:
                    return
                item = items.popleft()
                if self._producers:
                    self._condition.notify_all()  # a producer may wait for this slot
            for sink in item.sinks:
                try:
                    if sink.level_callback is not None:
                        sink.level_callback(item.line, item.level)
                    else:
                        sink.callback(item.line)
                except BaseException:  # worker callbacks cannot interrupt the caller
                    pass
            with self._condition:
                self.queue.unfinished_tasks -= 1
                if self.queue.unfinished_tasks == 0:
                    self._condition.notify_all()


class CallableSinkDispatcher:
    """Own callable-sink registration, queueing, lifecycle, and metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._configured: list[_Sink] = []
        self._runtime: dict[int, _Sink] = {}
        self._tokens = itertools.count(1)
        self._maxsize = 1024
        self._channel: _DispatchChannel | None = None
        # Retired generations remain visible until drained, so concurrent
        # remove/flush/shutdown cannot overlook callbacks still using a sink.
        self._channels: set[_DispatchChannel] = set()
        self._dropped = 0
        # Per-level eligibility cache, accessed under the registry lock and
        # invalidated on every registration change.
        self._eligible: dict[int, tuple[_Sink, ...]] = {}

    def add(
        self,
        callback: Callable[[str], None],
        min_level: int,
        *,
        enabled: bool,
        level_callback: Callable[[str, int], None] | None = None,
    ) -> int:
        """Register a runtime sink, starting dispatch when logging is enabled."""
        token = next(self._tokens)
        with self._lock:
            self._runtime[token] = _Sink(token, callback, min_level, level_callback)
            self._eligible = {}
            if enabled and self._channel is None:
                self._channel = _DispatchChannel(self._maxsize)
                self._channels.add(self._channel)
        return token

    def remove(self, token: int) -> bool:
        """Remove one sink, then drain every record that captured it."""
        with self._lock:
            removed = self._runtime.pop(token, None) is not None
            self._eligible = {}
            if not (self._configured or self._runtime):
                self._retire_active()
            channels = tuple(self._channels)
        self._drain(channels)
        return removed

    def configure(
        self,
        callbacks: Iterable[Callable[[str], None]],
        *,
        maxsize: int,
    ) -> None:
        """Atomically activate a replacement queue and drain its predecessor."""
        new_configured = [
            _Sink(-index, callback, 0) for index, callback in enumerate(callbacks, 1)
        ]
        with self._lock:
            self._retire_active()
            channels = tuple(self._channels)
            self._configured = new_configured
            self._eligible = {}
            self._maxsize = maxsize
            self._dropped = 0
            if self._configured or self._runtime:
                self._channel = _DispatchChannel(maxsize)
                self._channels.add(self._channel)
        self._drain(channels)

    def disable(self) -> None:
        """Stop dispatch and remove configured sinks while preserving runtime registrations."""
        with self._lock:
            self._retire_active()
            self._configured = []
            self._eligible = {}
            channels = tuple(self._channels)
        self._drain(channels)

    def _retire_active(self) -> None:
        """Retire the active generation; caller must hold ``_lock``."""
        if self._channel is not None:
            self._channel.retire()
            self._channel = None

    def _drain(self, channels: tuple[_DispatchChannel, ...]) -> None:
        """Wait outside state locks, except when invoked from a sink callback."""
        # A callback cannot wait for its own worker, nor for a worker that may be
        # waiting on it: a stdlib handler on the raw root-logger path runs its
        # emit() under the handler lock, and a queued native delivery to that
        # same handler blocks on that lock on the worker thread. Both cases are
        # marked by the shared callback scope. An external lifecycle call will
        # still find and drain every retired generation in _channels.
        if not in_callback():
            for channel in channels:
                channel.flush()
        with self._lock:
            self._channels.difference_update(
                channel for channel in channels if not channel.thread.is_alive()
            )

    def idle(self) -> bool:
        """True when no callable sink can receive a line.

        Read without the lock: the attribute read is atomic, and a stale
        ``True`` (a sink being added concurrently) only means this record
        predates the sink, exactly as if it had been logged one call sooner.
        """
        return self._channel is None

    def enqueue(self, line: str, level: int, *, overflow: str) -> bool:
        """Queue a record, reserving its sinks before removal can drain them.

        Logs emitted inside a sink callback, on the dispatch worker or during a
        raw stdlib delivery, bypass callable delivery. They still reach the
        native writer, but cannot recursively feed or block their own worker.
        """
        if in_callback():
            return True
        with self._lock:
            channel = self._channel
            if channel is None:
                return True
            sinks = self._eligible.get(level)
            if sinks is None:
                sinks = tuple(
                    sink
                    for sink in (*self._configured, *self._runtime.values())
                    if level >= sink.min_level
                )
                self._eligible[level] = sinks
            if not sinks:
                return True
            if not channel.reserve():
                return False
        # The lease covers the gap between selecting sinks and queue insertion.
        # Removal sees this producer even before its record enters the queue.
        accepted = channel.put_reserved(_Record(line, sinks, level), overflow=overflow)
        if not accepted:
            self._note_drop()
        return accepted

    def flush(self) -> None:
        """Block until all queued deliveries have completed."""
        with self._lock:
            channels = tuple(self._channels)
        self._drain(channels)

    def stop(self, *, drain: bool) -> None:
        """Stop the active dispatch queue while preserving registrations."""
        with self._lock:
            self._retire_active()
            channels = tuple(self._channels)
        # Even drain=False previously joined behind all accepted queue entries.
        self._drain(channels)

    def after_fork(self, *, enabled: bool) -> None:
        """Replace inherited synchronization state and restart in a forked child."""
        # The surviving thread may have forked from inside a callback; the
        # child does not continue that delivery.
        _callback_state.depth = 0
        self._lock = threading.Lock()
        self._eligible = {}
        self._channel = (
            _DispatchChannel(self._maxsize)
            if enabled and (self._configured or self._runtime)
            else None
        )
        self._channels = {self._channel} if self._channel is not None else set()

    def metrics(self) -> dict[str, int]:
        """Return callable dispatch queue and drop metrics."""
        with self._lock:
            channel = self._channel
            return {
                "callable_dropped": self._dropped,
                "callable_depth": channel.queue.qsize() if channel is not None else 0,
                "callable_maxsize": self._maxsize,
            }

    def reset_drop_count(self) -> None:
        """Reset the drop counter for isolated tests."""
        with self._lock:
            self._dropped = 0

    def _note_drop(self) -> None:
        with self._lock:
            self._dropped += 1
            dropped = self._dropped
        if dropped == 1 or dropped % 1000 == 0:
            import warnings

            warnings.warn(
                f"structguru callable sinks dropped {dropped} delivery record(s): queue full",
                stacklevel=4,
            )
