# Log Django requests

Add `StructguruMiddleware` to give every Django request a request ID, bind
request fields to every record logged while it runs, and log one summary line
per request. Install the `django` extra:

```bash
pip install "structguru[django]"
```

## Configure settings.py

```python
# settings.py
from structguru import configure
from structguru.integrations.django import build_logging_config

configure(service="my-django-app")

LOGGING = build_logging_config(
    service="my-django-app",
    level="INFO",
    json_logs=True,
    disable_existing_loggers=False,
)

MIDDLEWARE = [
    "django.contrib.sessions.middleware.SessionMiddleware",
    # ...
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # ...
    "structguru.integrations.django.StructguruMiddleware",
]
```

The middleware binds `request_id`, `method`, `path`, and `client_ip`, sets the
`X-Request-ID` response header, and logs a summary line with the status code
and duration:

```json
{"request_id":"…","method":"GET","path":"/orders/7","client_ip":"127.0.0.1","logger":"orders.views","level":"INFO","severity":6,"timestamp":"…","service":"my-django-app","message":"Loading order 7"}
{"status_code":200,"duration_ms":…,"request_id":"…","method":"GET","path":"/orders/7","client_ip":"127.0.0.1","logger":"structguru.django","level":"INFO","severity":6,"timestamp":"…","service":"my-django-app","message":"Request completed"}
```

- Place it **after** `AuthenticationMiddleware` to also bind `user_id` for
  authenticated users; placed before it, `request.user` does not exist yet.
- The request ID comes from an incoming `X-Request-ID` header when it holds a
  valid value, and is otherwise generated.
- `build_logging_config()` configures Django's standard-library logging only.
  structguru's own records, including the summary line, take their service name
  from `configure()` (or `STRUCTGURU_SERVICE`), so set it in both places.

## Choose JSON or console output for Django's logs

`build_logging_config()` adds `service` to every line of Django's own logs: as a
`service` field with `json_logs=True`, and after the level with
`json_logs=False`:

```text
2026-01-01 12:00:00,000 [INFO] my-django-app django.request: ...
```

When `disable_existing_loggers` is omitted, it reads
`STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS`, and otherwise defaults to `False`.
An explicit value always takes precedence.

## Render Django's logs through structguru instead

`build_logging_config()` formats Django's records with the standard library,
so they do not share structguru's layout, redaction, or request fields. To
render them like your own records, point `LOGGING` at `StructguruHandler`:

```python
# settings.py
from structguru import configure

configure(service="my-django-app", level="INFO")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "structguru": {"class": "structguru.integrations.stdlib.StructguruHandler"},
    },
    "root": {"handlers": ["structguru"], "level": "INFO"},
}
```

A standard-library record logged during a request then carries the request
fields:

```json
{"request_id":"…","method":"GET","path":"/orders/7","client_ip":"127.0.0.1","logger":"orders.views","level":"WARN","severity":4,"timestamp":"…","service":"my-django-app","message":"Stock is low"}
```

Values that structguru cannot render, such as the request object Django attaches
to `django.request` records, appear as `<unsupported: WSGIRequest>`.

## Stream responses

For `StreamingHttpResponse` (with a synchronous or asynchronous iterator) and
`FileResponse`, the summary is logged when the server closes the response, so
`duration_ms` includes the streaming time. The body's iterator runs with the
request context bound. A body that raises is logged as `Request failed`, and
one cancelled by a client disconnect under ASGI as `Request cancelled`. A
`FileResponse` over a file keeps the file, so WSGI servers can still send it
with `wsgi.file_wrapper`.

## See also

- [Django integration](../../reference/integrations/django.md) for bound fields
  and summary semantics
- [Capture standard-library logging](../capture-stdlib-logging.md)
- [Propagate context](../propagate-context.md)
