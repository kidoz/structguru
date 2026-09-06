"""Level-name helpers for structguru.

Since v1.0, structguru uses the native Rust renderer as its only logging path.
Runtime configuration lives in :func:`structguru.configure`; this module keeps
the level-name conversion helper shared by the integrations.
"""

from __future__ import annotations

import logging

from structguru.settings import _LEVELS


def _to_logging_level(level_name: str) -> int:
    """Convert a level name to its :mod:`logging` number.

    Accepts every name the library recognizes elsewhere (``configure(level=...)``,
    ``logger.catch(level=...)``), so the loguru-style aliases ``TRACE``,
    ``SUCCESS``, ``EXCEPTION``, and ``FATAL`` gate sinks the same way their
    logging methods do. Unknown names warn and fall back to ``INFO``.
    """
    result = _LEVELS.get(level_name.upper())
    if result is None:
        import warnings

        warnings.warn(
            f"Unknown log level {level_name!r}, falling back to INFO",
            stacklevel=2,
        )
        return logging.INFO
    return result
