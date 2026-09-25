# gRPC

`structguru.integrations.grpc.StructguruInterceptor` is a synchronous gRPC
server interceptor that binds per-RPC context. Install the `grpc` extra
(`grpcio>=1.50`).

```python
from concurrent import futures

import grpc
from structguru.integrations.grpc import StructguruInterceptor

server = grpc.server(
    futures.ThreadPoolExecutor(max_workers=10),
    interceptors=[StructguruInterceptor(request_id_key="x-request-id")],
)
```

## Parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `request_id_key` | `str` | `"x-request-id"` | Invocation-metadata key to read the request ID from. Lower-cased before lookup. |
| `logger_name` | `str` | `"structguru.grpc"` | Name of the interceptor's logger. |

## Context fields

| Field | Value |
|---|---|
| `grpc_method` | `handler_call_details.method`, for example `/package.Service/Method`, or `""`. |
| `request_id` | The metadata value under `request_id_key` when it is valid; otherwise a new UUID4 string. |

- A request ID is valid when it is a non-empty, printable string of at most
  128 characters. A binary (`-bin`) value must decode as strict UTF-8. An
  invalid value never fails the RPC.
- When a metadata key repeats, the last value is used.
- The fields are bound while the handler runs, for unary and streaming
  requests and responses. For a streaming response, the context present when
  the handler returned its iterator is bound again while the iterator is
  consumed.
- The context is cleared before the handler runs, after it returns or raises,
  and between `intercept_service()` returning and the handler starting.

The interceptor logs no request summary.

## API

::: structguru.integrations.grpc
    options:
      heading_level: 3

## See also

- [gRPC](../../how-to/integrations/grpc.md)
- [Context propagation](../../explanation/context-propagation.md)
