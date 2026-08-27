# Context Propagation

This page is for anyone writing or reviewing an integration adapter (ASGI,
Flask, Django, Celery, gRPC, or your own). It describes the invariants every
adapter must hold, and the two ways they are most often broken.

## The model

Context lives in a single `contextvars.ContextVar[dict]`, copy-on-write:
`bind_contextvars` merges into a **new** dict, so a bind never mutates a dict
another task can see. Because it is a `ContextVar`, scope follows Python's own
rules — each `asyncio` task and each thread sees its own copy automatically.

```python
from structguru import bind_contextvars, bound_contextvars, clear_contextvars

bind_contextvars(request_id="req-1")   # merge, persists until cleared
with bound_contextvars(user_id="u-1"): # merge, restored on exit
    ...
clear_contextvars()                    # reset to empty
```

`bound_contextvars` restores via a token and is the safest choice when the
scope is a plain block. Adapters usually cannot use it, because bind and
release happen in different callbacks.

## The adapter contract

Every adapter must satisfy three rules:

1. **Clear on entry.** Never assume the context is empty. Worker threads and
   pooled greenlets are reused, so a previous request's context may still be
   bound. Adapters call `clear_contextvars()` *before* binding, not after.
2. **Clear on every exit path**, including exceptions. A handler that raises
   must not leave its context behind for whatever runs next on that thread.
3. **Do not rebind mid-scope.** Application code may enrich the context after
   the adapter runs (binding `user_id` during auth, for example). Re-binding
   the adapter's own keys later would wipe those additions.

The shape that satisfies all three:

```python
def middleware(request, handler):
    clear_contextvars()
    bind_contextvars(request_id=request.id, path=request.path)
    try:
        return handler(request)
    finally:
        clear_contextvars()
```

## Lazy results defer the handler body

The contract above assumes the handler *runs* inside the `try`. When the
handler returns a **lazy** result — a generator, a streaming response iterator
— it does not. Calling a generator function runs none of its body; the body
executes on the first `next()`, which may be much later, or never if the caller
gives up.

That splits one scope into two, and both halves need handling:

- Between returning the iterator and the first `next()`, the context must be
  **clean** — otherwise it leaks into unrelated logs on that thread for an
  unbounded interval, and forever if the iterator is never consumed.
- During iteration the context must be **bound** again, including anything the
  handler bound eagerly before returning.

Snapshot, clear, then restore when iteration actually begins:

```python
from structguru._contextvars import get_contextvars


def call_streaming_handler(handler, request):
    result = handler(request)
    snapshot = get_contextvars()  # includes anything the handler bound eagerly
    clear_contextvars()           # nothing leaks while the iterator sits unconsumed
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
returns a list runs its body eagerly, during the call — so context it bound
there exists at snapshot time and would otherwise be discarded by the clear.
`structguru.integrations.grpc` implements exactly this for its four streaming
handler types.

## Testing

Context is process state, so a test that leaves it bound can change the
outcome of a later test. There is no autouse fixture that resets it — the
shared fixture in `tests/conftest.py` restores logging state only.

- **Start from a known state.** Call `clear_contextvars()` at the top of any
  test that reads or asserts on context.
- **Close generators explicitly.** A partially-consumed generator is
  *suspended*, not finished: its `finally` has not run, so its context is still
  bound. Cleanup happens whenever the object is garbage collected — which may
  be in the middle of an unrelated later test. Call `.close()` and assert the
  context is clean, as `test_streaming_partial_consumption_cleans_up_on_close`
  does.
- **Cover the pre-iteration window.** Asserting that context is bound *during*
  iteration will not catch a leak in the interval before it. Create the
  iterator, assert the context is empty, and only then consume it.

```python
stream = handler(...)
assert "request_id" not in get_contextvars()   # not bound yet
it = iter(stream)
assert next(it) == "item-0"
assert get_contextvars()["request_id"] == "req-1"
it.close()                                     # cleanup runs here, not at GC
assert "request_id" not in get_contextvars()
```
