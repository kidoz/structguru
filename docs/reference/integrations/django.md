# Django

`structguru.integrations.django` provides a request middleware and a
`LOGGING` dictionary builder. Install the `django` extra (`django>=4.0`).

```python
# settings.py
from structguru.integrations.django import build_logging_config

LOGGING = build_logging_config(service="my-django-app", level="INFO", json_logs=True)
MIDDLEWARE = [
    # ...
    "structguru.integrations.django.StructguruMiddleware",
]
```

## `StructguruMiddleware`

The middleware takes no options. It clears the context on entry and on every
exit path, so a reused worker thread never inherits a previous request's
fields.

### Context fields

| Field | Value |
|---|---|
| `request_id` | The `X-Request-ID` request header (`request.META["HTTP_X_REQUEST_ID"]`) when it is non-empty, at most 128 characters, and printable; otherwise a new UUID4 string. |
| `method` | `request.method`. |
| `path` | `request.path`. |
| `client_ip` | `request.META["REMOTE_ADDR"]`, or `""`. |
| `user_id` | `str(request.user.pk)`, bound only when `request.user` exists and its `pk` is truthy. |

The response header `X-Request-ID` is set to the request ID, replacing any
existing value. Summary records use the logger name `structguru.django`.

### Request summary

| Case | Logged when | Message | Level | Fields added to the context fields |
|---|---|---|---|---|
| Response returned | After `get_response()` returns | `Request completed` | `INFO` | `status_code`, `duration_ms` |
| Streaming response (`StreamingHttpResponse` with a synchronous or asynchronous iterator, `FileResponse`) | The body is exhausted or the server closes the response, whichever comes first | `Request completed` | `INFO` | `status_code`, `duration_ms` including streaming time |
| Streaming body raises | When it raises | `Request failed`, with the exception | `ERROR` | `status_code`, `duration_ms` |
| Streaming body cancelled (`asyncio.CancelledError`, a client disconnect under ASGI) | When it is cancelled | `Request cancelled` | `INFO` | `status_code`, `duration_ms` |
| Exception propagates out of `get_response` | Immediately; the exception is re-raised | `Request failed`, with the exception | `ERROR` | none |

`duration_ms` is rounded to two decimals. A streamed body's iterator runs with
the request context bound, and so do the response's closers. A `FileResponse`
over a file keeps its file, so WSGI servers can use `wsgi.file_wrapper`.

## `build_logging_config()`

Returns a `logging.config.dictConfig` dictionary. The records it configures are
formatted by the standard library, not by the structguru renderer.

| Parameter | Default | Effect |
|---|---|---|
| `service` | `"app"` | Added to every line. |
| `level` | `"INFO"` | Root logger level, upper-cased. |
| `json_logs` | `True` | Select the `json` or the `console` formatter. |
| `disable_existing_loggers` | `None` | Value of `disable_existing_loggers`. `None` reads `STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS` (`1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off`; empty means unset) and otherwise uses `False`. |

The dictionary has one handler, `console` (`logging.StreamHandler`, which
writes to `sys.stderr`), attached to the root logger.

| Formatter | Output |
|---|---|
| `json` | One JSON object per record with the keys `service`, `level` (the stdlib level name, such as `WARNING`), `name`, `message`, and, when present, `exception` (formatted traceback) and `stack`. `extra=` fields are not included. |
| `console` | `%(asctime)s [%(levelname)s] <service> %(name)s: %(message)s`, for example `2026-01-01 12:00:00,000 [INFO] my-django-app django.request: ...` |

## API

::: structguru.integrations.django
    options:
      heading_level: 3

## See also

- [Django](../../how-to/integrations/django.md)
- [Standard library bridge](stdlib.md)
- [Context propagation](../../explanation/context-propagation.md)
