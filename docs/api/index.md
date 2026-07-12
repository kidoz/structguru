# API Reference

Welcome to the `structguru` API reference. 

These pages are automatically generated from the source code docstrings using the `mkdocstrings` plugin. This ensures that the documentation is always up-to-date with the actual code implementation.

Navigate through the sub-sections to explore the modules:

*   **Core**: The main Loguru-style facade and logger implementation.
*   **Configuration**: `configure()` plus the runtime-control functions
    (`shutdown`, `is_available`, `set_level`, `writer_metrics`).
*   **Native Processing**: How redaction, sampling, rate limiting, exceptions,
    metrics, and OpenTelemetry injection are configured through `configure()`.
*   **Integrations**: Adapters for popular frameworks like ASGI, Celery, Django, Flask, etc.
