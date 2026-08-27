# structguru Integrations Guide

This guide provides in-depth information on how to use `structguru` with various frameworks and libraries.

## Shared Setup

The native runtime is configured automatically. Configure it explicitly at the
application entry point when you need a custom service, level, or sink:

```python
from structguru import configure

configure(service="myapp", level="INFO", format="json")
```

## HTTPX

To log outbound HTTP requests using HTTPX, you can attach event hooks to your client.

```python
import httpx
from structguru.integrations.httpx import StructguruHTTPXLoggingHooks

client = httpx.Client(event_hooks=StructguruHTTPXLoggingHooks.get_hooks())
response = client.get("https://example.com")
```

`AsyncClient` requires awaitable hooks, exposed separately:

```python
async def fetch() -> httpx.Response:
    async with httpx.AsyncClient(
        event_hooks=StructguruHTTPXLoggingHooks.get_async_hooks(),
    ) as client:
        return await client.get("https://example.com")
```

The hooks log every request that receives a response, and capture the `X-Request-ID` header if it's set. Responses with an HTTP error status (4xx/5xx) are logged at `ERROR`, everything else at `INFO`.

Note that logging happens in the `response` hook, so *transport-level* failures — connection refused, DNS failures, timeouts — are not logged here. Those raise before a response exists, and httpx never invokes the hook. Wrap the call site if you need those recorded — and use `logger.catch(reraise=True)`, because `catch()` defaults to `reraise=False` and would otherwise swallow the transport error instead of letting the caller handle it.

## Requests

If you use the `requests` library, you can get a pre-configured `Session` object.

```python
from structguru.integrations.requests import get_logging_session

session = get_logging_session()
session.get("https://example.com")
```

## Standard Library Integration

Third-party libraries log through the standard `logging` module. To render those
records through structguru's native path — same JSON/console formatting,
redaction, level filtering, and output stream as `structguru.logger` — install
the bridge:

```python
from structguru.integrations.stdlib import install_stdlib_bridge

install_stdlib_bridge(level="INFO", suppress_loggers=("urllib3", "botocore"))

import logging
logging.getLogger("sqlalchemy.engine").info("SELECT 1")
# -> {"logger":"sqlalchemy.engine","level":"INFO",...,"message":"SELECT 1"}
```

While the bridge is installed, sinks registered with `logger.add()` receive
third-party records only through the native path — rendered and redacted, once.
Without it those sinks also see the same records raw, straight off the root
logger. `uninstall_stdlib_bridge(bridge)` detaches the bridge and restores that
raw delivery.

`install_stdlib_bridge` attaches a `StructguruHandler` to the root logger, sets
the root and handler levels, and raises `suppress_loggers` to `suppress_level`
(default `WARNING`). The record's logger name becomes the `logger` field,
`extra=` fields are forwarded as structured fields, `exc_info` and `stack_info`
render like native structguru fields, and numeric levels are normalized into
structguru's canonical severity bands. Only one managed bridge may be installed
at a time: a second install raises `RuntimeError` unless it passes
`replace=True`.

When logging setup legitimately runs more than once per process (multiple
entrypoints, repeated setup in test suites), opt into last-call-wins semantics:

```python
bridge = install_stdlib_bridge(replace=True)
```

The previous bridge is released exactly as `uninstall_stdlib_bridge` would —
detached, closed, its existing-loggers snapshot restored — before the new
install applies its own policy. The swap runs in one critical section: a record
logged by another thread during it is delivered at most once (rendered by the
outgoing or incoming bridge, raw, or not at all — never twice) and never
raises. Suppression levels from the earlier install are not reverted, and
`uninstall_stdlib_bridge` on the replaced handler is a no-op.

Use `disable_existing_loggers` to control loggers registered before bridge
installation:

```python
bridge = install_stdlib_bridge(disable_existing_loggers=True)
```

- `None` (default) reads `STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS`, preserving
  current states when the variable is unset.
- `False` re-enables them.
- `True` disables them.

An explicit `True` or `False` always overrides the environment.

The root logger and loggers created later are unaffected. Uninstalling restores
states changed by the bridge unless application code changed the state again in
the meantime. This is intentionally time-of-configuration behavior, matching
the standard library's `dictConfig` model.

The regular installer reads only the existing-logger environment policy. To
configure every bridge option from the environment, call the explicit installer
after framework logging setup:

```python
from structguru.integrations.stdlib import install_stdlib_bridge_from_env

bridge = install_stdlib_bridge_from_env()
```

| Variable | Default |
|---|---|
| `STRUCTGURU_STDLIB_LEVEL` | `LOG_LEVEL`, then `INFO` |
| `STRUCTGURU_STDLIB_CLEAR_HANDLERS` | `true` |
| `STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS` | preserve existing states |
| `STRUCTGURU_STDLIB_SUPPRESS_LOGGERS` | empty comma-separated list |
| `STRUCTGURU_STDLIB_SUPPRESS_LEVEL` | `WARNING` |
| `STRUCTGURU_STDLIB_REPLACE` | `false` |

Boolean values accept `1/0`, `true/false`, `yes/no`, and `on/off`; empty values
are treated as unset. Other values raise `ValueError` before the bridge changes
logging state. Environment installation is never triggered merely by importing
`structguru`.

For a logger with `propagate=False` (its records never reach the root logger),
attach the handler directly:

```python
import logging
from structguru.integrations.stdlib import StructguruHandler

access = logging.getLogger("uvicorn.access")
access.handlers = [StructguruHandler()]
access.propagate = False
```

To only quiet noisy loggers without routing them, use `suppress_loggers` on its
own:

```python
from structguru.integrations.stdlib import suppress_loggers

suppress_loggers("urllib3", "botocore", level="WARNING")
```

## ASGI (FastAPI / Starlette)

The `StructguruMiddleware` provides automatic request ID generation, context binding, and request/response logging. It works with any ASGI framework (FastAPI, Starlette, Litestar, etc.).

### Basic Usage

```python
from fastapi import FastAPI
from structguru.integrations.asgi import StructguruMiddleware

app = FastAPI()
app.add_middleware(StructguruMiddleware)

@app.get("/")
async def root():
    return {"message": "Hello World"}
```

### Advanced Configuration

```python
app.add_middleware(
    StructguruMiddleware,
    request_id_header="X-Correlation-ID",  # Custom header to read (case-insensitive)
    logger_name="api.http",                # Custom logger name
    log_request=True,                      # Log a summary line on completion
    extract_headers=["x-tenant-id", "x-device-id"], # Additional headers to extract and bind
)
```

### Context Variables Bound
- `request_id`: Extracted from header or generated as a UUID4.
- `method`: HTTP method (e.g., `GET`, `POST`) or `WS` for WebSockets.
- `path`: The request path.
- `client_ip`: The client's IP address.
- Any headers specified in `extract_headers` will be bound as context variables (e.g. `x-tenant-id`).

## Celery

`setup_celery_logging` connects to Celery signals to ensure that task context (like `task_id` and `task_name`) is automatically bound to logs within Celery workers.

### Basic Usage

```python
from celery import Celery
from structguru.integrations.celery import setup_celery_logging

app = Celery("tasks")
setup_celery_logging()
```

### Context Propagation

When `propagate_context=True` (the default), selected keys from the producer's
structguru context are passed via message headers to the worker. You can control
which keys are propagated:

```python
setup_celery_logging(
    propagate_context=True,
    context_keys=["request_id", "user_id"],  # Only propagate these keys
)
```

## Flask

`setup_flask_logging` registers `before_request` and `after_request` hooks for structured request logging.

### Basic Usage

```python
from flask import Flask
from structguru.integrations.flask import setup_flask_logging

app = Flask(__name__)
setup_flask_logging(app)
```

### Advanced Configuration

```python
setup_flask_logging(
    app,
    request_id_header="X-Correlation-ID",
    logger_name="api.flask",
    log_request=True,
)
```

### Context Variables Bound
- `request_id`: Extracted from header or generated as a UUID4.
- `method`: HTTP method.
- `path`: The request path.
- `client_ip`: The client's IP address.

## Django

`structguru` provides a middleware and a logging configuration builder for Django.

### Configuration

In your `settings.py`:

```python
from structguru.integrations.django import build_logging_config

# 1. Build the LOGGING dict
LOGGING = build_logging_config(
    service="my-django-app",
    level="INFO",
    json_logs=True,
    disable_existing_loggers=False,
)

# 2. Add the middleware
MIDDLEWARE = [
    # ...
    "structguru.integrations.django.StructguruMiddleware",
    # ...
]
```

When `disable_existing_loggers` is omitted, the builder reads
`STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS` and otherwise defaults to `False`.
An explicit Python value always takes precedence.

The middleware automatically:
- Binds `request_id`, `method`, `path`, and `client_ip`.
- Binds `user_id` if `request.user.pk` is available.
- Adds `X-Request-ID` to the HTTP response.
- Logs a summary line with the status code and duration.

## SQLAlchemy

`setup_query_logging` attaches event listeners to a SQLAlchemy engine to track SQL execution time and log slow queries.

### Basic Usage

```python
from sqlalchemy import create_engine
from structguru.integrations.sqlalchemy import setup_query_logging

engine = create_engine("sqlite:///:memory:")
setup_query_logging(engine, slow_threshold_ms=100.0)
```

### Logging All Queries

For local development or debugging, you might want to see every query regardless of its duration:

```python
setup_query_logging(engine, slow_threshold_ms=0, log_all=True)
```

## gRPC

Add `StructguruInterceptor` to your gRPC server to bind request context automatically for both unary and streaming RPCs.

### Usage

```python
from concurrent import futures
import grpc
from structguru.integrations.grpc import StructguruInterceptor

server = grpc.server(
    futures.ThreadPoolExecutor(max_workers=10),
    interceptors=[StructguruInterceptor(request_id_key="x-request-id")],
)
```

### Context Variables Bound
- `grpc_method`: The gRPC method being called.
- `request_id`: Extracted from invocation metadata or generated as a UUID4.

## Sentry

`SentryProcessor` forwards log events to Sentry as breadcrumbs and, for the more severe ones, as captured events.

Two thresholds control this. Every event at or above `breadcrumb_level` is added as a breadcrumb. Events at or above `event_level` are additionally considered for capture — but by default only those carrying exception info (`logger.exception(...)`, or `exc_info=`) actually become Sentry *events*, via `capture_exception`. This matches `logging.LoggingIntegration` semantics: a plain `logger.error("...")` with no exception stays a breadcrumb. Set `capture_messages=True` to also send those through `capture_message`.

### Usage

```python
import logging
from structguru import configure
from structguru.integrations.sentry import SentryProcessor

sentry_processor = SentryProcessor(
    event_level=logging.ERROR,       # ERROR+ is eligible for capture as an event
    breadcrumb_level=logging.INFO,   # INFO+ is recorded as a breadcrumb
    tag_keys=frozenset({"service"}), # Keys to set as Sentry tags
    capture_messages=True,           # Also capture ERROR+ logs without an exception
)

configure(sentry_processor=sentry_processor)
```

The native hook redacts the message and structured fields before Sentry sees the
event. Raw `exc_info` is retained only for `capture_exception`. If `sentry-sdk`
is not installed, `SentryProcessor` acts as a no-op.
