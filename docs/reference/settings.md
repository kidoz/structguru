# Settings and environment variables

`structguru.Settings` is a frozen dataclass that describes one runtime
configuration. `configure()` and `update()` accept the same names as keyword
arguments. The class and its methods are documented on
[Configuration and runtime control](configuration.md#structguru.Settings);
the order in which defaults, environment values, and keywords combine is
described in [Resolution order](configuration.md#resolution-order).

## Fields

### Output

| Field | Type | Default | Environment | Description |
|---|---|---|---|---|
| `service` | `str` | `"app"` | `STRUCTGURU_SERVICE` | Value of the `service` key in every record. A user field named `service` takes precedence. |
| `format` | `str` | `"json"` | `STRUCTGURU_FORMAT` | Renderer: `"json"` or `"console"`. See [Record format](record-format.md). |
| `colors` | `bool \| None` | `None` | none | ANSI colors in console output. `None` uses `sys.stdout.isatty()` at configuration time. Ignored for JSON. |
| `target` | `str` | `"stdout"` | `STRUCTGURU_TARGET` | Destination of the native writer when `file_path` is unset: `"stdout"`, `"null"` (discard), or `"memory"` (in-process buffer). |
| `file_path` | `str \| None` | `None` | none | Path of a native rotating file sink. When set, the file replaces `target`. |
| `file_max_bytes` | `int` (>= 0) | `52428800` (50 MiB) | none | Size at which the file rotates. `0` disables rotation. |
| `file_backup_count` | `int` (>= 0) | `5` | none | Number of rotated files kept (`path.1` to `path.N`). `0` disables rotation. |
| `also_stdout` | `bool` | `False` | none | Also write every record to stdout. Without `file_path`, the writer writes to stdout whatever `target` is. |
| `stream_sink` | object with `write()`, or `None` | `None` | none | Stream that receives each rendered line synchronously on the calling thread, in addition to the native writer. |
| `callable_sinks` | `Sequence[Callable[[str], None]] \| None` | `None` | none | Callables that receive each rendered line on the dispatch thread. |
| `callable_queue_maxsize` | `int` (>= 1) | `1024` | none | Capacity of the dispatch queue shared by `callable_sinks` and `logger.add()` sinks. |
| `maxsize` | `int` (>= 0) | `8192` | none | Capacity of the native writer queue. `0` means unbounded. |
| `overflow` | `str` | `"block"` | none | Policy for a full writer queue and a full dispatch queue: `"block"` waits for space; `"drop"` discards the new record or delivery, counts it, and emits a rate-limited warning. |

### Levels and filtering

| Field | Type | Default | Environment | Description |
|---|---|---|---|---|
| `level` | `str \| int` | `"INFO"` | `STRUCTGURU_LEVEL` | Minimum level: a [level name](record-format.md#levels) (case-insensitive, stored upper-case) or an integer between `0` and `2**63 - 1`. |
| `sample_rate` | `float` (0.0 to 1.0) | `1.0` | `STRUCTGURU_SAMPLE_RATE` | Fraction of records kept by probabilistic sampling. `1.0` disables sampling. |
| `sample_max_level` | `str \| None` | `None` | none | Highest level name that is sampled; more severe records always pass. `None` samples every level. `NOTSET` is rejected. Stored upper-case. |
| `rate_limit_max` | `int \| None` (>= 1) | `None` | `STRUCTGURU_RATE_LIMIT` | Maximum records per formatted message within `rate_limit_period`. `None` disables rate limiting. |
| `rate_limit_period` | `float` (> 0) | `60.0` | `STRUCTGURU_RATE_LIMIT` | Length of the sliding rate-limit window, in seconds. |

Sampling and rate limiting run before rendering, after the level threshold
and message formatting. The rate limiter groups records by their formatted
message and tracks at most 8192 messages. When a new message would exceed that
limit, messages with no records inside the window are removed first, then the
least recently seen tenth of the table. Dropped records are counted in `writer_metrics()` as `sampled` and
`rate_limited` (see [Metrics](metrics.md)).

### Redaction

| Field | Type | Default | Environment | Description |
|---|---|---|---|---|
| `sensitive_keys` | `Sequence[str] \| None` | `None` | none | Field names whose values are redacted. `None` uses `DEFAULT_SENSITIVE_KEYS`; a sequence replaces that set. |
| `sensitive_patterns` | `Sequence[str] \| None` | `None` | none | Regular expressions whose matches are replaced in text, keys, and integers. |
| `pattern_replacement` | `str` | `"[REDACTED]"` | none | Replacement for pattern matches. Supports `$1`, `${name}`, and `$$`. |
| `allow_backtracking_patterns` | `bool` | `False` | none | Compile patterns that the linear-time engine rejects with a bounded backtracking engine instead of raising. |

Coverage and matching rules are listed on [Redaction](redaction.md).

### Exceptions

| Field | Type | Default | Environment | Description |
|---|---|---|---|---|
| `structured_exceptions` | `bool` | `False` | none | Render `exception` as an object instead of traceback text. |
| `exception_include_locals` | `bool` | `False` | none | Include each frame's local variables. Structured exceptions only. |
| `exception_max_frames` | `int` (>= 0) | `20` | none | Number of innermost frames kept. `0` omits frames. Structured exceptions only. |
| `exception_max_local_repr` | `int` (>= 0) | `200` | none | Maximum characters of each rendered local. Structured exceptions only. |
| `exception_carets` | `bool` | `True` | none | Keep PEP 657 position-marker lines in traceback text. Text tracebacks only. |

The structured schema is listed on [Exceptions](exceptions.md).

### Hooks

| Field | Type | Default | Environment | Description |
|---|---|---|---|---|
| `otel` | `bool` | `False` | none | Add `trace_id`, `span_id`, and `trace_flags` from the current OpenTelemetry span. See [OpenTelemetry](otel.md). |
| `metric_processor` | callable or `None` | `None` | none | Called as `metric_processor(None, method, event_dict)` for every record that passes filtering, before rendering. See [Metrics](metrics.md#metric-hook). |
| `sentry_processor` | callable or `None` | `None` | none | Called as `sentry_processor(None, method, event_dict)` with the redacted record after it is handed to the native writer. See [Sentry](integrations/sentry.md). |

## Validation

`Settings(...)`, `Settings.from_mapping()`, `Settings.from_env()`,
`configure()`, and `update()` validate every value:

| Rule | Error |
|---|---|
| `level` is a known name or an integer between `0` and `2**63 - 1`; `bool` is rejected. | `ValueError` |
| `service` and `pattern_replacement` are `str`; `file_path` is `str` or `None`. | `ValueError` |
| `target` is `"stdout"`, `"null"`, or `"memory"`; `overflow` is `"block"` or `"drop"`; `format` is `"json"` or `"console"` (case-sensitive). | `ValueError` |
| `otel`, `allow_backtracking_patterns`, `structured_exceptions`, `exception_include_locals`, `exception_carets`, and `also_stdout` are `bool`; `colors` is `bool` or `None`. | `ValueError` |
| `maxsize`, `file_max_bytes`, `file_backup_count`, `exception_max_frames`, and `exception_max_local_repr` are integers >= 0; `callable_queue_maxsize` and `rate_limit_max` are integers >= 1 (`rate_limit_max` may be `None`); `bool` is rejected. | `ValueError` |
| `sample_rate` is a finite number from 0.0 to 1.0; `rate_limit_period` is a finite number > 0. | `ValueError` |
| `sample_max_level` is a level name other than `NOTSET`, or `None`. | `ValueError` |
| `sensitive_keys`, `sensitive_patterns`, and `callable_sinks` are sequences other than `str`, or `None`; key and pattern entries are `str`. | `ValueError` |
| `callable_sinks` entries, `metric_processor`, and `sentry_processor` are callable. | `TypeError` |
| `stream_sink` has a callable `write` attribute, or is `None`. | `ValueError` |

Values are not coerced: `Settings.from_mapping({"sample_rate": "0.5"})` raises
`ValueError`. Sequences are copied into tuples; callables and streams are kept
by identity. `Settings.from_mapping()` rejects unknown keys with `ValueError`.

Two checks run only when settings are applied by `configure()` or `update()`:
compilation of `sensitive_patterns` and opening of `file_path` (including
creating missing parent directories). Both raise `ValueError`, and the active
runtime stays unchanged.

## Environment variables

| Variable | Setting | Accepted values | Default | Legacy fallback |
|---|---|---|---|---|
| `STRUCTGURU_SERVICE` | `service` | any string | `app` | none |
| `STRUCTGURU_LEVEL` | `level` | a level name, or ASCII digits for an integer threshold (`10`) | `INFO` | `LOG_LEVEL` |
| `STRUCTGURU_TARGET` | `target` | `stdout`, `null`, `memory` | `stdout` | `STRUCTGURU_NATIVE_TARGET` |
| `STRUCTGURU_FORMAT` | `format` | `json`, `console` | `json` | none |
| `STRUCTGURU_SAMPLE_RATE` | `sample_rate` | a number from 0.0 to 1.0 | `1.0` | `STRUCTGURU_NATIVE_SAMPLE_RATE` |
| `STRUCTGURU_RATE_LIMIT` | `rate_limit_max`, `rate_limit_period` | `MAX` or `MAX/PERIOD`: integer count, numeric period in seconds | disabled; period `60` | `STRUCTGURU_NATIVE_RATE_LIMIT` |
| `STRUCTGURU_AUTOCONFIGURE` | import-time `configure()` | `1`/`0`, `true`/`false`, `yes`/`no`, `on`/`off` (case-insensitive; surrounding whitespace ignored) | enabled | `STRUCTGURU_LEGACY` (inverse) |

Rules:

- Environment values are read by import-time configuration, by `configure()`
  called without a `Settings` object, and by `Settings.from_env()` (from
  `os.environ`, or from the mapping passed as `environ`). `configure(settings)`,
  `update()`, and `set_level()` never read them.
- When both a variable and its fallback are set, the variable wins. Fallback
  names produce no deprecation warning.
- A variable is parsed only when its setting is not passed as a keyword. An
  explicit keyword, including `None` or the built-in default, wins, even over
  an invalid environment value. `STRUCTGURU_RATE_LIMIT` is skipped only when
  both `rate_limit_max` and `rate_limit_period` are passed; otherwise the part
  that is not passed is taken from it.
- An empty value is not treated as unset. An empty `STRUCTGURU_SERVICE`
  sets an empty service name, an empty `STRUCTGURU_LEGACY` is ignored, and an
  empty value of any other variable in the table raises `ValueError`.
- Invalid values raise `ValueError`. During import-time configuration, the
  error propagates from `import structguru`.
- `STRUCTGURU_AUTOCONFIGURE` controls import only. When it is absent,
  `STRUCTGURU_LEGACY` set to `1`, `true`, `yes`, or `on` disables import-time
  configuration; any other value is ignored. With configuration disabled,
  logging stays off until `configure()` is called.
- File output, redaction, exceptions, hooks, and queue options have no
  environment variables.
- The stdlib bridge reads its own `STRUCTGURU_STDLIB_*` variables, and only
  when installed explicitly; see
  [Standard library bridge](integrations/stdlib.md#environment-variables).

## See also

- [Configure logging](../how-to/configure-logging.md)
- [Configuration and runtime control](configuration.md)
- [Delivery and lifecycle](../explanation/delivery-and-lifecycle.md)
