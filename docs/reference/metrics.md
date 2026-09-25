# Metrics

structguru reports two kinds of numbers: counters about its own delivery
pipeline, returned by `writer_metrics()` and `lifecycle_metrics()`, and
application metrics derived from log records by a `metric_processor` hook such
as `MetricProcessor`.

## `writer_metrics()`

Returns a new `dict` describing the current runtime, or `None` while logging
is disabled (before the first `configure()` when import-time configuration is
off, and after `shutdown()`).

### Native writer

| Key | Type | Meaning |
|---|---|---|
| `enqueued` | `int` | Records accepted into the writer queue. |
| `dropped` | `int` | Records the writer queue discarded because it was full (`overflow="drop"`). Such records still reach `stream_sink`, callable sinks, and the Sentry hook. |
| `dequeued` | `int` | Records the writer thread has taken from the queue. |
| `written` | `int` | Records written to at least one native destination. |
| `sink_errors` | `int` | Failed destination write or flush operations. A record written to one destination but not another (`file_path` with `also_stdout=True`) counts as written and as a sink error. |
| `depth` | `int` | Records currently in the writer queue. |
| `maxsize` | `int` | Writer queue capacity (`Settings.maxsize`; `0` is unbounded). |
| `in_flight` | `int` | Records taken from the queue whose write, and the destination flush that follows it, has not finished. |
| `closed` | `bool` | The writer has been closed. |
| `worker_done` | `bool` | The writer thread has drained the queue and exited. |
| `paused` | `bool` | The writer thread is paused. Always `False` for writers created by `configure()`. |

These counters belong to one writer. They restart when `configure()` or an
`update()` that changes an option other than `level` creates a new writer, and
in a forked child, which starts its own writer. `set_level()` and level-only
updates keep them.

### Callable sinks

These keys describe the dispatch queue shared by `callable_sinks` and sinks
added with `logger.add()`. They are present whether or not any sink is
registered.

| Key | Type | Meaning |
|---|---|---|
| `callable_dropped` | `int` | Deliveries discarded because the dispatch queue was full (`overflow="drop"`). |
| `callable_errors` | `int` | Sink calls that raised. |
| `callable_depth` | `int` | Deliveries currently in the dispatch queue. |
| `callable_maxsize` | `int` | Dispatch queue capacity (`Settings.callable_queue_maxsize`). |

`callable_dropped` and `callable_errors` restart when `configure()` or an
`update()` that changes an option other than `level` replaces the
configuration. Adding or removing a sink keeps them.

### Stream sink

| Key | Type | Meaning |
|---|---|---|
| `stream_errors` | `int` | `write()` calls on `Settings.stream_sink` that raised. Those lines still reached the native destinations. Cumulative for the process; not reset by `configure()`. |

### Filters

Present only when sampling (`sample_rate < 1.0`) or rate limiting
(`rate_limit_max` set) is configured. The counters restart when the filter is
rebuilt by `configure()` or a non-level `update()`.

| Key | Type | Meaning |
|---|---|---|
| `sampled` | `int` | Records dropped by sampling. |
| `rate_limited` | `int` | Records dropped by the rate limiter. |

Filtered records are not counted in `dropped`, which counts only queue
overflow.

## `lifecycle_metrics()`

Returns a new `dict` in every state, including while logging is disabled.

| Key | Type | Meaning |
|---|---|---|
| `rejected` | `int` | Native deliveries rejected because their writer had been closed by `configure()`, `update()`, or `shutdown()` while the call was formatting, running a hook, or waiting for queue space. Excludes queue-full drops, sink errors, filtered records, and calls made while logging is disabled. |

`rejected` is cumulative across configuration changes and shutdown. A forked
child starts at zero.

## Diagnostic warnings

The pipeline reports failures as `UserWarning`. A warning never raises into
the logging call: when a warnings filter turns it into an exception, the
exception is passed to `sys.unraisablehook`.

| Condition | Message begins with | Emitted |
|---|---|---|
| Writer queue full (`overflow="drop"`) | `structguru native logging dropped N record(s): queue full` | first drop and every 1000th |
| Dispatch queue full (`overflow="drop"`) | `structguru callable sinks dropped N delivery record(s): queue full` | first drop and every 1000th |
| Callable sink raised | `structguru callable sink raised N time(s)` | first error and every 1000th |
| `stream_sink.write()` raised | `structguru stream sink write failed N time(s)` | first error and every 1000th |
| Callable sink made no progress for 10 seconds | `structguru abandoned N callable sink delivery record(s)` | each time deliveries are abandoned |
| Message template failed to format | `structguru: failed to format log message` | once per template and error type |
| Message object's `str()` raised | `structguru: failed to convert log message to str` | once per message type and error type |

## Metric hook

`Settings.metric_processor` is called for every record that passes the level
threshold, sampling, and rate limiting:

```python
metric_processor(None, method, event_dict)
```

| Argument | Value |
|---|---|
| first | `None` |
| `method` | Lower-case name of the method that emitted the record: `"debug"`, `"info"`, `"warning"`, `"error"`, or `"critical"`. `trace()` passes `"debug"`, `success()` `"info"`, `warn()` `"warning"`, `exception()` `"error"`, and `fatal()` `"critical"`; `logger.catch(level=...)` passes its level name, with `"trace"` passed as `"debug"`. Bridged stdlib records pass the name for their level band. |
| `event_dict` | A new `dict`: `"event"` holds the formatted message, followed by the merged fields in [record key order](record-format.md#key-order), including OpenTelemetry fields and `exception` when present. |

- The hook runs on the calling thread before redaction and rendering: field
  values are unredacted. The message has placeholder redaction applied (see
  [Redaction](redaction.md#what-is-not-redacted)).
- A field named `event` does not replace the message in `"event"`.
- The return value is ignored, and exceptions raised by the hook are
  suppressed.
- Records dropped by the level threshold, sampling, or rate limiting never
  reach the hook.

## `MetricProcessor`

`MetricProcessor` is a callable suitable for `metric_processor`. It matches
registered patterns against `str(event_dict["event"])`:

| Registration | Fires when | Callback arguments |
|---|---|---|
| `counter(event_pattern, callback)` | `event_pattern` is a substring of the message | `callback(event_dict)` |
| `histogram(event_pattern, value_key, callback)` | `event_pattern` is a substring of the message and `value_key` is in `event_dict` | `callback(float(event_dict[value_key]), event_dict)` |

Registering the same pattern again replaces its callback. Both methods return
the processor, so calls chain. Exceptions raised by callbacks, including a
failed `float()` conversion, are suppressed.

## API

::: structguru.metrics
    options:
      heading_level: 3

## See also

- [Extract metrics](../how-to/extract-metrics.md)
- [Handle backpressure](../how-to/handle-backpressure.md)
- [Delivery and lifecycle](../explanation/delivery-and-lifecycle.md)
- [`writer_metrics()` and `lifecycle_metrics()` signatures](configuration.md#structguru.writer_metrics)
