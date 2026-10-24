# Log Requests calls

Use a logging session to log every outbound call made with the `requests`
library. Install the `requests` extra:

```bash
pip install "structguru[requests]"
```

## Use a logging session

```python
from structguru.integrations.requests import get_logging_session

session = get_logging_session()
session.get("https://api.example.com/orders", params={"page": 2})
```

```json
{"http_method":"GET","http_url":"https://api.example.com/orders?","duration_ms":…,"status_code":200,"logger":"structguru.requests","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"Outbound HTTP Request Completed"}
```

`get_logging_session()` returns a `StructguruRequestsSession`, a subclass of
`requests.Session`; configure it (headers, adapters, authentication) as you
would any session, or subclass `StructguruRequestsSession` directly.

- The logged `http_url` is the URL requests actually sends, including `params=`
  and session-level `params`, with credentials and query values removed: a bare
  `?` shows that parameters were present.
- Responses with a 4xx or 5xx status, and calls that raise (including
  `KeyboardInterrupt` and `SystemExit`), are logged as
  `Outbound HTTP Request Failed` at `ERROR`. The exception then propagates to
  your code unchanged.
- The final prepared request's `X-Request-ID` header is logged as `request_id`.
  Call headers merge case-insensitively with session headers; setting the header
  to `None` removes the session value for that call.

## Forward the current request ID

Set the header from the bound context, per call:

```python
from structguru import get_contextvars

request_id = get_contextvars().get("request_id")
session.get(
    "https://api.example.com/orders",
    headers={"X-Request-ID": request_id} if request_id else None,
)
```

## See also

- [Requests integration](../../reference/integrations/requests.md)
- [Log HTTPX requests](httpx.md)
- [Propagate context](../propagate-context.md)
