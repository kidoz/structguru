# structguru

A native structured logging library with a [loguru](https://github.com/Delgan/loguru)-style API.

Combines a loguru-style API — brace formatting, `bind`, `contextualize`, `opt`, sink management — with a native Rust renderer for maximum performance. Since v1.0, the Rust extension is the default (and only) rendering path; structlog and orjson are no longer dependencies.

## Features

- **Loguru-style API** — `logger.info("User {id} logged in", id=123)`
- **Structured JSON output** in production (rendered natively in Rust for speed)
- **Pretty colored console** output in development
- **Context management** — `bind()` for persistent context, `contextualize()` for request-scoped context
- **Sentry integration** — redacted breadcrumbs/events with raw exceptions preserved for capture
- **stdlib interop** — `logger.add()` sinks can also receive third-party `logging` records
- **RFC 5424 severity codes** included in every log record
- **Native Rust runtime** — rendering and output run through the bundled abi3 extension
- **Fully typed** — PEP 561 compliant with strict mypy

**Native processing:**

- **Redaction** — mask sensitive fields (passwords, tokens) by key name or regex
- **Sampling** — probabilistic and rate-limited log suppression
- **Metrics** — extract counters/histograms from log events via callbacks
- **Exception formatting** — render `exc_info` as text or a structured frame dictionary
- **Off-thread logging** — native Rust writer with a bounded queue and backpressure
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

Available extras: `otel`, `celery`, `flask`, `django`, `sqlalchemy`, `grpc`,
`sentry`, `httpx`, `requests`, `all`.

## Quick start

```python
from structguru import configure, logger

# Configure once at startup
configure(service="myapp", level="DEBUG", format="json")

# Use anywhere
logger.info("Hello {name}", name="world")
# → {"logger":"...","level":"INFO","severity":6,"timestamp":"...","service":"myapp","message":"Hello world"}
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

On Unix, new files created by either `logger.add(path)` or the native rotating
file sink are owner-only (`0600`). Existing files retain their permissions.

All sink forms receive structguru records. They are also registered with the
stdlib root logger for third-party records, which arrive raw (unrendered) on
that path. Install the [stdlib bridge](#stdlib-bridge) to receive them rendered
and redacted instead — while it is installed the raw delivery is suspended, so a
sink never sees the same record twice.

Native delivery uses the bounded callable queue and is drained on
reconfiguration, `shutdown()`, fork, and interpreter exit. Call
`structguru.flush()` when you need to block until buffered records have actually
been written:

```python
import structguru

logger.info("checkpoint")
structguru.flush()   # returns once the line has reached its sink
```

### Console vs JSON output

`format=` selects the renderer: `"json"` (default, production) or `"console"`
(colored, human-readable development output).

```python
# JSON (production)
configure(service="myapp", format="json")
# → {"logger":"...","level":"INFO","severity":6,"timestamp":"...","service":"myapp","message":"..."}

# Console (development) — colored, human-readable
configure(service="myapp", format="console")
# → 2026-01-15T12:00:00.123456Z [INFO    ] Hello world
```

## Native processing

### Redaction

Mask sensitive fields automatically:

```python
from structguru import configure

configure(
    sensitive_keys=["password", "token", "ssn"],
    sensitive_patterns=[r"\b\d{3}-\d{2}-\d{4}\b"],
    pattern_replacement="***",
)
```

Patterns run on Rust's linear-time regex engine (no ReDoS), which rejects
look-around and backreferences at `configure()` time. Most look-behinds rewrite
as capture groups — `(?<=password=)\S+` becomes `(password=)\S+` with
`pattern_replacement="$1[REDACTED]"`, so the prefix is re-emitted and the
secret is replaced (`password=hunter2` → `password=[REDACTED]`). Put the
capture group around the part you want to *keep*, never around the secret.
For patterns that can't be rewritten,
`allow_backtracking_patterns=True` opts them into a bounded backtracking
engine: look-around and backreferences then work as written, at the cost of
the linear-time guarantee for those patterns. If a value ever exceeds the
backtrack limit, it is redacted entirely (fail-closed) rather than emitted
unchecked.

```python
configure(
    sensitive_patterns=[r"(?<=password=)\S+"],
    allow_backtracking_patterns=True,
)
```

### Sampling & rate limiting

Suppress noisy logs:

```python
from structguru import configure

configure(sample_rate=0.1, rate_limit_max=5, rate_limit_period=60)
```

### Metric extraction

Derive metrics from log events:

```python
from structguru import MetricProcessor, configure

metrics = MetricProcessor()
metrics.counter("user.login", lambda ed: login_counter.inc())
metrics.histogram("db.query", "duration_ms", lambda v, ed: query_hist.observe(v))

configure(metric_processor=metrics)
```

### Exception formatting

Render exceptions as JSON-serializable dictionaries:

```python
from structguru import configure

configure(structured_exceptions=True, exception_max_frames=20)
```

`exception_max_frames=0` omits traceback frames entirely. Negative frame and
local-representation limits are rejected during configuration.

### OpenTelemetry correlation

Inject trace context into every log event:

```python
from structguru import configure

configure(otel=True)  # no-op injection when opentelemetry-api is absent
```

### Non-blocking logging

Since v1.0, log I/O is offloaded to a background thread by default. The native
Rust writer uses a bounded 8192-record queue with lossless backpressure. Set
`overflow="drop"` to favor caller latency, or explicitly pass `maxsize=0` only
when an unbounded queue is acceptable.

## Native runtime

structguru ships a required Rust extension that renders and enqueues logging
natively, off-thread. It is auto-enabled at import time. The runtime does not depend on `orjson`;
exotic values (`datetime`, `UUID`, `Enum`, dataclasses) are converted natively
in Rust.

```python
import structguru

# Native mode is already on. Logger calls route through the Rust renderer.
structguru.logger.info("order {id} accepted", id=987)
# → JSON line written to stdout by a background writer thread
```

No configuration is required for the default JSON-to-stdout behavior. Call
`configure(...)` to customize the renderer, filtering, or sinks.

```python
import structguru

structguru.configure(service="myapp", level="INFO", file_path="/var/log/app.log")
structguru.logger.bind(request_id="abc").info("order {id} accepted", id=987)
# → JSON line written to /var/log/app.log by a background writer thread
```

The default import-time configuration also honors environment variables:

```bash
LOG_LEVEL=INFO STRUCTGURU_SERVICE=myapp python -m myapp
```

Invalid native environment values fail import with an actionable exception. This
prevents a deployment from starting while the native-only logging path is disabled.

Public API:

| Symbol | Purpose |
|--------|---------|
| `configure(...)` | Configure rendering, filtering, redaction, and output sinks. See the API reference for the complete signature. |
| `shutdown()` | Stop the writer; logging is disabled until `configure()` is called. |
| `set_level(level)` | Adjust the level threshold at runtime. |
| `writer_metrics()` | Writer counters (enqueued/written/dropped/depth/...) plus filter counters (`sampled`/`rate_limited`) when active. |
| `is_available()` | Whether the compiled extension is importable. |

Behavior notes:

- **Overflow**: the default `maxsize=8192` uses `overflow="block"` for bounded, lossless backpressure. Use `overflow="drop"` for drop-newest behavior with metrics and rate-limited warnings. `maxsize=0` explicitly opts into an unbounded queue.
- **Redaction, level filtering, exceptions, and OpenTelemetry** injection are supported natively; redaction covers the message and all structured string values before rendering or Sentry export. `sensitive_keys` overrides the default redaction keys. Rust's linear-time regex engine rejects backreferences and look-around with `ValueError` at configuration time.
- **Sampling & rate limiting** (`sample_rate`, `rate_limit_max`, `rate_limit_period`) are applied as native pre-render filters — dropped records cost zero rendering. `sampled` and `rate_limited` counters are distinct from the transport `dropped` counter. `sample_max_level` restricts sampling to records at or below that level; more severe records always pass.
- **Metric hooks** (`metric_processor=...`) invoke a structlog-style processor (e.g. `MetricProcessor`) for every *kept* record on the caller's thread, with `(None, method, {"event": message, **fields})`. Dropped records (level/sampling/rate-limit) never reach it; hook errors are swallowed.
- **Fork/shutdown safe** — the writer is flushed on exit and respawned in forked children (gunicorn/celery prefork). Rotating-file writers sharing a path coordinate through an owner-only `.lock` sidecar; distributed hosts should still prefer stdout and an external collector.
- **Structured exceptions** (`structured_exceptions=True`) render `type`, `message`, `module`, and frames as a dictionary, with optional redacted/truncated locals controlled by the `exception_*` options.
- **`stack_info` is supported natively**: the stack is captured in Python and rendered in the same position as `StackInfoRenderer` (`stack` between `service` and `message`). Unlike the standard path, the stack ends at the *user's* calling frame (structguru-internal frames are skipped, the way structlog skips its own).
- **Console mode** (`format="console"`): renders colored, human-readable lines instead of JSON — structguru's own stable dev format (`<timestamp> [<LEVEL>] <message>  k=v`), with ANSI colors by default on a TTY. Override with `colors=True/False`.
- **File sinks** (`file_path=...`): write to a rotating file natively. Defaults mirror `RotatingFileHandler` (50 MB, 5 backups); configure via `file_max_bytes`/`file_backup_count`. Set `also_stdout=True` to mirror output to both file and stdout (e.g. container + persistent log).
- **Callable sinks** (`callable_sinks=[fn, ...]`): use a bounded queue (`callable_queue_maxsize=1024`). `overflow="block"` provides lossless backpressure; `overflow="drop"` reports `callable_dropped` metrics. Flush and lifecycle operations drain queued calls.
- **Sentry integration** (`sentry_processor=SentryProcessor(...)`): receives the already-redacted event and raw `exc_info` only for exception capture.
- **Scope**: the native renderer covers JSON and console rendering, file/stdout/callable sinks, redaction, sampling/rate limiting, metrics, exceptions, and stack information. `logger.add()` sinks receive native and stdlib records.

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
import logging

from structguru import configure
from structguru.integrations.sentry import SentryProcessor

sentry = SentryProcessor(event_level=logging.ERROR, tag_keys=frozenset({"service"}))
configure(sentry_processor=sentry)
```

### Stdlib bridge

Third-party libraries log through the standard `logging` module. Installing the
bridge re-emits those records through structguru, so they share the same JSON /
console formatting, redaction, and output stream as your own logs:

```python
from structguru.integrations.stdlib import install_stdlib_bridge

bridge = install_stdlib_bridge(
    level="INFO",
    suppress_loggers=("urllib3", "botocore"),
    disable_existing_loggers=False,
)

import logging
logging.getLogger("sqlalchemy.engine").info("SELECT 1")
# → {"logger":"sqlalchemy.engine","level":"INFO",...,"message":"SELECT 1"}
```

While the bridge is installed, `logger.add()` sinks receive third-party records
only through it — rendered once, never also raw. Pass the returned handler to
`uninstall_stdlib_bridge()` to restore the previous behavior.

Installing a second bridge while one is active raises `RuntimeError`. When
logging setup legitimately runs more than once per process (a Django
`manage.py` that imports a Celery app module, repeated setup in test suites),
pass `replace=True` to release the previous bridge first — last call wins:

```python
bridge = install_stdlib_bridge(level="INFO", replace=True)
```

The swap is atomic for callers: a record logged by another thread during it is
delivered at most once (rendered, raw, or dropped — never twice). Suppression
levels applied by the earlier install are not reverted, and calling
`uninstall_stdlib_bridge()` on the replaced handler is a no-op.

`disable_existing_loggers=True` disables named stdlib loggers that already
exist at installation time; `False` re-enables them, following `dictConfig`
semantics. When the option is omitted, `install_stdlib_bridge()` reads
`STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS`; if the variable is also unset,
existing states are preserved. Explicit Python values override the environment.
An empty environment value is treated as unset.

```bash
STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS=false python -m myapp
```

To configure all bridge options from environment variables at a controlled
point in application startup:

```bash
STRUCTGURU_STDLIB_LEVEL=INFO \
STRUCTGURU_STDLIB_DISABLE_EXISTING_LOGGERS=false \
python -m myapp
```

```python
from structguru.integrations.stdlib import install_stdlib_bridge_from_env

bridge = install_stdlib_bridge_from_env()
```

## Requirements

- Python 3.11+
- The compiled Rust extension (shipped as abi3 wheels for Linux/macOS/Windows)

## Documentation & Examples

- **[Integrations Guide](docs/integrations.md)** — Detailed instructions for setting up frameworks.
- **[Full-stack Example](examples/full_stack_fastapi/main.py)** — FastAPI + Celery + SQLAlchemy in action.
- **[Existing-loggers Example](examples/stdlib_existing_loggers/main.py)** — Configure the stdlib policy and bridge replacement from code or environment.

## Development

```bash
uv sync --all-extras
uv run pytest
make bench
uv run ruff check .
uv run mypy src/
```

## License

[MIT](LICENSE) — Copyright (c) 2025 Aleksandr Pavlov
