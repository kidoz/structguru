"""Route standard-library logging into structguru's native pipeline.

Third-party libraries log through the stdlib ``logging`` module. Installing
the bridge attaches a handler to the root logger that re-emits every such record
through structguru's native renderer, so foreign logs share the same JSON /
console formatting, redaction, level filtering, and output stream as
``structguru.logger``::

    from structguru.integrations.stdlib import install_stdlib_bridge

    install_stdlib_bridge(level="INFO", suppress_loggers=("urllib3", "botocore"))

    import logging
import os
    logging.getLogger("sqlalchemy.engine").info("SELECT 1")
    # -> {"logger":"sqlalchemy.engine","level":"INFO",...,"message":"SELECT 1"}

For a logger that sets ``propagate=False`` (so its records never reach the root
logger), attach ``StructguruHandler`` to it directly.
"""

from __future__ import annotations

import functools
import logging
import os
import sys
import threading
from collections.abc import Iterable, Mapping
from typing import Any

from structguru import _runtime
from structguru._contextvars import _ctx
from structguru._levels import method_for_level_number
from structguru.config import _to_logging_level
from structguru.core import Logger, _safe_format, _set_stdlib_bridge_active
from structguru.integrations._stdlib_env import (
    optional_bool_from_env,
    stdlib_bridge_config_from_env,
)

# Attributes present on a vanilla LogRecord (plus the two the Formatter adds).
# Anything else in ``record.__dict__`` came from a user ``extra=`` mapping and is
# forwarded as a structured field.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
        "message",
        "asctime",
    }
)


@functools.lru_cache(maxsize=1024)
def _logger_for(name: str) -> Logger:
    """Return a cached structguru ``Logger`` bound to *name*.

    The base logger is immutable in practice — ``bind()``/``opt()`` return fresh
    copies — so a shared per-name instance is safe to reuse across threads.
    """
    return Logger(name=name)


def _record_extras(record: logging.LogRecord) -> dict[str, Any]:
    """Extract user-supplied ``extra=`` fields from a record."""
    fields: dict[str, Any] = {}
    for key, value in record.__dict__.items():
        if key in _STANDARD_RECORD_ATTRS:
            continue
        # The former bind(**extras) rejected non-string keys before invoking
        # getMessage(). Retain that boundary without allocating a Logger.
        if type(key) is not str and not issubclass(type(key), str):
            msg = "keywords must be strings"
            raise TypeError(msg)
        fields[key] = value
    return fields


class StructguruHandler(logging.Handler):
    """A ``logging.Handler`` that re-emits stdlib records via structguru.

    The record's logger name becomes the ``logger`` field, its level selects the
    structguru method, ``extra=`` fields are forwarded as structured fields, and
    ``exc_info`` and ``stack_info`` are carried through. The already-formatted
    message is passed verbatim (no brace re-formatting), so literal ``{...}`` in
    a message is never misinterpreted.

    The level threshold and sampling judge a record by its own ``levelno``, so a
    custom level such as 25 ranks above ``INFO`` even though it renders as
    ``INFO``. The ``timestamp`` field is the record's ``created`` time, so a
    record replayed or delivered late through a ``QueueListener`` keeps the
    time it was logged.

    An ``extra=`` value the native renderer cannot represent — Django's
    ``log_response()`` attaches the raw request as ``extra["request"]``, for
    example — is replaced by a ``<unsupported: WSGIRequest>`` marker rather than
    failing the record, at any nesting depth. The record and its ``extra``
    objects are never modified, so later handlers still see the originals.
    """

    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level)
        self._existing_logger_states: list[tuple[logging.Logger, bool, bool]] = []

    def handle(self, record: logging.LogRecord) -> bool:
        """Apply handler filters and forward without holding an I/O lock.

        The native runtime synchronizes delivery itself. Holding the stdlib
        handler lock while waiting for callable queue space would deadlock if
        that queue's worker logs through this bridge before freeing a slot.
        """
        filtered = self.filter(record)
        if not filtered:
            return False
        # Python 3.12+ filters can substitute a record for this handler alone.
        if isinstance(filtered, logging.LogRecord):
            record = filtered
        self.emit(record)
        return True

    def emit(self, record: logging.LogRecord) -> None:
        """Re-emit one stdlib record through the structguru pipeline.

        structguru's own records are skipped, which keeps the bridge from
        looping and stops native output being wrapped a second time.
        """
        # Never intercept structguru's own records: it keeps the bridge from
        # looping and stops native output being wrapped a second time.
        if record.name == "structguru" or record.name.startswith("structguru."):
            return
        try:
            result = (
                None
                if (
                    record.exc_info
                    or record.stack_info
                    or type(_runtime._callable_dispatcher) is not _runtime._NATIVE_DISPATCHER_TYPE
                )
                else _runtime.submit_stdlib(
                    record,
                    _runtime.current_runtime,
                    _safe_format,
                    _ctx.get,
                    sys._getframe,
                    _runtime._callable_dispatcher,
                    _STANDARD_RECORD_ATTRS,
                )
            )
            if isinstance(result, int):
                if result:
                    _runtime._note_enqueue_outcome(result)
                return
            if result is not None:
                (
                    state,
                    name,
                    method,
                    message,
                    extras,
                    controls,
                    created,
                    levelno,
                    has_stack,
                ) = result
                if state is None:
                    return
                target = _logger_for(name)
                if has_stack:
                    target._log_slow(
                        state,
                        method,
                        "{}",
                        (message,),
                        controls,
                        levelno=levelno,
                        created=created,
                        _bound_fields=extras or None,
                    )
                else:
                    target._log_slow(
                        state,
                        method,
                        message,
                        (),
                        controls,
                        levelno=levelno,
                        created=created,
                        _bound_fields=extras or None,
                    )
                return

            # An unusual non-string logger name retains the original failure path.
            target = _logger_for(record.name)
            extras = _record_extras(record)
            if extras and target._bound:
                extras = {**target._bound, **extras}
            controls = {}
            if record.exc_info:
                controls["exc_info"] = record.exc_info
            method = method_for_level_number(record.levelno)
            if method == "trace":
                method = "debug"
            if record.stack_info:
                target._log(
                    method,
                    "{}",
                    (record.getMessage(),),
                    {**controls, "stack_info": record.stack_info},
                    levelno=record.levelno,
                    created=record.created,
                    _bound_fields=extras or None,
                )
            else:
                target._log(
                    method,
                    record.getMessage(),
                    (),
                    controls,
                    levelno=record.levelno,
                    created=record.created,
                    _bound_fields=extras or None,
                )
        except Exception as exc:  # noqa: BLE001 - a logging handler must never raise
            # handleError prints the original record, traceback and arguments.
            # Diagnostics must not bypass native redaction on a failed emit.
            if logging.raiseExceptions:
                try:
                    name = ascii(type(exc).__name__[:80]).encode("ascii")
                    os.write(2, b"structguru: stdlib emit failed (" + name + b")\n")
                except OSError:
                    pass


def _apply_suppression(names: Iterable[str], level: str) -> None:
    threshold = _to_logging_level(level)
    for name in names:
        logging.getLogger(name).setLevel(threshold)


def suppress_loggers(*names: str, level: str = "WARNING") -> None:
    """Raise the level threshold of the named loggers.

    Use this to quiet noisy third-party loggers (``"urllib3"``,
    ``"botocore"``, ``"sqlalchemy.engine"``, …) regardless of whether they reach
    structguru through the bridge or their own handlers.
    """
    _apply_suppression(names, level)


_bridge_lock = threading.RLock()
_active_bridge: StructguruHandler | None = None


def _after_fork() -> None:
    """Replace bridge synchronization inherited from vanished threads."""
    global _bridge_lock
    _bridge_lock = threading.RLock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


def _apply_existing_logger_policy(
    disable_existing_loggers: bool | None,
) -> list[tuple[logging.Logger, bool, bool]]:
    """Apply a dictConfig-like policy to currently registered named loggers."""
    if disable_existing_loggers is None:
        return []

    states: list[tuple[logging.Logger, bool, bool]] = []
    for candidate in list(logging.root.manager.loggerDict.values()):
        if not isinstance(candidate, logging.Logger):
            continue
        previous = candidate.disabled
        if previous != disable_existing_loggers:
            states.append((candidate, previous, disable_existing_loggers))
            candidate.disabled = disable_existing_loggers
    return states


def _restore_existing_logger_states(bridge: StructguruHandler) -> None:
    """Restore logger states that have not changed since bridge installation."""
    for logger, previous, applied in bridge._existing_logger_states:
        if logger.disabled == applied:
            logger.disabled = previous
    bridge._existing_logger_states.clear()


def _release_bridge(bridge: StructguruHandler) -> None:
    """Release bridge-owned state; caller holds ``_bridge_lock``, then ``logging._lock``."""
    global _active_bridge
    logging.getLogger().removeHandler(bridge)
    bridge.close()
    _restore_existing_logger_states(bridge)
    if _active_bridge is bridge:
        _active_bridge = None
        _set_stdlib_bridge_active(False)


def _install_stdlib_bridge_resolved(
    *,
    level: str,
    suppress_loggers: Iterable[str],
    suppress_level: str,
    clear_handlers: bool,
    disable_existing_loggers: bool | None,
    replace: bool,
) -> StructguruHandler:
    """Install a bridge after all environment-backed options are resolved."""
    global _active_bridge
    root = logging.getLogger()
    threshold = _to_logging_level(level)
    suppression_threshold = _to_logging_level(suppress_level)
    suppressed_names = tuple(suppress_loggers)

    # The bridge lock serializes replacement before the module lock protects
    # root handlers and existing logger states; every path uses this order.
    with _bridge_lock, logging._lock:  # type: ignore[attr-defined]
        if _active_bridge is not None:
            if not replace and _active_bridge in root.handlers:
                msg = "the structguru stdlib bridge is already installed"
                raise RuntimeError(msg)
            # The caller opted into replacement, or detached the handler
            # directly. Release the old bridge — restoring its policy snapshot
            # — before the new install applies its own policy and (with
            # ``clear_handlers``) sweeps the root handlers.
            _release_bridge(_active_bridge)

        bridge = StructguruHandler(threshold)
        bridge._existing_logger_states = _apply_existing_logger_policy(disable_existing_loggers)
        if clear_handlers:
            for handler in list(root.handlers):
                root.removeHandler(handler)
        # Suspend raw root delivery for `logger.add()` sinks before the bridge
        # goes live, so no record is ever delivered through both paths.
        _set_stdlib_bridge_active(True)
        root.addHandler(bridge)
        root.setLevel(threshold)
        for name in suppressed_names:
            logging.getLogger(name).setLevel(suppression_threshold)
        _active_bridge = bridge
        return bridge


def install_stdlib_bridge(
    *,
    level: str = "INFO",
    suppress_loggers: Iterable[str] = (),
    suppress_level: str = "WARNING",
    clear_handlers: bool = True,
    disable_existing_loggers: bool | None = None,
    replace: bool = False,
) -> StructguruHandler:
    """Route root stdlib logging through structguru and quiet noisy loggers.

    Installs a ``StructguruHandler`` on the root logger so any library
    logging through ``logging`` renders through structguru's native path.
    Pass the returned handler to ``uninstall_stdlib_bridge`` to undo it.

    Sinks registered with ``logger.add()`` already receive third-party records
    directly from the root logger, unrendered. The bridge delivers those same
    records to those same sinks through the native renderer, so installing it
    suspends the raw root-logger delivery; uninstalling restores it. Without
    that, every third-party record would reach each sink twice — once raw, once
    rendered.

    Parameters
    ----------
    level:
        Level set on the root logger — the floor for which stdlib records
        propagate to the bridge. structguru's own native level (set via
        ``structguru.configure``) still applies on top.
    suppress_loggers:
        Logger names to raise to ``suppress_level`` (see ``suppress_loggers``).
    suppress_level:
        Level applied to ``suppress_loggers`` (default ``"WARNING"``).
    clear_handlers:
        Remove existing root handlers first (default ``True``), so records are
        not also emitted by a previously configured stdlib handler.
    disable_existing_loggers:
        ``True`` disables named loggers that already exist when the bridge is
        installed, and ``False`` re-enables them, matching the corresponding
        ``logging.config.dictConfig`` policy. ``None`` (the default) reads
        ``STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS`` and leaves existing logger
        states unchanged when the variable is unset. Loggers created later are
        unaffected.
    replace:
        ``True`` releases an already-installed managed bridge first — with full
        ``uninstall_stdlib_bridge`` semantics, including restoring its
        existing-loggers snapshot — and then installs the new one, so the last
        call wins. With the default ``False``, installing while a bridge is
        still attached raises ``RuntimeError``. With no active bridge,
        ``replace=True`` behaves exactly like a plain install. The swap runs in
        one critical section: a record logged by another thread during it is
        delivered at most once — rendered by the outgoing or incoming bridge,
        raw, or not at all — never twice, and never by raising. Suppression
        levels applied by the previous install are not reverted, and calling
        ``uninstall_stdlib_bridge`` on the replaced handler is a no-op.
    """
    if disable_existing_loggers is None:
        disable_existing_loggers = optional_bool_from_env(
            os.environ, "STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS"
        )
    return _install_stdlib_bridge_resolved(
        level=level,
        suppress_loggers=suppress_loggers,
        suppress_level=suppress_level,
        clear_handlers=clear_handlers,
        disable_existing_loggers=disable_existing_loggers,
        replace=replace,
    )


def install_stdlib_bridge_from_env(
    environ: Mapping[str, str] | None = None,
) -> StructguruHandler:
    """Install the stdlib bridge using environment configuration.

    Reading the environment is explicit so importing ``structguru`` never
    mutates the application's root logger. ``environ`` is injectable for tests;
    normal applications should omit it to use ``os.environ``.
    """
    config = stdlib_bridge_config_from_env(os.environ if environ is None else environ)
    return _install_stdlib_bridge_resolved(
        level=config.level,
        suppress_loggers=config.suppress_loggers,
        suppress_level=config.suppress_level,
        clear_handlers=config.clear_handlers,
        disable_existing_loggers=config.disable_existing_loggers,
        replace=config.replace,
    )


def uninstall_stdlib_bridge(bridge: StructguruHandler) -> None:
    """Detach *bridge* from the root logger and restore direct sink delivery.

    Reverses ``install_stdlib_bridge``: third-party records stop flowing
    through the native renderer, and ``logger.add()`` sinks are re-attached to
    the root logger so they keep receiving them (raw, as before the install).
    Root handlers removed by ``clear_handlers=True`` are not restored.
    Suppression levels applied via ``suppress_loggers`` are not reverted.

    Calling this with a handler that has already been released — uninstalled
    earlier, or superseded by ``install_stdlib_bridge(replace=True)`` — is a
    no-op that never disturbs the currently active bridge.
    """
    with _bridge_lock, logging._lock:  # type: ignore[attr-defined]
        _release_bridge(bridge)
