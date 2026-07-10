# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [1.0.0] - 2026-07-10

### Changed (breaking)

- **structlog and orjson are no longer dependencies.** The native Rust renderer
  is the only rendering path. The wheel ships with zero runtime dependencies
  (only optional integration extras). `configure_structlog()` is now a
  compatibility shim that wires the native renderer to the configured stream.
- **Native mode is always-on** (no `enable_native()` call needed). It auto-enables
  at import time. Set `STRUCTGURU_LEGACY=1` to opt out.
- **`configure_queued_logging()` removed.** Use `enable_native()` (already the
  default) for off-thread I/O.
- **`InterceptHandler` removed** (`integrations/stdlib.py`). Foreign stdlib
  records go through stdlib's own logging; native mode does not intercept them.
- **`build_shared_processors`/`build_formatter_processors`/`orjson_serializer`
  removed** from `config.py` (structlog processor chains no longer needed).
- **`_StructlogMsgFixer`/`_install_exc_info_record_factory` removed** (structlog
  `ProcessorFormatter` artifacts, no longer relevant).
- **Integrations rewritten** to use structguru's own `Logger` and local
  `_contextvars` module instead of `structlog.get_logger`/`structlog.contextvars`.
- **`SamplingProcessor`/`RateLimitingProcessor`**: `structlog.DropEvent` replaced
  with a local `structguru.sampling.DropEvent` sentinel.

### Added

- `structguru._contextvars` — a lightweight `contextvars.ContextVar[dict]`-backed
  replacement for `structlog.contextvars`, used by `core.py` and all integrations.

### Notes

- structlog and orjson remain as **test-only** dependencies (`[dependency-groups].test`)
  for the golden parity suite, which compares native output against the standard
  structlog/orjson path. Production code has no dependency on either.

## [Unreleased]

### Added

- **Native exotic-value conversion (orjson-free native path).** The native Rust
  renderer now converts `datetime`, `date`, `UUID`, `Enum`, and dataclasses
  natively via the objects' own `isoformat()`/`str()`/`.value` methods, producing
  byte-identical output to the previous orjson delegation. The native path no
  longer imports `orjson` at all. Unsupported types (`bytes`, `set`, `Decimal`)
  raise `TypeError`, matching the orjson rejection contract. The `Value::Raw`
  reverse path uses `json.loads` (stdlib) instead of `orjson.loads`.
- **Native mode is now the default.** The Rust renderer is auto-enabled at import
  time (no `enable_native()` call needed). `configure_structlog()` opts back into
  the standard structlog path (disabling native, so output lands on the configured
  stream). Set `STRUCTGURU_LEGACY=1` to opt out of auto-enable entirely. The
  `STRUCTGURU_NATIVE` env var is now a no-op (deprecated).
- **Native Sentry integration.** `enable_native(sentry_processor=...)` invokes a
  structlog-style processor (e.g. `SentryProcessor`) for every kept record on
  the caller's thread, mirroring the `metric_processor` hook. The raw `exc_info`
  is passed so `_resolve_exception` works; when redaction is configured
  (`sensitive_keys`/`sensitive_patterns`), the hook injects
  `REDACTED_MARKER_KEY` so the processor's `require_redaction` guard recognizes
  that native Rust redaction already ran. `SentryProcessor` itself is unchanged.
- **Native rotating-file sink.** `enable_native(file_path=...)` writes rendered
  lines to a rotating file natively (append mode, size-based rotation). Defaults
  mirror `logging.handlers.RotatingFileHandler` (50 MB, 5 backups); configure via
  `file_max_bytes`/`file_backup_count`. Set `also_stdout=True` to mirror output
  to both file and stdout.
- **Native callable sinks.** `enable_native(callable_sinks=[fn, ...])` invokes
  `Callable[[str], None]` with each rendered line. They run on a dedicated
  daemon thread (never the Rust writer, which must not touch the GIL), so a
  blocking callable cannot deadlock the logging path. Callable errors are
  swallowed.
- **Native console renderer.** `enable_native(json=False)` renders colored,
  human-readable lines instead of JSON — structguru's own stable dev format
  (`<timestamp> [<LEVEL>] <message>  k=v`), with ANSI colors by default on a
  TTY. Override with `colors=True/False`. Not a `ConsoleRenderer` clone.
- **Native metric hooks.** `enable_native(metric_processor=...)` invokes a
  structlog-style processor (e.g. `MetricProcessor`) for every kept record on
  the caller's thread before rendering, with the pre-`EventRenamer` event-dict
  shape (`{"event": message, **fields}`). Records dropped by level filtering,
  sampling, or rate limiting never reach it; hook errors never break logging.
- **Native level-gated sampling.** `enable_native(sample_max_level=...)`
  restricts sampling to records at or below the given level; more severe
  records always pass — the native analog of wrapping `SamplingProcessor` in
  `ConditionalProcessor(max_level=...)`.
- **Native structured exceptions.** `enable_native(structured_exceptions=True)`
  renders the `exception` field as the structured dict produced by
  `ExceptionDictProcessor` (type/message/module/frames, chained cause, optional
  locals with redaction and repr truncation) instead of the formatted traceback
  string. The `exception_include_locals`, `exception_max_frames`, and
  `exception_max_local_repr` knobs mirror the processor's parameters;
  `sensitive_keys` is reused for locals redaction. Extraction is shared with
  the processor via the new `structguru.exceptions.build_exception_dict()`.
- **Native `stack_info` support.** `logger.info(..., stack_info=True)` (and
  `logger.opt(stack_info=True)`) no longer falls back to the standard structlog
  path. The stack is captured in Python and rendered by the native renderer in
  the same position as `StackInfoRenderer` (`stack` between `service` and
  `message`). The native stack ends at the user's calling frame —
  structguru-internal frames are skipped, the way structlog skips its own.
- **Golden byte-parity suite.** `tests/test_parity_golden.py` runs every
  scenario through both the standard and native paths and asserts the JSON
  lines are byte-identical (modulo timestamp), locking key order and
  serialization format for the full-Rust migration.

- **Group-preserving pattern replacement.**
  `enable_native(pattern_replacement=...)` sets the substitution text for
  `sensitive_patterns` matches and supports capture-group expansion (`$1`,
  `${name}`; `$$` for a literal `$`). This covers the main look-behind use case
  on the linear-time engine: `(?<=password=)\S+` becomes pattern
  `(password=)\S+` with `pattern_replacement="$1[REDACTED]"`.

### Changed

- **`configure()` is now the primary logging configuration API.**
  `enable_native()` remains as a backward-compatible alias, and
  `configure_structlog()` delegates to `configure()`. `configure_structlog()`
  now emits `DeprecationWarning` and will be removed in v2.0.
- **`configure_queued_logging()` is deprecated.** It emits a `DeprecationWarning`
  and will be removed in 1.0. Native mode (`enable_native()`) already offloads
  log I/O to a background thread, making the `QueueHandler`/`QueueListener`
  wrapper redundant. Use `enable_native(file_path=...)` or
  `enable_native(callable_sinks=[...])` for off-thread output.
- **Unsupported `sensitive_patterns` now raise `ValueError` at
  `enable_native()`** instead of emitting a `UserWarning` and silently leaving
  native mode disabled. Rust's `regex` engine guarantees linear-time matching
  (no ReDoS on the hot path) and therefore rejects backreferences and
  look-around; redaction that silently differs from the configuration is worse
  than a setup-time error. The error message includes rewrite guidance
  (migration plan decision gate B).

### Fixed

- **Redaction marker never leaks into native output.** The internal
  `_structguru_redacted` marker key is stripped by the native renderer,
  matching `strip_redaction_marker` on the standard path.
- **Native key order now matches structlog exactly.** User fields colliding
  with standard keys (`level`, `severity`, `logger`, `timestamp`) are
  overridden *in place* (previously dropped and re-appended), and contextvars
  are appended after event fields with setdefault semantics (previously
  prepended), matching `merge_contextvars`.

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
