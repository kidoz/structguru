# Redaction design

Logs end up in more places, and in front of more people, than the data they
describe. structguru therefore redacts sensitive values itself, before a
record goes anywhere. This page explains where redaction happens, why it is
split into two mechanisms, why patterns run on a restricted regex engine, and
where the protection ends. The exact list of what each mechanism covers is in
the [redaction reference](../reference/redaction.md), and the steps to
configure it are in [Redact sensitive data](../how-to/redact-sensitive-data.md).

## One place, before every consumer

Redaction runs inside the native call that renders a record, after the fields
are merged and converted and before the line is rendered. Everything
downstream receives the redacted line: the writer queue and its stdout and
file destinations, the synchronous stream sink, callable sinks and
`logger.add()` sinks, and the Sentry hook. Records captured from the standard
library through the bridge take the same path.

Doing it once, in the renderer, rather than in each destination has three
effects. There is one implementation and one configuration, compiled when
`configure()` runs. No destination can forget to redact. And because there is
no processor chain, nothing can be inserted between redaction and output (see
[Architecture](architecture.md)). The timestamp, the level, and the numeric
severity are the only parts of a record that are never redacted.

The guarantee covers records that pass through the renderer. A sink added with
`logger.add()` is also attached to the standard library's root logger, and
without the [stdlib bridge](../how-to/capture-stdlib-logging.md) third-party
records reach it on that path raw: unrendered and unredacted. Installing the
bridge routes them through the renderer instead.

## Keys and patterns

Secrets show up in two shapes, so there are two independent mechanisms.

**Key-based redaction** handles structured data, where a field's name says
what it holds: `password`, `token`, `authorization`. It replaces the value of
every field whose key is sensitive, at any depth in nested mappings and lists,
and keeps the key, so the record still shows that the field was present. Keys
match case-insensitively, including Unicode case variants. It is cheap, a
string comparison per key, and it cannot misfire on text that merely looks
secret. Naming `message`, `logger`, `service`, or `stack` as a sensitive key
redacts that whole text. Passing `sensitive_keys` replaces the fourteen
built-in keys rather than adding to them; `DEFAULT_SENSITIVE_KEYS` is exported
so a configuration can extend the defaults explicitly.

**Pattern-based redaction** handles secrets inside free text: an e-mail address
in a message, a token in a URL, a card number. Each pattern rewrites every
match, in declaration order, in the message, the logger name, the service
name, the stack text, every field key, every string value at any depth
(including a formatted traceback and the strings of a structured exception),
and every integer by its decimal digits.

Two of those targets need a reason:

- **Keys** are matched because a secret used as a mapping key, such as an
  e-mail address mapped to a count, must not survive where the same text as a
  value would not. When redaction makes two keys of one mapping equal, they
  collapse into one entry that keeps the first key's position and the last
  value, so JSON output never repeats a key.
- **Integers** are matched because a card number passed as an `int` would
  otherwise pass as a number. A matching integer is written as the redacted
  string. Floats, booleans, and `None` are never matched.

Keys and patterns also meet in one place before rendering: a message
placeholder that names a sensitive key. That case is covered
[below](#message-placeholders).

## Why patterns run on a linear-time engine

Patterns run on every string of every record, on the caller's thread, and much
of that text comes from outside the application: headers, query strings, user
input. A backtracking regex engine can take exponential time on some
combinations of pattern and input. One crafted value could then stall every
logging call in the process, which is a denial of service delivered through
the logs.

Patterns therefore run on Rust's `regex` crate, which guarantees matching in
time linear in the input. The price is that it does not support look-around or
backreferences. A pattern that uses them raises `ValueError` from
`configure()`, at startup, rather than being skipped at log time. Before 1.0
an unsupported pattern produced a warning and left the native renderer off;
the reasoning for the change was that redaction that silently differs from
the configuration is worse than an error at setup.

Most look-behinds can be rewritten for the linear engine. Instead of asserting
the prefix, capture it and emit it again through `pattern_replacement`:
`(password=)\S+` with `$1[REDACTED]`. The trap is capturing the secret instead
of the prefix, `password=(\S+)` with the same replacement, which re-emits the
secret it was meant to hide. The match is correct, so nothing fails; the leak
is in the replacement. The project's own documentation recommended the wrong
form until 1.2.0, which is why the guidance is to check a pattern's output on
a real example, not just whether it matches.

## The backtracking opt-in fails closed

Some patterns cannot be rewritten. `allow_backtracking_patterns=True` routes
exactly those, the patterns the linear engine rejects, to a bounded
backtracking engine where look-around and backreferences work as written.
Patterns the linear engine accepts keep using it, so only the exotic ones pay
the cost and give up the linear-time guarantee.

The bound is a limit of one million backtracking steps per evaluation. When a
value exceeds it, the pattern has not been checked against that value, and
there are two options: write the value unchecked, or give it up. structguru
gives it up: the whole string becomes `[REDACTED]`. Losing a value in a
pathological case is preferable to writing text that the configured pattern
could not vouch for.

## Hooks see the record as it was logged

`metric_processor` runs on the caller's thread before the native call, so it
receives the fields unredacted. The message it receives has already been
formatted, so a placeholder naming a sensitive key already reads
`[REDACTED]`, but no key or pattern redaction has been applied to it or to the
fields.

This follows from where the hook sits: metrics are computed from the record as
the application logged it, and the dictionary the hook receives never reaches
a sink. It also means a metric processor is trusted code. A value it copies
into a metric name or label leaves structguru without passing through
redaction.

## Sentry receives the redacted event

The Sentry hook runs after rendering, and the event it receives is rebuilt
from the redacted JSON record the renderer produced (in console mode, from a
separate JSON render of the same record). Every field value and the message
are therefore the redacted ones.

The hook also marks the event with `REDACTED_MARKER_KEY`. `SentryProcessor`,
with its default `require_redaction=True`, refuses to attach the event as
Sentry extras unless that marker is present. That turns an ordering
convention, "redact before exporting", into a check made at run time. The
marker is removed from Sentry extras and from rendered output, and a user field
with the same name never reaches a sink.

The one raw object that reaches Sentry is the exception. The hook passes the
original `exc_info` so that `SentryProcessor` can call `capture_exception`,
and it is used only for that. Breadcrumbs, extras, and tags never carry it:
the SDK serializes arbitrary objects, and before 1.2.1 an exception message
containing a secret reached Sentry that way. What the SDK itself extracts from
the exception passed to `capture_exception`, such as the message and frames,
is outside structguru's redaction and is governed by the SDK's own settings.
[Report errors to Sentry](../how-to/report-errors-to-sentry.md) covers the
setup.

## Field values are never stringified

When the renderer meets a field value it does not recognize, it writes a marker
that names the type, such as `<unsupported: WSGIRequest>`. It never calls
`str()` or `repr()` on the object and never reads its attributes. The values it
does recognize are JSON scalars, strings, mappings, lists and tuples,
`datetime`, `date`, and `time` values, `UUID`, `Enum` members, dataclasses, and
subclasses of `int` and `float`.

There are two reasons. An object's `repr()` is written by its author for
debugging, not for logs: a request object's text can include headers, cookies,
and bodies, and redaction can only catch what its keys and patterns
anticipate. And reading an attribute can run code. Django's `LazyObject`
evaluates itself and an ORM proxy may query the database, so every check the
converter makes looks at the object's type, never at the instance.

A marker is an ordinary string, so patterns apply to it too. A dataclass is
converted field by field into a mapping, so key-based redaction reaches inside
it. And a conversion that raises produces the marker instead of losing the
record; only `KeyboardInterrupt` and `SystemExit` propagate.

## Message placeholders

The message is formatted in Python before the renderer sees it, and after
formatting it is plain text. Key-based redaction can no longer tell which part
of the text came from which argument. Formatting therefore does its own
redaction:

- A placeholder that names a sensitive key, as the root (`{token}`) or
  anywhere along an attribute or index path (`{creds[password]}`,
  `{user.password}`), is filled with `[REDACTED]`. The value behind it is
  never looked up.
- A placeholder that interpolates a container by its `repr()`, meaning a
  mapping, sequence, set, dataclass with its generated `__repr__`, or
  namespace, renders with the values under sensitive keys redacted, as
  exception locals do.

The redacting formatter is several times slower than `str.format`, so it runs
only when a template names a sensitive key or when the plainly formatted text
could contain one. The protection has limits. An object with its own
`__str__` or `__format__` is formatted by its own code, and a plain string
interpolated under a harmless name, such as `"{v}"` with
`v="password=hunter2"`, is just text. Only `sensitive_patterns` see either of
them. The warnings that structguru issues when a template fails to format
include neither the template nor the calling source line, because warnings
bypass the renderer.

## Exception locals

With `structured_exceptions=True` and `exception_include_locals=True`, each
frame of a structured exception includes its local variables. This is the one
place where structguru renders arbitrary objects with `repr()`, because a
local's value is only useful as its representation. The protections are
layered:

- A local whose name is a sensitive key renders as `[REDACTED]`.
- Mappings, lists, tuples, named tuples, sets, dataclasses with their generated
  `__repr__`, `types.SimpleNamespace`, and `argparse.Namespace` are walked
  instead of asked for their `repr()`, so a value under a sensitive key is
  redacted at any depth. Keys match as strings, as UTF-8 `bytes`, or as `Enum`
  members by name or string value, and a `(key, value)` pair inside a list
  counts as a keyed value. The walk decides by type alone, is cycle-safe, and
  is bounded in depth and in items.
- `sensitive_patterns` see each local's whole rendering before it is cut to
  `exception_max_local_repr` characters, so a secret that straddles the cut is
  still matched. If applying the patterns fails, the whole local is redacted.

The limit is any other object's own `repr()`. A class whose `__repr__` prints a
secret under a harmless name is covered only by patterns. A dataclass with a
`__repr__` written in its class body is not walked either, because such a
method may hide fields on purpose. structguru's own `Logger` keeps its bound
values out of its `repr()`, so a logger held in a local does not expose them.

The default formatted traceback is a single string field, so patterns cover
it, including the exception messages and source lines it contains.
[Log exceptions](../how-to/log-exceptions.md) discusses when to enable locals.

## Beyond the renderer

A few integrations hand data to something other than a log record, and they
redact by key on their own:

- Celery context propagation replaces sensitive keys before the context is
  written into message headers, so the values never reach the broker.
- The ASGI middleware binds credential-bearing request headers, such as
  `authorization`, `cookie`, and any header whose name contains a sensitive
  key, as `[REDACTED]`.
- The httpx and requests integrations remove credentials and query strings
  from the URLs they log.

The [integration reference pages](../reference/index.md) describe each one.
