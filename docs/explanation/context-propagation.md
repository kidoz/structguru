# Context propagation

Request-scoped fields such as a request ID need to reach every record a
request produces, including records logged deep inside code that never sees
the request object. structguru carries them in Python's own `contextvars`.
This page explains that model and its consequences. For practical recipes for
thread pools, tasks, and generators, see
[Propagate context](../how-to/propagate-context.md). For the rules an
integration must follow, see
[Write an integration adapter](../how-to/write-an-integration-adapter.md).

## Three sources of fields

A record's fields come from three places:

- **Bound fields** belong to a logger object. `logger.bind(...)` returns a new
  logger that carries them, and they travel wherever that object goes.
- **Call keywords** belong to one call:
  `logger.info("payment accepted", amount=42)`.
- **Context fields** belong to the running execution context, not to any
  logger. `bind_contextvars()`, `bound_contextvars()`, `logger.contextualize()`,
  and the framework integrations all write here, and every logger reads from
  here.

They merge in that order. A call keyword replaces a bound value in place, and
a context field fills a key only if neither of the others set it. The context
can add information to a record, but it never overrides what the code at the
call site said. This is also how structlog's `merge_contextvars` behaved, and
structguru keeps that behavior (see
[Compatibility with loguru and structlog](compatibility.md)).

## One variable, copy-on-write

All context lives in a single `contextvars.ContextVar` whose value is a
dictionary. `bind_contextvars()` never changes that dictionary. It builds a new
one with the merged values and sets that instead. `clear_contextvars()` sets an
empty one. `logger.contextualize()` is `bound_contextvars()` under another
name, so integrations, `contextualize()`, and the `*_contextvars` functions all
read and write the same context.

Copy-on-write is what makes one shared dictionary safe. When Python copies a
context, for example for a new asyncio task, the copy starts out referring to
the same dictionary as the original. Since nobody mutates that dictionary in
place, a bind in the task replaces the task's reference and leaves the parent's
dictionary as it was. The same property lets a logging call hand the current
dictionary to the renderer without copying it: the mapping cannot change while
the record is being merged.

`get_contextvars()` returns a shallow snapshot: a new top-level dictionary
whose values are the same objects. Adding a key to the snapshot does not
change the context, but appending to a list stored in the context changes it
for everyone who shares that dictionary.

## Tasks inherit a copy; threads depend on the interpreter

Because the context is a `ContextVar`, its scope follows Python's rules, and
those rules differ between tasks and threads.

An asyncio task receives a copy of the context that was current when it was
created. Fields bound before `asyncio.create_task()` or `asyncio.gather()` are
visible inside the task, and fields the task binds stay inside the task.

A thread's context is decided when the thread is created, and what it starts
with depends on the interpreter:

- On the default CPython builds, a new `threading.Thread` starts with an empty
  context. Nothing bound in the creating thread is visible.
- On free-threaded CPython 3.14 builds, which structguru supports, a new thread
  starts with a copy of the creating thread's context by default. This is
  `sys.flags.thread_inherit_context`, which the `-X thread_inherit_context`
  option and the `PYTHON_THREAD_INHERIT_CONTEXT` environment variable control
  on either kind of build.

Neither behavior carries context into pooled work correctly. A thread pool
reuses its worker threads for many tasks. On default builds, the workers have
an empty context. With inheritance, each worker keeps the copy it received when
it was created, which is the context of whichever request happened to start
it, so later tasks on that worker can log another request's fields. Work handed
to another thread needs its context passed explicitly for each task; the
[how-to guide](../how-to/propagate-context.md) shows how.

## Binding until cleared, and binding for a block

`bind_contextvars()` merges fields that stay bound until something clears
them. It suits code where binding and releasing happen in different places,
such as a middleware's request-started and request-finished hooks.

`bound_contextvars()` and `logger.contextualize()` bind for the duration of a
`with` block. On exit they restore the context through the token that
`ContextVar.set()` returned, so the context is exactly what it was before the
block: values the block overrode come back, and anything bound inside the
block, even with `bind_contextvars()`, is gone.

`clear_contextvars()` resets the context to empty. It discards everything,
including fields that other code bound, which is why integrations clear at the
start and end of a request and never in the middle of one.

## When a block exits in a different context

A `with bound_contextvars(...)` block normally exits in the context it entered.
It can exit somewhere else when the block sits inside a generator:

- A generator resumed under a fresh copy of the context for every `next()`,
  as Starlette's `iterate_in_threadpool` and anyio's `run_sync` do, runs its
  exit in whatever copy the last `next()` used.
- An async generator that is garbage-collected while suspended is finalized
  by the event loop, which runs its `aclose()` as a separate task in that
  task's own copy of the context.

A token can only reset the variable in the context that created it, so it
cannot restore anything there. Before 1.4.0 the exit raised `ValueError`. Now
the exit looks at what the current context holds:

- If it holds exactly the dictionary the block installed, the current context
  was copied from one in which the block was active, and nothing has been
  bound in it since. The exit restores the entry values there.
- Otherwise the exit leaves the context untouched.
- Either way, the exit never raises.

Leaving an unrelated context alone is the safe choice. The context a
generator is finalized in may belong to a different request that happens to be
running at that moment. "Restoring" it would overwrite that request's fields
with stale values from the request that created the generator. The identity
check is what lets the exit tell the two cases apart: only a context that
still holds the very dictionary the block installed is known to descend from
the block with nothing bound since.

The cost is that the context in which the block entered keeps the block's
fields when the exit happens elsewhere. For async generators, that is the task
that was iterating. Closing the generator explicitly in that task, with
`contextlib.aclosing()` or `await agen.aclose()`, moves the exit back to where
it restores correctly. [Test code that logs](../how-to/test-code-that-logs.md)
shows how to catch this in tests.

## Why lazy results split a scope in two

An integration binds a request's context when the request starts and clears it
when the request ends, around the call to the handler. That works when the
handler does its work inside the call. A lazy result breaks the assumption.
Calling a generator function runs none of its body: the body runs on the first
`next()`, which may come much later, on another thread, or never, if the
client goes away. Streaming responses are the usual case.

One scope then becomes two, and each needs different treatment:

- **Before iteration**, between returning the iterator and the first `next()`,
  the context must be clean. Otherwise the request's fields leak into
  unrelated records logged on that thread in the meantime, indefinitely if the
  iterator is never consumed.
- **During iteration**, the context must be bound again, including anything the
  handler bound eagerly before it returned.

The answer is to take a snapshot of the context when the handler returns,
clear it, and restore the snapshot when iteration actually begins. The
snapshot matters because a handler is not always lazy: one that returns a list
ran its whole body during the call, so the context it bound exists at snapshot
time and would otherwise be thrown away by the clear. `get_contextvars()` was
exported from the package root in 1.2.0 for exactly this pattern, which the
gRPC integration applies to handlers that return a response stream. The
pattern itself is in
[Write an integration adapter](../how-to/write-an-integration-adapter.md).
