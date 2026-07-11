"""Environment parsing for native structguru startup configuration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def native_config_from_env(environ: Mapping[str, str]) -> dict[str, Any] | None:
    """Parse native configuration, returning ``None`` for the explicit opt-out."""
    if environ.get("STRUCTGURU_LEGACY", "").strip().lower() in ("1", "true", "yes", "on"):
        return None

    config: dict[str, Any] = {
        "service": environ.get("STRUCTGURU_SERVICE", "app"),
        "level": environ.get("LOG_LEVEL", "INFO"),
        "target": environ.get("STRUCTGURU_NATIVE_TARGET", "stdout"),
    }
    if sample_rate := environ.get("STRUCTGURU_NATIVE_SAMPLE_RATE"):
        config["sample_rate"] = float(sample_rate)
    if rate_limit := environ.get("STRUCTGURU_NATIVE_RATE_LIMIT"):
        max_count, _, period = rate_limit.partition("/")
        config["rate_limit_max"] = int(max_count)
        if period:
            config["rate_limit_period"] = float(period)
    return config
