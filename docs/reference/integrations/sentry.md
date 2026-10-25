# Sentry

`structguru.integrations.sentry.SentryProcessor` forwards records to Sentry
as breadcrumbs and captured events. Install the `sentry` extra
(`sentry-sdk>=1.0`). Without `sentry-sdk`, the processor does nothing.

```python
import logging

from structguru import update
from structguru.integrations.sentry import SentryProcessor

update(
    sentry_processor=SentryProcessor(event_level=logging.ERROR, tag_keys=frozenset({"service"}))
)
```

## Parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `event_level` | `int` | `logging.ERROR` (40) | Minimum level considered for capture as a Sentry event. |
| `tag_keys` | `frozenset[str] \| None` | `None` | Event keys set as Sentry tags. |
| `breadcrumb_level` | `int` | `logging.INFO` (20) | Minimum level recorded as a breadcrumb. |
| `capture_messages` | `bool` | `False` | Capture records at or above `event_level` that carry no exception with `capture_message()`. |
| `capture_raw_exceptions` | `bool` | `False` | Pass the original exception to `capture_exception()`, bypassing structguru redaction for SDK exception inspection. |
| `require_redaction` | `bool` | `True` | Attach the event as Sentry extras only when it carries the redaction marker. |

## Invocation

When `sentry_processor` is configured, the runtime calls it on the calling
thread after the record has been handed to the native writer, as
`sentry_processor(None, method, event_dict)`. It is not called for records
dropped by the level threshold, sampling, or rate limiting, for records
rejected by a closed writer, or after `shutdown()`. It is called for a record
that the full writer queue dropped (`overflow="drop"`). Exceptions it raises
are suppressed.

`event_dict` is built from the rendered, redacted JSON line:

| Key | Value |
|---|---|
| `event` | The redacted message. |
| each field of the record | The redacted value, including `exception` text or object and OpenTelemetry fields. A field named `event` does not replace the message. |
| `service`, `logger`, `level`, `severity`, `timestamp` | As rendered. |
| `exc_info` | A boolean exception-presence marker; original exception information only when the hook explicitly sets `capture_raw_exceptions=True`. |
| `_structguru_redacted` | `True` (`REDACTED_MARKER_KEY`). |

## Levels

`method` determines the level:

| `method` | Numeric level | Sentry level |
|---|---|---|
| `trace` | 5 | `debug` |
| `debug` | 10 | `debug` |
| `info`, `success` | 20 | `info` |
| `warning`, `warn` | 30 | `warning` |
| `error`, `exception` | 40 | `error` |
| `critical`, `fatal` | 50 | `critical` |
| any other name | 20 | the name itself |

Level methods pass the names described in
[Metrics](../metrics.md#metric-hook) (`"debug"`, `"info"`, `"warning"`,
`"error"`, `"critical"`); bridged stdlib records pass the name of their level
band.

## Behavior

For each call, `payload` is `event_dict` without `exc_info` and the redaction
marker.

1. When the level is at least `breadcrumb_level`:
   `sentry_sdk.add_breadcrumb(message=event, category="structguru", level=<Sentry level>, data=<payload without "event">)`.
2. When the level is below `event_level`, processing ends.
3. Otherwise, inside a new scope (`sentry_sdk.new_scope()` on sentry-sdk 2.x,
   `sentry_sdk.push_scope()` on 1.x):
    - each key of `tag_keys` present in `payload` is set as a tag with the
      value `str(payload[key])`;
    - the extra `structlog_event` is set to `payload` when the redaction
      marker is present or `require_redaction=False`;
    - with `exc_info`, `sentry_sdk.capture_event()` receives a `LoggedException`
      whose value is the sanitized exception text (or JSON for structured
      exceptions); no SDK traceback frames are supplied. The redaction marker
      is required unless `require_redaction=False`;
    - with `capture_raw_exceptions=True`, the original exception is passed to
      `sentry_sdk.capture_exception()` instead;
    - without `exc_info` and with `capture_messages=True`, the message is
      passed to `sentry_sdk.capture_message(event, level=<Sentry level>)`.

With the defaults, `logger.error("...")` without an exception produces only a
breadcrumb, and `logger.exception("...")` produces a captured event. `exc_info`
on log calls may be `True`, an exception instance, or a
`(type, value, traceback)` tuple. By default the hook receives only its presence
and the sanitized `exception` field. Raw capture explicitly opts into SDK
inspection outside structguru's redaction, including exception chains, source
context and locals (see
[Keep secrets out of Sentry](../../how-to/report-errors-to-sentry.md#keep-secrets-out-of-sentry)).
The processor returns `event_dict` unchanged.

## API

::: structguru.integrations.sentry
    options:
      heading_level: 3

## See also

- [Report errors to Sentry](../../how-to/report-errors-to-sentry.md)
- [Redaction](../redaction.md)
- [Redaction design](../../explanation/redaction.md)
