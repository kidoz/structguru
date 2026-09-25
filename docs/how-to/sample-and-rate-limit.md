# Sample and rate-limit logs

Use sampling to keep a fraction of high-volume records, and rate limiting to cap
how often the same message repeats. Both run before a record is rendered, so a
dropped record costs almost nothing.

## Keep a fraction of low-severity records

```python
from structguru import configure

configure(
    service="api",
    sample_rate=0.1,  # keep about 10% of records
    sample_max_level="INFO",  # sample only INFO and below
)
```

With `sample_max_level`, records more severe than that level always pass, so
warnings and errors are never sampled away. Without it, `sample_rate` applies to
every level. The level threshold applies first: records below `level` are
dropped whatever the sample rate.

## Cap repeated messages

```python
configure(service="api", rate_limit_max=100, rate_limit_period=60)
```

Each distinct message is kept at most `rate_limit_max` times per
`rate_limit_period` seconds (default 60), whatever its level. The limiter keys
on the **formatted** message, so a message that interpolates an ID or counter is
never limited. Pass changing values as fields instead:

```python
logger.warning("Upstream slow", attempt=attempt)  # limited as one message
logger.warning("Upstream {n} slow", n=attempt)  # a new message every time: never limited
```

The limiter tracks at most 8192 distinct messages and forgets the least recently
seen one when full.

## Configure from the environment

```bash
STRUCTGURU_SAMPLE_RATE=0.1 STRUCTGURU_RATE_LIMIT=100/60 python -m myapp
```

`STRUCTGURU_RATE_LIMIT` takes `MAX` or `MAX/PERIOD`, with an integer count and a
period in seconds; `sample_max_level` has no environment variable. See
[Settings and environment variables](../reference/settings.md).

## Check what was dropped

```python
from structguru import writer_metrics

metrics = writer_metrics()
print(metrics["sampled"], metrics["rate_limited"])
```

`sampled` and `rate_limited` count records the filters removed. They are
separate from `dropped`, which counts queue overflow.

## Keep limiter state across level changes

`set_level()` and `update(level=...)` keep the rate-limit windows and counters.
Any other `update()`, and every `configure()`, resets them.

## See also

- [Settings and environment variables](../reference/settings.md)
- [Metrics](../reference/metrics.md)
- [Handle backpressure](handle-backpressure.md)
