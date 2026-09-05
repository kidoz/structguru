# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [1.2.1] - 2026-09-05

### Fixed

- `stack_info` text is now redacted like every other field. The `stack` value
  was written to JSON and console output verbatim, so anything sensitive in a
  captured frame bypassed both `sensitive_patterns` and `sensitive_keys`.
  Pattern redaction now runs over the stack text, and listing `stack` in
  `sensitive_keys` replaces the whole value with `[REDACTED]`.
- Callable-sink lifecycle operations no longer deadlock or return early. A sink
  callback that called `shutdown()` or `disable()` while another thread was
  inside `configure()`, `logger.remove()`, `stop()`, or `shutdown()` deadlocked
  both threads; `flush()` or `disable()` racing such a callback returned before
  it had finished; and `logger.remove()` returned while a record from a retired
  queue generation was still being delivered to the removed sink. Retired
  generations now stay tracked until drained, waiting happens outside the state
  lock, and a callback never waits on its own worker.
- The Celery hook binds the running task's `task_id` / `task_name` after the
  propagated context instead of before it, so a task launched from inside
  another task no longer reports the parent's identity. Correlation fields such
  as `request_id` still propagate.
- `SentryProcessor` no longer forwards the raw `exc_info` object in breadcrumb
  data, the `structlog_event` extra, or tags. The SDK serializes arbitrary
  objects, so an exception message containing a secret bypassed redaction on
  those paths. The raw exception is now used only for `capture_exception`.

## [1.2.0] - 2026-08-27

### Added

- `get_contextvars()` is now exported from the package root. It returns a
  snapshot of the currently bound context and completes the public set
  alongside `bind_contextvars`, `bound_contextvars`, and `clear_contextvars`.
  Adapters that must capture context and restore it later — the
  snapshot/clear/restore pattern streaming handlers need — no longer have to
  import it from the private `structguru._contextvars`.

### Fixed

- The gRPC interceptor no longer leaves `grpc_method` / `request_id` bound
  after a streaming handler returns its response iterator. The iterator is
  lazy, so context stayed on the thread for the whole interval before
  iteration began — and indefinitely if the client cancelled and it was never
  consumed. The handler's context is now snapshotted and cleared on return,
  then restored when iteration actually starts.
- Corrected the documented look-behind rewrite for `sensitive_patterns`. The
  README and the `configure()` error message recommended `password=(\S+)`
  with `pattern_replacement="$1[REDACTED]"`, which captures the *secret* and
  re-emits it (`password=hunter2` → `hunter2[REDACTED]`). The capture group
  belongs around the prefix: `(password=)\S+`. Redaction behavior itself was
  always correct; only the guidance was wrong.

## [1.1.0] - 2026-08-25

### Added

- `install_stdlib_bridge(replace=True)` releases an already-installed managed
  bridge — with full `uninstall_stdlib_bridge` semantics, including restoring
  its existing-loggers snapshot — before installing the new one, so logging
  setup that runs more than once per process gets last-call-wins behavior
  instead of `RuntimeError`. `install_stdlib_bridge_from_env()` reads the same
  option from `STRUCTGURU_STDLIB_REPLACE`. Uninstalling a replaced (stale)
  handler is a documented no-op, and suppression levels from the previous
  install are kept.

## [1.0.6] - 2026-08-25

### Added

- The stdlib bridge now supports a reversible `disable_existing_loggers`
  policy and explicit environment configuration through
  `install_stdlib_bridge_from_env()`. The regular bridge installer and Django's
  `build_logging_config()` also read
  `STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS` when the code option is omitted.
- Added a runnable code/environment example for existing stdlib logger policy.

### Changed

- Managed stdlib bridge installation now rejects a second active bridge rather
  than allowing duplicate record delivery.

## [1.0.5] - 2026-07-25

### Fixed

- **An OpenTelemetry failure no longer breaks logging.** `add_otel_context` only
  caught `ImportError`, so any other exception from the installed SDK (broken
  context propagation, a provider raising from `get_current_span`) escaped
  `Logger._log` and took down every log call in the process. Trace enrichment now
  degrades to dropping the trace fields, matching the metric and Sentry hooks.
- **The stdlib bridge no longer delivers third-party records twice.**
  `logger.add()` attaches its sink to the root logger so it also receives foreign
  `logging` records raw, while the bridge routes those same records into the same
  sink already rendered. With both active, every third-party record arrived twice
  — once raw, once as JSON — breaking sinks that parse their input. Installing the
  bridge now suspends the raw root delivery; `uninstall_stdlib_bridge()` restores
  it. Previously the outcome also depended on call order, because
  `install_stdlib_bridge(clear_handlers=True)` silently detached sinks registered
  beforehand.
- **`logger.add(sink)` without `level=` now really accepts all levels.** It
  inherited the root logger's level for the stdlib delivery path, which is
  `WARNING` on an unconfigured root — so `INFO` records were dropped there while
  the native path still delivered them. Both paths now share one threshold.
- **`Logger` is hashable again**, so it can be used as a dict key or set member.

### Added

- **`structguru.flush()`** — public API for blocking until buffered records have
  been written. Previously only reachable as the private `_runtime.flush_native`.
- **`bind_contextvars`, `bound_contextvars`, `clear_contextvars`** are exported
  from the package root; custom middleware no longer has to import
  `structguru._contextvars`.
- **`uninstall_stdlib_bridge()`** for removing the bridge and restoring the
  pre-install delivery behavior.
- **`deny.toml`** — an explicit cargo-deny license/bans/sources policy, and a
  `supply-chain` CI job so dependency advisories surface on every pull request
  rather than only on the release path.

### Removed

- **`make governance` and the `governance` dependency group.** The target
  validated an agent-tooling tree that is not part of the source distribution, so
  it could never run from a clean clone. Removing it also drops `jsonschema` from
  the development dependencies.

## [1.0.4] - 2026-07-12

### Added

- **`format=` renderer selector.** `configure(format="json" | "console")` is the
  way to choose the output renderer. `"json"` remains the default. Additional
  format names (e.g. `logfmt`) will be added under the same parameter in future
  releases.

### Changed (breaking)

- **Removed `configure(json=...)`.** The boolean output selector is gone; pass
  `format="json"` (default) or `format="console"` instead.
- **Removed `configure_structlog()`.** The deprecated pre-1.0 configuration
  wrapper is gone; use `configure()` (with `stream_sink=` for the synchronous
  stream behavior it provided).
- **Renamed the public runtime functions** to drop the now-redundant `native`
  prefix (the Rust renderer is the only path): `disable_native()` → `shutdown()`,
  `native_available()` → `is_available()`, `native_metrics()` → `writer_metrics()`,
  `set_native_level()` → `set_level()`.

## [1.0.3] - 2026-07-12

### Added

- **stdlib logging bridge.** `structguru.integrations.stdlib.install_stdlib_bridge()`
  routes standard-library `logging` records (from third-party libraries) through
  structguru's native renderer, so foreign logs share the same JSON/console
  formatting, redaction, level filtering, and output stream. The record's logger
  name, `extra=` fields, `exc_info`, and `stack_info` are preserved; numeric levels
  are normalized to structguru's canonical levels; and the already-formatted
  message is passed through verbatim. A `StructguruHandler` class is exposed for
  `propagate=False` loggers, and `suppress_loggers()` quiets noisy loggers by
  raising their level threshold.

## [1.0.2] - 2026-07-11

### Added

- **Free-threaded CPython (3.13t/3.14t) support.** The native extension declares
  `gil_used = false`, so importing it no longer re-enables the GIL on a
  free-threaded build. Each log record captures one coherent runtime snapshot and
  threads it through formatting, filtering, rendering, and enqueue, so a
  concurrent `configure()`/`disable_native()` cannot expose partially updated
  state. A `free-threaded` release gate builds against 3.14t and runs the
  concurrency and lifecycle regressions before publish. (The abi3 wheel does not
  support free-threaded 3.13t — PyO3 requires 3.14t for the stable ABI there.)
- **Async httpx hooks.** `StructguruHTTPXLoggingHooks.get_async_hooks()` returns
  awaitable request/response hooks for `httpx.AsyncClient`.
- Benchmarks for the native logging pipeline (structured records, contextvars,
  redaction, fast paths, GIL vs free-threaded threaded logging) and value
  conversion.

### Fixed

- Rotating file writers that share a path coordinate every rotation through an
  owner-only `.lock` sidecar, so prefork workers no longer rename or delete each
  other's active files and backups.
- `configure()` rejects negative `exception_max_frames`/`exception_max_local_repr`,
  and `exception_max_frames=0` now correctly omits all traceback frames instead of
  including every frame.
- The source distribution now bundles the `LICENSE` file that the package metadata
  declares via `License-File`, which PyPI requires for sdist uploads.

## [1.0.1] - 2026-07-11

### Security

- Invalid import-time environment configuration and a missing required native
  extension now fail startup instead of silently disabling the only logging path.
- Reconfiguration constructs all fallible native resources before replacing the
  active writer, so a rejected target or file path cannot disable working logging.
- The native output queue is bounded to 8192 records by default with lossless
  backpressure; `maxsize=0` remains an explicit opt-in to an unbounded queue.
- Tagged releases now run the full Python and Rust quality gates before PyPI
  publication, audit locked Python/Rust dependencies, and attach CycloneDX SBOMs.

### Fixed

- Declared the `jsonschema` dependency required by strict agent-governance
  validation and exposed the complete check as `make governance`.

## [1.0.0] - 2026-07-11

### Security

- **Rotating file sink no longer panics on a failed rotation.** A rotation I/O
  error (disk full, permission change, a backup locked on Windows) previously
  left the sink without an active file handle, so the next write/flush panicked
  and killed the background writer thread — hanging every logging caller (block
  mode) or leaking memory (unbounded queue). Rotation now always restores a
  usable handle and write/flush recover instead of panicking.
- **Log files are created owner-only (`0600`) on Unix** instead of inheriting
  the umask default (commonly world-readable `0644`).
- **`rate_limit_period` values too large for a `Duration`** (e.g. `1e300`, also
  reachable via `STRUCTGURU_NATIVE_RATE_LIMIT` at import) now raise `ValueError`
  instead of an uncatchable native panic.
- **Cyclic enum `.value` chains** are caught by the recursion-depth guard and
  raise cleanly instead of overflowing the native stack into a process abort.
- **Console renderer escapes control characters** in the message, field keys,
  and string values, so a request-controlled value can no longer inject newline
  forged log lines or ANSI terminal-escape sequences. (The JSON renderer already
  escaped these.)
- **httpx and requests integrations strip credentials and query strings from
  logged URLs.** Userinfo (`https://user:pass@host`) and query parameters
  (`?api_key=...`) are removed before logging; a bare `?` marks that parameters
  were present.
- **Django `build_logging_config` JSON formatter escapes via `json.dumps`**
  instead of f-string interpolation, closing a log-injection/field-forgery hole
  when a message contained quotes or newlines.
- **Release and CI workflows pin all GitHub Actions by commit SHA**, add a
  least-privilege `permissions: contents: read` block to CI, enforce the
  lockfile with `uv sync --locked`, pin `abi3audit`, and scope the publish-job
  artifact download to this workflow's own wheels/sdist.

### Changed (breaking)

- **structlog and orjson are no longer dependencies.** The native Rust renderer
  is the only rendering path. The wheel ships with zero runtime dependencies
  (only optional integration extras). `configure_structlog()` is now a
  compatibility shim that wires the native renderer to the configured stream.
- **Native mode is always-on** (no `configure()` call needed). It auto-enables
  at import time. Set `STRUCTGURU_LEGACY=1` to opt out.
- **`configure_queued_logging()` removed.** Use `configure()` (already the
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
- **Opt-in backtracking redaction patterns.**
  `configure(allow_backtracking_patterns=True)` routes `sensitive_patterns`
  that the linear-time engine rejects (look-around, backreferences) through a
  bounded backtracking engine (`fancy-regex`), so they work as written.
  Patterns the linear engine accepts still use it. Evaluation is capped by a
  backtrack limit; a string whose evaluation exceeds it is redacted entirely
  (fail-closed) rather than emitted unchecked. The default remains the
  linear-time engine with its no-ReDoS guarantee, and the `ValueError` for
  rejected patterns now mentions the opt-in.

### Notes

- structlog and orjson remain as **test-only** dependencies (`[dependency-groups].test`)
  for the golden parity suite, which compares native output against the standard
  structlog/orjson path. Production code has no dependency on either.

### Added

- **Native exotic-value conversion (orjson-free native path).** The native Rust
  renderer now converts `datetime`, `date`, `UUID`, `Enum`, and dataclasses
  natively via the objects' own `isoformat()`/`str()`/`.value` methods, producing
  byte-identical output to the previous orjson delegation. The native path no
  longer imports `orjson` at all. Unsupported types (`bytes`, `set`, `Decimal`)
  raise `TypeError`, matching the orjson rejection contract. The `Value::Raw`
  reverse path uses `json.loads` (stdlib) instead of `orjson.loads`.
- **Native mode is now the default.** The Rust renderer is auto-enabled at import
  time (no `configure()` call needed). `configure_structlog()` configures the
  native path with synchronous output to its selected stream. Set
  `STRUCTGURU_LEGACY=1` to opt out of auto-enable entirely. The
  `STRUCTGURU_NATIVE` env var is now a no-op (deprecated).
- **Native Sentry integration.** `configure(sentry_processor=...)` invokes a
  structlog-style processor (e.g. `SentryProcessor`) for every kept record on
  the caller's thread, mirroring the `metric_processor` hook. The raw `exc_info`
  is passed so `_resolve_exception` works; when redaction is configured
  (`sensitive_keys`/`sensitive_patterns`), the hook injects
  `REDACTED_MARKER_KEY` so the processor's `require_redaction` guard recognizes
  that native Rust redaction already ran. `SentryProcessor` itself is unchanged.
- **Native rotating-file sink.** `configure(file_path=...)` writes rendered
  lines to a rotating file natively (append mode, size-based rotation). Defaults
  mirror `logging.handlers.RotatingFileHandler` (50 MB, 5 backups); configure via
  `file_max_bytes`/`file_backup_count`. Set `also_stdout=True` to mirror output
  to both file and stdout.
- **Native callable sinks.** `configure(callable_sinks=[fn, ...])` invokes
  `Callable[[str], None]` with each rendered line. They run on a dedicated
  daemon thread through a bounded queue. The configured overflow policy provides
  backpressure or counted dropping; flush and lifecycle operations drain pending
  deliveries. Callable errors are swallowed.
- **Native console renderer.** `configure(json=False)` renders colored,
  human-readable lines instead of JSON — structguru's own stable dev format
  (`<timestamp> [<LEVEL>] <message>  k=v`), with ANSI colors by default on a
  TTY. Override with `colors=True/False`. Not a `ConsoleRenderer` clone.
- **Native metric hooks.** `configure(metric_processor=...)` invokes a
  structlog-style processor (e.g. `MetricProcessor`) for every kept record on
  the caller's thread before rendering, with the pre-`EventRenamer` event-dict
  shape (`{"event": message, **fields}`). Records dropped by level filtering,
  sampling, or rate limiting never reach it; hook errors never break logging.
- **Native level-gated sampling.** `configure(sample_max_level=...)`
  restricts sampling to records at or below the given level; more severe
  records always pass — the native analog of wrapping `SamplingProcessor` in
  `ConditionalProcessor(max_level=...)`.
- **Native structured exceptions.** `configure(structured_exceptions=True)`
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
  `configure(pattern_replacement=...)` sets the substitution text for
  `sensitive_patterns` matches and supports capture-group expansion (`$1`,
  `${name}`; `$$` for a literal `$`). This covers the main look-behind use case
  on the linear-time engine: `(?<=password=)\S+` becomes pattern
  `(password=)\S+` with `pattern_replacement="$1[REDACTED]"`.

### Changed

- **`configure()` is the logging configuration API.** It replaces the
  unreleased native-configuration name. `configure_structlog()` delegates to
  `configure()`, emits `DeprecationWarning`, and will be removed in v2.0.
- **`configure_queued_logging()` was removed.** Native mode (`configure()`)
  already offloads log I/O to a background thread. Use
  `configure(file_path=...)` or `configure(callable_sinks=[...])`.
- **Unsupported `sensitive_patterns` now raise `ValueError` at
  `configure()`** instead of emitting a `UserWarning` and silently leaving
  native mode disabled. Rust's `regex` engine guarantees linear-time matching
  (no ReDoS on the hot path) and therefore rejects backreferences and
  look-around; redaction that silently differs from the configuration is worse
  than a setup-time error. The error message includes rewrite guidance
  (migration plan decision gate B).
- **Removed the obsolete public processor-chain helpers and modules**; native
  `configure()` options are the sole processing API in v1.
- Wheel CI validates tag/Python/Rust version coherence and smoke-installs
  host-native wheel artifacts before publication.

### Fixed

- Redaction now covers the rendered message as well as structured fields, and
  Sentry receives only the already-redacted event while retaining raw
  `exc_info` solely for exception capture.
- Callable sinks use a bounded, drainable queue. `flush_native()`, reconfigure,
  disable, fork, and interpreter shutdown now drain pending callable deliveries;
  runtime `logger.add()` registrations survive reconfiguration and are removed
  independently by handler ID.
- `logger.add()` file, stream, handler, and callable sinks now receive both
  structguru and stdlib records. `configure_structlog()` no longer duplicates
  its configured stream to stdout.
- Rotating files account for existing bytes, close the active handle before
  rename (including on Windows), and rotate before the threshold-crossing record.
- Malformed `exc_info` no longer breaks logging, and slots dataclasses serialize
  through their declared fields.
- **Redaction marker never leaks into native output.** The internal
  `_structguru_redacted` marker key is stripped by the native renderer,
  matching `strip_redaction_marker` on the standard path.
- **Native key order now matches structlog exactly.** User fields colliding
  with standard keys (`level`, `severity`, `logger`, `timestamp`) are
  overridden *in place* (previously dropped and re-appended), and contextvars
  are appended after event fields with setdefault semantics (previously
  prepended), matching `merge_contextvars`.

- **Native value-pattern redaction.** `configure(sensitive_patterns=[...])`
  applies compiled regex patterns to every string value (in addition to
  key-based redaction), mirroring `RedactingProcessor(patterns=...)` on the
  native fast path. Patterns are compiled once at enable time into a reusable
  `RedactionConfig`; Rust's `regex` engine does not support backreferences or
  look-around, so an unsupported pattern raises `ValueError` at configuration time.
- **Native sampling & rate limiting.** `configure(sample_rate=...)`,
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
  in binary wheels. Public API: `configure()`, `disable_native()`,
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
  it via the standard structlog path, so `configure()` is required to use it.
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
