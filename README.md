# structguru

A [loguru](https://github.com/Delgan/loguru)-style ergonomic API for [structlog](https://www.structlog.org/).

Combines structlog's powerful structured logging, performance, and processor chain with loguru's ease of use — brace formatting, `bind`, `contextualize`, `opt`, and sink management.

## Features

- **Loguru-style API** — `logger.info("User {id} logged in", id=123)`
- **Structured JSON output** in production (via `orjson` for speed)
- **Pretty colored console** output in development
- **Context management** — `bind()` for persistent context, `contextualize()` for request-scoped context
- **Sentry-compatible** — preserves `exc_info` on `LogRecord` for Sentry's logging integration
- **stdlib interop** — intercepts standard `logging` so third-party libraries use the same formatting
- **RFC 5424 severity codes** included in every log record
- **Fully typed** — PEP 561 compliant with strict mypy

**Processors & utilities:**

- **Redaction** — mask sensitive fields (passwords, tokens) by key name or regex
- **Sampling** — probabilistic and rate-limited log suppression
- **Metrics** — extract counters/histograms from log events via callbacks
- **Routing** — apply processors conditionally by log level range
- **Exception formatting** — convert `exc_info` to JSON-serializable dicts with full frame chains
- **Non-blocking logging** — offload I/O to a background thread with `configure_queued_logging()`
- **OpenTelemetry** — automatic `trace_id`/`span_id` injection from current span

**Framework integrations** (optional dependencies):

- **ASGI** (FastAPI, Starlette) — request ID, timing, context binding middleware
- **Celery** — task context binding and cross-worker context propagation via headers
- **Flask** — before/after request hooks with request ID tracking
- **Django** — logging dict config builder and request middleware
- **SQLAlchemy** — slow query detection and logging
- **gRPC** — server interceptor with per-RPC context binding
- **Sentry** — forward log events as breadcrumbs/events with configurable severity

## Installation

```bash
pip install structguru
```

With optional integrations:

```bash
pip install structguru[celery,flask,sentry]  # pick what you need
pip install structguru[all]                   # everything
```

Available extras: `otel`, `celery`, `flask`, `django`, `sqlalchemy`, `grpc`, `sentry`, `all`.

## Quick start

```python
from structguru import logger, configure_structlog

# Configure once at startup
configure_structlog(service="myapp", level="DEBUG", json_logs=True)

# Use anywhere
logger.info("Hello {name}", name="world")
# → {"timestamp": "2025-01-15T12:00:00Z", "service": "myapp", "level": "INFO", "severity": 6, "message": "Hello world", "name": "world"}
```

## Usage

### Log levels

```python
logger.debug("Debug message")
logger.info("Info message")
logger.warning("Warning message")
logger.error("Error message")
logger.critical("Critical message")

# Aliases
logger.trace("Maps to DEBUG")
logger.success("Maps to INFO")
logger.warn("Alias for warning")
logger.fatal("Alias for critical")
```

### Brace formatting

Arguments used in `str.format` placeholders are consumed by formatting (matching loguru behaviour). Extra kwargs that are **not** in any placeholder are forwarded as structured fields:

```python
logger.info("User {user_id} logged in", user_id=42, ip="10.0.0.1")
# message: "User 42 logged in"
# ip: "10.0.0.1"  (extra kwarg kept as structured field)
# user_id is consumed by formatting and not duplicated
```

### Bound context

```python
log = logger.bind(request_id="abc-123", user="alice")
log.info("Processing request")   # includes request_id and user
log.info("Request complete")     # same context carried through
```

### Request-scoped context

```python
with logger.contextualize(request_id="abc-123"):
    logger.info("Handling request")   # includes request_id
    do_work()                         # any logging inside also gets request_id
# request_id removed automatically
```

### Exception logging

```python
try:
    risky_operation()
except Exception:
    logger.exception("Operation failed")  # logs with exc_info at ERROR level

# Or with opt():
logger.opt(exception=True).error("Something went wrong")
```

### Sink management

```python
# Add a file sink
handler_id = logger.add("/var/log/app.log", level="ERROR")

# Add a callable sink
logger.add(lambda msg: send_to_monitoring(msg), level="CRITICAL")

# Remove a specific sink
logger.remove(handler_id)

# Remove all added sinks
logger.remove()
```

### Environment-based setup

`setup_structlog()` reads from environment variables for easy container deployment:

```python
from structguru import setup_structlog

setup_structlog(
    service="myapp",
    suppress_loggers=("elasticsearch", "urllib3"),
)
```

| Variable | Default | Description |
|----------|---------|-------------|
| `LOG_LEVEL` | `INFO` | Minimum log level |
| `JSON_LOGS` | `1` | `0` for console, `1` for JSON |
| `LOG_PATH` | *(none)* | Optional file sink with 50 MB rotation |

### Console vs JSON output

```python
# JSON (production)
configure_structlog(service="myapp", json_logs=True)
# → {"timestamp": "...", "service": "myapp", "level": "INFO", "message": "..."}

# Console (development) — colored, human-readable
configure_structlog(service="myapp", json_logs=False)
# → 2025-01-15 12:00:00 [info     ] Hello world
```

## Processors

### Redaction

Mask sensitive fields automatically:

```python
import re
from structguru import RedactingProcessor

redactor = RedactingProcessor(
    sensitive_keys=frozenset({"password", "token", "ssn"}),
    patterns=[re.compile(r"\b\d{3}-\d{2}-\d{4}\b")],  # SSN pattern
    replacement="***",
)
# Add to your structlog processor chain
```

### Sampling & rate limiting

Suppress noisy logs:

```python
from structguru import SamplingProcessor, RateLimitingProcessor

# Keep 10% of events
sampler = SamplingProcessor(rate=0.1)

# Max 5 messages per event name per 60 seconds
limiter = RateLimitingProcessor(max_count=5, period_seconds=60)
```

### Metric extraction

Derive metrics from log events:

```python
from structguru import MetricProcessor

metrics = MetricProcessor()
metrics.counter("user.login", lambda ed: login_counter.inc())
metrics.histogram("db.query", "duration_ms", lambda v, ed: query_hist.observe(v))
```

### Conditional routing

Apply processors only for certain log levels:

```python
from structguru import ConditionalProcessor

# Only redact ERROR+ logs (skip overhead for DEBUG/INFO)
routed = ConditionalProcessor(redactor, min_level="ERROR")
```

### Exception formatting

Convert exceptions to JSON-serializable dicts:

```python
from structguru import ExceptionDictProcessor

exc_processor = ExceptionDictProcessor(max_frames=20, include_locals=False)
# Produces: {"exception": {"type": "ValueError", "message": "...", "frames": [...]}}
```

### OpenTelemetry correlation

Inject trace context into every log event:

```python
from structguru import add_otel_context

# Add to processor chain — automatically picks up trace_id, span_id, trace_flags
# No-op when opentelemetry-api is not installed
```

### Non-blocking logging

Offload log I/O to a background thread:

```python
from structguru import configure_structlog, configure_queued_logging

configure_structlog(service="myapp", json_logs=True)
listener = configure_queued_logging()  # replaces handler with queue pair
```

## Framework integrations

### ASGI (FastAPI / Starlette)

```python
from structguru.integrations.asgi import StructguruMiddleware

app = FastAPI()
app.add_middleware(StructguruMiddleware, request_id_header="X-Request-ID")
```

### Celery

```python
from structguru.integrations.celery import setup_celery_logging

setup_celery_logging(propagate_context=True, context_keys=["request_id"])
# Binds task_id/task_name to context, propagates selected keys via headers
```

### Flask

```python
from structguru.integrations.flask import setup_flask_logging

app = Flask(__name__)
setup_flask_logging(app, request_id_header="X-Request-ID")
```

### Django

```python
# settings.py
from structguru.integrations.django import build_logging_config, StructguruMiddleware

LOGGING = build_logging_config(service="myapp", level="INFO", json_logs=True)
MIDDLEWARE = ["structguru.integrations.django.StructguruMiddleware", ...]
```

### SQLAlchemy

```python
from structguru.integrations.sqlalchemy import setup_query_logging

setup_query_logging(engine, slow_threshold_ms=100, log_all=False)
```

### gRPC

```python
from structguru.integrations.grpc import StructguruInterceptor

server = grpc.server(
    futures.ThreadPoolExecutor(),
    interceptors=[StructguruInterceptor()],
)
```

### Sentry

```python
from structguru.integrations.sentry import SentryProcessor

# Add to processor chain — sends ERROR+ as Sentry events, INFO+ as breadcrumbs
sentry = SentryProcessor(event_level=logging.ERROR, tag_keys=frozenset({"service"}))
```

## Requirements

- Python 3.10+
- structlog >= 24.1.0
- orjson >= 3.9.0

## Documentation & Examples

- **[Integrations Guide](docs/integrations.md)** — Detailed instructions for setting up frameworks.
- **[Full-stack Example](examples/full_stack_fastapi/main.py)** — FastAPI + Celery + SQLAlchemy in action.

## Development

```bash
uv sync
uv run pytest
make bench
uv run ruff check .
uv run mypy src/
```

## License

[MIT](LICENSE) — Copyright (c) 2025 Aleksandr Pavlov
