# Compatibility with loguru and structlog

structguru takes its API from [loguru](https://github.com/Delgan/loguru) and
its output from the [structlog](https://www.structlog.org/) pipeline it grew
out of. This page explains what each inheritance means in practice and where
structguru deliberately departs from both. It is background for readers who
know one of those libraries, not a migration checklist; the
[Logger reference](../reference/logger.md) documents each method exactly.

## The loguru side: the API

The calling conventions follow loguru closely:

- A ready-made `logger` object with the level methods `trace`, `debug`,
  `info`, `success`, `warning`, `error`, `critical`, and `exception`, plus the
  aliases `warn` and `fatal`.
- Brace formatting with `str.format` rules:
  `logger.info("User {id} logged in", id=123)`, with positional and keyword
  arguments.
- A positional-only message, as in loguru, so `message=` and `self=` passed as
  keywords are ordinary fields.
- `bind()` returns a child logger with persistent fields, and
  `contextualize()` binds fields for a `with` block through `contextvars`, so
  every logger sees them.
- `opt(exception=..., stack_info=...)` returns a logger whose options apply to
  every call it makes, not only the next one.
- `catch()` works as a context manager and as a decorator for both regular and
  coroutine functions.
- `add()` returns an integer handler id. Ids are process-wide, and removing an
  unknown id raises `ValueError` (a non-integer raises `TypeError`), as loguru
  does.

Calls to the logging methods written for loguru therefore often run
unchanged. The differences are mostly in what surrounds those calls.

## Where the same names mean something narrower

**Consumed keywords leave the record.** A keyword that a placeholder uses is
removed from the fields, so `user_id` in
`logger.info("User {user_id} logged in", user_id=42, ip="10.0.0.1")` appears
in the message only, and `ip` becomes a field. loguru, by default, keeps every
keyword in the record's `extra`, including the ones the message used. In
structured output a value then appears once rather than twice.

**`trace()` and `success()` are aliases.** They log at DEBUG and INFO. The
output has no TRACE or SUCCESS level, because structguru's output uses the
five level names it inherited from the pre-1.0 pipeline: `DEBUG`, `INFO`,
`WARN`, `ERROR`, and `CRITICAL`. That is also why `logger.warning()` renders
`WARN` where loguru says `WARNING`.

**`opt()` and `catch()` take fewer options.** `opt()` accepts only `exception`
and `stack_info`. `catch()` accepts the exception types, `level`, `message`, and
`reraise`.

**Sinks receive the finished line.** `add()` takes a sink and a `level`, and
nothing else: there is no per-sink format, filter, serialization switch, or
rotation. Every sink receives the same rendered line, JSON or console as
chosen by `configure(format=...)`, ending in a newline. A sink can be a
callable, a file path, a text stream, or a `logging.Handler`, and all of them
are called off the logging thread through a bounded queue (see
[Delivery and lifecycle](delivery-and-lifecycle.md)). loguru calls its
handlers synchronously unless `enqueue=True` is set.

**There is no default handler to remove.** loguru starts with a text handler
on stderr, and a common first step is `logger.remove()` followed by
`logger.add(...)` with the format you want. structguru's primary output is the
native writer: JSON to stdout by default, configured with `configure()` rather
than `add()` and `remove()`. `remove()` without an id removes only the sinks
added through that logger and its `bind()` and `opt()` children, while loguru's
removes every handler. Output format and destination are `configure()`
options; [Configure logging](../how-to/configure-logging.md) shows them.

**Named loggers exist.** `Logger(name=__name__)` gives a logger with a fixed
`logger` field. The default `logger` fills that field with the caller's module
name on each call. Sinks stay process-global either way.

**Tracebacks are CPython's.** An exception renders as the text that
`traceback.format_exception` produces, or as a structured dictionary. It is not
annotated with variable values, as loguru's default `diagnose` output is.
Local variables appear only with `structured_exceptions=True` and
`exception_include_locals=True`, and they pass through redaction (see
[Redaction design](redaction.md)).

## JSON by default

loguru's default output is human-readable text for a terminal. structguru's
default is one JSON object per line on stdout, with `format="console"` for
development. The console format is structguru's own stable layout,
`<timestamp> [<LEVEL>] <message>  key=value`, colored when stdout is a
terminal. It is not a copy of loguru's or structlog's console output.

The choice reflects what the library is for: records that a collector parses,
from a process that writes its log stream to stdout and leaves files and
shipping to the platform. The JSON layout itself comes from the structlog
side.

## The structlog side: the output

structguru began as a Python wrapper around structlog. The facade formatted the
message, and a structlog processor chain enriched the record and rendered it
with `JSONRenderer` backed by orjson. Version 0.3 added an optional Rust
accelerator that had to reproduce that output byte for byte. Version 1.0 made
the Rust renderer the only path and removed structlog and orjson as
dependencies. `configure_structlog()` was removed in 1.0.4, and the public
processor-chain helpers went with 1.0.

The output still reflects the chain it replaced:

- **Key order.** User fields come first, then `logger`, `level`, `severity`,
  `timestamp`, `service`, `stack` (when present), and `message`. The message
  sits under `message` because the chain renamed structlog's `event` key at
  the end. `stack` sits between `service` and `message` because that is where
  `StackInfoRenderer` ran.
- **Collisions.** A user field named `logger`, `level`, `severity`,
  `timestamp`, or `message` is overwritten in place, because the processors
  assigned into the event dictionary. A user field named `service` wins,
  because the service was added only when absent.
- **Levels and severity.** Levels are normalized to `DEBUG`, `INFO`, `WARN`,
  `ERROR`, and `CRITICAL`, and every record carries its RFC 5424 severity: 7,
  6, 4, 3, and 2 respectively.
- **Context merge.** Context fields fill only keys the call did not set, the
  semantics of structlog's `merge_contextvars`, and they follow the call's
  fields in the order they were bound.
- **Context functions.** `bind_contextvars`, `bound_contextvars`,
  `clear_contextvars`, and `get_contextvars` keep the names of
  `structlog.contextvars`, which structguru's own module replaced.
- **Hooks.** `metric_processor` and `sentry_processor` are called like
  structlog processors, `(logger, method_name, event_dict)`, with the message
  under `"event"` as it was before the rename. `MetricProcessor` and
  `SentryProcessor` are processors of that kind.
- **Exceptions.** The formatted traceback is the text structlog's
  `format_exc_info` produced, and a structured exception has the shape of the
  former exception-dictionary processor.

## How the parity is guarded

Byte-for-byte compatibility is easy to lose in a rewrite, so it is tested. The
golden parity suite in `tests/test_parity_golden.py` renders each scenario
twice: through the native path, and through an independent reconstruction of
the pre-1.0 chain built from structlog's own processors and orjson. The two
JSON lines must be byte-identical after the timestamp is normalized. Comparing
bytes rather than parsed objects also locks key order and the formatting of
numbers and strings, and one representative line is pinned as a committed
string, so the reference and the renderer cannot drift together. structlog and
orjson remain test-only dependencies for this purpose.

For a while the suite compared the native renderer with itself, which proves
nothing; 1.4.0 restored the comparison against a real structlog and orjson
pipeline.

## Deliberate differences from the old pipeline

Some differences are intentional. The suite checks the first two as accepted
divergences; the others are behaviors the old pipeline did not have:

- **Exception placement.** The native renderer places `exception` with the
  user fields, before `logger`. The structlog chain appended it after
  `message`, because `format_exc_info` ran after the rename. The values match.
- **Stack content.** The captured stack ends at the caller's frame and skips
  structguru's own frames, the way structlog skipped its own. Placement is the
  same.
- **Values the old pipeline rejected.** Where orjson raised an error, the
  native renderer writes something useful and keeps the record: an integer
  beyond 64 bits as a JSON number, a non-string mapping key as a string, and
  any other unsupported object as a marker such as
  `<unsupported: PosixPath>`. NaN and infinity become `null`, as orjson wrote
  them.
- **Wider redaction.** Patterns also apply to field keys and to integers, and
  message placeholders that name a sensitive key are redacted. The old
  redaction processor applied patterns to string values only.
- **Console output**, as described above, is structguru's own format.

## Standard-library logging

Both loguru and structguru have to coexist with libraries that log through the
standard `logging` module, and they solve it differently. loguru's
documentation suggests an interception handler that forwards records into
loguru. structguru removed its own `InterceptHandler` in 1.0 and provides a
bridge instead, `install_stdlib_bridge()`, which renders third-party records
through the native path with the same JSON, redaction, and level filtering.
A bridged record keeps its own level number for thresholds and sampling, and
its creation time for `timestamp`.

A `logging.Handler` can also be passed to `add()`. It keeps its own level and
formatter, and its formatter wraps the rendered line. Sinks added with `add()`
additionally receive third-party records directly from the root logger, raw,
unless the bridge is installed, in which case they receive them once, rendered.
[Capture standard-library logging](../how-to/capture-stdlib-logging.md) covers
the setup.
