"""Environment parsing for the standard-library logging integration."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class StdlibBridgeEnvConfig:
    """Environment-derived options used to install the stdlib bridge."""

    level: str
    suppress_loggers: tuple[str, ...]
    suppress_level: str
    clear_handlers: bool
    disable_existing_loggers: bool | None
    replace: bool


def optional_bool_from_env(environ: Mapping[str, str], name: str) -> bool | None:
    """Return a strict optional boolean from *environ*."""
    if name not in environ:
        return None
    value = environ[name].strip().lower()
    if not value:
        return None
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    msg = f"{name} must be one of 1/0, true/false, yes/no, or on/off; got {environ[name]!r}"
    raise ValueError(msg)


def stdlib_bridge_config_from_env(environ: Mapping[str, str]) -> StdlibBridgeEnvConfig:
    """Parse stdlib bridge configuration from an environment mapping."""
    clear_handlers = optional_bool_from_env(environ, "STRUCTGURU_STDLIB_CLEAR_HANDLERS")
    replace = optional_bool_from_env(environ, "STRUCTGURU_STDLIB_REPLACE")
    logger_names = (
        name.strip() for name in environ.get("STRUCTGURU_STDLIB_SUPPRESS_LOGGERS", "").split(",")
    )
    return StdlibBridgeEnvConfig(
        level=environ.get("STRUCTGURU_STDLIB_LEVEL", environ.get("LOG_LEVEL", "INFO")),
        suppress_loggers=tuple(dict.fromkeys(name for name in logger_names if name)),
        suppress_level=environ.get("STRUCTGURU_STDLIB_SUPPRESS_LEVEL", "WARNING"),
        clear_handlers=True if clear_handlers is None else clear_handlers,
        disable_existing_loggers=optional_bool_from_env(
            environ, "STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS"
        ),
        replace=False if replace is None else replace,
    )
