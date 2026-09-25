# OpenTelemetry

With `otel=True`, every record logged while an OpenTelemetry span is active
carries that span's identifiers. Install the `otel` extra
(`opentelemetry-api>=1.0`); without it, the setting has no effect.

```bash
pip install "structguru[otel]"
```

## Fields

| Field | Type | Value |
|---|---|---|
| `trace_id` | string | Trace ID as 32 lower-case hexadecimal digits. |
| `span_id` | string | Span ID as 16 lower-case hexadecimal digits. |
| `trace_flags` | integer | The span context's trace flags (`1` when sampled). |

- The fields come from `opentelemetry.trace.get_current_span()` and are added
  only when its span context is valid. Records logged outside a span have none
  of them.
- They are added on the calling thread after the context fields and before
  `exception`. A user field with the same name keeps its position and takes
  the span's value (see [Key order](record-format.md#key-order)).
- The metric hook and the Sentry hook receive them.
- Errors raised by the OpenTelemetry API are suppressed, and the fields are
  omitted.

## `add_otel_context`

`structguru.add_otel_context(logger, method_name, event_dict)` adds the same
fields to `event_dict` in place and returns it. The runtime calls it when
`otel=True`; it can also be called directly with any mutable mapping.

## API

::: structguru.otel
    options:
      heading_level: 3

## See also

- [Correlate with OpenTelemetry](../how-to/correlate-opentelemetry.md)
- [Settings](settings.md)
