# Log HTTPX requests

Attach structguru's event hooks to an HTTPX client to log every outbound
request that receives a response. Install the `httpx` extra:

```bash
pip install "structguru[httpx]"
```

## Add the hooks

`httpx.Client`:

```python
import httpx

from structguru.integrations.httpx import StructguruHTTPXLoggingHooks

client = httpx.Client(event_hooks=StructguruHTTPXLoggingHooks.get_hooks())
response = client.get("https://api.example.com/orders?token=abc")
```

```json
{"http_method":"GET","http_url":"https://api.example.com/orders?","status_code":200,"duration_ms":…,"logger":"structguru.httpx","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"Outbound HTTP Request Completed"}
```

`httpx.AsyncClient` needs awaitable hooks, which are exposed separately:

```python
async def fetch() -> httpx.Response:
    async with httpx.AsyncClient(
        event_hooks=StructguruHTTPXLoggingHooks.get_async_hooks(),
    ) as client:
        return await client.get("https://api.example.com/orders")
```

- Responses with a 4xx or 5xx status are logged as
  `Outbound HTTP Request Failed` at `ERROR`; everything else at `INFO`.
- The logged URL has credentials and query values removed; a bare `?` shows that
  parameters were present.
- If the request carries an `X-Request-ID` header, its value is logged as
  `request_id`.

Each call returns fresh lists, so you can add your own hooks:

```python
def raise_on_error(response: httpx.Response) -> None:
    response.raise_for_status()


hooks = StructguruHTTPXLoggingHooks.get_hooks()
hooks["response"].append(raise_on_error)  # runs after the logging hook
client = httpx.Client(event_hooks=hooks)
```

## Forward the current request ID

To correlate the outbound call with the request that made it, send the bound
`request_id` as a header with a request hook placed before the logging hooks:

```python
from structguru import get_contextvars


def forward_request_id(request: httpx.Request) -> None:
    request_id = get_contextvars().get("request_id")
    if request_id:
        request.headers["X-Request-ID"] = request_id


hooks = StructguruHTTPXLoggingHooks.get_hooks()
hooks["request"].insert(0, forward_request_id)
client = httpx.Client(event_hooks=hooks)
```

## Log transport errors

The hooks log in HTTPX's `response` hook, so transport-level failures (connection
refused, DNS failures, timeouts) are not logged: they raise before a response
exists, and HTTPX never calls the hook. Wrap the call site to record them, with
`reraise=True` so the caller still handles the error:

```python
from structguru import logger


@logger.catch(httpx.TransportError, message="Upstream unreachable", reraise=True)
def fetch_orders():
    return client.get("https://api.example.com/orders")
```

`logger.catch()` defaults to `reraise=False`, which would swallow the transport
error instead of letting the caller handle it.

## See also

- [HTTPX integration](../../reference/integrations/httpx.md)
- [Log Requests calls](requests.md)
- [Log exceptions](../log-exceptions.md)
