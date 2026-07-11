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
    level: int


class CallableSinkDispatcher:
    """Own callable-sink registration, queueing, lifecycle, and metrics."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._configured: list[_Sink] = []
        self._runtime: dict[int, _Sink] = {}
        self._tokens = itertools.count(1)
        self._maxsize = 1024
        self._queue: queue.Queue[_Record | object] | None = None
        self._thread: threading.Thread | None = None
        self._stop_token = object()
        self._dropped = 0

    def add(self, callback: Callable[[str], None], min_level: int, *, enabled: bool) -> int:
        """Register a runtime sink, starting dispatch when logging is enabled."""
        token = next(self._tokens)
        with self._lock:
            self._runtime[token] = _Sink(token, callback, min_level)
            should_start = enabled and (self._thread is None or not self._thread.is_alive())
            if should_start and self._queue is None:
                self._queue = queue.Queue(maxsize=self._maxsize)
        if should_start:
            self._start()
        return token

    def remove(self, token: int) -> bool:
        """Drain prior records and remove one runtime sink token."""
        self.flush()
        with self._lock:
            removed = self._runtime.pop(token, None) is not None
            should_stop = not (self._configured or self._runtime)
        if should_stop and threading.current_thread() is not self._thread:
            self.stop(drain=True)
        return removed

    def configure(
        self,
        callbacks: Iterable[Callable[[str], None]],
        *,
        maxsize: int,
    ) -> None:
        """Drain old configured sinks and activate a replacement set."""
        new_configured = [
            _Sink(-index, callback, 0) for index, callback in enumerate(callbacks, 1)
        ]
        self.stop(drain=True)
        with self._lock:
            self._configured = new_configured
            self._maxsize = maxsize
            self._dropped = 0
            self._queue = (
                queue.Queue(maxsize=maxsize) if self._configured or self._runtime else None
            )
        if self._queue is not None:
            self._start()

    def disable(self) -> None:
        """Stop dispatch and remove configured sinks while preserving runtime registrations."""
        self.stop(drain=True)
        with self._lock:
            self._configured = []

    def enqueue(self, line: str, level: int, *, overflow: str) -> bool:
        """Queue one rendered record; return false when drop mode rejects it."""
        dispatch_queue = self._queue
        if dispatch_queue is None:
            return True
        record = _Record(line, level)
        if overflow == "block":
            dispatch_queue.put(record)
            return True
        try:
            dispatch_queue.put_nowait(record)
        except queue.Full:
            self._note_drop()
            return False
        return True

    def flush(self) -> None:
        """Block until all queued deliveries have completed."""
        dispatch_queue = self._queue
        if dispatch_queue is not None and threading.current_thread() is not self._thread:
            dispatch_queue.join()

    def stop(self, *, drain: bool) -> None:
        """Stop the dispatch thread, optionally draining pending deliveries."""
        thread = self._thread
        dispatch_queue = self._queue
        if thread is not None and thread.is_alive() and dispatch_queue is not None:
            if threading.current_thread() is thread:
                dispatch_queue.put(self._stop_token)
                self._thread = None
                self._queue = None
                return
            if drain:
                dispatch_queue.join()
            dispatch_queue.put(self._stop_token)
            thread.join()
        self._thread = None
        self._queue = None

    def after_fork(self, *, enabled: bool) -> None:
        """Replace inherited synchronization state and restart in a forked child."""
        self._lock = threading.Lock()
        self._thread = None
        self._queue = (
            queue.Queue(maxsize=self._maxsize)
            if enabled and (self._configured or self._runtime)
            else None
        )
        if self._queue is not None:
            self._start()

    def metrics(self) -> dict[str, int]:
        """Return callable dispatch queue and drop metrics."""
        dispatch_queue = self._queue
        return {
            "callable_dropped": self._dropped,
            "callable_depth": dispatch_queue.qsize() if dispatch_queue is not None else 0,
            "callable_maxsize": self._maxsize,
        }

    def reset_drop_count(self) -> None:
        """Reset the drop counter for isolated tests."""
        self._dropped = 0

    def _start(self) -> None:
        dispatch_queue = self._queue
        if dispatch_queue is None:
            return
        self._thread = threading.Thread(target=self._loop, args=(dispatch_queue,), daemon=True)
        self._thread.start()

    def _loop(self, dispatch_queue: queue.Queue[_Record | object]) -> None:
        while True:
            item = dispatch_queue.get()
            if item is self._stop_token:
                dispatch_queue.task_done()
                return
            assert isinstance(item, _Record)
            with self._lock:
                sinks = [*self._configured, *self._runtime.values()]
            for sink in sinks:
                if item.level >= sink.min_level:
                    try:
                        sink.callback(item.line)
                    except Exception:  # noqa: BLE001 - sinks must never break logging
                        pass
            dispatch_queue.task_done()

    def _note_drop(self) -> None:
        self._dropped += 1
        if self._dropped == 1 or self._dropped % 1000 == 0:
            import warnings

            warnings.warn(
                f"structguru callable sinks dropped {self._dropped} delivery record(s): "
                "queue full",
                stacklevel=4,
            )
