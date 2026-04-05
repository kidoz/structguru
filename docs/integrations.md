# structguru Integrations Guide

This guide provides in-depth information on how to use `structguru` with various frameworks and libraries.

## Shared Setup

Most integrations assume you have configured `structlog` first. It's recommended to do this once at your application's entry point:

```python
from structguru import configure_structlog

configure_structlog(service="myapp", level="INFO", json_logs=True)
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
    request_id_header="x-correlation-id",  # Custom header to read (must be lowercase)
    logger_name="api.http",                # Custom logger name
    log_request=True,                      # Log a summary line on completion
)
```

### Context Variables Bound
- `request_id`: Extracted from header or generated as a UUID4.
- `method`: HTTP method (e.g., `GET`, `POST`) or `WS` for WebSockets.
- `path`: The request path.
- `client_ip`: The client's IP address.

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

When `propagate_context=True` (the default), selected keys from the producer's `structlog` context are automatically passed via message headers to the worker. You can control which keys are propagated:

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
)

# 2. Add the middleware
MIDDLEWARE = [
    # ...
    "structguru.integrations.django.StructguruMiddleware",
    # ...
]
```

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

`SentryProcessor` forwards log events as Sentry breadcrumbs or captured events based on severity.

### Usage

```python
import logging
import structlog
from structguru.integrations.sentry import SentryProcessor

# Important: Place RedactingProcessor before SentryProcessor if used.
sentry_processor = SentryProcessor(
    event_level=logging.ERROR,       # Logs ERROR+ as Sentry events
    breadcrumb_level=logging.INFO,   # Logs INFO+ as Sentry breadcrumbs
    tag_keys=frozenset({"service"}), # Keys to set as Sentry tags
)

# Add to your structlog processor chain during configuration
structlog.configure(
    processors=[
        # ... other processors
        sentry_processor,
        # ...
    ]
)
```

**Note:** If `sentry-sdk` is not installed, the `SentryProcessor` will gracefully act as a no-op. Exceptions in logs (via `exc_info=True` or `logger.exception`) are automatically normalized and sent to Sentry via `capture_exception`.