# Redaction

Redaction runs in the native renderer before a record is rendered, queued, or
passed to the Sentry hook. It applies to JSON and console output alike.

| Setting | Default | Effect |
|---|---|---|
| `sensitive_keys` | `None` (use `DEFAULT_SENSITIVE_KEYS`) | Key-based redaction. A sequence replaces the default set. |
| `sensitive_patterns` | `None` | Pattern-based redaction. |
| `pattern_replacement` | `"[REDACTED]"` | Replacement text for pattern matches. |
| `allow_backtracking_patterns` | `False` | Compile patterns the linear-time engine rejects with a bounded backtracking engine. |

## Default sensitive keys

`structguru.DEFAULT_SENSITIVE_KEYS` (also `structguru.redaction.DEFAULT_SENSITIVE_KEYS`)
is a `frozenset` of 14 names:

`access_token`, `api_key`, `apikey`, `authorization`, `cookie`,
`credit_card`, `passwd`, `password`, `private_key`, `refresh_token`,
`secret`, `session_id`, `ssn`, `token`

Passing `sensitive_keys` replaces this set. To extend it, include it:

```python
from structguru import DEFAULT_SENSITIVE_KEYS, configure

configure(sensitive_keys=[*DEFAULT_SENSITIVE_KEYS, "internal_ref"])
```

## Key-based redaction

- A key matches when it equals a sensitive key, ignoring case (Unicode
  lower-casing for non-ASCII text). Partial matches do not count:
  `password_hint` does not match `password`.
- The value of a matching field is replaced by the string `[REDACTED]`, at any
  depth in nested mappings and in mappings inside lists. The key is kept.
  `pattern_replacement` does not apply to key-based redaction.
- Mapping keys also match as UTF-8 `bytes` up to 128 bytes, or as Enum members
  by member name or string value. UTF-8 bytes keys are decoded for output;
  Enum keys retain their serialized value. Invalid UTF-8 or longer bytes keys
  remain unsupported markers. Normalization collisions keep the last value.
- Naming `message`, `logger`, `service`, or `stack` in `sensitive_keys`
  replaces that whole text with `[REDACTED]`.
- In the message, a placeholder whose name, attribute, or index names a
  sensitive key (`{token}`, `{creds[password]}`, `{user.password}`) is
  interpolated as `[REDACTED]`; the value is not looked up.
- A mapping, sequence, set, dataclass, or namespace that a placeholder
  interpolates as its `repr()` renders with the values under sensitive keys
  replaced by `[REDACTED]`.
- With `structured_exceptions=True` and `exception_include_locals=True`,
  locals are redacted by the same keys; see [Exceptions](exceptions.md#locals).

## Pattern-based redaction

Every pattern in `sensitive_patterns` replaces all of its matches, in
declaration order: each pattern runs on the output of the previous one.
Patterns apply to:

| Target | Notes |
|---|---|
| Message | After formatting and key-based placeholder redaction. |
| `logger`, `service`, `stack` | The whole text. |
| Field keys | Top-level keys and keys of nested mappings. |
| String values | At any depth, including a text `exception` and the strings inside a structured `exception`. |
| Integer values | Matched against their decimal text, including integers outside the 64-bit range. A matching integer is written as the redacted string: `card=4111111111111111` with `r"\b\d{16}\b"` renders `"card":"[REDACTED]"`. |
| Exception locals | Each local's full rendering, before it is truncated to `exception_max_local_repr`. |

Floats, booleans, and `null` are never matched.

When redaction makes two keys of one mapping equal (two e-mail addresses both
becoming `[REDACTED]`), they collapse into one key at the first key's position
with the last value, so JSON output never repeats a key.

### Replacement syntax

`pattern_replacement` supports capture-group expansion: `$1` or `${1}` for a
numbered group, `${name}` for a named group, and `$$` for a literal `$`.

### Regular expression engines

| `allow_backtracking_patterns` | Engine | Unsupported pattern |
|---|---|---|
| `False` (default) | Rust `regex` crate: linear-time matching; no look-around or backreferences. | `ValueError` from `configure()` or `update()`; the active runtime is unchanged. |
| `True` | Patterns the linear engine accepts still use it. Other patterns use a backtracking engine (`fancy-regex`) with a backtrack limit, so look-around and backreferences work. | `ValueError` only for patterns neither engine can compile. |

When evaluating a backtracking pattern exceeds the backtrack limit, the entire
string being matched is replaced by `[REDACTED]`.

## What is not redacted

- The `timestamp`, `level`, and `severity` fields.
- Floats, booleans, and `null` (by patterns).
- The arguments of `metric_processor`: it runs before rendering and receives
  unredacted field values. Its `"event"` message has only placeholder
  redaction applied.
- Anything a `logger.add()` sink receives from the standard library root
  logger directly (raw stdlib records) while the stdlib bridge is not
  installed.

The Sentry hook receives field values and the message reconstructed from the
redacted JSON line; the raw exception object is passed only for
`capture_exception`. See [Sentry](integrations/sentry.md).

## Integrations that reuse the keys

The active sensitive keys (or the defaults) are also applied outside the
renderer:

| Integration | Behavior |
|---|---|
| [ASGI](integrations/asgi.md) `extract_headers` | Credential headers are bound as `[REDACTED]`. |
| [Celery](integrations/celery.md) context headers | Values under sensitive keys are sent to the broker as `[REDACTED]`. |

## Redaction marker

`structguru.redaction.REDACTED_MARKER_KEY` is `"_structguru_redacted"`. The
native Sentry hook adds it, set to `True`, to the event it passes to
`sentry_processor`; `SentryProcessor(require_redaction=True)` attaches the
event as Sentry extras only when the marker is present. A field with this name
is removed from rendered output, and `SentryProcessor` removes it from
breadcrumbs and extras.

## API

::: structguru.redaction
    options:
      heading_level: 3
      show_if_no_docstring: true

## See also

- [Redact sensitive data](../how-to/redact-sensitive-data.md)
- [Redaction design](../explanation/redaction.md)
