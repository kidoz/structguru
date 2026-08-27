# API Reference: Redaction

Redaction runs inside the native Rust renderer, before a record is rendered,
queued, or forwarded to Sentry. It covers both the message and the structured
fields, so a secret cannot escape through either one.

Two independent mechanisms apply:

- **Key-based redaction** replaces the *value* of any field whose key is
  considered sensitive, at any depth (nested maps and lists included).
- **Pattern-based redaction** rewrites substrings matching a regex, in the
  message and in every string value.

```python
from structguru import configure

configure(
    sensitive_keys=["password", "token", "ssn"],
    sensitive_patterns=[r"\b\d{3}-\d{2}-\d{4}\b"],
    pattern_replacement="***",
)
```

Passing `sensitive_keys` **replaces** the built-in set rather than extending
it. To add to the defaults, spread them:

```python
from structguru.redaction import DEFAULT_SENSITIVE_KEYS

configure(sensitive_keys=[*DEFAULT_SENSITIVE_KEYS, "internal_ref"])
```

## Writing `sensitive_patterns`

Patterns run on Rust's `regex` crate, which guarantees linear-time matching
(no ReDoS). It does not support backreferences or look-around, and an
unsupported pattern raises `ValueError` from `configure()` — at setup time,
not silently at log time.

### Put the capture group around the prefix, never the secret

`pattern_replacement` supports capture-group expansion (`$1`, `${name}`; `$$`
for a literal `$`). That is how a look-behind is rewritten for the linear
engine: capture the part you want to **keep** and re-emit it.

```python
# CORRECT — the group holds the prefix, so only the secret is replaced.
configure(
    sensitive_patterns=[r"(password=)\S+"],
    pattern_replacement="$1[REDACTED]",
)
# "login password=hunter2 ok"  →  "login password=[REDACTED] ok"
```

Inverting the group is the easy mistake, and it silently defeats the
redaction — the match is replaced by the secret it was supposed to remove:

```python
# WRONG — the group holds the secret, so $1 re-emits it verbatim.
configure(
    sensitive_patterns=[r"password=(\S+)"],
    pattern_replacement="$1[REDACTED]",
)
# "login password=hunter2 ok"  →  "login hunter2[REDACTED] ok"   # leaked
```

If a pattern uses a capture group, check the *output* of a real example
before shipping it. A pattern that matches correctly can still leak, because
the leak lives in the replacement, not the match.

### Patterns that cannot be rewritten

`allow_backtracking_patterns=True` opts the patterns the linear engine rejects
into a bounded backtracking engine, where look-around and backreferences work
as written:

```python
configure(
    sensitive_patterns=[r"(?<=password=)\S+"],
    allow_backtracking_patterns=True,
)
```

The linear-time guarantee no longer holds for those patterns. Evaluation is
capped by a backtrack limit, and a value whose evaluation exceeds that limit is
redacted **entirely** (fail-closed) rather than emitted unchecked. Patterns the
linear engine accepts still use it — only the exotic ones pay the cost.

## Ordering and the Sentry guard

Redaction completes before any downstream consumer sees the event. The native
hook marks the finished event with `REDACTED_MARKER_KEY`, and `SentryProcessor`
refuses to upload the event dict as Sentry extras unless that marker is present
— turning the ordering convention into a runtime guard rather than a comment.
Raw `exc_info` is retained only for `capture_exception`.

The marker is stripped from rendered output and from Sentry extras; it never
reaches a sink.

::: structguru.redaction
