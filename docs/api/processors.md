# Native processing

Processing is configured directly through [`structguru.configure`](config.md).
The native runtime owns level normalization, RFC 5424 severity, redaction,
sampling, rate limiting, exception rendering, metrics hooks, and OpenTelemetry
injection; there is no public processor-chain configuration in v1.
