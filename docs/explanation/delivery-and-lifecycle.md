# Delivery and lifecycle

A logging call returns once its record has been handed to a queue, not once
the record has been written. Between those two points a writer thread does the
I/O, a second thread may call Python sinks, and the application may
reconfigure, shut down, exit, or fork. This page explains what structguru
promises across that gap and why it behaves as it does at each boundary.

## Three outcomes at the queue

When the native call pushes a rendered line into the writer queue, it observes
one of three outcomes under the queue lock:

- **Accepted.** The record is in the queue. The writer will write it, and
  shutdown, reconfiguration, fork, and interpreter exit all drain it first.
- **Dropped.** The queue was full and the overflow policy is `"drop"`. The
  record is counted in `writer_metrics()["dropped"]`, and a `UserWarning` is
  issued for the first drop and every 1000th after it.
- **Rejected.** The writer was already closed. This happens only at a
  lifecycle boundary, to a call that was still formatting, running a hook, or
  waiting for queue space when `configure()`, `update()`, or `shutdown()`
  retired its writer. The record is counted in
  `lifecycle_metrics()["rejected"]`, without a queue-full warning, and it is
  not retried.

Two other kinds of loss happen earlier and are counted elsewhere: records
removed by sampling or rate limiting (`sampled` and `rate_limited`), and sink
operations that fail after a record was dequeued (`sink_errors`, which also
counts a destination that failed while another one succeeded). A call made
while logging is disabled does nothing and is not counted anywhere.

The counters live in two places because they describe different things.
`writer_metrics()` describes the current writer: it returns `None` after
shutdown, and its counters restart whenever a new writer is built, although a
level-only change keeps them. `lifecycle_metrics()` describes the process: its
`rejected` count accumulates across reconfigurations and survives shutdown,
and a forked child starts it at zero. The
[configuration reference](../reference/configuration.md) lists every counter.

## Why the queue is bounded

Writing on a background thread keeps sink latency out of the logging call,
but only while the sink keeps up on average. When it does not, the difference
has to go somewhere. An unbounded queue puts it in memory, and a sink that
stops completely makes that memory grow without limit. Before 1.0.1 an
unbounded queue was the default; since then the queue holds 8192 records
unless `maxsize` says otherwise.

What happens at the bound is the overflow policy:

- `"block"` (the default) makes the caller wait for space. Nothing is lost to
  overflow, and callers run at the sink's pace while it is the bottleneck. The
  wait releases the GIL, so other threads keep running.
- `"drop"` discards the new record and returns at once. Caller latency stays
  flat, and the loss is counted and reported.
- `maxsize=0` removes the bound. It exists for the rare case where memory is
  the cheaper resource, and it has to be asked for explicitly.

Blocking protects against overflow, not against lifecycle boundaries: a caller
waiting for space when its writer closes is woken and its record is rejected.
[Handle backpressure](../how-to/handle-backpressure.md) covers choosing between
the policies.

## Every destination follows the writer

A record can have destinations besides the writer: a synchronous
`stream_sink`, callable sinks, and a Sentry processor. They all follow the
writer's decision about the record's lifecycle. A rejected record reaches none
of them. A record the writer accepted just before a reconfiguration reaches
all of them, the callable sinks through the replacement queue, which delivers
after its predecessors. Once `shutdown()` has begun, callable sinks and Sentry
receive nothing more.

The reason is that a record should have one fate. Before 1.4.0, the stream
received a record even when the writer had rejected it, and a record accepted
just before `update()` or `configure()` swapped the runtime could be lost to
callable sinks and Sentry without being counted anywhere.

An overflow drop in drop mode is different: it is a loss for the writer's own
destinations only. The stream, the callable sinks, and Sentry still receive
the record, and the callable queue applies its own bound and its own drop
count.

## Callable sinks: one thread, in log order

Callable sinks, the functions passed as `callable_sinks` and the sinks added
with `logger.add()`, are Python code, so they cannot run on the writer thread
without making it depend on the interpreter. They have their own bounded
queue, 1024 records by default, governed by the same overflow policy, with its
own `callable_dropped` counter.

A single thread at a time calls them, in log order, and that remains true
across `configure()`, `update()`, and `logger.add()`. Each reconfiguration
starts a new queue generation, which accepts records immediately but begins
delivering only after every earlier generation has finished or been
abandoned. The backlog of the old configuration therefore never interleaves
with the new one. `logger.add()` never replaces the active generation and
never waits for its backlog.

Three rules keep sink code from turning against the logging system:

- **Logs emitted inside a sink callback skip callable sinks.** They still
  reach the native writer. Without this rule a sink that logs would feed its
  own queue, and in block mode it could wait for space that only it can free.
  The rule covers both delivery paths: native records, and raw
  standard-library records delivered to `logger.add()` sinks.
- **Failures stay on the delivery thread.** An exception raised by a sink,
  including `SystemExit` and `KeyboardInterrupt`, is counted in
  `callable_errors`, with a warning for the first and every 1000th, and the
  thread moves on to the next record. Sink-failure warnings report the error
  type and count without stringifying the exception: warning text bypasses
  record redaction and must not include sensitive exception text.
- **Lifecycle calls from inside a callback do not wait for it.** A callback
  cannot wait for the thread that is running it. A `shutdown()` requested from
  a callback therefore does not wait for that callback, and logs the callback
  emits afterwards see logging disabled.

Outside callbacks, `logger.remove()` waits for producers that already selected
the sink and for deliveries in progress, and closes a `logging.Handler` only
after the last queued delivery to it has run.

## When a sink stops returning

Draining the callable queue means waiting for Python code, and Python code can
hang. `flush()`, `shutdown()`, `configure()`, `update()`, and `logger.remove()`
therefore bound that wait by progress rather than by total time. If no
delivery completes for 10 seconds, the generation being drained is abandoned. A
slow
sink that keeps completing deliveries is never abandoned, however long its
backlog takes, because a large backlog is legitimate and only a lack of
progress indicates a hang.

Abandonment has fixed consequences:

- A warning reports how many records were abandoned, including the one handed
  to the stuck callback. Those records cannot be recovered.
- The stuck thread is left detached, not killed. When the callback eventually
  returns, the thread stops without starting another delivery or re-entering
  the interpreter, which may be shutting down by then.
- When the abandonment happens during `flush()`, `shutdown()`, or
  `logger.remove()`, callable delivery stays off until the configuration is
  replaced, by `configure()` or by an `update()` that changes more than the
  level, even if a sink is added in the meantime. When it happens during
  `configure()` or `update()`, the new generation that call started keeps
  delivering.
- A producer waiting for space in the abandoned generation is released and its
  record counts as abandoned, not as a queue-full drop.

The native writer has the same limit. When its sink makes no progress for 10
seconds, `flush()`, `shutdown()`, and reconfiguration abandon it: records still
queued are discarded, the write already in progress may finish, and the worker
then stops. The warning does not wait on stderr, so a full pipe shared by
stdout and stderr cannot hold the caller past the stall limit. The warning is
dropped when stderr cannot accept a write.

## What flush() waits for

`flush()` exists because a returning `logger.info()` does not mean the line
has reached its destination. It first waits for submissions that started before
the call to finish their native capture and hand the resulting line to the
synchronous stream, callable queue, and Sentry. It then drains callable
deliveries, since a callback may itself log to the writer, and waits for the
writer to write and flush every covered record.

This includes ordinary calls that render and enqueue entirely in Rust: they
hold a native submission lease before rendering or waiting for queue space.
The flush keeps references to the current and retiring writers at entry and
checks again after callable delivery, so a concurrent reconfiguration cannot
hide a previous writer that is still draining.

"Before the call" matters. Submission leases and callable producer leases have
monotonically increasing identities, so work that starts later neither satisfies
nor prolongs an earlier flush. A reentrant `flush()` from conversion, a stream,
or a Sentry hook excludes the submission currently running on that thread
instead of waiting for itself. A submission that makes no progress for 10 seconds produces a bounded
warning and stops blocking the flush. Inherited leases are reset in a forked
child. Until 1.3.0, a flush waited for the queue to become empty, a moment that
never arrives while other threads refill the queue faster than the sink drains
it; targets are fixed instead of waiting for a quiet moment.

## Reconfiguration

`configure()` and `update()` compile patterns, open files, and acquire sidecar
locks before publishing a new runtime. A failure raises and leaves the previous
runtime running, so a typo in a regex or an unwritable path cannot turn working
logging off.

After publication, callable dispatch prepares a replacement worker before
retiring its previous generation. Existing sinks remain active throughout this
transition, including on free-threaded Python. If worker creation fails, the
call raises `RuntimeError` and restores its previous runtime unless a newer
configuration has already replaced it. The previous callable generation stays
usable.

Callable dispatch then drains its old generation, subject to the stall limit.
The old writer is closed afterwards: it drains the records it accepted, then
its thread is joined. Calls still in flight against a closed snapshot are
rejected as described above.

A level-only change, through `set_level()` or `update(level=...)`, skips all of
this. It keeps the writer, its queue and counters, the sampling and
rate-limiting state, and in-flight callable and Sentry delivery. Every
`configure()`, and any `update()` that changes more than the level, rebuilds
the writer, resets filter state, and restarts the callable counters. Sinks
added with `logger.add()` survive every reconfiguration.

## Shutdown and interpreter exit

`shutdown()` works in a specific order. It first drains callable deliveries
while the writer is still open, so that logs emitted by those callbacks can
still be written. Then it disables logging and closes the writer, which
drains what it accepted. Later calls do nothing until `configure()` runs
again.

Interpreter exit runs the same shutdown from an `atexit` handler that is
registered when structguru is imported, not when it is first configured.
Because `atexit` runs handlers in last-in, first-out order, every handler that
the application registers after importing structguru runs before the drain and
can still log, even when `STRUCTGURU_AUTOCONFIGURE=0` defers configuration to
a later `configure()` call. A handler registered before structguru was
imported runs after the drain and finds logging disabled; its calls do
nothing rather than failing against a closed writer.

## What happens at fork()

Only the thread that calls `fork()` exists in the child. The writer thread and
the delivery thread are gone, and any lock one of them held at that moment
stays locked forever. Because the writer is a background thread, CPython 3.12
and later may also emit a `DeprecationWarning` about forking a multi-threaded
process.

structguru registers fork handlers (on POSIX systems, when it is first
configured) that deal with this in two steps.

**In the parent, before the fork**, it flushes, so what the parent logged
before the fork has been written by the time the child starts. The child never
writes the parent's records: the queue it inherits is abandoned together with
the writer.

**In the child, after the fork**, it never touches what the vanished threads
may have left locked:

- It replaces its own Python locks.
- It abandons the inherited writer without joining its thread and without
  detaching it. On Linux, glibc reuses a dead thread's identity for the next
  thread the child creates, so even a detach could hit the child's new, live
  writer thread. The stale handle is leaked instead.
- It starts a new writer with the full sink configuration, including the file
  path, rotation settings, and stdout mirroring, so a prefork worker logs
  exactly where its parent did.
- It rebuilds callable dispatch from a lock-free copy of the sink
  registrations. Deliveries queued in the parent are not repeated in the
  child.
- It resets `lifecycle_metrics()` to zero for the new process.
- It publishes the fresh runtime before running deferred sink finalizers, which
  may themselves log or reconfigure.

Inherited native writer handles reject submissions and return from flush and
close without acquiring parent locks, even before structguru's child hook runs.
Public logging calls made by earlier child hooks are inactive until the runtime
has been rebuilt. Register application child hooks after the first
`configure()` if their log records must be delivered.

If the child cannot start its writer, logging in that child is disabled as
after `shutdown()`, and one line explaining why is written straight to file
descriptor 2, because `sys.stderr` and the warnings machinery take locks that a
vanished thread may hold. Before 1.4.0 such a child kept the dead writer, lost
records without counting them, and hung in `flush()`.

The stdout destination follows the same principle. On Unix it writes each line
directly to file descriptor 1, with no buffer and no process-wide lock, so a
child never waits on a lock held by the parent. Before 1.4.0 it used Rust's
standard stdout handle, whose lock the parent's writer thread could be holding
at the instant of the fork; in the child, the new writer, `flush()`, exit,
and, once the queue filled, every logging call then blocked on it forever.
Writing to the descriptor by number also means output follows a later `dup2()`
onto fd 1, and a closed stdout is counted in `sink_errors` instead of
vanishing.

## Sharing a rotating file between processes

Prefork servers often point every worker at the same `file_path`. Each process
has its own writer, so the writers must agree on who renames what. They
coordinate through an owner-only sidecar file next to the log, named after it
with a `.lock` suffix. Every write takes an exclusive lock on the sidecar, then
checks (on Unix, with one `stat()`) whether the path still names the file the
writer has open, reopens it if another process or an external tool such as
logrotate renamed or deleted it, rotates if the next line would reach the size limit,
writes, and releases the lock. No worker renames or deletes another worker's
active file or backups.

Rotation mirrors `logging.handlers.RotatingFileHandler`: 50 MB and five backups
by default, rolling over before the record that would reach the limit. With
rotation disabled, a file renamed or deleted by an external tool is still
reopened on Unix. New log and lock files are created with mode `0600` on Unix,
because log files routinely carry sensitive data; existing files keep their
permissions. A relative `file_path` is resolved to an absolute one at
`configure()`, so a later `os.chdir()` cannot split the log or move its lock
and backups.

On Unix, opening a log or lock path rejects a final-component symlink and
validates that the opened handle names a regular file, including on reopen.
The active file needs read and append access so writers can inspect its final
byte while holding the sidecar lock; after an interrupted write, the next record
starts on a new line even when a different writer produced the partial line.
Inherited sidecar descriptors are closed in forked children before replacement
writers open the same lock path; this does not unlock a live parent's descriptor.

Log directories must not be attacker-writable: no-follow opens do not protect
ancestor directories, hard links, or replacement of the sidecar lock path.
On Windows, file creation uses inherited permissions, does not install an
owner-only ACL, and does not provide Unix no-follow protection. Applications
must provision their log directory and existing files accordingly.

The coordination has limits. The sidecar sits next to the log, and both should
be on a local filesystem. File locking does not extend across hosts: for
containers and distributed deployments, writing to stdout and letting an
external collector handle files remains the recommended model.
[Send logs to files and sinks](../how-to/send-logs-to-sinks.md) shows both
setups.
