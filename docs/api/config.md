# API Reference: Configuration

## Configure the runtime

::: structguru.configure

## Settings and incremental changes

::: structguru.Settings

::: structguru.get_config

::: structguru.update

`configure(settings)` uses the supplied settings without reading environment variables;
keyword overrides still win. `configure(**changes)` layers defaults, supported environment
values, then explicit keywords. It replaces the active configuration. `update(**changes)`
retains omitted options and never reads environment variables. Level-only updates retain
writers and filter state; other updates rebuild them. A snapshot does not capture pending
records, limiter counters, bridge ownership, or sinks registered with `logger.add()`.

The supported environment names are `STRUCTGURU_SERVICE`, `STRUCTGURU_LEVEL`,
`STRUCTGURU_TARGET`, `STRUCTGURU_FORMAT`, `STRUCTGURU_SAMPLE_RATE`, and
`STRUCTGURU_RATE_LIMIT` (`MAX` or `MAX/PERIOD`, seconds). New names take precedence over
`LOG_LEVEL`, `STRUCTGURU_NATIVE_TARGET`, `STRUCTGURU_NATIVE_SAMPLE_RATE`, and
`STRUCTGURU_NATIVE_RATE_LIMIT`. Explicit keyword values, even `None` or a built-in default,
override environment values. `STRUCTGURU_AUTOCONFIGURE=0` disables import-time setup;
its boolean value takes precedence over the legacy inverse switch `STRUCTGURU_LEGACY`.
The switches do not disable explicit configuration. The stdlib bridge is configured separately.

Settings accept ordinary Python values. Strings are not coerced by `from_mapping()`.
Levels accept known case-insensitive names or non-negative integer thresholds, excluding
booleans. Native regex and sink initialization errors leave the active runtime unchanged.

## Runtime control

::: structguru.shutdown

::: structguru.flush

::: structguru.is_available

::: structguru.set_level

::: structguru.writer_metrics

## Request-scoped context

::: structguru.bind_contextvars

::: structguru.bound_contextvars

::: structguru.clear_contextvars

::: structguru.get_contextvars
