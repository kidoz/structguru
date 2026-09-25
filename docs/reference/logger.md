# Logger

`structguru.logger` is a module-level `Logger()` with no name. `Logger` is the
class in `structguru.core`, also importable from `structguru`.

```python
from structguru import Logger, logger

logger.info("Started")  # logger field: the calling module's __name__
log = Logger(name="app.billing")  # logger field: "app.billing"
```

When `name` is `None`, each record's `logger` field is the `__name__` of the
module that made the call.

## Level methods

Every level method has the signature `method(message, /, *args, **kwargs)`
and returns `None`.

| Method | Emits at | Rendered `level` | Notes |
|---|---|---|---|
| `trace()` | 10 | `DEBUG` | Alias; there is no separate TRACE output level. |
| `debug()` | 10 | `DEBUG` | |
| `info()` | 20 | `INFO` | |
| `success()` | 20 | `INFO` | Alias. |
| `warning()` | 30 | `WARN` | |
| `warn()` | 30 | `WARN` | Alias of `warning()`. |
| `error()` | 40 | `ERROR` | |
| `exception()` | 40 | `ERROR` | Sets `exc_info=True` unless `exc_info` is passed. |
| `critical()` | 50 | `CRITICAL` | |
| `fatal()` | 50 | `CRITICAL` | Alias of `critical()`. |

Level numbers, names, and RFC 5424 severities are listed in
[Record format](record-format.md#levels).

## Message formatting

- `message` is positional-only, so `message=` and `self=` are ordinary fields.
  A non-string message is converted with `str()`.
- The message is formatted with `str.format` semantics when positional
  arguments or keyword arguments other than `exc_info` and `stack_info` are
  passed and the message contains `{` or `}`. `"a }} b"` then becomes
  `"a } b"`. `logger.exception("{{x}}")` logs `{{x}}` unchanged.
- Keyword arguments named by a placeholder, including placeholders nested in a
  format spec (`"{x:{width}}"`), are consumed and do not become fields. Other
  keyword arguments become fields. Positional arguments never become fields.
- A placeholder that names a sensitive key, as its root or through an
  attribute or index (`{token}`, `{creds[password]}`), is interpolated as
  `[REDACTED]`. See [Redaction](redaction.md#key-based-redaction).
- A template that fails to format is logged unformatted, with a
  `UserWarning` issued once per template and error type. A message whose
  `str()` raises is logged as `<unprintable TypeName object>`, with a
  `UserWarning` issued once per type and error type. Logging calls do not raise
  because of their arguments.

## Control keyword arguments

| Keyword | Accepted values | Effect |
|---|---|---|
| `exc_info` | `True` (the exception being handled), an exception instance, or a `(type, value, traceback)` tuple | Adds the `exception` field. See [Exceptions](exceptions.md). |
| `stack_info` | `True` | Adds the `stack` field. See [Record format](record-format.md#stack-field). |

Neither keyword becomes a field. `opt()` sets both for every call of the
returned logger; a keyword passed to a call takes precedence for `exc_info`.

## Context methods

| Method | Returns | Behavior |
|---|---|---|
| `bind(**kwargs)` | new `Logger` | Copies the bound fields and adds `kwargs`. The child shares its parent's sink registry. |
| `contextualize(**kwargs)` | context manager yielding the logger | Binds `kwargs` as context variables for the `with` block, like `bound_contextvars()`. |
| `opt(*, exception=None, stack_info=False)` | new `Logger` | Every call of the returned logger carries the exception information and stack flag. `exception=None` keeps the parent's setting. |
| `catch(exception=Exception, *, level="error", message="An error occurred", reraise=False)` | context manager and decorator | Logs a matching exception with `exc_info` at `level` (`trace` logs at `DEBUG`); suppresses it unless `reraise=True`. An unknown `level` raises `ValueError` when `catch()` is called. Decorates synchronous and coroutine functions; on a logger without a name, records from a decorated function use the function's `__module__` as `logger`. |

Fields merge in this order: bound fields, then call keyword arguments, then
context variables for keys still absent. See
[Key order](record-format.md#key-order).

## Sinks

`add(sink, *, level=None)` registers a sink and returns an integer handler ID.
`remove(handler_id=None)` removes it.

| `sink` value | Delivery |
|---|---|
| `str` or `pathlib.Path` | Appends to the file with UTF-8 encoding; no rotation. On Unix, a file created by `add()` has mode `0600`; an existing file keeps its mode. |
| object with `write()` | Wrapped in `logging.StreamHandler`. |
| `logging.Handler` | Used as given: its own formatter and filters apply, the record's message is the rendered line, and its level is left unchanged. `remove()` closes it. |
| callable | Called with each rendered line, including its trailing newline. |

Other values raise `TypeError`.

| Rule | Behavior |
|---|---|
| `level` | A level name or a non-negative integer; `None` delivers all levels. Anything else raises `ValueError`, before any file is opened. Applies to every sink type. |
| Handler IDs | Process-wide: any logger can remove a sink another logger added. |
| `remove(handler_id)` | An ID that names no active sink raises `ValueError`; a non-integer raises `TypeError`. Outside sink callbacks, waits until deliveries already selected for the sink have finished; inside a callback, those deliveries finish as the dispatch thread drains. |
| `remove()` | Removes the sinks added through this logger and the loggers derived from it with `bind()` or `opt()`. |
| structguru records | Delivered through the callable-sink dispatch queue (`callable_queue_maxsize`, `overflow`), one record at a time in log order, on a dispatch thread. Drained by `flush()`, reconfiguration, `shutdown()`, fork handling, and interpreter exit. |
| Standard-library records | Each sink is also attached to the stdlib root logger and receives third-party records that reach it, unrendered. While the [stdlib bridge](integrations/stdlib.md) is installed, this direct delivery is suspended and the sinks receive those records rendered and redacted instead. |
| Nested logging | A log call made inside a sink callback reaches the native writer but no callable or `add()` sink. |
| Failures | Exceptions raised by a sink, including `BaseException` subclasses, are counted in `writer_metrics()["callable_errors"]` and do not stop later deliveries. |
| Reconfiguration | Sinks survive `configure()` and `update()`, and are not part of `get_config()`. After `shutdown()`, they receive records again once `configure()` is called. |

## Value conversion

Field values are converted natively; a value the renderer cannot represent is
replaced by a marker such as `<unsupported: PosixPath>`. The full table is in
[Record format](record-format.md#value-conversion).

## API

::: structguru.core
    options:
      heading_level: 3
      show_if_no_docstring: true
      filters: ["!^_[^_]", "!^__init__$"]

## See also

- [Send logs to files and sinks](../how-to/send-logs-to-sinks.md)
- [Log exceptions](../how-to/log-exceptions.md)
- [Propagate context](../how-to/propagate-context.md)
- [Compatibility with loguru and structlog](../explanation/compatibility.md)
