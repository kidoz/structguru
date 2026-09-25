# Log FastAPI and Starlette requests

Add `StructguruMiddleware` to give every HTTP and WebSocket request a request
ID, bind request fields to every record logged while it runs, and log one
summary line per request. It works with any ASGI framework (FastAPI, Starlette,
Litestar, and others). No extra is needed; install your framework as usual.

## Add the middleware

FastAPI:

```python
# main.py
from fastapi import FastAPI

from structguru import logger
from structguru.integrations.asgi import StructguruMiddleware

app = FastAPI()
app.add_middleware(StructguruMiddleware)


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    logger.info("Loading order {order_id}", order_id=order_id)
    return {"id": order_id}
```

A request to `/orders/7` logs:

```json
{"request_id":"…","method":"GET","path":"/orders/7","client_ip":"127.0.0.1","logger":"main","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"Loading order 7"}
{"duration_ms":…,"status_code":200,"request_id":"…","method":"GET","path":"/orders/7","client_ip":"127.0.0.1","logger":"structguru.asgi","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"Request completed"}
```

Starlette:

```python
from starlette.applications import Starlette
from starlette.middleware import Middleware

from structguru.integrations.asgi import StructguruMiddleware

app = Starlette(routes=routes, middleware=[Middleware(StructguruMiddleware)])
```

Any other ASGI application: wrap it.

```python
app = StructguruMiddleware(app)
```

The request ID comes from the `X-Request-ID` header when it holds a valid value,
and is otherwise generated. It is echoed on the response under the same header,
unless the application already set it.

## Change the header, logger name, or summary

```python
app.add_middleware(
    StructguruMiddleware,
    request_id_header="X-Correlation-ID",  # read and echoed; case-insensitive
    logger_name="api.http",  # logger field of the summary line
    log_request=True,  # False turns the summary line off
)
```

## Bind selected request headers

```python
app.add_middleware(StructguruMiddleware, extract_headers=["x-tenant-id", "x-api-key"])
```

```json
{"x-tenant-id":"acme","x-api-key":"[REDACTED]","request_id":"req-123","method":"GET","path":"/orders/7","client_ip":"127.0.0.1","logger":"main","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"Loading order 7"}
```

Each header is bound under its lower-case name. Credential-bearing headers
(such as `authorization`, `cookie`, `x-api-key`, or any name containing an
active sensitive key) are bound as `[REDACTED]`. Values that are not printable
UTF-8 are dropped, and long values are truncated. For the exact rules, see
[ASGI integration](../../reference/integrations/asgi.md).

## Know what the summary covers

- `duration_ms` stops when the final response body is sent. Work that runs after
  the client has the response, such as Starlette `BackgroundTasks`, is not
  counted, although it still runs and logs inside the request context.
- A request that raises is logged as `Request failed` at `ERROR`, and the
  exception is re-raised unchanged. A cancelled request (for example, a client
  disconnect) is logged as `Request cancelled`.
- Each request runs with a clean context holding only its own fields. Context
  bound by an outer middleware is not visible inside the request, and is
  restored when the middleware returns.

## Route the server's own logs

Server logs (uvicorn, Hypercorn, Granian) go through the standard `logging`
module. uvicorn's default logging configuration sets `propagate=False` on its
`uvicorn` and `uvicorn.access` loggers, so installing the stdlib bridge alone
does not capture them. Start uvicorn with `log_config=None`:

```python
import uvicorn

from structguru import configure
from structguru.integrations.stdlib import install_stdlib_bridge

configure(service="my-api")
install_stdlib_bridge(level="INFO")

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000, log_config=None, access_log=False)
```

`access_log=False` (`--no-access-log` on the command line) avoids a second
line per request next to the middleware's summary. For the `uvicorn` command,
the `--log-config` recipe, and uvicorn's `color_message` field, see
[Capture standard-library logging](../capture-stdlib-logging.md). For Granian,
see [Run behind Granian](granian.md).

## See also

- [ASGI integration](../../reference/integrations/asgi.md) for bound fields and
  summary semantics
- [Propagate context](../propagate-context.md)
- [Log a web service](../../tutorials/web-service.md)
