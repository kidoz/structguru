# structguru

`structguru` is a native structured logging library with a loguru-style Python
API. Rust is the only rendering path in v1; the package has no required Python
runtime dependencies.

## Install

```bash
pip install structguru
```

Optional integrations are available through the `otel`, `celery`, `flask`,
`django`, `sqlalchemy`, `grpc`, `sentry`, `httpx`, `requests`, and `all` extras.

## Quick start

```python
from structguru import Logger, configure, logger, update

configure(service="checkout", level="INFO")
logger.bind(request_id="req-1").info("order {order_id} accepted", order_id=42)
```

Logging is configured automatically for JSON output to stdout at import time.
`configure()` replaces settings: explicit keywords override environment values, which
override built-in defaults. Use `update()` to retain existing settings while changing
selected options, as in the examples below. Use `Logger(name=__name__)` for an explicit
module name. See [configuration](api/config.md) for reusable Settings and environment options.

## Context

```python
log = logger.bind(component="billing")

with log.contextualize(request_id="req-1"):
    log.info("payment accepted", amount=42.5)
```

Bound values persist on the child logger. Contextual values use Python
`contextvars` and are restored when the block exits.

## Redaction

```python
from structguru import update

update(
    sensitive_keys=["password", "token"],
    sensitive_patterns=[r"secret=\w+"],
)
```

Key and pattern redaction covers the message and structured fields before data
is rendered, queued, or forwarded to Sentry.

Patterns run on Rust's linear-time regex engine, which rejects look-around and
backreferences at `configure()` time; rewrite them as capture groups with
`pattern_replacement="$1[REDACTED]"`, or pass `allow_backtracking_patterns=True`
to opt those patterns into a bounded backtracking engine (fail-closed on
backtrack-limit overruns).

## Sinks

```python
from structguru import logger

file_id = logger.add("application.log", level="ERROR")
callback_id = logger.add(lambda line: send_to_monitoring(line), level="CRITICAL")

logger.remove(callback_id)
logger.remove(file_id)
```

`logger.add()` supports file paths, streams, stdlib handlers, and callables. Each
sink receives structguru records and is attached to the stdlib root logger for
third-party records. Native delivery uses a bounded queue and is drained on
flush, reconfiguration, `shutdown()`, fork handling, and interpreter exit.

For a primary rotating file sink, configure it directly:

```python
update(file_path="application.log", file_max_bytes=50 * 1024 * 1024)
```

The rotating sink coordinates workers that share a path through an owner-only
`application.log.lock` sidecar. Keep that lock file on the same local filesystem
as the log; for containers or distributed hosts, stdout plus an external log
collector remains the recommended deployment model.

## Exceptions and tracing

```python
try:
    risky_operation()
except Exception:
    logger.exception("operation failed")

update(otel=True, structured_exceptions=True)
```

OpenTelemetry support is optional and becomes a no-op when its extra is not
installed. Structured exceptions can include bounded, redacted local values.

## Next steps

- See the [API reference](api/index.md) for exact signatures.
- See the [integrations guide](integrations.md) for framework setup.
- See the project README for development and release commands.
