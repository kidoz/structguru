# Flask

`structguru.integrations.flask.setup_flask_logging()` registers request hooks
on a Flask application. Install the `flask` extra (`flask>=2.0`).

```python
from structguru.integrations.flask import setup_flask_logging

setup_flask_logging(app, request_id_header="X-Request-ID")
```

## Parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `app` | `flask.Flask` | required | The application. |
| `request_id_header` | `str` | `"X-Request-ID"` | Request header to read the request ID from; also the response header that carries it. |
| `log_request` | `bool` | `True` | Log one summary record per request. |
| `logger_name` | `str` | `"structguru.flask"` | `logger` field of the request summary. |

## Registered hooks

| Hook | Position | Action |
|---|---|---|
| `before_request` | First application-level hook, also ahead of hooks registered earlier | Clears the context and binds the context fields. |
| `after_request` | Registered at setup | Sets the response header; wraps a streamed body. |
| `teardown_request` | Runs after every other application-level teardown hook | Logs a summary if none was logged; clears the context. |
| `request_finished` signal | Connected for this application (Flask 2.3+, or earlier with blinker) | Logs the summary of a non-streamed response. |

Context bound by later `before_request` hooks is kept for the rest of the
request. A `before_request` hook that ends the request early (a `401`, a
`429`) still gets a request ID, the response header, and a summary.

## Context fields

| Field | Value |
|---|---|
| `request_id` | The request-ID header value when it is non-empty, at most 128 characters, and printable; otherwise a new UUID4 string. |
| `method` | `request.method`. |
| `path` | `request.path`. |
| `client_ip` | `request.remote_addr`, or `""`. |

The response header `request_id_header` is set to the request ID, replacing
any value the application set, on every response that passes through
`after_request`.

## Request summary

Every request logs exactly one summary record, with the context fields plus:

| Field | Value |
|---|---|
| `duration_ms` | Milliseconds from the binding hook to the summary, rounded to two decimals. |
| `status_code` | The response status. `500` when the summary is logged from the teardown hook because of an exception. |

| Case | Logged when | Message | Level |
|---|---|---|---|
| Non-streamed response | Flask has finalized the response (`request_finished`); without signal support, when `after_request` returns | `Request completed` | `INFO` |
| Streamed body produced by application code (a generator, with or without `stream_with_context`) | The body is exhausted or closed; `duration_ms` includes streaming time | `Request completed` | `INFO` |
| Streamed body raises | When it raises | `Request failed`, with the exception | `ERROR` |
| Exception propagates past Flask (`PROPAGATE_EXCEPTIONS`, testing, debug mode) | Teardown | `Request failed`, with the exception, `status_code=500` | `ERROR` |
| An `after_request` hook that runs later fails | Flask finalizes its `500` response | `Request completed`, `status_code=500` | `INFO` |
| The error response also fails to finalize | Teardown | `Request failed` | `ERROR` |

Bodies passed to the server without application code (`send_file`,
`direct_passthrough`) and Werkzeug's own error responses are summarized as
non-streamed responses. A streamed body is produced with the request context
bound. Werkzeug's test client reads streamed bodies lazily, so in tests the
summary appears after `response.get_data()` or `response.close()`.

## API

::: structguru.integrations.flask
    options:
      heading_level: 3

## See also

- [Flask](../../how-to/integrations/flask.md)
- [Context propagation](../../explanation/context-propagation.md)
