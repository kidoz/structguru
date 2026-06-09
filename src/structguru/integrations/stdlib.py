"""Standard library logging InterceptHandler.

Routes standard library `logging` records into structguru's structlog
rendering pipeline.
"""

from __future__ import annotations

import logging
import sys

import structlog

from structguru.config import (
    build_formatter_processors,
    build_shared_processors,
    orjson_serializer,
)


def _structguru_handlers() -> list[logging.Handler]:
    """Return the root handlers installed by ``configure_structlog``.

    These are the handlers whose formatter is a structlog
    :class:`~structlog.stdlib.ProcessorFormatter`; forwarding a foreign record
    to them renders it through the same stream and processor chain that
    structguru already uses for its own logs.
    """
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(getattr(handler, "formatter", None), structlog.stdlib.ProcessorFormatter)
    ]


class InterceptHandler(logging.Handler):
    """Route stdlib logs into structguru's structlog rendering pipeline.

    ``configure_structlog`` already renders foreign stdlib records that reach
    the **root** logger, so you do not need this handler for the common case.
    Use it for a logger configured with ``propagate=False`` (or its own
    handlers) that would otherwise bypass that pipeline — e.g. a noisy library
    such as ``uvicorn.access``::

        import logging
        from structguru.integrations.stdlib import InterceptHandler

        access = logging.getLogger("uvicorn.access")
        access.handlers = [InterceptHandler()]
        access.propagate = False  # avoid double-logging via the root handler

    When ``configure_structlog`` has run, intercepted records are forwarded to
    the existing structguru handler(s) so they share the same stream, formatter,
    and processor chain. Otherwise the record is rendered as JSON to
    ``sys.stdout`` using structguru's default processor chain.
    """

    def __init__(self, *, service: str = "app") -> None:
        super().__init__()
        self._service = service
        self._fallback: structlog.stdlib.ProcessorFormatter | None = None

    def emit(self, record: logging.LogRecord) -> None:
        targets = _structguru_handlers()
        if targets:
            # Forward to the live structguru handler(s): the ProcessorFormatter
            # foreign_pre_chain renders the plain stdlib record exactly as if it
            # had propagated to the root logger. No new log call is made, so
            # there is no risk of an interception loop.
            for handler in targets:
                handler.handle(record)
            return

        try:
            self._render_fallback(record)
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def _render_fallback(self, record: logging.LogRecord) -> None:
        """Render a record when ``configure_structlog`` has not been called."""
        if self._fallback is None:
            renderer = structlog.processors.JSONRenderer(serializer=orjson_serializer)
            self._fallback = structlog.stdlib.ProcessorFormatter(
                processors=build_formatter_processors(renderer, json_mode=True),
                foreign_pre_chain=build_shared_processors(self._service),
            )
        sys.stdout.write(self._fallback.format(record) + "\n")
