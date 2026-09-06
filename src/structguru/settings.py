"""Validated, reusable configuration for the native logging runtime."""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, TypedDict, Unpack

from structguru._native_env import native_options_from_env

_LEVELS = {
    "NOTSET": 0,
    "TRACE": 5,
    "DEBUG": 10,
    "INFO": 20,
    "SUCCESS": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
    "EXCEPTION": 40,
    "CRITICAL": 50,
    "FATAL": 50,
}


def _level_number(level: str | int) -> int:
    if isinstance(level, int) and not isinstance(level, bool) and level >= 0:
        return level
    if isinstance(level, str) and level.upper() in _LEVELS:
        return _LEVELS[level.upper()]
    msg = (
        f"level must be a known name ({', '.join(_LEVELS)}) "
        f"or a non-negative integer, got {level!r}"
    )
    raise ValueError(msg)


class SettingsChanges(TypedDict, total=False):
    """Typed keyword overrides accepted by configuration functions."""

    service: str
    maxsize: int
    target: str
    overflow: str
    level: str | int
    otel: bool
    sensitive_keys: Sequence[str] | None
    sensitive_patterns: Sequence[str] | None
    pattern_replacement: str
    allow_backtracking_patterns: bool
    sample_rate: float
    sample_max_level: str | None
    rate_limit_max: int | None
    rate_limit_period: float
    metric_processor: Any
    sentry_processor: Any
    structured_exceptions: bool
    exception_include_locals: bool
    exception_max_frames: int
    exception_max_local_repr: int
    exception_carets: bool
    format: str
    colors: bool | None
    file_path: str | None
    file_max_bytes: int
    file_backup_count: int
    also_stdout: bool
    callable_sinks: Sequence[Callable[[str], None]] | None
    callable_queue_maxsize: int
    stream_sink: Any


@dataclass(frozen=True)
class Settings:
    """Describe a native runtime without opening sinks or starting workers.

    Defaults match :func:`structguru.configure`. Collections are copied to
    tuples; callbacks and streams retain their identity and ownership. Python
    values are validated here. Native regex compilation and file access are
    validated when applying the settings, before replacing a working runtime.
    """

    service: str = "app"
    maxsize: int = 8192
    target: str = "stdout"
    overflow: str = "block"
    level: str | int = "INFO"
    otel: bool = False
    sensitive_keys: Sequence[str] | None = None
    sensitive_patterns: Sequence[str] | None = None
    pattern_replacement: str = "[REDACTED]"
    allow_backtracking_patterns: bool = False
    sample_rate: float = 1.0
    sample_max_level: str | None = None
    rate_limit_max: int | None = None
    rate_limit_period: float = 60.0
    metric_processor: Any = None
    sentry_processor: Any = None
    structured_exceptions: bool = False
    exception_include_locals: bool = False
    exception_max_frames: int = 20
    exception_max_local_repr: int = 200
    exception_carets: bool = True
    format: str = "json"
    colors: bool | None = None
    file_path: str | None = None
    file_max_bytes: int = 50 * 1024 * 1024
    file_backup_count: int = 5
    also_stdout: bool = False
    callable_sinks: Sequence[Callable[[str], None]] | None = None
    callable_queue_maxsize: int = 1024
    stream_sink: Any = None

    def __post_init__(self) -> None:
        """Validate scalar values and detach caller-owned collections."""
        _level_number(self.level)
        if isinstance(self.level, str):
            object.__setattr__(self, "level", self.level.upper())
        for name in ("service", "pattern_replacement"):
            if not isinstance(getattr(self, name), str):
                msg = f"{name} must be a string"
                raise ValueError(msg)
        if self.file_path is not None and not isinstance(self.file_path, str):
            msg = "file_path must be a string or None"
            raise ValueError(msg)
        for name, allowed in (
            ("target", ("stdout", "null", "memory")),
            ("overflow", ("block", "drop")),
            ("format", ("json", "console")),
        ):
            if getattr(self, name) not in allowed:
                msg = f"{name} must be one of {allowed}, got {getattr(self, name)!r}"
                raise ValueError(msg)
        for name in (
            "otel",
            "allow_backtracking_patterns",
            "structured_exceptions",
            "exception_include_locals",
            "exception_carets",
            "also_stdout",
            "colors",
        ):
            value = getattr(self, name)
            if name == "colors" and value is None:
                continue
            if not isinstance(value, bool):
                msg = f"{name} must be a boolean"
                raise ValueError(msg)
        for name, minimum in (
            ("maxsize", 0),
            ("file_max_bytes", 0),
            ("file_backup_count", 0),
            ("exception_max_frames", 0),
            ("exception_max_local_repr", 0),
            ("callable_queue_maxsize", 1),
            ("rate_limit_max", 1),
        ):
            value = getattr(self, name)
            if name == "rate_limit_max" and value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                msg = f"{name} must be an integer >= {minimum}, got {value!r}"
                raise ValueError(msg)
        for name in ("sample_rate", "rate_limit_period"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
            ):
                msg = f"{name} must be finite, got {value!r}"
                raise ValueError(msg)
        if not 0 <= self.sample_rate <= 1:
            msg = f"sample_rate must be between 0.0 and 1.0, got {self.sample_rate}"
            raise ValueError(msg)
        if self.rate_limit_period <= 0:
            msg = f"rate_limit_period must be > 0, got {self.rate_limit_period}"
            raise ValueError(msg)
        if self.sample_max_level is not None:
            if (
                not isinstance(self.sample_max_level, str)
                or self.sample_max_level.upper() not in _LEVELS
                or self.sample_max_level.upper() == "NOTSET"
            ):
                msg = f"sample_max_level must be a known level name, got {self.sample_max_level!r}"
                raise ValueError(msg)
            object.__setattr__(self, "sample_max_level", self.sample_max_level.upper())
        for name in ("sensitive_keys", "sensitive_patterns", "callable_sinks"):
            value = getattr(self, name)
            if value is None:
                continue
            if isinstance(value, str) or not isinstance(value, Sequence):
                msg = f"{name} must be a sequence or None"
                raise ValueError(msg)
            items = tuple(value)
            for item in items:
                if name == "callable_sinks":
                    if not callable(item):
                        msg = f"callable_sinks entries must be callable, got {type(item)!r}"
                        raise TypeError(msg)
                elif not isinstance(item, str):
                    msg = f"{name} entries must be strings"
                    raise ValueError(msg)
            object.__setattr__(self, name, items)
        for name in ("metric_processor", "sentry_processor"):
            value = getattr(self, name)
            if value is not None and not callable(value):
                msg = f"{name} must be callable, got {type(value)!r}"
                raise TypeError(msg)
        if self.stream_sink is not None and not callable(getattr(self.stream_sink, "write", None)):
            msg = "stream_sink must provide a write() method"
            raise ValueError(msg)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> Settings:
        """Overlay a mapping on built-in defaults, rejecting unknown keys.

        Values use the same Python types as the constructor; this method does
        not coerce strings, read files, or read the process environment.
        """
        unknown = values.keys() - {field.name for field in fields(cls)}
        if unknown:
            msg = f"unknown settings: {', '.join(sorted(unknown))}"
            raise ValueError(msg)
        return cls(**values)

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None, **changes: Unpack[SettingsChanges]
    ) -> Settings:
        """Overlay environment values on defaults, then explicit overrides.

        Only selected values are parsed: an explicit override also overrides an
        invalid environment value. Pass an empty mapping to ignore the process
        environment. Autoconfiguration switches affect import only.
        """
        values = native_options_from_env(os.environ if environ is None else environ, changes)
        values.update(changes)
        return cls.from_mapping(values)

    def to_mapping(self) -> dict[str, Any]:
        """Return a shallow snapshot, preserving stream and callback identity."""
        return {field.name: getattr(self, field.name) for field in fields(self)}
