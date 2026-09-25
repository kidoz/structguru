# Log gRPC calls

Add `StructguruInterceptor` to a gRPC server to bind `grpc_method` and
`request_id` to every record logged while an RPC runs, for unary and streaming
RPCs alike. Install the `grpc` extra:

```bash
pip install "structguru[grpc]"
```

## Add the interceptor

```python
from concurrent import futures

import grpc

from structguru.integrations.grpc import StructguruInterceptor

server = grpc.server(
    futures.ThreadPoolExecutor(max_workers=10),
    interceptors=[StructguruInterceptor(request_id_key="x-request-id")],
)
```

Records logged by your servicers then carry the RPC fields:

```json
{"grpc_method":"/shop.Orders/Get","request_id":"req-123","logger":"orders.service","level":"INFO","severity":6,"timestamp":"…","service":"orders","message":"Loading order"}
```

- `request_id` comes from the invocation metadata key `request_id_key` (default
  `x-request-id`). gRPC delivers metadata keys in lower case, so the key matches
  case-insensitively (`"X-Request-ID"` works).
- A binary `-bin` key's value must be UTF-8. An invalid, oversized, or
  non-printable ID is replaced by a generated UUID; it never fails the RPC.
- For server-streaming RPCs, the fields are bound while gRPC consumes the
  response iterator, not in the time before.
- The interceptor binds context only; it does not log a per-call summary line.

The interceptor works with `grpc.server()`. `grpc.aio.server()` rejects it,
because asyncio servers require `grpc.aio.ServerInterceptor` instances.

## Send a request ID from the client

```python
with grpc.insecure_channel("localhost:50051") as channel:
    stub = OrdersStub(channel)
    stub.Get(request, metadata=[("x-request-id", "req-123")])
```

## See also

- [gRPC integration](../../reference/integrations/grpc.md)
- [Propagate context](../propagate-context.md)
- [Write an integration adapter](../write-an-integration-adapter.md)
