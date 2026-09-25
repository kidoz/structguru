# Log a web service

In this tutorial we will build a small FastAPI service, `orders-api`, and give
it structured request logs. You will add the structguru middleware, read the
summary line it writes for each request, follow one request through its log
lines with a request ID, add your own fields in a handler, and route uvicorn's
own log lines through structguru.

Complete [Get started with structguru](getting-started.md) first. You need
Python 3.11 or newer, `curl`, and two terminal windows: one runs the server,
the other sends requests.

## Set up a project

In the first terminal, create a directory and a virtual environment, and
install structguru, FastAPI, and the uvicorn server:

```bash
mkdir orders-api
cd orders-api
python -m venv .venv
source .venv/bin/activate
pip install structguru fastapi uvicorn
```

The FastAPI middleware is part of structguru itself, so no extra is needed.

## Create the service

Create a file named `main.py` with one route that looks up an order and logs
what it does:

```python
import uvicorn
from fastapi import FastAPI

from structguru import configure, logger

configure(service="orders-api")

app = FastAPI()


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    logger.info("Looking up order {order_id}", order_id=order_id)
    return {"order_id": order_id, "status": "shipped"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

Start the server:

```bash
python main.py
```

You should see uvicorn start:

```text
INFO:     Started server process [52767]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)
```

In the second terminal, request an order:

```bash
curl http://127.0.0.1:8000/orders/1042
```

You should get the order back:

```json
{"order_id":1042,"status":"shipped"}
```

In the first terminal, two new lines have appeared:

```text
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:28.614086Z","service":"orders-api","message":"Looking up order 1042"}
INFO:     127.0.0.1:58409 - "GET /orders/1042 HTTP/1.1" 200 OK
```

The JSON line is the record your handler logged. The text line after it is
uvicorn's own access log; you will bring it into structguru at the end of this
tutorial.

Press Ctrl+C in the first terminal to stop the server.

## Add the middleware

Import `StructguruMiddleware` and add it to the app. Your `main.py` should now
look like this:

```python
import uvicorn
from fastapi import FastAPI

from structguru import configure, logger
from structguru.integrations.asgi import StructguruMiddleware

configure(service="orders-api")

app = FastAPI()
app.add_middleware(StructguruMiddleware)


@app.get("/orders/{order_id}")
async def get_order(order_id: int):
    logger.info("Looking up order {order_id}", order_id=order_id)
    return {"order_id": order_id, "status": "shipped"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

Start the server again:

```bash
python main.py
```

In the second terminal, send the same request:

```bash
curl http://127.0.0.1:8000/orders/1042
```

In the first terminal, you should now see three new lines:

```text
{"request_id":"77a47864-9591-412a-8a90-f5be70730d03","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:33.859128Z","service":"orders-api","message":"Looking up order 1042"}
INFO:     127.0.0.1:58420 - "GET /orders/1042 HTTP/1.1" 200 OK
{"duration_ms":0.51,"status_code":200,"request_id":"77a47864-9591-412a-8a90-f5be70730d03","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"structguru.asgi","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:33.859368Z","service":"orders-api","message":"Request completed"}
```

Two things changed:

- Your handler's record now carries `request_id`, `method`, `path`, and
  `client_ip`, although the handler did not pass them. The middleware adds them
  to the context of each request, the way `contextualize()` did in the first
  tutorial.
- A new record, `Request completed`, comes from the `structguru.asgi` logger.
  It is the summary of the request, with its `status_code` and its
  `duration_ms` in milliseconds.

Both records have the same `request_id`. The request did not bring an ID of its
own, so the middleware generated a random one. Searching your logs for that ID
finds every line this request wrote.

## Send your own request ID

Callers often have an ID already, such as one set by a load balancer or by
another service. Leave the server running, and in the second terminal send a
request with an `X-Request-ID` header. The `-i` option shows the response
headers:

```bash
curl -i -H "X-Request-ID: demo-123" http://127.0.0.1:8000/orders/1042
```

You should see the ID in the response:

```text
HTTP/1.1 200 OK
date: Fri, 25 Sep 2026 08:22:37 GMT
server: uvicorn
content-length: 36
content-type: application/json
x-request-id: demo-123

{"order_id":1042,"status":"shipped"}
```

In the first terminal, both records now use your ID:

```text
{"request_id":"demo-123","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:37.365160Z","service":"orders-api","message":"Looking up order 1042"}
INFO:     127.0.0.1:58427 - "GET /orders/1042 HTTP/1.1" 200 OK
{"duration_ms":0.49,"status_code":200,"request_id":"demo-123","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"structguru.asgi","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:37.365392Z","service":"orders-api","message":"Request completed"}
```

The middleware took the ID from the request instead of generating one, and
returned it in the `x-request-id` response header, so the caller can quote it
when reporting a problem.

Press Ctrl+C to stop the server.

## Add your own fields in a handler

The middleware knows about the request, but only your handler knows who is
asking. Add a `user` query parameter, bind it to a logger, and log one more
record with it:

```python
import uvicorn
from fastapi import FastAPI

from structguru import configure, logger
from structguru.integrations.asgi import StructguruMiddleware

configure(service="orders-api")

app = FastAPI()
app.add_middleware(StructguruMiddleware)


@app.get("/orders/{order_id}")
async def get_order(order_id: int, user: str = "guest"):
    log = logger.bind(user=user)
    log.info("Looking up order {order_id}", order_id=order_id)
    log.info("Order {order_id} found", order_id=order_id, status="shipped")
    return {"order_id": order_id, "status": "shipped"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

Start the server:

```bash
python main.py
```

In the second terminal, request the order as `alice`:

```bash
curl -H "X-Request-ID: demo-123" "http://127.0.0.1:8000/orders/1042?user=alice"
```

In the first terminal, you should see four new lines:

```text
{"user":"alice","request_id":"demo-123","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:42.348458Z","service":"orders-api","message":"Looking up order 1042"}
{"user":"alice","status":"shipped","request_id":"demo-123","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:42.348492Z","service":"orders-api","message":"Order 1042 found"}
INFO:     127.0.0.1:58433 - "GET /orders/1042?user=alice HTTP/1.1" 200 OK
{"duration_ms":0.53,"status_code":200,"request_id":"demo-123","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"structguru.asgi","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:42.348697Z","service":"orders-api","message":"Request completed"}
```

Both of your handler's records carry `"user":"alice"` next to the request
fields from the middleware. The `Request completed` summary has no `user`,
because the middleware writes it, not your bound logger.

Press Ctrl+C to stop the server.

## Route uvicorn's logs through structguru

uvicorn's lines are still plain text, because uvicorn logs through Python's
standard `logging` module. `install_stdlib_bridge()` sends those records
through structguru. Make three changes to `main.py`: import
`install_stdlib_bridge`, call it after `configure()`, and pass
`log_config=None` to `uvicorn.run()` so that uvicorn does not install its own
text handlers. This is the finished `main.py`:

```python
import uvicorn
from fastapi import FastAPI

from structguru import configure, logger
from structguru.integrations.asgi import StructguruMiddleware
from structguru.integrations.stdlib import install_stdlib_bridge

configure(service="orders-api")
install_stdlib_bridge()

app = FastAPI()
app.add_middleware(StructguruMiddleware)


@app.get("/orders/{order_id}")
async def get_order(order_id: int, user: str = "guest"):
    log = logger.bind(user=user)
    log.info("Looking up order {order_id}", order_id=order_id)
    log.info("Order {order_id} found", order_id=order_id, status="shipped")
    return {"order_id": order_id, "status": "shipped"}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_config=None)
```

Start the server:

```bash
python main.py
```

uvicorn's startup messages are now JSON records from the `uvicorn.error`
logger:

```text
{"color_message":"Started server process [\u001b[36m%d\u001b[0m]","logger":"uvicorn.error","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:48.278366Z","service":"orders-api","message":"Started server process [52941]"}
{"logger":"uvicorn.error","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:48.278448Z","service":"orders-api","message":"Waiting for application startup."}
{"logger":"uvicorn.error","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:48.278582Z","service":"orders-api","message":"Application startup complete."}
{"color_message":"Uvicorn running on \u001b[1m%s://%s:%d\u001b[0m (Press CTRL+C to quit)","logger":"uvicorn.error","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:48.278780Z","service":"orders-api","message":"Uvicorn running on http://127.0.0.1:8000 (Press CTRL+C to quit)"}
```

uvicorn attaches a `color_message` field to some of its messages, and
structguru keeps the extra fields of standard-library records, so it appears
here as well.

In the second terminal, send the same request again:

```bash
curl -H "X-Request-ID: demo-123" "http://127.0.0.1:8000/orders/1042?user=alice"
```

Every line in the first terminal is now JSON:

```text
{"user":"alice","request_id":"demo-123","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:48.361838Z","service":"orders-api","message":"Looking up order 1042"}
{"user":"alice","status":"shipped","request_id":"demo-123","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:48.361861Z","service":"orders-api","message":"Order 1042 found"}
{"request_id":"demo-123","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"uvicorn.access","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:48.361919Z","service":"orders-api","message":"127.0.0.1:58472 - \"GET /orders/1042?user=alice HTTP/1.1\" 200"}
{"duration_ms":0.48,"status_code":200,"request_id":"demo-123","method":"GET","path":"/orders/1042","client_ip":"127.0.0.1","logger":"structguru.asgi","level":"INFO","severity":6,"timestamp":"2026-09-25T08:22:48.362040Z","service":"orders-api","message":"Request completed"}
```

The access line from the `uvicorn.access` logger is a structured record too,
and it carries the same `request_id` as the rest of the request, because
uvicorn writes it while the request is being handled.

Press Ctrl+C to stop the server. Its shutdown messages are JSON as well.

## What you learned

You built a FastAPI service whose every log line is structured JSON and can be
traced back to one request. Along the way you:

- added `StructguruMiddleware`, which puts `request_id`, `method`, `path`, and
  `client_ip` on every record logged while a request is handled, and writes a
  `Request completed` summary with `status_code` and `duration_ms`;
- saw the middleware generate a request ID, or take it from the `X-Request-ID`
  header and return it in the response;
- used `logger.bind()` in a handler to add your own fields;
- routed uvicorn's standard-library logs through structguru with
  `install_stdlib_bridge()`.

## Next steps

- [FastAPI and Starlette](../how-to/integrations/asgi.md): use a different
  request ID header, bind more headers, or turn off the summary line.
- [Capture standard-library logging](../how-to/capture-stdlib-logging.md):
  quiet noisy libraries and control which standard-library records reach
  structguru.
- [Context propagation](../explanation/context-propagation.md): how request
  context reaches every record, across function calls, threads, and async
  tasks.
