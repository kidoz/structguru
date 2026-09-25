# Configuration and runtime control

The functions on this page are importable from `structguru`. Option names,
types, defaults, and environment variables are listed on
[Settings and environment variables](settings.md).

| Function | Purpose |
|---|---|
| [`configure(settings=None, **changes)`](#structguru.configure) | Replace the active configuration. |
| [`update(**changes)`](#structguru.update) | Change selected options of the active configuration. |
| [`get_config()`](#structguru.get_config) | Return the active `Settings`, or `None` when logging is disabled. |
| [`Settings`](#structguru.Settings) | Validated, reusable configuration value. |
| [`set_level(level)`](#structguru.set_level) | Change the level threshold, keeping writers and filter state. |
| [`flush()`](#structguru.flush) | Block until records buffered before the call are written. |
| [`shutdown()`](#structguru.shutdown) | Drain and close the writer, then disable logging. |
| [`is_available()`](#structguru.is_available) | Return `True` (compatibility helper). |
| [`writer_metrics()`](#structguru.writer_metrics) | Current writer, dispatch, and filter counters; see [Metrics](metrics.md). |
| [`lifecycle_metrics()`](#structguru.lifecycle_metrics) | Cumulative closed-writer rejections; see [Metrics](metrics.md). |
| [`bind_contextvars(**kwargs)`](#structguru.bind_contextvars) | Merge fields into the current context. |
| [`bound_contextvars(**kwargs)`](#structguru.bound_contextvars) | Bind fields for the duration of a `with` block. |
| [`clear_contextvars()`](#structguru.clear_contextvars) | Reset the current context to empty. |
| [`get_contextvars()`](#structguru.get_contextvars) | Return a shallow copy of the current context. |

## Resolution order

| Call | Base | Environment | Keyword arguments |
|---|---|---|---|
| `configure(**changes)` | built-in defaults | read | override |
| `configure(settings, **changes)` | `settings` | not read | override |
| `update(**changes)` | active settings | not read | override |
| `Settings.from_env(environ=None, **changes)` | built-in defaults | read from `environ`, or `os.environ` | override |
| `Settings.from_mapping(values)` | built-in defaults | not read | none; `values` overrides |
| `Settings(**values)` | built-in defaults | not read | none; `values` overrides |

- A keyword argument wins even when its value is `None` or equals the default.
- `configure()` does not carry omitted options over from the previous call.
- `update()` raises `RuntimeError` when logging is not configured, including
  when it is called without arguments. With an active runtime, `update()`
  without arguments does nothing.
- Invalid settings raise before anything is replaced; the active runtime stays
  unchanged. This includes pattern compilation and file-sink errors, which are
  detected only when settings are applied.

## Effects of a change

| Operation | Writer and queues | Sampling and rate-limit state | `writer_metrics()` counters | `logger.add()` sinks |
|---|---|---|---|---|
| `configure()` | replaced; the old writer is drained and closed | reset | restart | kept |
| `update()` changing only `level` | kept | kept | kept | kept |
| `set_level()` | kept | kept | kept | kept |
| `update()` changing any other option | replaced; the old writer is drained and closed | reset | restart | kept |
| `shutdown()` | drained and closed | discarded | `writer_metrics()` returns `None` | kept, receive nothing until `configure()` |

`set_level()` also updates the `level` reported by `get_config()`. While
logging is disabled, `set_level()` does nothing and `update()` raises
`RuntimeError`. `get_config()` does not report queued records, counters, the
stdlib bridge, or `logger.add()` sinks. Streams and callables in settings keep
their identity across `get_config()`, `configure()`, and `update()`.

## Import-time configuration

Importing `structguru` calls `configure()` with defaults and the supported
environment variables, unless `STRUCTGURU_AUTOCONFIGURE` is false (or, when it
is absent, `STRUCTGURU_LEGACY` is true). An invalid environment value raises
from the import. A missing native extension raises `RuntimeError` from the
import. See [Environment variables](settings.md#environment-variables).

## Record pipeline

Each logging call runs these stages in order. A record dropped at a stage never
reaches the later ones.

| Stage | Where it runs | Configured by |
|---|---|---|
| 1. Level threshold | caller's thread, before formatting | `level`, `set_level()` |
| 2. Message formatting, with placeholder redaction | caller's thread | `sensitive_keys` |
| 3. Sampling, then rate limiting (keyed by the formatted message) | caller's thread (native) | `sample_rate`, `sample_max_level`, `rate_limit_max`, `rate_limit_period` |
| 4. Exception and stack capture | caller's thread | `structured_exceptions`, `exception_*` |
| 5. OpenTelemetry fields, then the metric hook, both on the merged fields | caller's thread | `otel`, `metric_processor` |
| 6. Redaction, rendering, enqueue to the writer | caller's thread (native) | `sensitive_*`, `pattern_replacement`, `format`, `colors`, `maxsize`, `overflow` |
| 7. Stream sink write, callable-sink enqueue | caller's thread | `stream_sink`, `callable_sinks`, `logger.add()` |
| 8. Sentry hook | caller's thread | `sentry_processor` |
| 9. Output | native writer thread; callable dispatch thread | `target`, `file_*`, `also_stdout` |

There is no processor-chain API in version 1: each stage is configured by the
settings above, and levels are filtered by `level` and `set_level()`.
`sample_max_level` restricts sampling to records at or below that level. See
[Architecture](../explanation/architecture.md) for the design.

## Delivery and shutdown contract

- A record is accepted when it enters the native writer queue. Reconfiguration,
  `shutdown()`, fork, and interpreter exit drain accepted records.
- A call that is still formatting, running a hook, or waiting for queue space
  when its writer closes is rejected, even with `overflow="block"`. The
  rejection increments `lifecycle_metrics()["rejected"]`, emits no warning, and
  is not retried.
- A record rejected by a closed writer reaches none of the stream sink,
  callable sinks, and Sentry hook. A record accepted just before a
  reconfiguration reaches all of them. A record dropped because the writer
  queue is full (`overflow="drop"`) still reaches all of them.
- Calls made while logging is disabled do nothing and are not counted.
- `shutdown()` first drains accepted callable-sink deliveries, so logs emitted
  by those callbacks still reach the native writer, then drains and closes the
  writer. Logging stays disabled until `configure()` is called.
- `shutdown()` called from inside a sink callback does not wait for that
  callback; later logs from the callback see the disabled runtime.
- `flush()` drains callable-sink deliveries, then waits until every record the
  writer accepted before the flush began has been written and the sink flushed.
- During `flush()`, `shutdown()`, reconfiguration, and `logger.remove()`, a
  callable sink that makes no progress for 10 seconds is abandoned with a
  `UserWarning`. When `flush()` or `logger.remove()` abandons the active
  dispatch queue, callable-sink delivery stops until the next `configure()`,
  or `update()` that changes an option other than `level`.
- Interpreter exit calls `shutdown()` from an `atexit` handler registered when
  `structguru` is imported. Handlers registered after the import run first and
  can still log.
- On POSIX, the parent flushes before `fork()`. The child starts a new writer
  with the same destinations and starts `lifecycle_metrics()["rejected"]` at
  zero. If the new writer cannot start, logging is disabled in the child and a
  message is written to file descriptor 2.

See [Delivery and lifecycle](../explanation/delivery-and-lifecycle.md) for the
reasons behind these rules.

## Request-scoped context

Context fields live in one `contextvars.ContextVar` holding a dictionary that
is replaced, never mutated, on every bind. An `asyncio` task starts with a copy
of the context active when it was created; a new thread starts with an empty
context. Context fields are added to a record only for keys that bound fields
and call keywords did not set (see [Key order](record-format.md#key-order)).
`Logger.contextualize()` uses `bound_contextvars()`.

## API

::: structguru.configure
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.update
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.get_config
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.Settings
    options:
      show_root_heading: true
      heading_level: 3
      filters: ["!^_"]

::: structguru.set_level
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.flush
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.shutdown
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.is_available
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.writer_metrics
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.lifecycle_metrics
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.bind_contextvars
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.bound_contextvars
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.clear_contextvars
    options:
      show_root_heading: true
      heading_level: 3

::: structguru.get_contextvars
    options:
      show_root_heading: true
      heading_level: 3

## See also

- [Configure logging](../how-to/configure-logging.md)
- [Propagate context](../how-to/propagate-context.md)
- [Context propagation](../explanation/context-propagation.md)
