# Log SQLAlchemy queries

Call `setup_query_logging()` on an engine to time every SQL statement and log
the slow ones. Install the `sqlalchemy` extra:

```bash
pip install "structguru[sqlalchemy]"
```

## Log slow queries

```python
from sqlalchemy import create_engine

from structguru.integrations.sqlalchemy import setup_query_logging

engine = create_engine("sqlite:///:memory:")
setup_query_logging(engine, slow_threshold_ms=100.0)
```

A statement that takes at least `slow_threshold_ms` milliseconds (default 100)
is logged at `WARNING`:

```json
{"query":"<omitted>","duration_ms":223.7,"slow":true,"logger":"structguru.sqlalchemy","level":"WARN","severity":4,"timestamp":"…","service":"app","message":"Slow query"}
```

By default, `query` is `"<omitted>"` so SQL text and embedded literals stay out
of logs. Pass `include_query=True` to record the first 500 characters of the
statement. Query parameters are never logged by this integration.

Records logged during a request carry the request's context fields, like any
other record.

## Log every query

For local development or debugging, log every statement regardless of its
duration:

```python
setup_query_logging(engine, log_all=True, include_query=True)
```

Statements under `slow_threshold_ms` are logged at `DEBUG` as `Query executed`
(`slow=false`); slower ones keep their `Slow query` warning. Lower the level to
see them:

```python
from structguru import configure

configure(level="DEBUG")
```

```json
{"query":"SELECT 1","duration_ms":0.07,"slow":false,"logger":"structguru.sqlalchemy","level":"DEBUG","severity":7,"timestamp":"…","service":"app","message":"Query executed"}
```

Leave the threshold above zero: `slow_threshold_ms=0` marks every query as slow
and logs each one as a warning.

## Use a different logger name

```python
setup_query_logging(engine, logger_name="db.queries")
```

## See also

- [SQLAlchemy integration](../../reference/integrations/sqlalchemy.md)
- [Sample and rate-limit logs](../sample-and-rate-limit.md)
