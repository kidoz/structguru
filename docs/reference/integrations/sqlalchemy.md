# SQLAlchemy

`structguru.integrations.sqlalchemy.setup_query_logging()` attaches query
timing listeners to an engine. Install the `sqlalchemy` extra
(`sqlalchemy>=2.0`).

```python
from structguru.integrations.sqlalchemy import setup_query_logging

setup_query_logging(engine, slow_threshold_ms=100.0)
```

## Parameters

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `engine` | `sqlalchemy.engine.Engine` | required | Engine to instrument. |
| `slow_threshold_ms` | `float` | `100.0` | Duration, in milliseconds, at or above which a query is slow. `0` makes every query slow. |
| `log_all` | `bool` | `False` | Also log queries below the threshold. |
| `include_query` | `bool` | `False` | Include the first 500 characters of the SQL statement instead of `"<omitted>"`. |
| `logger_name` | `str` | `"structguru.sqlalchemy"` | `logger` field of the records. |

## Listeners

| Event | Action |
|---|---|
| `before_cursor_execute` | Records the start time for the execution context. |
| `after_cursor_execute` | Measures the duration and logs a record when required. |
| `handle_error` | Discards the start time of a failed execution; logs nothing. |

## Logged records

| Query | Message | Level | `slow` |
|---|---|---|---|
| duration >= `slow_threshold_ms` | `Slow query` | `WARNING` (rendered `WARN`) | `true` |
| duration < `slow_threshold_ms`, `log_all=True` | `Query executed` | `DEBUG` | `false` |
| duration < `slow_threshold_ms`, `log_all=False` | not logged | | |

| Field | Value |
|---|---|
| `query` | `"<omitted>"` by default. With `include_query=True`, the SQL statement text cut to its first 500 characters. Parameters are not logged. |
| `duration_ms` | Cursor execution time in milliseconds, rounded to two decimals. |
| `slow` | Whether the query reached the threshold. |

Failed executions are not logged by this integration.

## API

::: structguru.integrations.sqlalchemy
    options:
      heading_level: 3

## See also

- [SQLAlchemy](../../how-to/integrations/sqlalchemy.md)
