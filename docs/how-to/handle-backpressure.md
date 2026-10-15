# Handle backpressure

Records are written by a background thread through a bounded queue. When a
destination cannot keep up, the queue fills. Use this guide to choose what
happens then, and to watch for it.

## Keep every record (the default)

With the default `overflow="block"`, a logging call waits for queue space when
the queue is full, so no record is lost. The native queue holds 8192 records;
the queue for callable and `logger.add()` sinks holds 1024.

## Drop records instead of waiting

When caller latency matters more than completeness, drop the newest record
when a queue is full:

```python
from structguru import configure

configure(service="api", overflow="drop")
```

Each dropped record is counted, and a `UserWarning` is issued for the first drop
and every 1000th after it:

```text
UserWarning: structguru native logging dropped 1 record(s): queue full
```

The same policy applies to the callable-sink queue, whose drops are reported as
`callable sinks dropped … delivery record(s)` and counted separately.

## Size the queues

```python
configure(
    service="api",
    maxsize=32768,  # native writer queue, default 8192
    callable_queue_maxsize=4096,  # callable and logger.add() sinks, default 1024
)
```

`maxsize=0` makes the native queue unbounded. Pass it only when unbounded memory
growth under a stalled destination is acceptable.

## Watch the queues

`writer_metrics()` returns the current writer's counters, or `None` while
logging is shut down:

```python
from structguru import writer_metrics

metrics = writer_metrics()
print(
    {key: metrics[key] for key in ("enqueued", "written", "dropped", "depth", "callable_dropped")}
)
```

After a burst that overflowed a small queue in drop mode, this printed:

```text
{'enqueued': 27690, 'written': 27690, 'dropped': 22310, 'depth': 0, 'callable_dropped': 0}
```

- `dropped` counts native queue overflow; `callable_dropped` counts overflow of
  the callable-sink queue. Both stay at zero with `overflow="block"`.
- `depth` and `callable_depth` show how many records are waiting now.
- Writer counters restart when `configure()` or a non-level `update()` builds a
  new writer.

Records rejected because their writer was closing, during `shutdown()` or a
reconfiguration, are counted separately, cumulatively across configurations:

```python
from structguru import lifecycle_metrics

lifecycle_metrics()  # {'rejected': 0}
```

For every counter, see [Metrics](../reference/metrics.md).

## Reduce the load before it reaches the queue

Records dropped by the level threshold, sampling, or rate limiting are discarded
before they are rendered or queued. See
[Sample and rate-limit logs](sample-and-rate-limit.md).

## Deal with a stuck callable sink

A callable or `logger.add()` sink that makes no progress for 10 seconds while
`flush()`, `shutdown()`, `configure()`, `update()`, or `logger.remove()` waits
for it is abandoned with a warning, so the process does not hang. After an
abandonment, callable delivery stays off until the next `configure()`. A slow
sink that keeps completing deliveries is not abandoned. A native writer that
stalls the same way is abandoned too: records still queued are discarded, and
the warning itself does not wait on a full stderr.

## Wait for buffered records

Call `flush()` at a checkpoint where losing buffered records would matter:

```python
import structguru

structguru.flush()
```

`shutdown()`, reconfiguration, and interpreter exit drain the queues on their
own, and forked children (gunicorn, Celery prefork) get a fresh writer. The exit
drain is registered when `structguru` is imported, so `atexit` handlers
registered after the import can still log.

## See also

- [Metrics](../reference/metrics.md)
- [Settings and environment variables](../reference/settings.md)
- [Delivery and lifecycle](../explanation/delivery-and-lifecycle.md)
