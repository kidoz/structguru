# Run behind Granian

Route [Granian](https://github.com/emmett-framework/granian)'s server logs
through structguru and add structured request summaries to an ASGI application.
Server logs go through `StructguruHandler`; request IDs and summaries come from
`StructguruMiddleware`. Install Granian and your framework separately; no
structguru extra is needed.

This setup was verified with Granian 2.8.2, two workers, and CPython 3.14 on
macOS.

## Route Granian's logs

Granian's `_granian` and `granian.access` loggers normally disable propagation,
so installing a root bridge alone does not capture them. Save this as
`logging.json` in your application directory:

```json
{
  "version": 1,
  "disable_existing_loggers": false,
  "handlers": {
    "structguru": {
      "class": "structguru.integrations.stdlib.StructguruHandler"
    }
  },
  "root": {"handlers": ["structguru"], "level": "INFO"},
  "loggers": {
    "_granian": {"handlers": [], "level": "INFO", "propagate": true},
    "granian.access": {"handlers": [], "level": "INFO", "propagate": true}
  }
}
```

This replaces the server loggers' handlers with propagation to a single root
handler. structguru renders the records as JSON; no other JSON formatter and no
`install_stdlib_bridge()` call is needed.

## Wrap the application

For example, save this FastAPI application as `app.py`:

```python
from fastapi import FastAPI

from structguru import logger
from structguru.integrations.asgi import StructguruMiddleware

api = FastAPI()


@api.get("/health")
async def health():
    logger.info("Health checked")
    return {"ok": True}


app = StructguruMiddleware(api)
```

## Launch Granian

Run from the directory that contains `app.py` and `logging.json`:

```bash
STRUCTGURU_AUTOCONFIGURE=1 \
STRUCTGURU_SERVICE=my-api \
STRUCTGURU_LEVEL=INFO \
STRUCTGURU_FORMAT=json \
STRUCTGURU_TARGET=stdout \
granian --interface asgi --workers 2 --log-level info \
  --log-config logging.json --no-access-log app:app
```

- Set the environment variables before launching Granian, so they apply when the
  handler imports structguru in the parent and worker processes. Granian
  configures logging before it loads the application and its `--env-files`, so
  configuring only inside `app.py`, or relying on those files, is too late for
  the first server logs.
- Pass `--interface asgi` explicitly: Granian's default interface is RSGI, which
  the middleware does not support.

A request to `/health` then logs:

```json
{"request_id":"…","method":"GET","path":"/health","client_ip":"127.0.0.1","logger":"app","level":"INFO","severity":6,"timestamp":"…","service":"my-api","message":"Health checked"}
{"duration_ms":…,"status_code":200,"request_id":"…","method":"GET","path":"/health","client_ip":"127.0.0.1","logger":"structguru.asgi","level":"INFO","severity":6,"timestamp":"…","service":"my-api","message":"Request completed"}
```

## Choose who writes the request summary

| Summary source | Granian option | Middleware option | Output |
| --- | --- | --- | --- |
| structguru (recommended) | `--no-access-log` | `log_request=True` (default) | Structured request ID, method, path, client IP, duration, status, and outcome |
| Granian | `--access-log` | `log_request=False` | Access text in the JSON `message`; application logs still receive middleware context |

For Granian's summaries, change the application wrapper to:

```python
app = StructguruMiddleware(api, log_request=False)
```

- Enabling both yields two records per HTTP request.
- Granian passes its access fields as message-formatting arguments, so they do
  not become separate `status` or `dt_ms` fields. A JSON access format would
  become a string inside the outer JSON `message`.
- Granian writes access logs after the middleware has cleared the request
  context, so those records do not carry the request ID. Use the middleware's
  summaries for request correlation.
- The middleware only sees requests that reach the application. Traffic the
  server handles itself, such as direct static-file serving, needs separate
  access-log consideration.

## Launch from Python

The equivalent programmatic setup passes the same dictionary as
`log_dictconfig`:

```python
import json
from pathlib import Path

from granian import Granian
from granian.constants import Interfaces
from granian.log import LogLevels


if __name__ == "__main__":
    Granian(
        "app:app",
        interface=Interfaces.ASGI,
        workers=2,
        log_level=LogLevels.info,
        log_dictconfig=json.loads(Path("logging.json").read_text()),
        log_access=False,
    ).serve()
```

Start this launcher with the same environment variables. For a complete version
that configures logging in both the launcher and workers, see the runnable
[Granian + FastAPI example](https://github.com/kidoz/structguru/blob/main/examples/granian_fastapi/main.py).
Its separate `app.py` and `main.py` support `HOST`, `PORT`, and
`WEB_CONCURRENCY` and use one structured request summary per request.

## Log at DEBUG

Set `STRUCTGURU_LEVEL=DEBUG` and Granian's log level to `debug`, and change the
root level in `logging.json` to `DEBUG` for application stdlib loggers that
inherit it. Granian applies its log-level option to `_granian` after reading the
dictionary. Supply complete handler and logger mappings, because Granian's
configuration merge replaces nested sections.

## Keep logging active through shutdown

Keep structguru active until the server has shut down, to capture Granian's last
messages. Use `flush()` for an explicit checkpoint; normal interpreter exit
drains the writer. Calling `shutdown()` during application lifespan teardown
disables logging before the server writes its own shutdown messages.

## Add destinations

Prefer `configure(file_path=...)` or `configure(callable_sinks=[...])`, applied
in each process. Configuring `StructguruHandler` directly does not activate the
managed bridge's suppression of raw `logger.add()` delivery, so combining this
recipe with `logger.add()` can deliver third-party records twice to those
sinks. Remember that `configure()` replaces the whole configuration.

## Other interfaces

The ASGI middleware does not support Granian's RSGI interface. For WSGI
applications, use the [Flask](flask.md) or [Django](django.md) integration for
request context.

## See also

- [Log FastAPI and Starlette requests](asgi.md)
- [Capture standard-library logging](../capture-stdlib-logging.md)
- [ASGI integration](../../reference/integrations/asgi.md)
- [Standard library integration](../../reference/integrations/stdlib.md)
