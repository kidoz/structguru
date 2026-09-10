"""Level names and numbers, sourced from the native extension.

The Rust core owns the table (``LEVEL_TABLE`` in ``structguru-core``); this
module builds the Python lookups from it once at import, so the facade, the
settings validation, the stdlib bridge, and the native filters can never
disagree about what a level name means.
"""

from __future__ import annotations

import importlib
import logging


def _load_level_table() -> tuple[tuple[str, int], ...]:
    """Read the core's table, failing loudly when the extension is missing."""
    try:
        module = importlib.import_module("structguru._rust")
    except ModuleNotFoundError as exc:
        if exc.name != "structguru._rust":
            raise
        msg = "structguru requires its native extension, but structguru._rust is unavailable"
        raise RuntimeError(msg) from exc
    table: list[tuple[str, int]] = module.level_table()
    return tuple(table)


# ``(method name, number)`` in the core's display order; the canonical method
# for a number precedes its aliases (``warning`` before ``warn``).
_TABLE: tuple[tuple[str, int], ...] = _load_level_table()

# Logger method name -> the numeric level it emits at. TRACE and SUCCESS are
# ranked separately for thresholds although the renderer folds them into
# DEBUG and INFO.
METHOD_LEVELS: dict[str, int] = dict(_TABLE)

# Upper-case level name -> number, as accepted by ``configure(level=...)``,
# ``logger.add(level=...)`` and the stdlib bridge. ``NOTSET`` is the stdlib
# "all levels" threshold, not a logger method.
LEVELS: dict[str, int] = {
    "NOTSET": logging.NOTSET,
    **{name.upper(): number for name, number in _TABLE},
}

# Distinct numbers, descending, each with its canonical method name.
_CANONICAL_METHODS: tuple[tuple[int, str], ...] = tuple(
    sorted({number: name for name, number in reversed(_TABLE)}.items(), reverse=True)
)


def method_for_level_number(levelno: int) -> str:
    """Map a stdlib numeric level to the canonical method emitting at or below it.

    ``CRITICAL`` and above map to ``critical``, then ``error``, ``warning``,
    ``info``, ``debug``, and ``trace`` in turn; anything lower logs as ``debug``.
    """
    for number, name in _CANONICAL_METHODS:
        if levelno >= number:
            return name
    return "debug"
