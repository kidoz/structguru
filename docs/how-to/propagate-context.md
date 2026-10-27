# Propagate context

Use this guide to attach fields such as `request_id` to every record logged
while a piece of work runs, including work handed to tasks, threads, and
generators. For how the context model works, see
[Context propagation](../explanation/context-propagation.md).

## Bind fields for a block

`logger.contextualize()` binds fields for the duration of a `with` block and
restores the previous context when it exits, on every exit path:

```python
from structguru import logger

with logger.contextualize(request_id="req-1"):
    logger.info("Handling request")  # includes request_id
    process_order()  # any logging inside also gets request_id
# request_id is gone here
```

`bound_contextvars()` does the same without a logger:

```python
from structguru import bound_contextvars

with bound_contextvars(user_id="u-1"):
    ...
```

## Bind fields on a logger instead

`logger.bind()` returns a child logger that carries the fields on every call it
makes, wherever it is passed:

```python
log = logger.bind(component="billing")
log.info("Invoice sent", invoice_id=7)
```

Use `bind()` for fields that belong to a component, and `contextualize()` for
fields that belong to the unit of work.

## Bind fields from separate callbacks

When binding and cleanup happen in different callbacks (a framework's request
start and end hooks), use the functions directly. Clear on entry, because a
reused worker thread may still hold a previous request's fields, and clear
again on every exit path:

```python
from structguru import bind_contextvars, clear_contextvars


def on_request_start(request):
    clear_contextvars()
    bind_contextvars(request_id=request.id, path=request.path)


def on_request_end(request):
    clear_contextvars()
```

`bind_contextvars()` merges into the current context, so it can add fields later
in the request (for example, `user_id` after authentication) without removing
the others. Do not call `clear_contextvars()` in the middle of a request: it
removes everything, including fields other code bound. The framework
integrations already do this for you; to write your own, see
[Write an integration adapter](write-an-integration-adapter.md).

## Inspect the current context

```python
from structguru import get_contextvars

get_contextvars()  # {'request_id': 'req-1', 'user_id': 'u-1'}
```

It returns a shallow copy: changing the dictionary does not change the bound
context, but nested mutable values are shared.

## Carry context into asyncio tasks

Nothing to do: a task created with `asyncio.create_task()` or `asyncio.gather()`
starts with a copy of the creating task's context. `asyncio.to_thread()` copies
the context into its thread as well. Fields a task binds stay in that task.

`loop.run_in_executor()` does **not** copy the context. Wrap the call:

```python
import contextvars

ctx = contextvars.copy_context()
await loop.run_in_executor(None, ctx.run, work, item)
```

## Carry context into threads and thread pools

What a new thread sees depends on the CPython build. On the default build, a new
`threading.Thread` starts with an empty context. On free-threaded CPython 3.14,
it starts with a copy of its creator's context, so a thread-pool worker keeps
the fields of whichever request happened to create it, and later tasks on that
worker can log a previous request's fields.

Behave the same on both builds by running each piece of work inside an explicit
copy of the submitting code's context:

```python
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

from structguru import logger

with logger.contextualize(request_id="req-1"):
    ctx = contextvars.copy_context()
    threading.Thread(target=ctx.run, args=(work, item)).start()

    with ThreadPoolExecutor() as pool:
        pool.submit(contextvars.copy_context().run, work, item)
```

Use a fresh `copy_context()` for each submission; a `Context` object cannot be
entered by two threads at once. `ctx.run()` runs the work in that copy, so it
neither sees a worker's leftover fields nor leaves its own behind. The runnable
[context-propagation example](https://github.com/kidoz/structguru/blob/main/examples/context_propagation/main.py)
demonstrates this pattern together with asyncio tasks and cleanup assertions.

Alternatively, pass the values and bind them in the worker, clearing first and
on exit:

```python
from structguru import bind_contextvars, clear_contextvars


def work(item, request_id):
    clear_contextvars()
    bind_contextvars(request_id=request_id)
    try:
        process(item)
    finally:
        clear_contextvars()
```

## Close generators that bind context

A generator that binds context inside its body binds it in the context of the
code iterating it. If you stop iterating early, the generator is suspended, not
finished: its cleanup has not run, and the fields stay bound until it is closed
or garbage-collected. Close it explicitly:

```python
from contextlib import closing

from structguru import bound_contextvars


def rows():
    with bound_contextvars(batch_id="b-1"):
        yield from fetch_rows()


with closing(rows()) as it:
    for row in it:
        if row.done:
            break  # close() runs here and removes batch_id
```

For async generators this matters more: when a suspended async generator is
garbage-collected, the event loop runs its cleanup as a separate task, in that
task's own context, so the iterating task keeps the fields. Consume async
generators through `contextlib.aclosing()`:

```python
from contextlib import aclosing

async with aclosing(stream_events()) as events:
    async for event in events:
        if event.done:
            break  # aclose() runs here, in this task's context
```

## Carry context to other processes

Context is per process. To send selected fields with Celery tasks, see
[Log Celery tasks](integrations/celery.md). For outbound HTTP calls, send the
request ID as a header; see [Log HTTPX requests](integrations/httpx.md).

## See also

- [Context propagation](../explanation/context-propagation.md)
- [Configuration and runtime control](../reference/configuration.md) for the
  context functions
- [Logger](../reference/logger.md)
- [Test code that logs](test-code-that-logs.md)
