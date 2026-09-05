"""Bounded background dispatch for Python callable logging sinks."""

from __future__ import annotations

import itertools
import queue
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass


@dataclass(frozen=True)
class _Sink:
    token: int
    callback: Callable[[str], None]
    min_level: int


@dataclass(frozen=True)
class _Record:
    line: str
    sinks: tuple[_Sink, ...]


class _DispatchChannel:
    """One queue generation with producer-aware shutdown semantics."""

    def __init__(self, maxsize: int) -> None:
        self.queue: queue.Queue[_Record] = queue.Queue(maxsize=maxsize)
        self.maxsize = maxsize
        self._condition = threading.Condition()
        self._accepting = True
        self._producers = 0
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def reserve(self) -> bool:
        """Reserve one producer before a lifecycle transition can close the queue."""
        with self._condition:
            if not self._accepting:
                return False
            self._producers += 1
            return True

    def put_reserved(self, record: _Record, *, overflow: str) -> bool:
        """Insert a previously reserved record and release its producer lease."""
        try:
            if overflow == "block":
                self.queue.put(record)
                return True
            try:
                self.queue.put_nowait(record)
            except queue.Full:
                return False
            return True
        finally:
            with self._condition:
                self._producers -= 1
                self._condition.notify_all()

    def flush(self) -> None:
        """Wait for producers already using this generation and all queued work."""
        if threading.current_thread() is self.thread:
            return
        with self._condition:
            while self._producers:
                self._condition.wait()
        self.queue.join()
        with self._condition:
            retired = not self._accepting
        if retired:
            self.thread.join()

    def retire(self) -> None:
        """Reject new producers without waiting for callbacks or queue space."""
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def close(self, *, drain: bool) -> None:
        """Reject new producers and stop after every accepted producer finishes."""
        self.retire()
        if threading.current_thread() is self.thread:
            return
        if drain:
            self.flush()
        self.thread.join()

    def _loop(self) -> None:
        while True:
            with self._condition:
                # Producers notify after insertion, including when a reserved
                # producer finishes after retirement. No stop token can be left
                # behind if several callers close the same generation.
                self._condition.wait_for(
                    lambda: (
                        not self.queue.empty() or (not self._accepting and self._producers == 0)
                    )
                )
                if self.queue.empty():
                    return
                item = self.queue.get_nowait()
            for sink in item.sinks:
                try:
                    sink.callback(item.line)
                except Exception:  # noqa: BLE001 - sinks must never break logging
                    pass
            self.queue.task_done()


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

    def add(self, callback: Callable[[str], None], min_level: int, *, enabled: bool) -> int:
        """Register a runtime sink, starting dispatch when logging is enabled."""
        token = next(self._tokens)
        with self._lock:
            self._runtime[token] = _Sink(token, callback, min_level)
            if enabled and self._channel is None:
                self._channel = _DispatchChannel(self._maxsize)
                self._channels.add(self._channel)
        return token

    def remove(self, token: int) -> bool:
        """Remove one sink, then drain every record that captured it."""
        with self._lock:
            removed = self._runtime.pop(token, None) is not None
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
            channels = tuple(self._channels)
        self._drain(channels)

    def _retire_active(self) -> None:
        """Retire the active generation; caller must hold ``_lock``."""
        if self._channel is not None:
            self._channel.retire()
            self._channel = None

    def _drain(self, channels: tuple[_DispatchChannel, ...]) -> None:
        """Wait outside state locks, except when invoked by a sink callback."""
        current = threading.current_thread()
        # A callback cannot wait for itself, or for another generation whose
        # callback may be waiting for this one. An external lifecycle call will
        # still find and drain every retired generation in _channels.
        if not any(current is channel.thread for channel in channels):
            for channel in channels:
                channel.flush()
        with self._lock:
            self._channels.difference_update(
                channel for channel in channels if not channel.thread.is_alive()
            )

    def enqueue(self, line: str, level: int, *, overflow: str) -> bool:
        """Queue one rendered record; return false when delivery is rejected."""
        with self._lock:
            sinks = tuple(
                sink
                for sink in (*self._configured, *self._runtime.values())
                if level >= sink.min_level
            )
            channel = self._channel
            if not sinks or channel is None:
                return True
            reserved = channel.reserve()
        if not reserved:
            return False

        accepted = channel.put_reserved(_Record(line, sinks), overflow=overflow)
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
        self._lock = threading.Lock()
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
