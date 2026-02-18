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
app.add_middleware(StructguruMiddleware, request_id_header="X-Request-ID")

@app.get("/")
async def root():
    return {"message": "Hello World"}
```

### Advanced Configuration

```python
app.add_middleware(
    StructguruMiddleware,
    request_id_header="X-Correlation-ID",  # Custom header to read
    logger_name="api.http",                # Custom logger name
    log_request=True,                       # Log a summary line on completion
)
```

### Configuration Options

- `request_id_header`: (Default: `"x-request-id"`) Case-insensitive header name to read for existing request IDs. If missing, a new UUID is generated.
- `logger_name`: (Default: `"structguru.asgi"`) Name for the structlog logger used for the completion log.
- `log_request`: (Default: `True`) Whether to log a summary line (including status code and duration) when each request completes.

## Celery

`setup_celery_logging` ensures that task context (like `task_id` and `task_name`) is automatically bound to logs within Celery workers.

### Basic Usage

```python
from celery import Celery
from structguru.integrations.celery import setup_celery_logging

app = Celery("tasks")
setup_celery_logging(propagate_context=True)
```

### Context Propagation

When `propagate_context=True`, selected keys from the current `structlog` context are automatically passed via message headers to the worker. You can control which keys are propagated:

```python
setup_celery_logging(
    propagate_context=True,
    context_keys=["request_id", "user_id"],
)
```

## Flask

`setup_flask_logging` configures request ID tracking and logging for Flask applications.

### Basic Usage

```python
from flask import Flask
from structguru.integrations.flask import setup_flask_logging

app = Flask(__name__)
setup_flask_logging(app, request_id_header="X-Request-ID")
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
- Binds `user_id` if the user is authenticated.
- Adds `X-Request-ID` to the HTTP response.
- Logs a summary line with the status code and duration.

## SQLAlchemy

`setup_query_logging` tracks SQL execution time and logs slow queries.

### Basic Usage

```python
from sqlalchemy import create_engine
from structguru.integrations.sqlalchemy import setup_query_logging

engine = create_engine("sqlite:///:memory:")
setup_query_logging(engine, slow_threshold_ms=100)
```

### Logging All Queries

For local development, you might want to see every query regardless of its duration:

```python
setup_query_logging(engine, slow_threshold_ms=0, log_all=True)
```

## gRPC

Add `StructguruInterceptor` to your gRPC server to bind request context automatically.

### Usage

```python
from concurrent import futures
import grpc
from structguru.integrations.grpc import StructguruInterceptor

server = grpc.server(
    futures.ThreadPoolExecutor(max_workers=10),
    interceptors=[StructguruInterceptor()],
)
```

### Configuration

- `request_id_key`: (Default: `"x-request-id"`) The metadata key to extract for the request ID.
- `logger_name`: (Default: `"structguru.grpc"`) The logger name for gRPC-related logs.

## Sentry

`SentryProcessor` forwards log events as Sentry breadcrumbs or events.

### Usage

```python
import logging
from structguru.integrations.sentry import SentryProcessor

sentry_processor = SentryProcessor(
    event_level=logging.ERROR,       # Logs ERROR+ as Sentry events
    breadcrumb_level=logging.INFO,   # Logs INFO+ as Sentry breadcrumbs
)

# Add to your structlog processor chain during configuration
```

**Note:** If `sentry-sdk` is not installed, the `SentryProcessor` will gracefully act as a no-op.
