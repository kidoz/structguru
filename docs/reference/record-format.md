# Record format

Every record passes through the native Rust renderer, which produces one line
in the format selected by `format` (`"json"`, the default, or `"console"`).
This page describes both layouts, the level names and numbers, the conversion
of Python values, and the `stack` and `exception` fields.

## JSON layout

A JSON record is one compact JSON object followed by a newline:

```python
from structguru import Logger, configure

configure(service="checkout")
log = Logger(name="app.orders").bind(order_id=42)
log.warning("Payment retry {attempt}", attempt=2, gateway="stripe")
```

```json
{"order_id":42,"gateway":"stripe","logger":"app.orders","level":"WARN","severity":4,"timestamp":"2026-09-25T08:28:11.465630Z","service":"checkout","message":"Payment retry 2"}
```

### Key order

Keys appear in this order:

1. User fields, in merge order:
    1. fields bound with `bind()`;
    2. keyword arguments of the call that were not consumed by message
       formatting (a keyword with the name of a bound field replaces that
       field's value in place);
    3. context variables (`contextualize()`, `bind_contextvars()`) whose keys
       are not already present;
    4. `trace_id`, `span_id`, and `trace_flags` when `otel=True` and a span is
       active (a user field with the same name is replaced in place);
    5. `exception`, when the call carries exception information.
2. The standard keys `logger`, `level`, `severity`, `timestamp`, `service`,
   `stack` (only when stack information is rendered), and `message`.

A standard key whose name is already used by a user field does not move: the
user field keeps its position and the standard value replaces it, except for
`service`, where the user value is kept.

### Standard keys

| Key | JSON type | Value | Same-named user field |
|---|---|---|---|
| `logger` | string | The logger name: `Logger(name=...)`, otherwise the `__name__` of the calling module; for bridged stdlib records, the stdlib logger name. Redacted like other text. | Replaced in place |
| `level` | string | Canonical level name: `DEBUG`, `INFO`, `WARN`, `ERROR`, or `CRITICAL` (see [Levels](#levels)). Never redacted. | Replaced in place |
| `severity` | integer | RFC 5424 severity of `level`. Never redacted. | Replaced in place |
| `timestamp` | string | UTC time in ISO 8601 with microseconds and a `Z` suffix, for example `2026-09-25T08:28:11.465630Z`. The time of the call; for bridged stdlib records, the record's `created` time. Never redacted. | Replaced in place |
| `service` | string | `Settings.service` (default `"app"`). Redacted like other text. | User value kept |
| `stack` | string | Present only for calls with `stack_info` (see [Stack field](#stack-field)). | Replaced in place when a stack is rendered |
| `message` | string | The formatted message (see [Logger](logger.md#message-formatting)). | Replaced in place |
| `exception` | string or object | Present only for calls with exception information (see [Exception field](#exception-field)). | Replaced in place |

A field named `_structguru_redacted` (`structguru.redaction.REDACTED_MARKER_KEY`)
is removed from the output.

## Console layout

With `format="console"`, a record renders as:

```text
<timestamp> [<LEVEL>] <message>  <key>=<value>  <key>=<value>
```

The example above renders as:

```text
2026-09-25T08:28:11.465726Z [WARN    ] Payment retry 2  order_id=42  gateway="stripe"
```

| Element | Rule |
|---|---|
| Timestamp | Same text as the JSON `timestamp` field. |
| Level | Canonical level name, left-aligned and padded to eight characters inside the brackets (`[INFO    ]`, `[CRITICAL]`). |
| Fields | Two spaces before each `key=value` pair, in JSON key order. User fields named `logger`, `level`, `severity`, `timestamp`, or `message` are omitted; a user field named `stack` is omitted when a stack is rendered. |
| Not shown | `logger`, `severity`, and the configured `service`. A user field named `service` is shown as an ordinary field. |
| Stack | When present, follows the line as a block; every line is indented by two spaces. |
| Exception | A formatted (string) `exception` follows the stack as an indented block. A structured (dictionary) `exception` renders as an ordinary `exception={...}` field. |

### Console values

| Value | Rendering |
|---|---|
| String | Double-quoted; `"` and `\` are escaped (`"a\"b\\c"`). |
| Integer | Decimal digits. |
| Float | The JSON renderer's text (`1.0`, `1e+300`); NaN and infinities render as `None`. |
| `true`, `false`, `null` | `True`, `False`, `None`. |
| List | `[1, "x"]` |
| Mapping | `{key:value, key2:value2}` |

A key is written in double quotes when it is empty or contains whitespace,
`=`, `"`, `\`, or a character from the escaped set below; inside a nested
mapping, `:`, `,`, `{`, and `}` also cause quoting.

### Escaping

In every part of a console line (message, keys, values, stack, and exception
lines), C0 and C1 control characters, U+2028, U+2029, and the bidirectional
controls U+061C, U+200E, U+200F, U+202A–U+202E, and U+2066–U+2069 are escaped
as `\n`, `\r`, `\t`, `\xNN` (code points up to U+00FF), or `\uNNNN`. In the
message, stack, and exception text, backslashes are written as is.

### Colors

`colors=None` (the default) enables colors when `sys.stdout.isatty()` is true
at configuration time; `True` and `False` force them on or off. JSON output
ignores `colors`. When colors are enabled, the bracketed level is wrapped in an
ANSI sequence and followed by the reset sequence `ESC[0m`:

| Level | Sequence |
|---|---|
| `DEBUG` | `ESC[2m` (dim) |
| `INFO` | none |
| `WARN` | `ESC[33m` (yellow) |
| `ERROR` | `ESC[31m` (red) |
| `CRITICAL` | `ESC[1;31m` (bold red) |

## Levels

The level table is defined once, in the native core. Level names are
case-insensitive everywhere. `configure(level=...)`, `update(level=...)`,
`set_level()`, and `logger.add(level=...)` accept a name from this table or a
integer between `0` and `2**63 - 1`; `sample_max_level` and `logger.catch(level=...)` accept
names except `NOTSET`; the stdlib bridge's `level` and `suppress_level`
accept names only.

| Name | Number | Logger method | Rendered `level` | `severity` (RFC 5424) |
|---|---|---|---|---|
| `NOTSET` | 0 | none (threshold only) | none | none |
| `TRACE` | 5 | `trace()` emits at `DEBUG` (10) | `DEBUG` | 7 (debug) |
| `DEBUG` | 10 | `debug()` | `DEBUG` | 7 (debug) |
| `INFO` | 20 | `info()` | `INFO` | 6 (informational) |
| `SUCCESS` | 20 | `success()` emits at `INFO` | `INFO` | 6 (informational) |
| `WARNING` | 30 | `warning()` | `WARN` | 4 (warning) |
| `WARN` | 30 | `warn()` | `WARN` | 4 (warning) |
| `ERROR` | 40 | `error()` | `ERROR` | 3 (error) |
| `EXCEPTION` | 40 | `exception()` emits at `ERROR` | `ERROR` | 3 (error) |
| `CRITICAL` | 50 | `critical()` | `CRITICAL` | 2 (critical) |
| `FATAL` | 50 | `fatal()` | `CRITICAL` | 2 (critical) |

`TRACE` (5) is a threshold between `NOTSET` and `DEBUG`; `logger.trace()`
itself emits at `DEBUG`, so the number 5 only affects bridged stdlib records
with levels 5–9.

### Stdlib record levels

A record received through the [stdlib bridge](integrations/stdlib.md) keeps
its own `levelno` for the level threshold, sampling, and `logger.add()` sink
levels, and renders with the level of the highest band it reaches:

| `levelno` | Rendered `level` |
|---|---|
| 50 and above | `CRITICAL` |
| 40–49 | `ERROR` |
| 30–39 | `WARN` |
| 20–29 | `INFO` |
| below 20 | `DEBUG` |

## Value conversion

Field values (bound fields, call keywords, context variables, and `extra=`
fields of bridged stdlib records) are converted natively. Conversion never
fails a record.

| Python value | JSON output |
|---|---|
| `None` | `null` |
| `bool` | `true` or `false` |
| `int` and subclasses | number; integers outside the 64-bit range are written as JSON numbers with all digits |
| `float` and subclasses (`numpy.float64`) | number; NaN and infinities become `null` |
| `str` and subclasses | string; an unpaired surrogate becomes U+FFFD, and a surrogate pair split across two code points becomes the character it encodes |
| `dict` and subclasses | object (see [Mapping keys](#mapping-keys)) |
| `list`, `tuple`, named tuples | array |
| objects whose type defines `isoformat()` (`datetime`, `date`, `time`) | the `isoformat()` string |
| `uuid.UUID` | canonical string form |
| `Enum` member | its `value`, converted by these rules (an `IntEnum` member becomes a number) |
| dataclass instance | object of its fields, in field order |
| any other object (`set`, `frozenset`, `bytes`, `bytearray`, `Decimal`, `timedelta`, `Path`, `complex`, `SimpleNamespace`, framework objects) | the string `<unsupported: TypeName>` |
| a container that contains itself | the string `<cycle: TypeName>` |
| a value nested deeper than 64 levels | the string `<max depth exceeded>` |

Markers name the type only. The renderer does not call `str()` or `repr()`
on an unsupported object or read its attributes. A conversion that raises an
`Exception` (a failing `isoformat()`, a dataclass field whose access raises)
produces the `<unsupported: TypeName>` marker; `BaseException` subclasses
that are not `Exception` subclasses propagate. Markers are redacted like any
other string.

### Mapping keys

String keys are used as they are. Any other key is converted by the value
rules: a string result is used directly, a scalar result becomes its JSON
text (`{200: 3}` renders as `{"200":3}`, `None` as `"null"`, `True` as
`"true"`, `1.5` as `"1.5"`), and a container result (a tuple, a dataclass)
becomes `<unsupported: TypeName>`. When keys of one mapping render to the same
text (`1` and `"1"`), they collapse into one key at the first key's position
with the last value.

## Stack field

`stack` is present when a call passes `stack_info=True` or uses
`logger.opt(stack_info=True)`. Its text is `Stack (most recent call last):`
followed by the output of `traceback.print_stack()` for the calling frame,
without a trailing newline. Frames of the `structguru` package are skipped, so
the stack ends at the frame that called the logger. Bridged stdlib records use
the record's own `stack_info` text.

## Exception field

`exception` is present when a call carries exception information:
`logger.exception()`, `exc_info=` on any level method, `logger.opt(exception=...)`,
or `logger.catch()`. Accepted values are `True` (the exception being handled),
an exception instance, or a `(type, value, traceback)` tuple. A value that does
not resolve to an exception adds no field.

| Setting | `exception` value |
|---|---|
| `structured_exceptions=False` (default) | String: the `traceback.format_exception()` text without its trailing newline. With `exception_carets=False`, the PEP 657 position-marker lines are omitted, matching CPython under `PYTHONNODEBUGRANGES=1`. |
| `structured_exceptions=True` | Object with `type`, `message`, `module`, `frames`, and optional `cause`, `exceptions`, and `exceptions_truncated` keys; see [Exceptions](exceptions.md). |

## See also

- [Exceptions](exceptions.md) for the structured exception schema and limits.
- [Redaction](redaction.md) for what is redacted in each field.
- [Log exceptions](../how-to/log-exceptions.md)
- [Architecture](../explanation/architecture.md)
