# Log exceptions

Use this guide to attach an exception and its traceback to a record, log
exceptions from a whole function, and choose between a formatted traceback and
a structured one.

## Log the exception you are handling

Inside an `except` block, `logger.exception()` logs at `ERROR` with the active
exception:

```python
from structguru import logger


class PaymentError(Exception):
    pass


def charge(order_id):
    raise PaymentError("card declined")


try:
    charge(42)
except PaymentError:
    logger.exception("Charge failed", order_id=42)
```

```json
{"order_id":42,"exception":"Traceback (most recent call last):\n  File \"…/billing.py\", line 13, in <module>\n    charge(42)\n    ~~~~~~^^^^\n  File \"…/billing.py\", line 9, in charge\n    raise PaymentError(\"card declined\")\nPaymentError: card declined","logger":"__main__","level":"ERROR","severity":3,"timestamp":"…","service":"app","message":"Charge failed"}
```

The same record at another level, or for an exception object you hold:

```python
logger.warning("Retrying charge", exc_info=True)  # the active exception
logger.error("Charge failed", exc_info=exc)  # an exception instance
logger.opt(exception=True).error("Charge failed")
```

`opt()` returns a logger that keeps the option for every call it makes, so
chain it directly for a single call.

## Log exceptions from a function or block

`logger.catch()` works as a decorator, including on `async def` functions, and
as a context manager:

```python
from structguru import logger


@logger.catch()
def nightly_job(): ...


with logger.catch(message="Cleanup failed"):
    remove_temp_files()
```

By default it catches `Exception`, logs it at `ERROR` with the message
`An error occurred`, and **suppresses** it: a decorated function then returns
`None`. Narrow it, and let the exception continue to the caller, with the
keyword arguments:

```python
@logger.catch(ValueError, level="warning", message="Row skipped", reraise=True)
async def import_row(row): ...
```

Level names are case-insensitive; an unknown name raises `ValueError` when the
catcher is created. When the logger has no explicit name, a decorated function
logs under its own module name.

## Log a structured exception

For log pipelines that query exception fields, render the `exception` field as
an object instead of a traceback string:

```python
from structguru import configure, logger

configure(service="api", structured_exceptions=True, exception_max_frames=20)


def pay(amount):
    return 100 / amount


try:
    pay(0)
except ZeroDivisionError:
    logger.exception("Charge failed")
```

```json
{"exception":{"type":"ZeroDivisionError","message":"division by zero","module":"builtins","frames":[{"filename":"…/billing.py","lineno":11,"name":"<module>","line":"pay(0)"},{"filename":"…/billing.py","lineno":7,"name":"pay","line":"return 100 / amount"}]},"logger":"__main__","level":"ERROR","severity":3,"timestamp":"…","service":"api","message":"Charge failed"}
```

- `exception_max_frames` (default 20) limits the frames; `0` omits them.
- Exception groups include their members under `exceptions`.
- Negative limits are rejected by `configure()`.

For the complete layout, see [Exceptions](../reference/exceptions.md).

## Include local variables

```python
configure(
    service="api",
    structured_exceptions=True,
    exception_include_locals=True,
    exception_max_local_repr=200,  # default: characters kept per local
)
```

Each frame then carries a `locals` mapping of `repr()` strings. For a frame of

```python
def pay(card_token, amount):
    password = "hunter2"
    return 100 / amount
```

the mapping is:

```json
"locals":{"card_token":"'tok_123'","amount":"0","password":"[REDACTED]"}
```

A local named like a sensitive key is redacted, as is any value under a
sensitive key inside a mapping, list, tuple, named tuple, set, dataclass (with
its generated `__repr__`), or `SimpleNamespace`. Other objects' own `repr()`
text is covered only by `sensitive_patterns`, which scan each local's
bounded rendering prefix before truncation: at most
`exception_max_local_repr + 4096` characters, with depth and item limits also
applying. Patterns requiring later context can miss visible text. See
[Redact sensitive data](redact-sensitive-data.md).

## Format tracebacks faster

Formatted tracebacks include CPython's per-frame position markers (the `~~~^^^`
lines). Computing them is most of the cost of `logger.exception()`. Omit them to
format a traceback about five times faster:

```python
from structguru import update

update(exception_carets=False)
```

The output then matches what CPython prints under `PYTHONNODEBUGRANGES=1`.
Structured exceptions never include the markers.

## Log the current call stack

Attach the stack without an exception with `stack_info`:

```python
logger.info("Cache rebuilt", stack_info=True)
logger.opt(stack_info=True).warning("Unexpected call path")
```

The record gets a `stack` field that ends at your calling frame; structguru's
own frames are skipped.

## See also

- [Exceptions](../reference/exceptions.md)
- [Logger](../reference/logger.md)
- [Record format](../reference/record-format.md)
- [Report errors to Sentry](report-errors-to-sentry.md)
