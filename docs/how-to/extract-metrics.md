# Extract metrics from logs

Use a metric processor to update counters and histograms from records you
already log, instead of instrumenting the same code twice.

## Register counters and histograms

```python
from collections import Counter

from structguru import MetricProcessor, configure, logger

counts = Counter()
durations = []

metrics = (
    MetricProcessor()
    .counter("logged in", lambda event: counts.update(["logins"]))
    .histogram("Query finished", "duration_ms", lambda value, event: durations.append(value))
)
configure(service="api", metric_processor=metrics)

logger.info("User {user_id} logged in", user_id=42)
logger.info("Query finished", duration_ms=12.5)
logger.debug("Query finished", duration_ms=99.0)  # below INFO: never reaches the processor

print(counts, durations)
```

```text
Counter({'logins': 1}) [12.5]
```

Replace the `Counter` and list with your metrics client, for example a
Prometheus counter's `inc()` and a histogram's `observe()`.

- A pattern matches when it appears anywhere in the **formatted** message:
  `"logged in"` matches `"User 42 logged in"`, but `"User logged in"` does not.
- A counter callback receives the event dictionary: the message under `"event"`,
  plus the record's fields.
- A histogram callback receives the field named by its second argument,
  converted with `float()`, and the event dictionary. Records without that field
  are skipped.
- Keyword arguments consumed by message placeholders are not fields, so they
  are not in the event dictionary (`user_id` above). Pass values that callbacks
  need as extra keyword arguments.

## Know which records reach the processor

- It runs on the logging thread, before rendering, for every record that is
  kept. Records removed by the level threshold, sampling, or rate limiting never
  reach it.
- It sees field values as logged, **before** redaction. Do not copy fields into
  metric labels without checking them.
- An exception raised by a callback, or by the processor, is swallowed; logging
  continues.

## Write your own processor

`metric_processor` accepts any callable with the signature
`(logger, method_name, event_dict)`:

```python
def audit(_logger, method, event):
    if method in ("error", "critical"):
        error_counter.inc()
    return event


configure(service="api", metric_processor=audit)
```

`method` is the level method name (`"info"`, `"warning"`, ...), and `event` is
the same dictionary a `MetricProcessor` receives.

## See also

- [Metrics](../reference/metrics.md)
- [Configuration and runtime control](../reference/configuration.md)
- [Sample and rate-limit logs](sample-and-rate-limit.md)
