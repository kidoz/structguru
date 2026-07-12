"""Level-name helpers for structguru.

Since v1.0, structguru uses the native Rust renderer as its only logging path.
Runtime configuration lives in :func:`structguru.configure`; this module keeps
the level-name conversion helper shared by the integrations.
"""

from __future__ import annotations

import logging


def _to_logging_level(level_name: str) -> int:
    """Convert a human-readable level name to its :mod:`logging` constant."""
    upper_level = level_name.upper()
    if upper_level == "WARN":
        return logging.WARNING
    result: int | None = getattr(logging, upper_level, None)
    if not isinstance(result, int):
        import warnings

        warnings.warn(
            f"Unknown log level {level_name!r}, falling back to INFO",
            stacklevel=2,
        )
        return logging.INFO
    return result
