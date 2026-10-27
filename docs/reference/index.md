# Reference

Technical descriptions of the current structguru public API, its options, and
its output. Signatures and docstrings are generated from the source code with
mkdocstrings. For step-by-step instructions, see the
[how-to guides](../how-to/index.md); for background, see the
[explanation](../explanation/index.md) pages.

## Pages

| Page | Contents |
|---|---|
| [Logger](logger.md) | `Logger` and `logger`: level methods, message formatting, context, sinks. |
| [Configuration and runtime control](configuration.md) | `configure()`, `update()`, `Settings`, runtime control, delivery contract, context-variable functions. |
| [Settings and environment variables](settings.md) | Every `Settings` field with type and default; validation; environment variables. |
| [Record format](record-format.md) | JSON and console layouts, levels and severities, value conversion, `stack` and `exception` fields. |
| [Metrics](metrics.md) | `writer_metrics()` and `lifecycle_metrics()` keys, warnings, the metric hook, `MetricProcessor`. |
| [Redaction](redaction.md) | Default sensitive keys, key and pattern redaction coverage, regex engines. |
| [Exceptions](exceptions.md) | Text and structured exception output, schema, limits, locals. |
| [OpenTelemetry](otel.md) | Trace fields and `add_otel_context`. |
| [ASGI](integrations/asgi.md) | `StructguruMiddleware` for FastAPI, Starlette, and other ASGI applications. |
| [Celery](integrations/celery.md) | `setup_celery_logging()` and context headers. |
| [Django](integrations/django.md) | `StructguruMiddleware` and `build_logging_config()`. |
| [Flask](integrations/flask.md) | `setup_flask_logging()`. |
| [gRPC](integrations/grpc.md) | `StructguruInterceptor`. |
| [HTTPX](integrations/httpx.md) | `StructguruHTTPXLoggingHooks`. |
| [Requests](integrations/requests.md) | `StructguruRequestsSession` and `get_logging_session()`. |
| [Sentry](integrations/sentry.md) | `SentryProcessor`. |
| [SQLAlchemy](integrations/sqlalchemy.md) | `setup_query_logging()`. |
| [Standard library](integrations/stdlib.md) | The stdlib bridge, `StructguruHandler`, and `STRUCTGURU_STDLIB_*` variables. |

## Public names

`structguru.__all__` contains exactly these names:

| Name | Kind | Reference |
|---|---|---|
| `DEFAULT_SENSITIVE_KEYS` | `frozenset[str]` | [Redaction](redaction.md#default-sensitive-keys) |
| `Logger` | class | [Logger](logger.md) |
| `MetricProcessor` | class | [Metrics](metrics.md#metricprocessor) |
| `Settings` | class | [Configuration](configuration.md#structguru.Settings), [Settings](settings.md) |
| `add_otel_context` | function | [OpenTelemetry](otel.md) |
| `bind_contextvars` | function | [Configuration](configuration.md#structguru.bind_contextvars) |
| `bound_contextvars` | function (context manager) | [Configuration](configuration.md#structguru.bound_contextvars) |
| `clear_contextvars` | function | [Configuration](configuration.md#structguru.clear_contextvars) |
| `configure` | function | [Configuration](configuration.md#structguru.configure) |
| `flush` | function | [Configuration](configuration.md#structguru.flush) |
| `get_config` | function | [Configuration](configuration.md#structguru.get_config) |
| `get_contextvars` | function | [Configuration](configuration.md#structguru.get_contextvars) |
| `is_available` | function | [Configuration](configuration.md#structguru.is_available) |
| `lifecycle_metrics` | function | [Configuration](configuration.md#structguru.lifecycle_metrics), [Metrics](metrics.md#lifecycle_metrics) |
| `logger` | `Logger` instance | [Logger](logger.md) |
| `set_level` | function | [Configuration](configuration.md#structguru.set_level) |
| `shutdown` | function | [Configuration](configuration.md#structguru.shutdown) |
| `update` | function | [Configuration](configuration.md#structguru.update) |
| `writer_metrics` | function | [Configuration](configuration.md#structguru.writer_metrics), [Metrics](metrics.md#writer_metrics) |

`structguru.__version__` holds the package version string.

## Modules

| Module | Contents |
|---|---|
| `structguru` | The names above. |
| `structguru.core` | `Logger`, `logger`, `WritableSink`. |
| `structguru.settings` | `Settings`. |
| `structguru.exceptions` | `build_exception_dict()`. |
| `structguru.metrics` | `MetricProcessor`. |
| `structguru.otel` | `add_otel_context()`. |
| `structguru.redaction` | `DEFAULT_SENSITIVE_KEYS`, `REDACTED_MARKER_KEY`. |
| `structguru.integrations.*` | One module per integration; none is imported by `import structguru`. |

## Optional extras

| Extra | Installs |
|---|---|
| `otel` | `opentelemetry-api>=1.0` |
| `celery` | `celery>=5.0` |
| `flask` | `flask>=2.0` |
| `django` | `django>=4.0` |
| `sqlalchemy` | `sqlalchemy>=2.0` |
| `grpc` | `grpcio>=1.50` |
| `sentry` | `sentry-sdk>=1.0` |
| `httpx` | `httpx>=0.20` |
| `requests` | `requests>=2.0` |
| `all` | every extra above |

The package requires Python 3.11 or later and has no required runtime
dependencies. The ASGI middleware and the stdlib bridge need no extra.
