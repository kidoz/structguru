# Write an integration adapter

Use this guide when you connect structguru to a framework it does not
integrate with yet (a web framework, a task queue, an RPC server), or review
such an adapter. The built-in ASGI, Flask, Django, Celery, and gRPC
integrations follow the same rules.

An adapter binds per-unit-of-work fields (a request ID, a task name) when the
work starts, and removes them when it ends. For why the rules below are
necessary, see [Context propagation](../explanation/context-propagation.md).

## Follow the three rules

1. **Clear on entry.** Never assume the context is empty. Worker threads and
   pooled greenlets are reused, so a previous request's context may still be
   bound. Call `clear_contextvars()` *before* binding, not after.
2. **Clear on every exit path**, including exceptions. A handler that raises
   must not leave its context behind for whatever runs next on that thread.
3. **Do not clear mid-scope.** Application code may add to the context after
   the adapter runs (binding `user_id` during authentication, for example).
   Re-binding your own keys is safe, because `bind_contextvars()` merges and the
   additions survive. `clear_contextvars()` is not: it resets the context to
   empty, so a clear-then-rebind in the middle of a request discards everything
   the application added. Bind once at entry and leave the context alone until
   the exit path.

The shape that satisfies all three:

```python
from structguru import bind_contextvars, clear_contextvars


def middleware(request, handler):
    clear_contextvars()
    bind_contextvars(request_id=request.id, path=request.path)
    try:
        return handler(request)
    finally:
        clear_contextvars()
```

When binding and cleanup happen in separate framework callbacks (a
`before_request` and a `teardown_request` hook), clear in the first callback and
in a cleanup callback that the framework runs on every outcome, including
errors.

## Handle handlers that return lazy results

The shape above assumes the handler *runs* inside the `try`. When it returns a
lazy result, such as a generator or a streaming response iterator, it does not.
Calling a generator function runs none of its body; the body runs on the first
`next()`, which may be much later, or never if the caller gives up.

That splits one scope into two, and both halves need handling:

- Between returning the iterator and the first `next()`, the context must be
  **clean**. Otherwise it leaks into unrelated records on that thread for an
  unbounded time, and forever if the iterator is never consumed.
- During iteration the context must be **bound** again, including anything the
  handler bound eagerly before returning.

Snapshot the context, clear it, and restore it when iteration actually begins:

```python
from structguru import bind_contextvars, clear_contextvars, get_contextvars


def call_streaming_handler(handler, request):
    clear_contextvars()
    bind_contextvars(request_id=request.id)
    try:
        result = handler(request)
    except BaseException:
        clear_contextvars()
        raise
    snapshot = get_contextvars()  # includes anything the handler bound eagerly
    clear_contextvars()  # nothing leaks while the iterator sits unconsumed
    return _wrap_iterator(result, snapshot)


def _wrap_iterator(it, snapshot):
    clear_contextvars()
    bind_contextvars(**snapshot)
    try:
        yield from it
    finally:
        clear_contextvars()
```

The snapshot matters because a handler is not always a generator. One that
returns a list runs its body eagerly, during the call, so the context it bound
exists at snapshot time and would otherwise be discarded by the clear.
`structguru.integrations.grpc` implements exactly this for its streaming
handler types.

## Log a summary line

Log through a named `Logger`, so the summary is easy to find and filter, and
log it on every exit path:

```python
import time

from structguru import Logger, bind_contextvars, clear_contextvars

log = Logger(name="myframework.http")


def middleware(request, handler):
    clear_contextvars()
    bind_contextvars(request_id=request.id, path=request.path)
    start = time.perf_counter()
    try:
        response = handler(request)
    except BaseException:
        log.exception("Request failed", duration_ms=round((time.perf_counter() - start) * 1000, 2))
        raise
    else:
        log.info(
            "Request completed",
            status_code=response.status,
            duration_ms=round((time.perf_counter() - start) * 1000, 2),
        )
        return response
    finally:
        clear_contextvars()
```

Log the summary before the final clear, so it carries the request fields.

## Test the adapter

Assert that the context is empty after each exit path, including an exception,
and before the first item of a lazy result. See
[Test code that logs](test-code-that-logs.md) for the patterns.

## See also

- [Context propagation](../explanation/context-propagation.md)
- [Propagate context](propagate-context.md)
- [Configuration and runtime control](../reference/configuration.md) for the
  context functions
- [gRPC integration](../reference/integrations/grpc.md)
