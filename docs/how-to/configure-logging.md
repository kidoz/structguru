# Configure logging

Importing `structguru` already configures JSON output to stdout at `INFO` with
the service name `app`. Configure it yourself when you need another service
name, level, output format, or destination.

## Configure at startup

Call `configure()` once, at the application entry point:

```python
from structguru import configure, logger

configure(service="checkout", level="INFO", format="json")

logger.info("Order {order_id} accepted", order_id=42, amount=19.99)
```

```json
{"amount":19.99,"logger":"__main__","level":"INFO","severity":6,"timestamp":"…","service":"checkout","message":"Order 42 accepted"}
```

Each `configure()` call replaces the whole configuration. Options you omit
return to their environment value or built-in default; they are not carried
over from the previous call. Sinks added with `logger.add()` are the exception:
they survive every reconfiguration.

## Change selected options

Use `update()` to change some options and keep the rest:

```python
from structguru import configure, update

configure(service="checkout", sensitive_patterns=[r"token=\w+"])
update(otel=True, structured_exceptions=True)  # keeps service and redaction
```

- `update()` never rereads the environment.
- It needs an active runtime: before `configure()` (or after `shutdown()`) it
  raises `RuntimeError`.
- An empty `update()` does nothing.
- A level-only update keeps the writer, its queue, and rate-limit state. Any
  other update rebuilds the writers and resets sampling and rate-limit state,
  as `configure()` does.

## Change the level at runtime

```python
from structguru import set_level

set_level("DEBUG")
```

`set_level()` and `update(level=...)` keep queued records and rate-limit state.
Levels accept case-insensitive names (`"debug"`, `"WARNING"`, `"NOTSET"`) or
non-negative integer thresholds (`10`). Unknown names, booleans, and negative
integers raise `ValueError`.

## Switch between JSON and console output

`format="json"` (the default) writes one compact JSON object per line.
`format="console"` writes colored, human-readable lines for development:

```python
from structguru import configure, logger

configure(service="checkout", format="console", colors=False)
logger.info("Order {order_id} accepted", order_id=42, amount=19.99)
```

```text
2026-09-25T08:21:41.671284Z [INFO    ] Order 42 accepted  amount=19.99
```

`colors` defaults to whether stdout is a terminal. Set `colors=True` or
`colors=False` to override it; JSON output ignores it.

## Configure from environment variables

`configure()` without a `Settings` object, and the automatic configuration at
import, read `STRUCTGURU_*` variables for the options you do not pass:

```bash
STRUCTGURU_SERVICE=checkout \
STRUCTGURU_LEVEL=INFO \
STRUCTGURU_FORMAT=json \
STRUCTGURU_RATE_LIMIT=100/60 \
python -m myapp
```

Explicit keyword arguments always win, even `None` or a value equal to the
default:

```python
from structguru import configure

configure(level="DEBUG")  # service, format, and rate limit still come from the environment
```

An invalid value fails the import with an actionable exception, so a deployment
cannot start with logging silently disabled. File output, redaction, exception
formatting, and the other options are set from Python only. The standard-library
bridge has its own `STRUCTGURU_STDLIB_*` variables; see
[Capture standard-library logging](capture-stdlib-logging.md).

For every variable, its default, and its compatibility fallback (such as
`LOG_LEVEL`), see [Settings and environment variables](../reference/settings.md).

## Configure explicitly instead of at import

Set `STRUCTGURU_AUTOCONFIGURE=0` before the process starts. Logging then stays
disabled until you call `configure()`:

```bash
STRUCTGURU_AUTOCONFIGURE=0 python -m myapp
```

```python
from structguru import configure, logger

logger.info("dropped: logging is not configured yet")
configure(service="checkout")
logger.info("now logged")
```

The variable accepts `1/0`, `true/false`, `yes/no`, or `on/off`, and controls
only the import-time step. Use this when an invalid import-time environment
value would otherwise stop the application before it can call `configure()`.

## Reuse a configuration

Build a `Settings` object when you load configuration yourself, or want to
apply the same options in several places. `Settings` validates Python values
when it is constructed:

```python
from structguru import Settings, configure

settings = Settings.from_mapping({"service": "checkout", "level": "DEBUG"})
configure(settings)  # uses these values instead of the environment
configure(settings, level="INFO")  # keyword overrides still win
```

- `Settings(...)` takes the same keywords as `configure()`.
- `Settings.from_mapping()` rejects unknown keys and does not coerce strings; it
  reads no files and no environment.
- `Settings.from_env()` resolves the environment without applying it. Pass a
  mapping instead of the process environment, for example in tests:
  `Settings.from_env({"STRUCTGURU_LEVEL": "debug"})`.

Regex compilation and file access are checked by `configure()`. If they fail,
it raises and the previous configuration stays active.

## Save and restore the active configuration

`get_config()` returns the active `Settings`, or `None` when logging is shut
down:

```python
from structguru import configure, get_config

saved = get_config()
configure(service="maintenance-task")
...
configure(saved)
```

The snapshot describes configured options only. It does not include buffered
records, counters, the standard-library bridge, or sinks added with
`logger.add()`. Streams and callbacks keep their identity.

## Stop logging

```python
from structguru import shutdown

shutdown()
```

`shutdown()` drains buffered records and then disables logging until the next
`configure()`. You do not need it for a normal exit: interpreter exit drains the
writer on its own.

## See also

- [Configuration and runtime control](../reference/configuration.md)
- [Settings and environment variables](../reference/settings.md)
- [Record format](../reference/record-format.md)
- [Delivery and lifecycle](../explanation/delivery-and-lifecycle.md)
