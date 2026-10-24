# Redact sensitive data

structguru masks values under sensitive keys such as `password` and `token` out
of the box. Use this guide to change which keys are masked, mask values by
pattern, and check that a secret really stays out of the output.

Redaction runs in the native renderer before a record is written, queued, or
sent to Sentry, and applies to both JSON and console output.

## Rely on the default keys

With no configuration, fields named like one of `DEFAULT_SENSITIVE_KEYS`
(case-insensitive, at any depth) are replaced by `[REDACTED]`, and so are
message placeholders with those names:

```python
from structguru import logger

logger.info("login", user="alice", password="hunter2", headers={"Authorization": "Bearer x"})
logger.info("login token={token}", token="abc123")
logger.info("payload {}", {"user": "alice", "api_key": "k-1"})
```

```json
{"user":"alice","password":"[REDACTED]","headers":{"Authorization":"[REDACTED]"},"logger":"__main__","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"login"}
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"login token=[REDACTED]"}
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"payload {'user': 'alice', 'api_key': [REDACTED]}"}
```

The default keys are `password`, `passwd`, `secret`, `token`, `api_key`,
`apikey`, `access_token`, `refresh_token`, `authorization`, `cookie`,
`session_id`, `credit_card`, `ssn`, and `private_key`.

## Add your own keys

`sensitive_keys` **replaces** the default set. To add to it, spread the
defaults:

```python
from structguru import DEFAULT_SENSITIVE_KEYS, configure

configure(sensitive_keys=[*DEFAULT_SENSITIVE_KEYS, "internal_ref"])
```

`configure(sensitive_keys=["ssn"])` alone would stop masking `password`.

## Mask values by pattern

`sensitive_patterns` rewrites every match of a regular expression in the
message, field names, and string values at any depth, and in integers by their
decimal digits:

```python
from structguru import configure, logger

configure(
    sensitive_patterns=[r"\b\d{3}-\d{2}-\d{4}\b"],
    pattern_replacement="***",  # default "[REDACTED]"
)
logger.info("SSN 123-45-6789 on file", note="ssn 987-65-4321")
```

```json
{"note":"ssn ***","logger":"__main__","level":"INFO","severity":6,"timestamp":"…","service":"app","message":"SSN *** on file"}
```

Patterns apply in the order given. A matching integer is emitted as the
redacted string: with `r"\b\d{16}\b"`, `card=4111111111111111` renders
`"card":"[REDACTED]"`.

Patterns run on Rust's linear-time regex engine, so they cannot cause
catastrophic backtracking. The engine does not support look-around or
backreferences: such a pattern raises `ValueError` from `configure()` or
`update()`, at setup time rather than at log time.

## Keep a prefix with a capture group

`pattern_replacement` expands capture groups (`$1`, `${name}`; `$$` for a
literal `$`). Use a group to re-emit the part you want to **keep**, which is also
how you rewrite a look-behind:

```python
# CORRECT: the group holds the prefix, so only the secret is replaced.
configure(
    sensitive_patterns=[r"(password=)\S+"],
    pattern_replacement="$1[REDACTED]",
)
# "login password=hunter2 ok"  ->  "login password=[REDACTED] ok"
```

Put the group around the prefix, never around the secret. Inverting it silently
defeats the redaction, because the replacement re-emits the secret it was
supposed to remove:

```python
# WRONG: the group holds the secret, so $1 re-emits it verbatim.
configure(
    sensitive_patterns=[r"password=(\S+)"],
    pattern_replacement="$1[REDACTED]",
)
# "login password=hunter2 ok"  ->  "login hunter2[REDACTED] ok"   # leaked
```

A pattern that matches correctly can still leak, because the leak is in the
replacement. When a pattern uses a capture group, check the output of a real
example before shipping it (see [Check the result](#check-the-result)).

## Use look-around or backreferences

If a pattern cannot be rewritten with a capture group, opt it into a bounded
backtracking engine:

```python
configure(
    sensitive_patterns=[r"(?<=password=)\S+"],
    allow_backtracking_patterns=True,
)
# "login password=hunter2 ok"  ->  "login password=[REDACTED] ok"
```

Only the patterns the linear engine rejects use the backtracking engine, and
they lose the linear-time guarantee. Their evaluation is capped: a value that
exceeds the backtrack limit is redacted entirely rather than emitted unchecked.
Prefer the capture-group rewrite where you can.

## Cover values interpolated into the message

A placeholder that names a sensitive key, as the root (`{token}`) or as an
attribute or index (`{creds[password]}`, `{user.password}`), is interpolated as
`[REDACTED]`. A mapping, sequence, set, dataclass, or namespace interpolated
whole renders with the values under sensitive keys redacted.

An object with its own `__str__` interpolated whole is covered by
`sensitive_patterns` only:

```python
logger.info("creds {}", creds)  # Creds.__str__ output: only patterns apply
```

Pass such values as fields, or add a pattern for what their text contains.

## Cover exception locals

With `structured_exceptions=True` and `exception_include_locals=True`, a local
variable named like a sensitive key renders as `[REDACTED]`, and so does any
value under a sensitive key inside mappings, sequences, sets, dataclasses, and
namespaces. Other objects' `repr()` text is covered by `sensitive_patterns`. See
[Log exceptions](log-exceptions.md).

Local patterns scan at most `exception_max_local_repr + 4096` rendered
characters before the display cut, with depth and item limits also applying.
A match requiring context beyond this prefix can be missed; use sensitive
keys for known secret fields.

## Check the result

Capture a rendered line and assert that the secret is gone:

```python
import io

from structguru import configure, logger

buffer = io.StringIO()
configure(
    target="null",
    stream_sink=buffer,
    sensitive_patterns=[r"(password=)\S+"],
    pattern_replacement="$1[REDACTED]",
)
logger.info("login password=hunter2 ok")
assert "hunter2" not in buffer.getvalue()
```

Hooks that see the record before it is rendered are not redacted:
`metric_processor` receives the fields as logged. Headers bound by the ASGI
middleware and context propagated by Celery apply the sensitive keys themselves;
see their guides.

## See also

- [Redaction reference](../reference/redaction.md) for the exact coverage
- [Redaction design](../explanation/redaction.md)
- [Test code that logs](test-code-that-logs.md)
