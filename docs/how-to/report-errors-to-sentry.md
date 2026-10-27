# Report errors to Sentry

Forward records to Sentry as breadcrumbs, and exceptions as Sentry events, with
redaction applied before anything leaves the process.

Install the `sentry` extra and initialize the Sentry SDK as usual:

```bash
pip install "structguru[sentry]"
```

## Connect structguru to Sentry

```python
import logging

import sentry_sdk

from structguru import configure
from structguru.integrations.sentry import SentryProcessor

sentry_sdk.init(dsn="https://examplePublicKey@o0.ingest.sentry.io/0")

configure(
    service="checkout",
    sentry_processor=SentryProcessor(
        event_level=logging.ERROR,  # ERROR and above can become events
        breadcrumb_level=logging.INFO,  # INFO and above become breadcrumbs
        tag_keys=frozenset({"service"}),  # fields to set as Sentry tags
    ),
)
```

With these settings:

- every record at `INFO` or above is added as a breadcrumb;
- a record at `ERROR` or above that carries an exception
  (`logger.exception(...)`, `exc_info=`, `logger.catch()`) is sent as a Sentry
  event through `capture_event`, with the record's fields under the
  `structlog_event` extra and the `tag_keys` fields as tags;
- a plain `logger.error("...")` without an exception stays a breadcrumb, as with
  Sentry's own `LoggingIntegration`.

```python
from structguru import logger

logger.info("Cart loaded", items=3)  # breadcrumb
logger.error("Payment declined")  # breadcrumb only
try:
    charge(order)
except PaymentError:
    logger.exception("Charge failed", order_id=42)  # event with both breadcrumbs
```

## Send errors without an exception too

```python
SentryProcessor(capture_messages=True)
```

Records at or above `event_level` without an exception are then sent through
`capture_message`.

## Keep secrets out of Sentry

By default, exception events are constructed from the rendered, sanitized
`exception` field using `capture_event`. The SDK receives no original exception
or traceback to inspect, so it cannot extract raw chained messages, source
context or frame locals from that exception. `exception_include_locals=False`
remains the default. If you explicitly include locals in structured exceptions,
the sanitized structured data is included in the event's exception text and extras.

To let the SDK inspect the original exception, opt in explicitly:

```python
SentryProcessor(capture_raw_exceptions=True)
```

This bypasses structguru redaction for exception messages, chains, source context
and traceback locals. Configure the SDK's `include_local_variables=False` and
`before_send` scrubbing as needed; turning off locals alone does not redact
messages or source context. See [Redact sensitive data](redact-sensitive-data.md).

## Avoid duplicate events with the stdlib bridge

Sentry's default `LoggingIntegration` also reports standard-library `logging`
records. If you route those through structguru with the
[stdlib bridge](capture-stdlib-logging.md), a bridged exception is reported
twice, once by Sentry's integration and once by `SentryProcessor` (with
`capture_messages=True`, so is a bridged error without an exception). Turn off
Sentry's logging capture and let structguru report them:

```python
import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

sentry_sdk.init(
    dsn="https://examplePublicKey@o0.ingest.sentry.io/0",
    integrations=[LoggingIntegration(level=None, event_level=None)],
)
```

## Run without the Sentry SDK

If `sentry-sdk` is not installed, `SentryProcessor` does nothing, so the same
configuration works in environments without it. Both sentry-sdk 2.x and 1.x are
supported.

## See also

- [Sentry integration](../reference/integrations/sentry.md)
- [Log exceptions](log-exceptions.md)
- [Redaction design](../explanation/redaction.md)
