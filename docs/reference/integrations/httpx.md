# HTTPX

`structguru.integrations.httpx` provides event hooks that log outbound
requests made with `httpx.Client` and `httpx.AsyncClient`. Install the
`httpx` extra (`httpx>=0.20`).

```python
import httpx
from structguru.integrations.httpx import StructguruHTTPXLoggingHooks

client = httpx.Client(event_hooks=StructguruHTTPXLoggingHooks.get_hooks())
async_client = httpx.AsyncClient(event_hooks=StructguruHTTPXLoggingHooks.get_async_hooks())
```

## Hooks

| Method | Returns | For |
|---|---|---|
| `StructguruHTTPXLoggingHooks.get_hooks()` | `{"request": [log_request], "response": [log_response]}` | `httpx.Client` |
| `StructguruHTTPXLoggingHooks.get_async_hooks()` | `{"request": [async_log_request], "response": [async_log_response]}` | `httpx.AsyncClient` |

Each call returns a new dictionary. The request hook records the start time
on the request; it logs nothing. The response hook logs one record.

## Logged record

Logger name: `structguru.httpx`.

| Condition | Message | Level |
|---|---|---|
| `response.is_error` (status 400–599) | `Outbound HTTP Request Failed` | `ERROR` |
| any other response | `Outbound HTTP Request Completed` | `INFO` |

| Field | Value |
|---|---|
| `http_method` | `request.method`. |
| `http_url` | The request URL without user information, query values, or fragment; a trailing `?` marks that a query string was present. A URL that cannot be parsed safely is logged as `<unparsable-url>`. |
| `status_code` | The response status. |
| `duration_ms` | Milliseconds from the request hook to the response hook, rounded to two decimals. Absent when the request hook did not run. |
| `request_id` | The request's `x-request-id` header, when present and non-empty. |

Transport failures (connection errors, timeouts, DNS errors) raise before
httpx calls the response hook, so they are not logged.

## API

::: structguru.integrations.httpx
    options:
      heading_level: 3

## See also

- [HTTPX](../../how-to/integrations/httpx.md)
- [Requests](requests.md)
