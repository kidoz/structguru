# Log Celery tasks

Call `setup_celery_logging()` to bind `task_id` and `task_name` to every record
logged inside a task, and to carry selected context fields, such as
`request_id`, from the code that sends a task to the worker that runs it.
Install the `celery` extra and the driver for your broker (`redis` in this example):

```bash
pip install "structguru[celery]" redis
```

## Connect the signals

Call it once per process, in the module that creates the Celery app, so that
both the producer (your web process) and the worker run it:

```python
# tasks.py
from celery import Celery

from structguru import configure, logger
from structguru.integrations.celery import setup_celery_logging

configure(service="worker")

app = Celery("tasks", broker="redis://localhost:6379/0")
setup_celery_logging()


@app.task(name="send_receipt")
def send_receipt(order_id):
    logger.info("Sending receipt for order {order_id}", order_id=order_id)
```

## Propagate request context to tasks

With the default `propagate_context=True`, the producer's structguru context is
sent in a message header and bound in the worker. Limit it to the fields you
need:

```python
setup_celery_logging(context_keys=["request_id", "user_id"])
```

```python
from structguru import logger

with logger.contextualize(request_id="req-123", user_id="u-1", tenant="acme"):
    send_receipt.delay(42)
```

The worker logs:

```json
{"request_id":"req-123","user_id":"u-1","task_id":"…","task_name":"send_receipt","logger":"tasks","level":"INFO","severity":6,"timestamp":"…","service":"worker","message":"Sending receipt for order 42"}
```

- Values in the header are converted to JSON-safe types and bounded, so the
  header is safe for every transport and serializer. Publishing a task never
  fails because of bound context; if the context cannot be exported, the header
  is omitted.
- Keys that structguru redacts (`sensitive_keys`, or the defaults such as
  `password` and `token`) are sent as `[REDACTED]`, so secrets do not reach the
  broker. This also recognizes bounded UTF-8 bytes keys and Enum member names
  or string values. The producer's `sensitive_patterns` apply to exported keys,
  strings, and decimal integers before string truncation.
- The producer's `task_id` and `task_name` never replace the worker task's own.

To bind only `task_id` and `task_name`, turn propagation off:

```python
setup_celery_logging(propagate_context=False)
```

For the exact conversion rules and limits, see
[Celery integration](../../reference/integrations/celery.md).

## Run tasks eagerly in tests

With `task_always_eager` (or `task.apply()`) and propagation on, a task runs in
the calling process and inherits the selected fields of the caller's context
directly; the caller's context is restored when the task returns. In workers,
each task starts with a clean context, and a nested task restores its parent's
context when it finishes.

## Route Celery's own logs through structguru

A Celery worker configures standard-library logging at startup and replaces the
root logger's handlers, which removes a stdlib bridge installed earlier. Install
the bridge from Celery's `setup_logging` signal instead; connecting a receiver
also stops Celery from configuring logging itself:

```python
from celery.signals import setup_logging

from structguru.integrations.stdlib import install_stdlib_bridge


@setup_logging.connect
def route_celery_logs(**kwargs):
    install_stdlib_bridge(level="INFO", replace=True)
```

`replace=True` keeps this from raising when other startup code in the worker
process, such as Django settings loaded by the Celery app, already installed a
bridge. See
[Capture standard-library logging](../capture-stdlib-logging.md).

## Call it only once

Later calls do not connect duplicate handlers. A later call with different
options emits a `RuntimeWarning` and leaves the first configuration active:

```text
RuntimeWarning: setup_celery_logging() was already called with different options; the first configuration stays active.
```

If the first call fails because Celery cannot be imported, nothing is connected,
and a later call can retry.

## See also

- [Celery integration](../../reference/integrations/celery.md)
- [Propagate context](../propagate-context.md)
- [Capture standard-library logging](../capture-stdlib-logging.md)
