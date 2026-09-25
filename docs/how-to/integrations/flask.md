# Log Flask requests

Call `setup_flask_logging()` to give every Flask request a request ID, bind
request fields to every record logged while it runs, and log one summary line
per request. Install the `flask` extra:

```bash
pip install "structguru[flask]"
```

## Set up the hooks

```python
# shop.py
from flask import Flask

from structguru import logger
from structguru.integrations.flask import setup_flask_logging

app = Flask(__name__)
setup_flask_logging(app)


@app.get("/orders/<int:order_id>")
def get_order(order_id):
    logger.info("Loading order {order_id}", order_id=order_id)
    return {"id": order_id}
```

A request to `/orders/7` logs:

```json
{"request_id":"…","method":"GET","path":"/orders/7","client_ip":"127.0.0.1","logger":"shop","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"Loading order 7"}
{"duration_ms":…,"status_code":200,"request_id":"…","method":"GET","path":"/orders/7","client_ip":"127.0.0.1","logger":"structguru.flask","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"Request completed"}
```

The request ID comes from the `X-Request-ID` header when it holds a valid value,
and is otherwise generated. It is set on the response under the same header.

## Change the header, logger name, or summary

```python
setup_flask_logging(
    app,
    request_id_header="X-Correlation-ID",
    logger_name="api.flask",  # logger field of the summary line
    log_request=True,  # False turns the summary line off
)
```

## Add fields in your own hooks

The binding hook runs before every other application `before_request` hook,
including ones registered before `setup_flask_logging()`. Fields your hooks bind
stay for the rest of the request:

```python
from flask import abort

from structguru import bind_contextvars


@app.before_request
def authenticate():
    user = load_user()
    if user is None:
        abort(401)  # still gets a request ID, the header, and a summary line
    bind_contextvars(user_id=user.id)
```

A hook that ends the request early, such as an authentication `401` or a
rate-limit `429`, still gets a request ID, the response header, and a summary
line. The context is cleared after every other application `teardown_request`
hook, so those hooks log with it too.

## Stream responses

A streamed body (a generator, with or without `stream_with_context`) is produced
with the request context bound, and its `Request completed` summary is logged
when the body is exhausted or closed, so `duration_ms` includes the streaming
time. A body that raises while streaming is logged as `Request failed` at
`ERROR`. Files passed straight to the server (`send_file`) and error pages are
summarized when the response is returned.

Werkzeug's test client reads a streamed body lazily. In tests, the summary
appears only after `response.get_data()` or `response.close()`:

```python
response = app.test_client().get("/export")
response.get_data()  # the summary line is logged here
```

## Know what happens on errors

- When an exception propagates past Flask (`PROPAGATE_EXCEPTIONS`, testing, or
  debug mode), `after_request` does not run; the summary is logged from the
  teardown hook as `Request failed` with status `500` and the exception.
- Every request logs exactly one summary. A non-streamed summary is logged once
  Flask has finalized the response, so when a later `after_request` hook fails
  and Flask answers with a `500` instead, the summary reports the `500`.

## See also

- [Flask integration](../../reference/integrations/flask.md) for bound fields
  and summary semantics
- [Propagate context](../propagate-context.md)
- [Capture standard-library logging](../capture-stdlib-logging.md) for
  Werkzeug's and other libraries' logs
