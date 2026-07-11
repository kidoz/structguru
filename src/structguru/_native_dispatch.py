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
        self.queue: queue.Queue[_Record | object] = queue.Queue(maxsize=maxsize)
        self.maxsize = maxsize
        self._condition = threading.Condition()
        self._accepting = True
        self._producers = 0
        self._close_from_worker = False
        self._stop_token = object()
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

    def close(self, *, drain: bool) -> None:
        """Reject new producers and stop after every accepted producer finishes."""
        is_worker = threading.current_thread() is self.thread
        with self._condition:
            self._accepting = False
            self._condition.notify_all()
            if is_worker:
                # The callback currently running on the worker may itself disable
                # logging. Let the loop drain accepted producers and exit naturally;
                # blocking here would deadlock a producer waiting for queue space.
                self._close_from_worker = True
                return
            while self._producers:
                self._condition.wait()

        if drain:
            self.queue.join()
        self.queue.put(self._stop_token)
        self.thread.join()

    def _loop(self) -> None:
        while True:
            item = self.queue.get()
            if item is self._stop_token:
                self.queue.task_done()
                return
            assert isinstance(item, _Record)
            for sink in item.sinks:
                try:
                    sink.callback(item.line)
                except Exception:  # noqa: BLE001 - sinks must never break logging
                    pass
            self.queue.task_done()

            with self._condition:
                should_exit = (
                    self._close_from_worker and self._producers == 0 and self.queue.empty()
                )
            if should_exit:
                return


class CallableSinkDispatcher:
    """Own callable-sink registration, queueing, lifecycle, and metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._transition_lock = threading.RLock()
        self._configured: list[_Sink] = []
        self._runtime: dict[int, _Sink] = {}
        self._tokens = itertools.count(1)
        self._maxsize = 1024
        self._channel: _DispatchChannel | None = None
        self._dropped = 0

    def add(self, callback: Callable[[str], None], min_level: int, *, enabled: bool) -> int:
        """Register a runtime sink, starting dispatch when logging is enabled."""
        token = next(self._tokens)
        with self._transition_lock, self._lock:
            self._runtime[token] = _Sink(token, callback, min_level)
            if enabled and self._channel is None:
                self._channel = _DispatchChannel(self._maxsize)
        return token

    def remove(self, token: int) -> bool:
        """Remove one sink, then drain every record that captured it."""
        with self._transition_lock:
            with self._lock:
                removed = self._runtime.pop(token, None) is not None
                channel = self._channel
                should_stop = not (self._configured or self._runtime)
                if should_stop:
                    self._channel = None
            if channel is not None:
                if should_stop:
                    channel.close(drain=True)
                else:
                    channel.flush()
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
        with self._transition_lock:
            with self._lock:
                old_channel = self._channel
                self._configured = new_configured
                self._maxsize = maxsize
                self._dropped = 0
                self._channel = (
                    _DispatchChannel(maxsize) if self._configured or self._runtime else None
                )
            if old_channel is not None:
                old_channel.close(drain=True)

    def disable(self) -> None:
        """Stop dispatch and remove configured sinks while preserving runtime registrations."""
        with self._transition_lock:
            with self._lock:
                channel = self._channel
                self._channel = None
                self._configured = []
            if channel is not None:
                channel.close(drain=True)

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
            channel = self._channel
        if channel is not None:
            channel.flush()

    def stop(self, *, drain: bool) -> None:
        """Stop the active dispatch queue while preserving registrations."""
        with self._transition_lock:
            with self._lock:
                channel = self._channel
                self._channel = None
            if channel is not None:
                channel.close(drain=drain)

    def after_fork(self, *, enabled: bool) -> None:
        """Replace inherited synchronization state and restart in a forked child."""
        self._lock = threading.Lock()
        self._transition_lock = threading.RLock()
        self._channel = (
            _DispatchChannel(self._maxsize)
            if enabled and (self._configured or self._runtime)
            else None
        )

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
