# Exceptions

A record carries an `exception` field when the logging call has exception
information: `logger.exception()`, `exc_info=` on a level method,
`logger.opt(exception=...)`, `logger.catch()`, or a bridged stdlib record with
`exc_info`. The value is text by default and an object with
`structured_exceptions=True`. Frame walking, locals, and `repr()` run in
Python; the native renderer serializes the result and applies redaction.

## Settings

| Setting | Default | Applies to | Effect |
|---|---|---|---|
| `structured_exceptions` | `False` | both | `False`: text; `True`: object. |
| `exception_carets` | `True` | text | `False` omits the PEP 657 position-marker lines (`~~~^^^`), matching CPython under `PYTHONNODEBUGRANGES=1`. |
| `exception_max_frames` | `20` | object | Number of innermost frames kept per exception. `0` gives an empty `frames` list. |
| `exception_include_locals` | `False` | object | Add a `locals` mapping to each frame. |
| `exception_max_local_repr` | `200` | object | Maximum characters of each rendered local. |

Text output is `traceback.format_exception()` for the exception, joined and
stripped of its trailing newline; it includes chained exceptions and exception
group members.

## Structured schema

### Exception object

| Key | Type | Present | Value |
|---|---|---|---|
| `type` | string | always | `__qualname__` of the exception class. |
| `message` | string | always | `str(exception)`; `<str failed: ErrorType>` when `str()` raises. |
| `module` | string | always | `__module__` of the exception class (`builtins` for built-in exceptions). |
| `frames` | array | always | Frame objects, outermost first, limited to the last `exception_max_frames`. |
| `cause` | object | when chained | `{"type": ..., "message": ...}` of `__cause__`, or of `__context__` when there is no cause and context is not suppressed. |
| `exceptions` | array | exception groups | Exception objects for the group's members, each with the same keys. |
| `exceptions_truncated` | integer | when members were omitted | Number of direct members left out by the limits below. |

### Frame object

| Key | Type | Value |
|---|---|---|
| `filename` | string | Source file path. |
| `lineno` | integer | Line number. |
| `name` | string | Function name. |
| `line` | string | Stripped source line text; empty when the source is unavailable. |
| `locals` | object | Present with `exception_include_locals=True`: local name to rendered text. |

### Limits

| Limit | Value |
|---|---|
| Group nesting | 10 levels; deeper groups have an empty `exceptions` list. |
| Exception objects per record | 100, counting the top-level exception and all group members. |
| Frames per exception | `exception_max_frames` (innermost frames kept). |
| Characters per local | `exception_max_local_repr`, then `...<N more>` (`...<N+ more>` when rendering stopped early). |
| Container nesting inside a local | 8 levels; deeper containers, and a container that contains itself, render as `...`. |
| Items per container inside a local | 100; further items render as a single `...`. |

## Locals

With `exception_include_locals=True`:

- A local whose name matches a sensitive key, ignoring case, renders as
  `[REDACTED]`.
- Mappings, lists, tuples, named tuples, sets, dataclasses with their
  generated `__repr__`, `types.SimpleNamespace`, and `argparse.Namespace`
  objects are walked rather than asked for their `repr()`; a value under a
  sensitive key renders as `[REDACTED]` at any depth. A key matches as a
  string, as UTF-8 `bytes`, or as an `Enum` member by name or string value,
  and a `(key, value)` pair inside a walked sequence counts as a keyed value.
- Any other object renders with its own `repr()`; `<repr failed: ErrorType>`
  replaces a `repr()` that raises.
- `sensitive_patterns` apply to each local's full rendering before it is
  truncated. A pattern that fails during evaluation causes the whole local to
  render as `[REDACTED]`.
- Sensitive keys are the configured `sensitive_keys`, or
  `DEFAULT_SENSITIVE_KEYS`.

## API

`build_exception_dict()` produces the structured object. The runtime calls it
with the `exception_*` settings, the active sensitive keys, and the compiled
patterns.

::: structguru.exceptions
    options:
      heading_level: 3

## See also

- [Log exceptions](../how-to/log-exceptions.md)
- [Record format](record-format.md#exception-field)
- [Redaction](redaction.md)
