# Requests

`structguru.integrations.requests.StructguruRequestsSession` is a
`requests.Session` subclass that logs each request. Install the `requests`
extra (`requests>=2.0`). This module imports `requests` when it is imported.

```python
from structguru.integrations.requests import get_logging_session

session = get_logging_session()
session.get("https://example.com")
```

`get_logging_session()` returns a new `StructguruRequestsSession`.

## Logged record

Every call of `request()` (and therefore `get()`, `post()`, and the other verb
methods) logs one record when it returns or raises. Logger name:
`structguru.requests`.

| Condition | Message | Level |
|---|---|---|
| The call raised, including `KeyboardInterrupt` and `SystemExit` | `Outbound HTTP Request Failed` | `ERROR` |
| Status 400 or higher | `Outbound HTTP Request Failed` | `ERROR` |
| any other response | `Outbound HTTP Request Completed` | `INFO` |

The record is logged before the exception propagates; the exception is not
attached to it.

| Field | Value |
|---|---|
| `http_method` | The method, upper-cased. |
| `http_url` | The URL requests sends, including `params=` and session `params`, without user information, query values, or fragment; a trailing `?` marks that a query string was present. A URL that cannot be parsed safely is logged as `<unparsable-url>`. |
| `duration_ms` | Milliseconds spent in the call, rounded to two decimals. |
| `status_code` | The response status. Absent when the call raised. |
| `request_id` | The `X-Request-ID` header (matched case-insensitively) from a `headers=` dictionary passed to the call, otherwise from the session headers. Absent when neither has one. |

## API

::: structguru.integrations.requests
    options:
      heading_level: 3

## See also

- [Requests](../../how-to/integrations/requests.md)
- [HTTPX](httpx.md)
