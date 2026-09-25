# structguru

`structguru` is a structured logging library for Python 3.11+ with a
loguru-style API: brace formatting, `bind()`, `contextualize()`, `opt()`, and
sink management. Every record is rendered by a bundled native Rust extension,
as compact JSON for production or readable console lines for development. The
package has no required runtime dependencies.

## Install

```bash
pip install structguru
```

Framework and service integrations are optional extras: `otel`, `celery`,
`flask`, `django`, `sqlalchemy`, `grpc`, `sentry`, `httpx`, `requests`, and
`all`.

## Example

```python
from structguru import configure, logger

configure(service="checkout")

log = logger.bind(request_id="req-1")
log.info("Order {order_id} accepted", order_id=42, total=19.99)
```

```json
{"request_id":"req-1","total":19.99,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:23:16.414683Z","service":"checkout","message":"Order 42 accepted"}
```

`order_id` fills the `{order_id}` placeholder in the message, `total` becomes a
field of its own, and `request_id` comes from `bind()`.

## How these docs are organized

The documentation follows [Diátaxis](https://diataxis.fr/) and has four parts:

- [Tutorials](tutorials/index.md) are lessons that take you from an empty
  directory to working, structured logs, one step at a time.
- [How-to guides](how-to/index.md) are recipes for a specific task, such as
  writing to a file, redacting secrets, or logging a Flask or Celery app.
- [Reference](reference/index.md) describes every public function, setting,
  environment variable, and output field.
- [Explanation](explanation/index.md) discusses how structguru works and why
  it is designed that way.

## Start here

- New to structguru: follow [Get started with structguru](tutorials/getting-started.md).
- Adding logging to a FastAPI or Starlette service: follow
  [Log a web service](tutorials/web-service.md).
- Setting up logging for production: see
  [Configure logging](how-to/configure-logging.md) and
  [Settings and environment variables](reference/settings.md).
- Looking up a method or argument: see the [Logger reference](reference/logger.md).
- Moving from loguru or structlog: read
  [Compatibility with loguru and structlog](explanation/compatibility.md).
