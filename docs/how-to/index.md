# How-to guides

Each guide solves one task and assumes you already know the basics from
[Get started](../tutorials/getting-started.md). For complete option lists, see
the [reference](../reference/index.md); for background, see the
[explanation](../explanation/index.md) pages.

## Set up output

- [Configure logging](configure-logging.md): set the service, level, and format
  from code or environment variables, and change them at runtime.
- [Send logs to files and sinks](send-logs-to-sinks.md): write to a rotating
  file, a function, or a stream, and add or remove sinks at runtime.
- [Handle backpressure](handle-backpressure.md): choose between waiting and
  dropping when a destination falls behind, and watch the queues.

## Control what is logged

- [Redact sensitive data](redact-sensitive-data.md): mask secrets by key or
  pattern, and check that they stay out of the output.
- [Log exceptions](log-exceptions.md): attach tracebacks, catch exceptions from
  whole functions, and emit structured exceptions.
- [Sample and rate-limit logs](sample-and-rate-limit.md): keep a fraction of
  noisy records and cap repeated messages.
- [Capture standard-library logging](capture-stdlib-logging.md): render
  third-party and server logs through structguru.

## Enrich and export

- [Propagate context](propagate-context.md): carry fields such as `request_id`
  through tasks, threads, and generators.
- [Extract metrics](extract-metrics.md): update counters and histograms from
  records you already log.
- [Correlate with OpenTelemetry](correlate-opentelemetry.md): add trace and
  span IDs to every record.
- [Report errors to Sentry](report-errors-to-sentry.md): send breadcrumbs and
  exception events with redaction applied.

## Test and extend

- [Test code that logs](test-code-that-logs.md): capture records in tests and
  keep configuration and context from leaking between them.
- [Write an integration adapter](write-an-integration-adapter.md): bind and
  clear context correctly for a framework structguru does not cover.

## Frameworks and libraries

Web frameworks and servers:

- [FastAPI and Starlette](integrations/asgi.md): request IDs, context, and
  request summaries for any ASGI application, plus uvicorn's own logs.
- [Granian](integrations/granian.md): route Granian's server logs through
  structguru and choose who writes request summaries.
- [Flask](integrations/flask.md): request hooks, including streamed responses.
- [Django](integrations/django.md): middleware and `LOGGING` settings.
- [gRPC](integrations/grpc.md): a server interceptor for unary and streaming
  RPCs.

Background work and data access:

- [Celery](integrations/celery.md): task fields and context propagation from
  producer to worker.
- [SQLAlchemy](integrations/sqlalchemy.md): slow-query and all-query logging.

Outbound HTTP:

- [HTTPX](integrations/httpx.md): event hooks for `Client` and `AsyncClient`.
- [Requests](integrations/requests.md): a logging `Session`.
