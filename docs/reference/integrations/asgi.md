# ASGI

`structguru.integrations.asgi.StructguruMiddleware` wraps any ASGI 3
application (FastAPI, Starlette, Litestar). It needs no extra.

```python
from structguru.integrations.asgi import StructguruMiddleware

app.add_middleware(StructguruMiddleware, request_id_header="X-Request-ID")
# or: app = StructguruMiddleware(app)
```

## Parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `app` | ASGI application | required | The wrapped application. |
| `request_id_header` | `str` | `"x-request-id"` | Request header to read the request ID from, case-insensitively; also the response header that carries it. |
| `logger_name` | `str` | `"structguru.asgi"` | `logger` field of the request summary. |
| `log_request` | `bool` | `True` | Log one summary record per request. |
| `extract_headers` | `Sequence[str] \| None` | `None` | Additional request headers to bind as context fields. |

## Scopes

`http` and `websocket` scopes are handled. Other scopes (`lifespan`) are passed
to the application unchanged.

## Context fields

The request runs with a context that holds only these fields. The context bound
before the middleware is restored when the request ends, on every exit path.

| Field | Value |
|---|---|
| `request_id` | The request-ID header value if valid (see [Request ID](#request-id)); otherwise a new UUID4 string. |
| `method` | The HTTP method; `WS` for WebSocket scopes. |
| `path` | `scope["path"]`. |
| `client_ip` | The client host from `scope["client"]`, or `""`. |
| each header in `extract_headers` | The header value, under the lower-case header name (`x-tenant-id`). |

When an extracted header has the same name as one of the four request fields,
the request field wins.

### Extracted headers

| Rule | Behavior |
|---|---|
| Missing header | Not bound. |
| Credential header | Bound as `[REDACTED]`. Credential headers are `authorization`, `proxy-authorization`, `cookie`, `set-cookie`, `x-api-key`, `x-auth-token`, `x-access-token`, `x-csrf-token`, `x-csrftoken`, `x-xsrf-token`, `x-amz-security-token`, and any header in which a run of consecutive `-`- or `_`-separated words, joined by `_` or with nothing, equals an active sensitive key (`x-session-id` matches `session_id`; `x-goog-api-key` matches `api_key`). |
| Encoding | The value must decode as strict UTF-8; otherwise it is not bound. |
| Length | Values longer than 256 characters are cut to 256 characters and suffixed with `...[truncated]`. |
| Non-printable characters | A value containing any (CR, LF, NUL, other controls) is not bound. |

Active sensitive keys are the configured `sensitive_keys`, or
`DEFAULT_SENSITIVE_KEYS`.

## Request ID

The request-ID header value is used when it is valid UTF-8, non-empty, at most
128 characters, and printable; otherwise a UUID4 string is generated. An
invalid value never fails the request. For HTTP responses, the ID is added to
the response start message under `request_id_header` unless the application
already set that header.

## Request summary

With `log_request=True`, one record is logged when the application returns or
raises. Its fields are the context fields plus:

| Field | Value |
|---|---|
| `duration_ms` | Milliseconds from the start of the request to the final `http.response.body` message (no `more_body`), rounded to two decimals. Without such a message, until the application returned or raised. Work after the final body message (Starlette `BackgroundTasks`) is not counted. |
| `status_code` | The status of `http.response.start` when the application sent one. Otherwise `500` for an HTTP request that was not cancelled, and absent for a cancelled request or a WebSocket scope. |

| Outcome | Message | Level |
|---|---|---|
| Application returns | `Request completed` | `INFO` |
| Application raises, including `KeyboardInterrupt`, `SystemExit`, and exception groups | `Request failed` | `ERROR` |
| Task cancelled (`asyncio.CancelledError`, `GeneratorExit`), for example on client disconnect | `Request cancelled` | `INFO` |

The exception is re-raised unchanged and is not attached to the summary
record.

## API

::: structguru.integrations.asgi
    options:
      heading_level: 3

## See also

- [FastAPI and Starlette](../../how-to/integrations/asgi.md)
- [Granian](../../how-to/integrations/granian.md)
- [Context propagation](../../explanation/context-propagation.md)
