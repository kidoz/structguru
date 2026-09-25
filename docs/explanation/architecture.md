# Architecture

structguru is a thin Python facade over a runtime written in Rust. The facade
provides the loguru-style `Logger`. The runtime merges fields, redacts,
renders, queues, and writes each record. Since 1.0 the Rust runtime is the
only logging path: there is no pure-Python fallback, and the package has no
required runtime dependencies. This page follows one record through the
system and explains the choices that shape that path.

## The path of one record

```text
caller thread
  logger.info("order {id} accepted", id=42, user="u1")
   |
   |  Python facade
   |   1. read the runtime snapshot ............. logging disabled -> return
   |   2. compare the level with the threshold .. below it -> return
   |   3. brace-format the message; placeholders consume their keywords
   |   4. sampling and rate limiting ............ dropped -> return (counted)
   |   5. logger name; exception text or dict; stack text if requested
   |   6. only if configured: OpenTelemetry ids, metric_processor
   |
   |  one native call, using the RuntimeConfig compiled by configure()
   |   merge bound fields, keywords, and context -> convert values
   |   -> redact -> timestamp -> render JSON or console -> enqueue
   |
   +--> bounded writer queue --> writer thread (Rust) --> stdout, file, or both
   |
   |  only if configured, and only if the writer did not reject the record
   +--> stream_sink.write(line) ......................... on the caller thread
   +--> callable queue --> delivery thread --> callable and logger.add() sinks
   +--> sentry_processor(redacted event) ................ on the caller thread
```

## On the caller's thread

The Python steps run in order of increasing cost, and each runs only if the
record survived the step before it.

The runtime snapshot is read once, when the call begins (see
[One snapshot per call](#one-snapshot-per-call)). When logging is disabled,
after `shutdown()` or when import-time configuration was switched off and
`configure()` has not run yet, the call returns here.

The level check comes before any formatting. A debug call below the threshold
costs a dictionary lookup and a comparison.

Message formatting follows `str.format` rules. Keywords that a placeholder
names are consumed by the message; the remaining keywords become fields. A
placeholder that names a sensitive key is filled with `[REDACTED]` at this
stage, because the formatted message leaves Python as plain text and the
renderer can no longer tell which part of it came from which argument (see
[Redaction design](redaction.md)).

Sampling and rate limiting run next, on the formatted message. They come after
formatting because the rate limiter groups records by their formatted message,
and before everything else so that a record they drop costs no field merge,
no traceback formatting, and no rendering.

Then Python collects what only the interpreter can see: the caller's module
name for the `logger` field, the exception, and the stack for `stack_info`.
Exceptions are formatted with CPython's own `traceback` module, so the text is
what `traceback.format_exception` prints, or built into a structured
dictionary when `structured_exceptions=True`. The Rust renderer only
serializes what Python hands it.

Finally, the hooks. OpenTelemetry trace injection and `metric_processor` need
the merged fields as a Python mapping, so when either one (or a Sentry
processor) is configured, the fields are merged first by the same native merge
the renderer uses, enriched, and handed over whole. Without hooks, no Python
dictionary of merged fields is ever built.

## The native call

Everything else happens in a single call into the extension.

`configure()` compiles the per-record policy once: the service name, the
redaction keys, the compiled regex patterns, the output format and colors, and
the overflow policy. The result is a frozen native `RuntimeConfig`, and each
record passes a reference to it, so nothing about the configuration is
converted per record. Introduced in 1.3.0 together with the single native
call, this made a simple `logger.info()` about 30 percent faster.

The call then:

- merges the field sources: bound fields first, call keywords replacing a
  bound value in place, context fields only for keys that are still absent,
  and the exception last;
- converts Python values into the renderer's value model, natively for JSON
  scalars, mappings, sequences, `datetime`, `date`, `UUID`, `Enum`, and
  dataclasses, and as a type-only marker such as `<unsupported: PosixPath>`
  for anything else, so a field value never fails the record;
- redacts by key and by pattern;
- stamps the time and renders the line. In JSON, user fields come first,
  followed by `logger`, `level`, `severity`, `timestamp`, `service`, `stack`
  (when present), and `message`;
- pushes the line into the writer queue and reports one of three outcomes,
  observed under the queue lock: accepted, dropped because the queue is full
  (drop mode only), or rejected because the writer is closed.

In the common production shape (no stream sink, no Sentry processor, and no
callable sink registered) the rendered line goes from the renderer straight
into the queue and never becomes a Python string. When one of those
destinations exists, the same call also returns the line, and for Sentry the
redacted JSON record, to Python.

The [record format reference](../reference/record-format.md) lists the fields
and value conversions exactly.

## Three kinds of threads

**Caller threads** are the application's own threads. They do all the work
above. In block mode they also wait when the queue is full. The wait releases
the GIL, but an ordinary push does not: releasing the GIL around every push
forced a hand-off between threads on each record whenever another thread was
runnable, which cost more than the push itself.

**The writer thread** belongs to one writer and is pure Rust. It never needs
the interpreter. It writes each line to its destination: standard output,
the rotating file, both, or nowhere for `target="null"`. It flushes the sink
when the queue runs empty or when a `flush()` call is waiting for it. On Unix
the stdout destination writes each line straight to file descriptor 1,
without a buffer or a process-wide lock.

**The delivery thread** serves callable sinks, the functions passed as
`callable_sinks` and the sinks added with `logger.add()`. Those sinks are
Python code, and running them on the writer thread would make the writer
depend on the interpreter. They get their own bounded queue instead, and one
delivery thread at a time calls them in log order. That thread attaches to the
interpreter for a batch of records at a time rather than once per record.
While no callable sink is registered, this part of the path is skipped
entirely.

The extension declares that it does not rely on the GIL, so importing it on a
free-threaded CPython build does not switch the GIL back on.

## One snapshot per call

The active configuration is an immutable snapshot: the writer, the compiled
`RuntimeConfig`, the level threshold, the filters, the hooks, and the sink
settings. `configure()` and `update()` build a complete new snapshot and
publish it with one assignment. A logging call reads the current snapshot once
and uses it for every stage.

This has two consequences. A call never observes a half-applied
configuration, even while another thread reconfigures and even without a GIL.
And a call that began before a reconfiguration finishes against the snapshot
it started with. If that snapshot's writer has been closed in the meantime,
the record is rejected rather than silently moved to the new writer;
[Delivery and lifecycle](delivery-and-lifecycle.md) explains how that is
counted. A level-only change (`set_level()`, or `update(level=...)`) publishes
a snapshot that shares the existing writer, queue, and filters.

## Why native-only

structguru began as a pure-Python wrapper around structlog. Version 0.3 added
an optional Rust accelerator that had to produce output byte-identical to the
structlog path, and 1.0 made the Rust renderer the only path and dropped
structlog and orjson as dependencies. With two paths, every behavior existed
twice and had to be kept in agreement. With one, redaction guarantees,
lifecycle semantics, and performance work apply to every record.

The cost is a compiled extension. structguru ships abi3 wheels for Linux,
macOS, and Windows, and free-threaded CPython 3.14t wheels for Linux x86_64,
macOS arm64, and Windows x64. Anywhere else, installing from the source
distribution needs a Rust toolchain. Importing the package without its
extension raises `RuntimeError`, and an invalid import-time environment value
fails the import too. Both are deliberate: when the native runtime is the only
logging path, disabling it quietly would leave an application running with no
logs at all.

## Why the queue is bounded

A background writer keeps sink latency out of the logging call, but only
while the sink keeps up on average. When it falls behind, an unbounded queue
turns the backlog into memory growth, and a sink that stops completely turns
it into growth without limit. The writer queue therefore holds 8192 records by
default, and the default overflow policy blocks: callers slow down to the
sink's pace and nothing is lost to overflow. `overflow="drop"` trades
completeness for caller latency, with every drop counted, and `maxsize=0`
remains available as an explicit opt-in to an unbounded queue.
[Delivery and lifecycle](delivery-and-lifecycle.md) discusses the trade-off in
full, and [Handle backpressure](../how-to/handle-backpressure.md) shows how to
choose.

## Why filters run before rendering

Sampling and rate limiting exist to shed load, so they run before the
expensive work. A record they drop never reaches `metric_processor`, Sentry,
or any sink, and it is counted in `sampled` or `rate_limited`, separately from
transport drops, so the two kinds of loss stay distinguishable.

Keying the rate limiter on the formatted message has a consequence worth
knowing. A message that embeds an ID or a counter produces a new key for
every record, so it is effectively never limited. The limiter's table holds at
most 8192 keys and, when full, evicts the keys seen least recently, so such
messages cannot grow memory either, while keys that are being limited right
now stay. `sample_max_level` limits sampling to
records at or below a level, so more severe records always pass. See
[Sample and rate-limit logs](../how-to/sample-and-rate-limit.md).

## Why there is no processor chain

Before 1.0, records went through a structlog processor chain, and a processor
could be inserted anywhere in it. In 1.0 the public processor-chain helpers
were removed. The native runtime now owns level normalization, RFC 5424
severity, redaction, sampling, rate limiting, exception rendering, metric
hooks, and OpenTelemetry injection, and `configure()` options are the whole
processing API.

Three properties of the design depend on that:

- **One native call.** A user-supplied step between the merge and the render
  would need the record as a Python dictionary at that point, on every call.
  That is exactly the cost the single call avoids.
- **A fixed order.** Nothing can run between redaction and output, so every
  destination receives the redacted line. A reorderable chain cannot promise
  that.
- **Hooks observe; they do not transform.** `metric_processor` (before
  rendering, for kept records) and `sentry_processor` (after rendering, with
  the redacted event) keep structlog's `(logger, method_name, event_dict)`
  call signature, so structlog-style processors fit. But each receives a new
  top-level dictionary and its return value is discarded, so a hook is not a
  way to change the record.

The trade-off is that you cannot add a custom field transformation or a new
output format from Python. What remains is enough for most needs: fields come
from `bind()`, call keywords, and context; `format=` selects JSON or console
output; and callable sinks receive the finished line and can forward it
anywhere.

## Standard-library records take the same path

The [standard-library bridge](../how-to/capture-stdlib-logging.md) does not
have a renderer of its own. It re-emits each `logging.LogRecord` through the
facade, passing the record's own level number and creation time. Level
thresholds, sampling, and routing to callable sinks use that number, and the
`timestamp` field uses that time, so a bridged record receives the same
formatting, redaction, queueing, and lifecycle guarantees as a native one.

## Related pages

- [Configuration and runtime control](../reference/configuration.md) documents
  every option this page mentions.
- [Delivery and lifecycle](delivery-and-lifecycle.md) continues where the
  queue begins.
- [Redaction design](redaction.md) looks closely at the redaction step.
