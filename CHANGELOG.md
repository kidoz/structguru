# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Native value-pattern redaction.** `enable_native(sensitive_patterns=[...])`
  applies compiled regex patterns to every string value (in addition to
  key-based redaction), mirroring `RedactingProcessor(patterns=...)` on the
  native fast path. Patterns are compiled once at enable time into a reusable
  `RedactionConfig`; Rust's `regex` engine does not support backreferences or
  look-around, so an unsupported pattern emits a `UserWarning` and falls back to
  the standard structlog path.
- **Native sampling & rate limiting.** `enable_native(sample_rate=...)`,
  `rate_limit_max=...`, and `rate_limit_period=...` add pre-render filters
  (implemented in Rust) so dropped records cost zero rendering. Drop counters
  `sampled` and `rate_limited` are reported via `native_metrics()`, distinct
  from the writer's transport `dropped` counter. New env vars:
  `STRUCTGURU_NATIVE_SAMPLE_RATE` (float) and
  `STRUCTGURU_NATIVE_RATE_LIMIT` (`"MAX/PERIOD"` seconds).

## [0.3.0] - 2026-07-09

### Added

- **Optional Rust accelerator (native mode).** An opt-in, off-thread native
  render path for the common JSON logging path, shipped as a compiled extension
  in binary wheels. Public API: `enable_native()`, `disable_native()`,
  `set_native_level()`, `native_metrics()`, `native_available()`, plus a
  `STRUCTGURU_NATIVE=1` environment toggle. See the README "Native mode" section.
  - Native rendering with byte-parity to the structlog JSON output (including
    redaction, level normalization, RFC 5424 severity, timestamps, and
    `datetime`/`UUID`/`Enum` values via orjson delegation).
  - Native support for **exceptions** (`exc_info`), **level filtering** (with a
    near-free disabled path), **custom redaction keys**, and **OpenTelemetry**
    trace-context injection.
  - Background writer with **fork safety** (respawns in gunicorn/celery prefork),
    **shutdown flush** (`atexit`), and a configurable **overflow policy**
    (`block` — the default, no loss — or `drop` with counted, rate-limited
    warnings).
- Wheel build matrix (manylinux, musllinux, macOS, Windows) via `maturin-action`
  with `abi3` wheels, `abi3audit`, and PyPI Trusted Publishing (OIDC) on tags.
- Rust CI checks (`cargo fmt`, `clippy`, tests) and an MSRV (1.89) gate.

### Changed

- **Minimum Python is now 3.11** (dropped 3.10); the `abi3` wheel baseline is
  `abi3-py311`.
- Build backend switched to **maturin**; structguru now ships as binary wheels.
  The Rust extension is an **optional accelerator** — the library works without
  it via the standard structlog path, so `enable_native()` is required to use it.
- Native mode applies **level filtering** to `logger` calls (native mode
  previously would have emitted below-threshold records); the standard path is
  unchanged.

### Notes

- Native mode is **opt-in and experimental**; it is not the default. It renders
  JSON to stdout/file. `stack_info`, console output, custom `logger.add()` sinks,
  and advanced processors (sampling/rate-limiting/routing/metrics) continue to
  use the standard structlog path.

## [0.2.0]

- Initial packaged release (pure-Python structlog wrapper).
