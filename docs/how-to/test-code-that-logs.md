# Test code that logs

Use this guide to assert on the records your code logs, and to keep logging
configuration and context from leaking between tests.

## Capture rendered records

Configure a `stream_sink` for the test. Lines are written to it on the calling
thread, so they are available as soon as the logging call returns:

```python
# conftest.py
import io
import json

import pytest

import structguru


@pytest.fixture
def log_records():
    """Capture every record the test logs, parsed from JSON."""
    previous = structguru.get_config()
    stream = io.StringIO()
    structguru.configure(service="test", level="DEBUG", target="null", stream_sink=stream)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    if previous is not None:
        structguru.configure(previous)
    else:
        structguru.shutdown()
```

```python
# test_billing.py
from structguru import logger


def charge(order_id):
    logger.info("Charged order {order_id}", order_id=order_id, amount=10, token="t-1")


def test_charge_logs_the_order(log_records):
    charge(42)

    [record] = log_records()
    assert record["message"] == "Charged order 42"
    assert record["level"] == "INFO"
    assert record["amount"] == 10
    assert record["token"] == "[REDACTED]"
```

`target="null"` keeps the records out of the test output. The fixture restores
the previous configuration afterward, because `configure()` replaces it for the
whole process.

pytest's `caplog` fixture does not see structguru records: they do not pass
through the `logging` module.

## Capture with a callable sink

A callable sink receives each line on a background thread. Call
`structguru.flush()` before asserting:

```python
import structguru
from structguru import logger


def test_slow_warning():
    lines = []
    sink_id = logger.add(lines.append, level="WARNING")
    try:
        logger.info("ignored")
        logger.warning("slow")
        structguru.flush()
    finally:
        logger.remove(sink_id)
    assert len(lines) == 1
    assert '"message":"slow"' in lines[0]
```

`logger.add()` leaves the active configuration alone, so this works alongside
whatever the application configured. `configure(callable_sinks=[lines.append])`
works the same way but replaces the configuration. Each line ends with a
newline.

## Test code that uses the stdlib bridge

Install the bridge with `replace=True`, so a bridge left by application setup or
another test does not raise, and uninstall it afterward:

```python
import pytest

from structguru.integrations.stdlib import install_stdlib_bridge, uninstall_stdlib_bridge


@pytest.fixture
def stdlib_bridge():
    bridge = install_stdlib_bridge(level="DEBUG", replace=True)
    yield bridge
    uninstall_stdlib_bridge(bridge)
```

Uninstalling does not restore root handlers that the bridge removed, or revert
`suppress_loggers` levels.

## Test environment-based configuration

Pass a mapping instead of changing the process environment:

```python
from structguru import Settings


def test_rate_limit_from_env():
    settings = Settings.from_env({"STRUCTGURU_LEVEL": "debug", "STRUCTGURU_RATE_LIMIT": "10/5"})
    assert settings.level == "DEBUG"
    assert (settings.rate_limit_max, settings.rate_limit_period) == (10, 5.0)
```

`install_stdlib_bridge_from_env()` takes an `environ` mapping the same way.

## Keep context from leaking between tests

Bound context is process state, and structguru does not reset it between
tests. A test that leaves fields bound can change the outcome of a later one.

- **Start from a known state.** Call `clear_contextvars()` at the start of any
  test that reads or asserts on context.
- **Close generators explicitly.** A partly consumed generator is suspended,
  not finished: its `finally` has not run, so its context is still bound, and
  cleanup happens whenever it is garbage-collected, possibly during a later
  test. Call `.close()`, then assert that the context is clean.
- **Close async generators with `aclose()`.** A suspended async generator that
  is garbage-collected is cleaned up in a separate task, in that task's context,
  so the iterating task keeps the fields. Consume it through
  `contextlib.aclosing()` (or `await agen.aclose()`).
- **Cover the window before iteration.** Asserting that context is bound during
  iteration does not catch a leak before it. Create the iterator, assert that the
  context is empty, and only then consume it.

```python
from structguru import clear_contextvars, get_contextvars


def test_stream_binds_only_while_iterating():
    clear_contextvars()
    stream = handler(request)
    assert "request_id" not in get_contextvars()  # not bound yet
    it = iter(stream)
    assert next(it) == "item-0"
    assert get_contextvars()["request_id"] == "req-1"
    it.close()  # cleanup runs here, not at garbage collection
    assert "request_id" not in get_contextvars()
```

```python
from contextlib import aclosing

async with aclosing(stream_events()) as events:
    async for event in events:
        if event.done:
            break  # aclose() runs here, in this task's context
assert "request_id" not in get_contextvars()
```

## See also

- [Send logs to files and sinks](send-logs-to-sinks.md)
- [Propagate context](propagate-context.md)
- [Write an integration adapter](write-an-integration-adapter.md)
- [Configuration and runtime control](../reference/configuration.md)
