# Correlate logs with OpenTelemetry traces

Add the active trace and span IDs to every record, so your log backend can link
records to the distributed trace they belong to.

Install the `otel` extra, which provides `opentelemetry-api`, and set up
tracing (the OpenTelemetry SDK or your vendor's distribution) as usual:

```bash
pip install "structguru[otel]"
```

## Turn on trace fields

```python
from structguru import update

update(otel=True)
```

Or pass `otel=True` to `configure()`. Every record logged while a valid span is
current then carries `trace_id` (32 hex digits), `span_id` (16 hex digits), and
`trace_flags`:

```python
from opentelemetry import trace

from structguru import configure, logger

configure(service="api", otel=True)
tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("charge"):
    logger.info("Charging card")
```

With a tracing SDK configured and the span sampled, the record looks like this
(the IDs vary):

```json
{"trace_id":"4bf92f3577b34da6a3ce929d0e0e4736","span_id":"00f067aa0ba902b7","trace_flags":1,"logger":"__main__","level":"INFO","severity":6,"timestamp":"…","service":"api","message":"Charging card"}
```

Records logged outside a span, or with only the API and no SDK configured, have
no trace fields. Records from standard-library loggers routed through the
[stdlib bridge](capture-stdlib-logging.md) get the fields too.

## Run without OpenTelemetry installed

`otel=True` is safe to leave on: if `opentelemetry-api` is not installed, or the
tracing SDK fails while reading the current span, the record is logged without
trace fields.

## See also

- [OpenTelemetry](../reference/otel.md)
- [Record format](../reference/record-format.md)
- [Propagate context](propagate-context.md)
