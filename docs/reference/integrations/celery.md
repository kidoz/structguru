# Celery

`structguru.integrations.celery.setup_celery_logging()` connects Celery
signal handlers that bind task context in workers and, optionally, carry the
producer's context to the worker in a message header. Install the `celery`
extra (`celery>=5.0`).

```python
from structguru.integrations.celery import setup_celery_logging

setup_celery_logging(propagate_context=True, context_keys=["request_id"])
```

## Parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `propagate_context` | `bool` | `True` | Export the producer's context into a task header and bind it in the worker. |
| `context_keys` | `Sequence[str] \| None` | `None` | Keys to propagate. `None` propagates every context key. A single `str` is treated as one key. |

## Setup rules

- Call it once per process. A later call connects nothing. A later call with
  different options emits a `RuntimeWarning` and the first configuration stays
  active.
- If `celery` cannot be imported, the call raises, nothing is connected, and a
  later call can succeed.

## Signals

| Signal | Handler behavior |
|---|---|
| `before_task_publish` | With `propagate_context=True`: exports the selected context into the `structguru_context` header. |
| `task_prerun` | Saves the current context when the task runs eagerly or inside another task, clears the context, binds the propagated fields, then binds `task_id` and `task_name`. |
| `task_postrun` | Clears the context, then restores the context saved by `task_prerun`, if any. |

## Context fields in the task

| Field | Value |
|---|---|
| `task_id` | The task ID. |
| `task_name` | `task.name`. |
| propagated keys | The exported values from the `structguru_context` header (string keys only). |

`task_id` and `task_name` always describe the running task, even when the
producer was itself a task that propagated its own values. Worker tasks start
with an empty context. An eager task (`task.request.is_eager`) inherits the
selected keys from the caller's context when propagation is enabled; explicit
header values take precedence, and the caller's context is restored when the
task finishes. A nested task restores its parent's context when it finishes.

## Header export

The `structguru_context` header holds a JSON-safe mapping built from the
selected context keys:

| Value | Exported as |
|---|---|
| `None`, `bool` | unchanged |
| `str` | pattern-redacted, then cut to 1024 characters and suffixed with `...[truncated]` if needed |
| `int` | pattern-redacted decimal string if changed; otherwise unchanged inside the signed 64-bit range, or its decimal string outside it |
| `float` | unchanged; NaN and infinities become `None` |
| `Enum` member | its exported `value` |
| `datetime`, `date`, `time` | `isoformat()` string |
| `UUID`, `Decimal`, paths (`PurePath`) | `str()`, bounded like other strings |
| dataclass instance | mapping of its fields |
| mapping | mapping with at most 100 entries |
| `list`, `tuple`, `set`, `frozenset` | list with at most 100 items |
| a container or other non-scalar value at nesting depth 8 | the string `<max depth exceeded>` |
| any other object | the string `<unsupported: TypeName>`; `str()` and `repr()` are not called |

Mapping keys:

- `str` keys are kept; UTF-8 `bytes` keys of at most 128 bytes are decoded;
  `Enum` keys use their exported value. `None`, `bool`, `int`, and `float`
  keys become text (`"None"`, `"True"`, `"1.5"`); other types are dropped.
- Keys longer than 255 bytes in UTF-8 are dropped.
- A value whose key matches an active sensitive key, ignoring case, is exported
  as `[REDACTED]` at every nesting level. Active sensitive keys are the
  configured `sensitive_keys`, or `DEFAULT_SENSITIVE_KEYS`. Enum member names
  and string values are checked before key conversion.
- The producer's active `sensitive_patterns` apply to keys and string values,
  including converted text and decimal integers, before string truncation.
  Pattern evaluation errors redact the entire text. Floating-point numbers,
  booleans, and `None` remain scalar values and are not pattern targets.

If the context cannot be exported, the header is omitted. Publishing never
fails because of the context.

## API

::: structguru.integrations.celery
    options:
      heading_level: 3

## See also

- [Celery](../../how-to/integrations/celery.md)
- [Propagate context](../../how-to/propagate-context.md)
- [Context propagation](../../explanation/context-propagation.md)
