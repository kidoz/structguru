"""Environment parsing for native structguru startup configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def autoconfigure_from_env(environ: Mapping[str, str]) -> bool:
    """Resolve the import-time switch, preferring its descriptive name."""
    if "STRUCTGURU_AUTOCONFIGURE" in environ:
        value = environ["STRUCTGURU_AUTOCONFIGURE"].strip().lower()
        if value in ("1", "true", "yes", "on"):
            return True
        if value in ("0", "false", "no", "off"):
            return False
        msg = "STRUCTGURU_AUTOCONFIGURE must be one of 1/0, true/false, yes/no, or on/off"
        raise ValueError(msg)
    return environ.get("STRUCTGURU_LEGACY", "").strip().lower() not in ("1", "true", "yes", "on")


def native_options_from_env(
    environ: Mapping[str, str], overrides: Mapping[str, Any]
) -> dict[str, Any]:
    """Read supported native options without parsing explicitly overridden values."""
    config: dict[str, Any] = {}
    for field, names in (
        ("service", ("STRUCTGURU_SERVICE",)),
        ("level", ("STRUCTGURU_LEVEL", "LOG_LEVEL")),
        ("target", ("STRUCTGURU_TARGET", "STRUCTGURU_NATIVE_TARGET")),
        ("format", ("STRUCTGURU_FORMAT",)),
        ("sample_rate", ("STRUCTGURU_SAMPLE_RATE", "STRUCTGURU_NATIVE_SAMPLE_RATE")),
    ):
        if field in overrides:
            continue
        for name in names:
            if name in environ:
                value = environ[name]
                if field == "sample_rate":
                    try:
                        config[field] = float(value)
                    except ValueError as exc:
                        msg = f"{name} must be a number, got {value!r}"
                        raise ValueError(msg) from exc
                else:
                    config[field] = value
                break
    if not {"rate_limit_max", "rate_limit_period"} <= overrides.keys():
        for name in ("STRUCTGURU_RATE_LIMIT", "STRUCTGURU_NATIVE_RATE_LIMIT"):
            if name not in environ:
                continue
            maximum, separator, period = environ[name].partition("/")
            try:
                if "rate_limit_max" not in overrides:
                    config["rate_limit_max"] = int(maximum)
                if separator and "rate_limit_period" not in overrides:
                    config["rate_limit_period"] = float(period)
            except ValueError as exc:
                msg = f"{name} must be MAX or MAX/PERIOD with a numeric period in seconds"
                raise ValueError(msg) from exc
            break
    return config
