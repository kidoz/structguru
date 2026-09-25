# Capture standard-library logging

Libraries and servers log through the standard `logging` module. Install the
stdlib bridge to render those records through structguru, with the same JSON or
console format, redaction, level filtering, context, and destinations as your
own records.

## Install the bridge

Call `install_stdlib_bridge()` once at startup, after `configure()`:

```python
import logging

from structguru import configure
from structguru.integrations.stdlib import install_stdlib_bridge

configure(service="api")
install_stdlib_bridge(level="INFO", suppress_loggers=("urllib3", "botocore"))

logging.getLogger("sqlalchemy.engine").info("SELECT 1", extra={"rows": 3})
```

```json
{"rows":3,"logger":"sqlalchemy.engine","level":"INFO","severity":6,"timestamp":"…","service":"api","message":"SELECT 1"}
```

The bridge adds a `StructguruHandler` to the root logger, first removing the
root's existing handlers (pass `clear_handlers=False` to keep them), and sets
the root level to `level`; structguru's own level still applies on top.
The record's logger name becomes the `logger` field, `extra=` values become
fields, and `exc_info` and `stack_info` render as they do for `structguru.logger`.
`suppress_loggers` raises the named loggers to `suppress_level` (default
`WARNING`).

To undo it, pass the returned handler to `uninstall_stdlib_bridge()`:

```python
from structguru.integrations.stdlib import uninstall_stdlib_bridge

bridge = install_stdlib_bridge()
...
uninstall_stdlib_bridge(bridge)
```

Uninstalling does not restore root handlers removed at install, and does not
revert suppression levels.

### Effect on `logger.add()` sinks

Without the bridge, sinks added with `logger.add()` receive third-party records
raw, straight from the root logger. While the bridge is installed they receive
them only through structguru, rendered and redacted, and never twice.
Uninstalling restores the raw delivery.

## Install again without an error

Only one bridge can be installed at a time; a second `install_stdlib_bridge()`
raises `RuntimeError`. When setup legitimately runs more than once per process
(several entry points, a Django `manage.py` that imports a Celery app module,
repeated setup in a test suite), let the last call win:

```python
bridge = install_stdlib_bridge(level="INFO", replace=True)
```

The previous bridge is released exactly as `uninstall_stdlib_bridge()` would
release it, including restoring its existing-loggers snapshot, before the new
one applies its own policy. A record logged by another thread during the swap
is delivered at most once, and never raises. Suppression levels from the
earlier install are not reverted, and `uninstall_stdlib_bridge()` on the
replaced handler does nothing.

## Control loggers that already exist

Like `logging.config.dictConfig()`, the bridge can disable or re-enable named
loggers that exist when it is installed:

```python
bridge = install_stdlib_bridge(disable_existing_loggers=True)
```

- `True` disables them; `False` re-enables them.
- `None` (the default) reads `STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS`, and
  leaves existing states unchanged when the variable is unset or empty.
- An explicit `True` or `False` always overrides the environment.

```bash
STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS=false python -m myapp
```

The policy is applied once, at installation. The root logger and loggers created
later are unaffected. Uninstalling restores the states the bridge changed,
unless your code changed them again in the meantime.

## Configure the bridge from environment variables

`install_stdlib_bridge()` reads only the existing-loggers variable. To take every
bridge option from the environment, call the explicit installer after your
framework has set up logging:

```python
from structguru.integrations.stdlib import install_stdlib_bridge_from_env

bridge = install_stdlib_bridge_from_env()
```

```bash
STRUCTGURU_STDLIB_LEVEL=INFO \
STRUCTGURU_STDLIB_SUPPRESS_LOGGERS=urllib3,botocore \
STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS=false \
python -m myapp
```

Boolean variables accept `1/0`, `true/false`, `yes/no`, and `on/off`; empty
values count as unset, and any other value raises `ValueError` before logging
state changes. Importing `structguru` never installs the bridge. For every
variable and its default, see
[Standard library integration](../reference/integrations/stdlib.md).

## Capture a logger that does not propagate

A logger with `propagate=False` never reaches the root logger, so the bridge
does not see it. Attach the handler to it directly:

```python
import logging

from structguru.integrations.stdlib import StructguruHandler

access = logging.getLogger("some.server.access")
access.handlers = [StructguruHandler()]
access.propagate = False
```

## Capture uvicorn's logs

uvicorn's default logging configuration gives the `uvicorn` and
`uvicorn.access` loggers their own handlers and sets `propagate=False`, so the
bridge alone does not capture them.

When you start uvicorn from Python, pass `log_config=None` so that uvicorn
leaves logging alone and its records propagate to the bridge:

```python
import uvicorn

from structguru import configure
from structguru.integrations.stdlib import install_stdlib_bridge

configure(service="my-api")
install_stdlib_bridge(level="INFO")

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, log_config=None)
```

With the `uvicorn` command, pass a logging configuration file that routes
everything to `StructguruHandler`. Save it as `logging.json`:

```json
{
  "version": 1,
  "disable_existing_loggers": false,
  "handlers": {
    "structguru": {
      "class": "structguru.integrations.stdlib.StructguruHandler"
    }
  },
  "root": {"handlers": ["structguru"], "level": "INFO"}
}
```

```bash
STRUCTGURU_SERVICE=my-api uvicorn app:app --log-config logging.json
```

`disable_existing_loggers` must be `false`: uvicorn's loggers already exist when
the file is applied. Configuring the handler this way does not install the
managed bridge, so sinks added with `logger.add()` also receive these records
raw; use `configure()` destinations with this recipe.

uvicorn attaches an `extra={"color_message": ...}` value to some records, which
the bridge forwards as a `color_message` field containing ANSI escape codes. To
drop it, add a filter to the handler:

```python
# logfilters.py
import logging


class DropColorMessage(logging.Filter):
    def filter(self, record):
        record.__dict__.pop("color_message", None)
        return True
```

```json
{
  "version": 1,
  "disable_existing_loggers": false,
  "filters": {
    "drop_color_message": {"()": "logfilters.DropColorMessage"}
  },
  "handlers": {
    "structguru": {
      "class": "structguru.integrations.stdlib.StructguruHandler",
      "filters": ["drop_color_message"]
    }
  },
  "root": {"handlers": ["structguru"], "level": "INFO"}
}
```

With `log_config=None`, attach the same filter in Python, for example
`bridge.addFilter(DropColorMessage())` on the handler `install_stdlib_bridge()`
returns. To avoid a second request line next to the ASGI middleware's summary,
disable uvicorn's access log (`--no-access-log`, or `access_log=False`). See
[Log FastAPI and Starlette requests](integrations/asgi.md).

## Quiet noisy loggers only

To raise the level of some loggers without routing anything, use
`suppress_loggers()` on its own:

```python
from structguru.integrations.stdlib import suppress_loggers

suppress_loggers("urllib3", "botocore", level="WARNING")
```

## See also

- [Standard library integration](../reference/integrations/stdlib.md)
- [Send logs to files and sinks](send-logs-to-sinks.md)
- [Run behind Granian](integrations/granian.md)
- [Architecture](../explanation/architecture.md)
