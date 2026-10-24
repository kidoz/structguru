# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/), and this project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- Updated Rust dependencies to PyO3 0.30.1 and fancy-regex 0.20.0, refreshed
  the Cargo lockfile, and removed the duplicate syn version.
- The Python 3.15 source-gate CI legs are back. `make install` no longer
  installs the docs toolchain, whose PyYAML dependency has no cp315 wheel, and
  the requests integration tests use `pytest-httpserver` instead of
  `responses`, the other PyYAML consumer. Use `make docs` to install MkDocs
  explicitly.

### Security

- Backtracking redaction evaluation failures now fail closed throughout the
  complete render and Sentry paths instead of leaking the original string.

### Fixed

- `flush()` now covers records between native acceptance and stream, callable,
  and Sentry submission, tracks exact pre-call leases, excludes later producers,
  and remains bounded and reentrant. After fork, leases from vanished threads
  reset while the surviving thread's in-flight submissions remain tracked.
- Stdlib bridge installation and removal use the same lock order as sink root
  attachment, preventing concurrent `logger.add()` or `remove()` deadlocks.
- Tuple-subclass pairs are redacted in messages and exception locals, list
  conversion snapshots elements before Python callbacks, and unsupported
  dataclass mapping keys no longer traverse their fields.
- Handler remove/re-add generations close exactly once after every delivery
  retires; stale finalizers cannot close new registrations, and immediate and
  deferred close errors are contained.
- gRPC context cleanup covers all `BaseException` exits, Celery first setup is
  serialized, Requests logs the final prepared URL/request ID, stdlib logger
  iteration is synchronized, and Celery/Requests edge cases restore context and
  header overrides correctly.
- `flush()`, `shutdown()`, and reconfiguration no longer block on the abandonment
  warning when stdout and stderr share a full pipe. The stall limit still ends
  the wait. The warning is dropped when stderr cannot accept a write.
- An abandoned native writer discards records still in its queue and stops after
  the write already in progress returns. Those queued records are not written
  if the sink later recovers.
- The GitHub Release that attaches SBOMs is created only after PyPI publication
  succeeds. A failed test, wheel build, or publish job no longer creates it.

## [1.5.0] - 2026-10-13

### Added

- CPython 3.15 support. The abi3 wheel already covers 3.15 unchanged, and
  free-threaded 3.15t wheels join the 3.14t ones for Linux x86_64, macOS
  arm64, and Windows x64. The 3.15 source-gate leg returns once PyYAML ships
  cp315 wheels — it has none for 3.15 and its sdist build crashes the CI
  runners today — while the free-threaded 3.15t gate already runs on 3.15.

### Changed

- Adding the same `logging.Handler` twice raises `ValueError` until it is
  removed. A second `add()` used to register the handler again and deliver
  every rendered record to it twice; the first registration's delivery and
  ownership are preserved.

### Fixed

- Callable-sink removal and reconfiguration release retired Python callbacks
  after unlocking the dispatcher, so reentrant callback destructors cannot
  deadlock it.
- `flush()`, `shutdown()`, and reconfiguration no longer block forever when
  the native writer's sink stops making progress — for example a full stdout
  pipe whose reader has stopped. A writer stuck for 10 seconds is abandoned
  with a warning naming the number of undrained records, matching the stall
  limit callable sinks already had.
- A panic inside a native sink no longer kills the writer thread silently,
  which used to leave `flush()` waiting forever and producers enqueueing into
  a queue nobody drained. The interruption is counted in `sink_errors`, the
  writer closes, and blocked flushers and producers wake up.

### Security

- Sink-failure warnings and fork-restart diagnostics omit exception text and
  report only the error type. Exceptions are never stringified for these
  diagnostics, preventing sensitive values from bypassing record redaction.
- The Sentry how-to no longer reads as a redaction guarantee: the raw
  exception passed to `sentry_sdk.capture_exception` lets the SDK attach
  unredacted frame locals unless it is initialised with
  `include_local_variables=False`, and the docs now say so.

## [1.4.0] - 2026-09-25

### Added

- Free-threaded CPython 3.14t wheels for Linux x86_64, macOS arm64, and Windows
  x64. pip never selects an abi3 wheel on a free-threaded interpreter, so a
  3.14t install previously compiled the sdist and needed a Rust toolchain.
- `writer_metrics()["callable_errors"]` counts exceptions raised by callable
  sinks. They were discarded without a trace; the first failure and every
  1000th now also emit a `UserWarning` from the delivery thread.

### Changed

- `logger.remove(handler_id)` accepts an id returned by any `Logger` instance,
  and an unknown or already-removed id raises `ValueError` (a non-integer id
  raises `TypeError`), as in loguru. It used to be ignored silently, which also
  left a sink added through `Logger(name=...)` impossible to remove.
- `logger.add(handler)` no longer rewrites the caller's `logging.Handler`: its
  level and formatter are left alone, and its formatter now wraps the rendered
  line. `add(level=...)` accepts integers and raises `ValueError` for an
  unknown level instead of warning and falling back to INFO.
- The message argument of the level methods and `self` on `bind()` and
  `contextualize()` are positional-only, as in loguru. Extra `message=` and
  `self=` keywords (`logger.info("m", self=x)`) become fields instead of
  raising `TypeError`; passing the message itself by keyword
  (`logger.info(message="m")`) now raises `TypeError`.
- Callable sinks are called by one thread at a time, in log order, across
  `configure()`, `update()` and `logger.add()`. `logger.add()` no longer waits
  for queued deliveries, and the stall limit that `flush()` and `shutdown()`
  apply to a sink that stops making progress now also bounds `configure()`,
  `update()` and `logger.remove()`. After an abandonment, callable delivery
  stays off until the next `configure()`, even if a sink is added.
- A record rejected by a closed writer during reconfiguration or shutdown
  reaches no other destination: the synchronous `stream_sink`, callable sinks
  and Sentry now follow the native writer's outcome. The stream used to
  receive it anyway.
- The stdout sink (`target="stdout"` and `also_stdout=True`) writes each
  record straight to file descriptor 1 on Unix, with no buffering and no
  process-wide lock, and keeps following fd 1 after a later `dup2()`. A
  closed or invalid stdout is counted in `sink_errors` instead of `written`.
- A relative `file_path` is resolved to an absolute path at `configure()`, so
  a later `os.chdir()` no longer splits the log or points the lock file and
  backups elsewhere. With rotation disabled (`file_max_bytes=0` or
  `file_backup_count=0`), a log file renamed or deleted by an external tool
  such as logrotate is now reopened on Unix, as it already was with rotation
  on.
- If a forked child cannot start its writer, logging in that child is disabled
  as after `shutdown()`, with one line on stderr; it used to keep the dead
  writer, drop records uncounted and hang in `flush()`.
- Console output: quoted values escape `"` and `\`, keys that contain spaces,
  `=`, quotes or backslashes are quoted, and U+2028, U+2029 and bidirectional
  controls are escaped, so a field key or value can no longer imitate another
  field or line. Floats use the JSON renderer's text (`1.0`, `1e+300`, `-0.0`)
  instead of Rust's (`1`, a 301-digit number, `-0`), a formatted `exception`
  is an indented block like the stack, and user `service` and `stack` fields
  are shown instead of dropped.
- Standard-library records bridged into structguru are thresholded, sampled
  and routed to `logger.add()` sinks by their real `levelno` rather than the
  level band they render as, so a level-25 record passes `level=25`, is never
  sampled away under `sample_max_level="INFO"`, and levels 5 to 9 fall below a
  DEBUG threshold. Their `timestamp` is `record.created`, which keeps the
  original time for records replayed through a `QueueListener`; a `created`
  that is not a number falls back to the current time.
- Integrations:
  - Flask and Django bind the request context while a streamed body is
    produced and log the request summary when it finishes, with the full
    duration.
  - Flask's context hook runs before every other `before_request` hook, a
    propagated exception logs `Request failed` from teardown, and a request
    whose later `after_request` hook fails logs one summary with the final
    status instead of two.
  - The ASGI middleware restores the context of outer middleware, echoes the
    request id under `request_id_header`, stops `duration_ms` when the final
    response body is sent, logs `BaseException`s as failures, bounds extracted
    header values, and redacts credential headers.
  - `setup_celery_logging()` warns when called again with different options,
    and a failed first call no longer disables later ones.
  - Django's console logging config includes the service name.

### Fixed

- A forked child could hang forever with `target="stdout"`: Rust's
  `std::io::stdout()` lock was held by the parent's writer thread at `fork()`,
  so the child's writer, `flush()`, exit and (once the queue filled) every log
  call blocked on it.
- A rotating file sink dropped a record when a signal interrupted its wait for
  the shared lock file, and records it had counted as failed after a write
  error were written later from its buffer; a partially written line is now
  ended before the next record.
- Exception locals no longer leak secrets through `repr()`. Values under a
  sensitive key inside mappings, sequences, sets, dataclasses,
  `SimpleNamespace` and `argparse.Namespace` are redacted at any depth, with
  `bytes` and `Enum` keys and `(key, value)` pairs in a list matched too;
  `sensitive_patterns` see the whole rendering before it is cut to
  `exception_max_local_repr`; and a bound `Logger` no longer shows its bound
  values in its `repr`.
- A message placeholder that names a sensitive key (`{token}`,
  `{creds[password]}`) is interpolated as `[REDACTED]`, and a placeholder that
  interpolates a whole container (`logger.info("{}", {"password": ...})`)
  renders it with the values under sensitive keys redacted, as exception
  locals do. The value used to go into the message, and to Sentry, unredacted.
- `sensitive_patterns` also apply to mapping keys at every depth and to
  integers by their digits, so `{"alice@corp.com": 3}` or a card number passed
  as an `int` is masked. Keys that become identical after redaction are
  written once.
- Celery context propagation can no longer make `task.delay()` fail: header
  values are converted to transport-safe types and bounded, sensitive keys are
  redacted before they reach the broker, and a failed export drops the header
  instead of raising.
- Records accepted by the writer just before `update()` or `configure()`
  swapped the runtime were lost for callable sinks and Sentry without being
  counted.
- A slow callable sink that keeps delivering is no longer abandoned by the
  stall limit: progress counts completed deliveries, not just released
  producers. A producer released by an abandonment is counted as abandoned,
  not as a queue-full drop.
- `logger.add()` of an unrelated sink could block for the whole backlog, and
  forever behind a stuck sink.
- `callable_dropped` was reset by every `logger.add()`.
- An infinite stall timeout panicked in `flush()` and at exit.
- Logging diagnostics (queue-full, stream-error, sink-error, abandonment and
  message-formatting warnings) never raise into the logging call under
  `-W error`; a failure goes to `sys.unraisablehook`.
- `@logger.catch()` used as a decorator logged under the `contextlib` or
  `asyncio.events` logger name instead of the decorated function's module.
- Leaving `contextualize()` or `bound_contextvars()` in a different
  `contextvars.Context` (a generator iterated from a thread pool, an async
  generator finalized by the garbage collector) raised `ValueError`. Exit now
  restores the entry context only when the current context still holds the
  block's own mapping, and leaves any other context untouched.
- The exit drain is registered at import, so an `atexit` handler registered
  before an explicit `configure()` can still log.
- A message whose `str()` raises, or an argument whose `__format__` raises,
  no longer propagates out of the logging call.
- `LOG_LEVEL=10` and other numeric environment levels were rejected at import.
- A field named `event` replaced the message that `metric_processor` matched.
- Message templates longer than 512 characters are no longer cached, which
  kept large dynamic messages alive.
- `stack_info` hid the frames of user modules whose names start with
  `structguru` (such as `structguru_ext`).
- Brace formatting: nested format-spec fields (`{x:{width}}`) are consumed,
  `}}` is unescaped whenever arguments are given, `logger.exception("{{x}}")`
  renders like `logger.error("{{x}}")`, and an `{exc_info}` placeholder no
  longer drops the exception.
- An unpaired surrogate rendered as three U+FFFD characters and a split
  surrogate pair as six; they now render as one U+FFFD and as the character.
- `int` and `float` subclasses such as `numpy.float64` rendered as
  `<unsupported: T>`.
- Keys that render to the same text (a non-string key next to its string
  form, lone surrogates) produced duplicate JSON keys.
- Sentry events were silently dropped on sentry-sdk 1.x, which has no
  `new_scope()`.
- The gRPC interceptor never matched a mixed-case `request_id_key`, and a
  `-bin` request-id key failed every RPC.
- The requests adapter logged URLs without the `?` for `params=`, and logged
  a `KeyboardInterrupt` as a completed request.
- The SQLAlchemy "log all queries" recipe in the integrations guide logged
  every query as a slow-query warning.
- The golden parity tests compared the native renderer with itself; they run
  against a real structlog and orjson pipeline again.

## [1.3.1] - 2026-09-17

### Fixed

- Structured exceptions extract source only for the frames they keep. The
  frame builder ran `traceback.extract_tb()` over the whole traceback, reading
  and dedenting source for every frame, and then sliced to the last
  `exception_max_frames`: a `RecursionError` cost 1.7 ms to report twenty
  frames and now costs 64 µs. Frames captured with
  `exception_include_locals=True` now carry the `line` field like every other
  frame instead of `null`, which makes that path pay for source lookup too
  (about 50 µs instead of 10 µs for twenty frames); the two modes now produce
  the same frames apart from `locals`.

## [1.3.0] - 2026-09-17

### Added

- Frozen `Settings` with mapping/environment constructors, `get_config()`, and
  incremental `update()`. Existing `configure()` keywords remain accepted through
  a typed keyword wrapper; a Settings object can also be supplied as the base.
  Level-only updates preserve writers, buffered records and rate-limit state.
- `STRUCTGURU_LEVEL`, `STRUCTGURU_TARGET`, `STRUCTGURU_FORMAT`,
  `STRUCTGURU_SAMPLE_RATE`, `STRUCTGURU_RATE_LIMIT`, and
  `STRUCTGURU_AUTOCONFIGURE`; existing environment names remain supported.
- `configure(exception_carets=False)` omits the PEP 657 position markers (the
  `~~~^^^` lines under each frame) from formatted tracebacks. CPython computes
  them per frame while formatting, and on 3.11+ they are most of the cost of
  `logger.exception()`: a 22-frame traceback formats in 92 µs instead of
  475 µs without them. The output is exactly what CPython prints under
  `PYTHONNODEBUGRANGES=1`, including chained exceptions and exception groups.
  The default is unchanged, and `structured_exceptions=True` never renders
  the markers.
- `lifecycle_metrics()` reports cumulative native deliveries rejected by closed
  writers, retaining counts across reconfiguration and shutdown and starting fresh
  in forked children.

### Changed

- Explicit `configure()` calls now layer defaults, supported environment variables,
  then explicit keywords. Pass `configure(Settings(...))` to ignore the environment.
  `configure()` still replaces prior settings; `update()` retains omitted options.
- Native configuration and `set_level()` reject unknown level names, booleans and
  negative integers with `ValueError`, and accept non-negative integer thresholds.
  Invalid selected environment values fail import unless autoconfiguration is disabled.
  Settings also reject invalid scalar types instead of allowing coercion downstream.
- The rotating file sink no longer closes, reopens, and stats the log file
  before every record to detect a rotation by another process. It stats the
  path once and compares the file identity with its open handle, reopening
  only when the path names a new file. With rotation enabled (the default)
  the writer thread handles 256 records in 0.63 ms instead of 5.08 ms, and a
  caller throttled by a full queue waits 2.6 µs per record instead of 21 µs,
  the same as with rotation disabled.
- The blocking enqueue pushes under the GIL and releases it only when the
  queue is actually full. Releasing it around every push forced a GIL
  hand-off per record whenever another thread was runnable: four threads
  logging concurrently now spend 3.0 ms instead of 5.6 ms per 1,000 records,
  eight threads 6.0 ms instead of 10.5 ms. Single-threaded cost and the
  release-while-blocked behaviour are unchanged.
- A JSON record with no stream sink, callable sink, or Sentry processor is
  rendered and enqueued in one native call, so the line never becomes a
  Python string, and the callable-sink dispatcher is skipped entirely while
  no sink is registered. `logger.info("Hello world")` takes 1.75 µs instead
  of 2.42 µs; long or escaping-heavy messages gain more.
- `Logger.bind()` and `Logger.opt()` copy the logger directly instead of
  re-running the dataclass constructor: about 0.25 µs instead of 0.75 µs
  each, which also speeds up every record that crosses the stdlib bridge.
- Callable sinks cache the set of destinations admitting each level. Producer
  reservations synchronize sink selection with removal before queue insertion;
  blocking waits happen outside the sink registry lock.
- The native writer merges bound fields, call kwargs, and contextvars, redacts,
  renders, and enqueues a record in one call using a `RuntimeConfig` compiled at
  configuration time, so no per-record configuration is marshalled and the
  console format is fused like JSON. A simple `logger.info()` runs about 30
  percent faster; records with fields about 20 percent. Output is unchanged.
- Level names and numbers come from one table in the Rust core; the facade,
  settings validation, the stdlib bridge, and native filters all read it.
  Importing the package without the native extension now fails at import with
  the existing "requires its native extension" error.
- Callable-sink dispatch (`configure(callable_sinks=...)`, `logger.add()`) runs
  in the native extension: the bounded queue, its worker thread, sink selection,
  delivery accounting, and deferred handler closes. Caller cost with a sink
  configured drops by a third to a half; lifecycle, backpressure, fork, and
  callback-reentrancy semantics are unchanged.
- The opt-in backtracking redaction engine (`allow_backtracking_patterns=True`)
  builds against fancy-regex 0.19.2 instead of 0.19.0. An optional capture
  group such as `(\w+)?` that does not participate in a match is no longer
  reported as an empty match, and patterns compile faster; matching
  behaviour is otherwise unchanged.
- Building from the sdist requires maturin 1.15 or newer.

### Fixed

- `flush()`, and with it the pre-fork drain, no longer stalls while other
  threads keep logging. The native writer waited for its queue to be empty,
  a moment that never arrives when producers refill it faster than a file sink
  drains it: `os.fork()` in a prefork server with four logging threads took
  tens of seconds. A flush now waits only for the records enqueued before it
  was called, so its cost is bounded by the queue depth.
- A callable sink that never returns no longer blocks `shutdown()` and
  interpreter exit forever. `flush()` and `shutdown()` give a stalled dispatch
  worker 10 seconds to make progress, then abandon the generation, warn with
  the number of undelivered records, and disable callable delivery until the
  next `configure()`. Records already handed to the stuck callback cannot be
  recovered, and the sink's own thread is left detached rather than killed;
  it stops without starting another delivery or re-entering the interpreter.
- The rate limiter's key table is capped at 8192 messages, evicting the least
  recently seen when full. It keys on the formatted message, so messages that
  embed a counter or an ID create a new key per record; one million unique
  messages within a period previously grew the process by hundreds of
  megabytes. Keys that are actively being limited are kept.
- A synchronous `stream_sink` whose `write()` raises is now counted in
  `writer_metrics()["stream_errors"]` and warned about at a bounded rate.
  The failure is still contained, and the record still reaches the native
  destinations, but the lost stream copies are no longer invisible.
- The wheel no longer installs a stray top-level `LICENSE` file into
  `site-packages`. The license ships in the wheel's `dist-info/licenses/`
  directory and in the sdist, as before.
- Native queue overflow and closed-writer rejections are distinguished at enqueue
  time, so racing reconfiguration or shutdown cannot silently hide a rejection or
  mislabel it as a full queue.
- Shutdown drains callable deliveries before closing the native writer, allowing
  their nested native logs to finish. Interpreter exit clears the active runtime,
  so later atexit handlers see disabled logging instead of a closed writer.
- `logger.catch(level="trace")` uses DEBUG severity consistently with
  `logger.trace()` for level filtering, sampling, and sink thresholds, including
  synchronous and coroutine decorators.
- Console stacks escape terminal control characters and indent every continuation
  line, including stacks forwarded by the stdlib bridge. Stack redaction still
  runs before escaping, and JSON output is unchanged.
- Django middleware clears request context when lazy user evaluation or user-ID
  conversion fails, including interrupted requests, so fields cannot leak into
  later work on the same worker.
- Native sink construction releases the GIL while opening files and acquiring
  sidecar locks, so other Python threads can keep running and logging during
  configuration. Construction failures leave the previous runtime active.
- Closing, reconfiguring, or shutting down the logger in a forked child on
  Linux no longer panics with `failed to join thread: Invalid argument`. glibc
  reuses the parent's dead worker thread identity for the child's respawned
  worker, and releasing the abandoned parent writer detached that live thread;
  the stale handle is now leaked instead of detached.
- The stdlib bridge no longer holds its handler lock while waiting for callable
  queue space, preventing deadlocks when a sink callback logs through stdlib.
  Handler filters, including replacement records on Python 3.12+, still apply.
- Sensitive-key matching handles Unicode case variants, including nested fields,
  consistently with Python exception-locals redaction.
- Sampling, rate limiting, and top-level field keys accept unpaired surrogates
  using the renderer's U+FFFD replacement policy instead of losing the record.
- A sink removed from inside a sink callback now closes its handler only after
  the native deliveries already queued for it have run. The callback cannot
  wait for the worker, and closing at once left those deliveries writing to a
  closed handler: a `FileHandler` silently reopened its file and leaked the
  stream. The close is deferred to the worker that finishes the last delivery.
- The Django `build_logging_config(json_logs=True)` formatter includes the
  formatted exception (`exception`) and stack (`stack`) of a record, so
  `logger.exception()` no longer loses the type, message, and traceback.
- Message, logger, service, and stack text containing unpaired surrogates, such
  as a filename from `os.fsdecode()`, is rendered with U+FFFD like field values
  instead of being rejected, which silently dropped the record on the stdlib
  bridge with `logging.raiseExceptions` off.
- URL sanitization rejects URLs whose authority failed to parse
  (`https:/user:pw@host/path`), where `urlsplit()` leaves the credentials in the
  path, instead of logging them.
- Raw stdlib delivery to `add()` sinks shares the callback reentrancy guard: a
  record emitted inside any sink callback bypasses `add()` sinks on both paths,
  so a callback that logs through `logging` no longer re-enters itself.
- `logger.remove()` waits for raw stdlib deliveries in progress, matching the
  guarantee it already gave native deliveries.
- `logger.add(level=...)` and the stdlib bridge accept `TRACE`, `SUCCESS`,
  `EXCEPTION`, and `FATAL` like the rest of the library instead of warning and
  falling back to `INFO`.
- A field named `event` no longer replaces the message exported to Sentry.
- The ASGI middleware logs `Request cancelled` when the application is cancelled,
  without inventing a 500 status, and still propagates the cancellation.
- Structured exception logging tolerates failing exception messages and snapshots
  locals before calling `repr()`. Exception groups preserve nested members,
  frames, and causes, with bounded traversal and native redaction.
- Public flush and the pre-fork drain include native records emitted by callbacks.
- Eager and nested Celery tasks restore caller context on success and failure;
  worker tasks still clear stale context. Eager tasks inherit selected fields and
  respect explicit propagation headers in `request.headers`.
- Sentry recognizes catch-level aliases and exports canonical severity names,
  including `EXCEPTION` as error and `FATAL` as critical.
- Callback failures, including `SystemExit` and `KeyboardInterrupt`, no longer
  terminate the dispatch worker or strand flush/removal/shutdown operations.
- Forked children reset sink registry, logger, and stdlib bridge locks while
  preserving the sink registry shared by bound loggers.
- `logger.catch()` validates case-insensitive level names before execution,
  preventing uppercase error levels from silently filtering caught exceptions.
- Native flush waits for sink flushing and its error accounting to finish.
- Concurrent stdlib bridge uninstall and sink removal cannot reattach a removed
  sink. Handler sinks retain their original severity in JSON and console modes.
- Stdlib messages containing braces remain verbatim when stack info is included.
- Extracted ASGI headers cannot overwrite request metadata or crash a request
  because their names collide with reserved context fields.
- Callable sink callbacks can log without blocking their own queue or creating
  a delivery loop. Their logs reach the native writer and bypass callable sinks.
  Sink removal now waits for producers that selected a sink before queue insertion.
  A stdlib handler added with `logger.add()` can call `logger.remove()` from its
  own `emit()` while a native record for that handler is queued: removal no
  longer waits for the dispatch worker, which is blocked on the handler's lock,
  so the two no longer deadlock.
- Level-only configuration changes preserve in-flight callable and Sentry delivery,
  as well as the existing native writer and filter state.
- Native dictionary conversion snapshots entries before invoking Python converters,
  preventing a PyO3 panic when a converter mutates its containing dictionary.
- URL sanitization decodes bytes URLs, preserves IPv6 brackets, and handles invalid
  ports without leaking userinfo or replacing the original request exception.
- Formatting diagnostics omit raw templates, exception details, and user source
  lines. Native logger/service metadata now obeys key and pattern redaction.
- Native sink error metrics include partial failures while successful destinations
  continue receiving records. Sentry receives redacted native metadata, including
  the configured service for tags.
- `logger.catch()` handles awaited coroutine exceptions, including suppression and
  reraising. Stream sinks are represented in the public typing contract.

## [1.2.3] - 2026-09-06

### Fixed

- A field value the native renderer cannot represent no longer loses the whole
  record. `logger.info(..., path=Path("/tmp"))` raised `TypeError` from the
  logging call, and a stdlib record carrying such a value in `extra=` — Django's
  `log_response()` attaches the raw `WSGIRequest` as `extra["request"]` on every
  HTTP error — went to `Handler.handleError()` as a `--- Logging error ---`
  diagnostic instead of the JSON event. The value is now replaced at the
  rendering boundary, at any nesting depth, by a marker that names the type
  only (`<unsupported: WSGIRequest>`, `<cycle: dict>`, `<max depth exceeded>`),
  so the message, level, other fields, and the exception traceback are kept.
  `str()`/`repr()` are never called on an arbitrary object, and markers are
  redacted like any other string. A conversion that raises — a duck-typed
  `isoformat()`, a dataclass field access — falls back the same way; only
  `KeyboardInterrupt` and `SystemExit` still propagate. Integers outside the
  64-bit range are now written losslessly as JSON numbers, non-string mapping
  keys are rendered as strings (`{200: 3}` becomes `{"200": 3}`), and text
  with unpaired surrogates is written with U+FFFD instead of rejected.
- `Enum`, `datetime`, `UUID`, and dataclass detection now inspects the class
  rather than the instance, so an object with a lazy `__getattr__` (Django's
  `LazyObject`, ORM proxies) is no longer evaluated as a side effect of being
  logged.
- A value reachable twice on different paths — the same dict in two list
  slots — was reported as a cycle by release builds, because the cycle
  tracker's pop lived inside a `debug_assert_eq!` that release builds compile
  out. Repeated references now convert as ordinary data.

## [1.2.2] - 2026-09-06

### Changed

- The native sampling filter now draws from `fastrand`'s per-thread WyRand
  generator instead of `rand`'s ChaCha-based thread RNG. Sampling behavior is
  unchanged — one uniform draw in `[0, 1)` per record — but the wheel build
  drops seven transitive crates (`rand`, `rand_core`, `chacha20`, `getrandom`,
  `cpufeatures`, `cfg-if`, `r-efi`), including the yanked `chacha20` release
  that delayed the 1.2.1 publish.

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
