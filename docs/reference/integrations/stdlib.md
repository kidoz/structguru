# Standard library bridge

`structguru.integrations.stdlib` routes records from the standard `logging`
module through the native renderer, so they share the format, redaction,
level filtering, and destinations of `structguru.logger`. It needs no extra.
Importing `structguru` never installs it.

```python
from structguru.integrations.stdlib import install_stdlib_bridge

bridge = install_stdlib_bridge(level="INFO", suppress_loggers=("urllib3", "botocore"))
```

## Functions

| Function | Returns | Effect |
|---|---|---|
| `install_stdlib_bridge(*, level="INFO", suppress_loggers=(), suppress_level="WARNING", clear_handlers=True, disable_existing_loggers=None, replace=False)` | `StructguruHandler` | Installs the managed bridge on the root logger. |
| `install_stdlib_bridge_from_env(environ=None)` | `StructguruHandler` | Installs the managed bridge with every option read from `STRUCTGURU_STDLIB_*` variables. |
| `uninstall_stdlib_bridge(bridge)` | `None` | Releases the bridge. |
| `suppress_loggers(*names, level="WARNING")` | `None` | Sets the level of each named logger. |

## Installation

`install_stdlib_bridge()` performs these steps under one lock:

1. If a managed bridge is active: raises `RuntimeError` unless `replace=True`;
   with `replace=True`, releases it exactly as `uninstall_stdlib_bridge()`
   would. A bridge whose handler was removed from the root logger directly is
   released without `replace`.
2. Applies `disable_existing_loggers` to the named loggers that exist.
3. With `clear_handlers=True`, removes every handler from the root logger.
4. Suspends direct root-logger delivery to `logger.add()` sinks.
5. Adds a `StructguruHandler` with level `level` to the root logger and sets
   the root logger's level to `level`.
6. Sets each logger in `suppress_loggers` to `suppress_level`.

| Option | Values | Effect |
|---|---|---|
| `level` | level name | Root logger level and handler level. The native `level` setting still applies on top. |
| `suppress_loggers` | iterable of logger names | Loggers raised to `suppress_level`. |
| `suppress_level` | level name | Level for `suppress_loggers`. |
| `clear_handlers` | `bool` | Remove existing root handlers first. |
| `disable_existing_loggers` | `True`, `False`, `None` | `True` disables and `False` re-enables every named logger that exists at installation, as in `logging.config.dictConfig`. `None` reads `STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS`; when that is unset or empty, logger states are left unchanged. The root logger and loggers created later are unaffected. |
| `replace` | `bool` | Replace an active managed bridge instead of raising. |

Level names are case-insensitive. An unknown name, including a numeric string
such as `"10"`, emits a `UserWarning` and is replaced by `INFO`.

With `replace=True`, a record logged by another thread during the swap is
delivered at most once (rendered by the outgoing or the incoming bridge, raw,
or not at all) and the swap does not raise. Suppression levels set by the
replaced bridge are not reverted, and `uninstall_stdlib_bridge()` on the
replaced handler does nothing.

## Uninstallation

`uninstall_stdlib_bridge(bridge)`:

- removes the handler from the root logger and closes it;
- restores each logger whose disabled state the installation changed, unless
  application code changed that state again since;
- restores direct root-logger delivery to `logger.add()` sinks.

It does not restore root handlers removed by `clear_handlers=True`, the root
logger's level, or suppression levels. Called with a handler that was already
released, it does nothing.

## Record conversion

`StructguruHandler` converts each record it receives:

| Record attribute | Result |
|---|---|
| `name` | `logger` field. |
| `levelno` | Rendered level band (see [Record format](../record-format.md#stdlib-record-levels)); the number itself is used for the native level threshold, sampling, and `logger.add()` sink levels. |
| `getMessage()` | `message`, passed through without brace formatting. |
| `extra=` attributes | Fields, converted by the native [value rules](../record-format.md#value-conversion); the record and its objects are not modified. |
| `exc_info` | `exception` field. |
| `stack_info` | `stack` field, with the record's own stack text. |
| `created` | `timestamp`. |

Records from loggers named `structguru` or `structguru.*` are ignored. The
handler applies its own filters (including record-replacing filters on
Python 3.12+) and does not hold the handler lock while delivering. Errors are
reported through `Handler.handleError()`.

A `StructguruHandler` attached by other means (to a logger with
`propagate=False`, or in a `dictConfig`) converts records the same way but is
not a managed bridge: it does not suspend the direct delivery to `logger.add()`
sinks, so a record that reaches both that handler and the root logger arrives
at those sinks twice, rendered and raw.

## Environment variables

`install_stdlib_bridge_from_env()` reads every option from the environment
(or from the `environ` mapping). `install_stdlib_bridge()` and Django's
`build_logging_config()` read only `STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS`,
and only when `disable_existing_loggers` is omitted.

| Variable | Option | Default when unset |
|---|---|---|
| `STRUCTGURU_STDLIB_LEVEL` | `level` | `LOG_LEVEL`, then `INFO` |
| `STRUCTGURU_STDLIB_CLEAR_HANDLERS` | `clear_handlers` | `true` |
| `STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS` | `disable_existing_loggers` | leave existing logger states unchanged |
| `STRUCTGURU_STDLIB_SUPPRESS_LOGGERS` | `suppress_loggers` | none |
| `STRUCTGURU_STDLIB_SUPPRESS_LEVEL` | `suppress_level` | `WARNING` |
| `STRUCTGURU_STDLIB_REPLACE` | `replace` | `false` |

- Boolean variables accept `1`/`0`, `true`/`false`, `yes`/`no`, and `on`/`off`,
  case-insensitively and with surrounding whitespace ignored; an empty value
  counts as unset. Other values raise `ValueError` before logging state
  changes.
- `STRUCTGURU_STDLIB_SUPPRESS_LOGGERS` is a comma-separated list; whitespace
  around names and empty entries are ignored, and duplicates are removed.
- Level variables are used as given, including when empty, and follow the
  level-name rule above.

## API

::: structguru.integrations.stdlib
    options:
      heading_level: 3

## See also

- [Capture standard-library logging](../../how-to/capture-stdlib-logging.md)
- [Granian](../../how-to/integrations/granian.md)
- [Settings and environment variables](../settings.md)
