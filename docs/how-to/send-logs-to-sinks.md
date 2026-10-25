# Send logs to files and sinks

By default every record goes to stdout. Use this guide to write records to a
file, hand them to your own code, or add and remove destinations at runtime.

There are two ways to add a destination:

- **Options of `configure()`** (`file_path`, `callable_sinks`, `stream_sink`,
  `target`) are part of the configuration: `get_config()` reports them and the
  next `configure()` replaces them.
- **`logger.add()`** registers a sink at runtime. It survives reconfiguration,
  is not part of `get_config()`, and also receives records from the standard
  `logging` module.

## Write to a rotating file

```python
from structguru import configure

configure(
    service="api",
    file_path="/var/log/api/app.log",
    file_max_bytes=10 * 1024 * 1024,  # default 50 MB
    file_backup_count=3,  # default 5
)
```

The file replaces stdout. To write to both, add `also_stdout=True`:

```python
configure(service="api", file_path="/var/log/api/app.log", also_stdout=True)
```

- A relative `file_path` is resolved to an absolute path when you call
  `configure()`.
- On Unix, a new log file is created owner-only (`0600`); an existing file
  keeps its permissions. On Unix, log and lock opens reject symlinks and
  non-regular files. Log directories must not be attacker-writable. On Windows, files
  inherit filesystem permissions; structguru does not install an owner-only ACL.
- Processes that write the same path (for example, prefork workers) coordinate
  through an owner-only `app.log.lock` sidecar next to the log. Keep it on the
  same local filesystem as the log.
- For containers and distributed hosts, prefer stdout and an external log
  collector.

## Discard output

```python
configure(service="api", target="null")
```

Use this when another destination, such as a callable sink, should be the only
output.

## Send each line to a function

`callable_sinks` receives every rendered line, including its trailing newline:

```python
from structguru import configure, logger


def ship(line: str) -> None:
    send_to_collector(line)


configure(service="api", callable_sinks=[ship])
logger.info("order accepted")
```

- Sinks run on a dedicated background thread, one record at a time and in log
  order.
- Their queue holds `callable_queue_maxsize` records (default 1024); the
  `overflow` policy applies when it is full. See
  [Handle backpressure](handle-backpressure.md).
- An exception raised by a sink is counted in
  `writer_metrics()["callable_errors"]` and reported by a rate-limited
  `UserWarning`; logging continues.
- A record logged inside a sink still reaches the native writer, but skips all
  callable and `logger.add()` sinks, so a sink never feeds itself.

## Write to a stream synchronously

`stream_sink` writes each rendered line to an object with a `write()` method on
the calling thread, in addition to the configured target. The line is there as
soon as the logging call returns:

```python
import io

from structguru import configure, logger

buffer = io.StringIO()
configure(service="api", target="null", stream_sink=buffer)
logger.info("order accepted")
print(buffer.getvalue())
```

A `write()` that raises is counted in `writer_metrics()["stream_errors"]`.

## Add and remove sinks at runtime

`logger.add()` accepts a file path (`str` or `Path`), a callable that takes the
line, an object with a `write()` method, or a `logging.Handler`. It returns an
id for `logger.remove()`:

```python
import sys

from structguru import logger

file_id = logger.add("errors.log", level="ERROR")
alert_id = logger.add(lambda line: send_to_monitoring(line), level="CRITICAL")
stderr_id = logger.add(sys.stderr, level=40)

logger.remove(alert_id)  # remove one sink
logger.remove()  # remove every sink added through this logger
```

- `level` takes a name or an integer between `0` and `2**63 - 1`; anything else raises
  `ValueError`. Without it, the sink receives every level.
- Ids are process-wide: any `Logger` can remove a sink another one added.
  Removing an id that names no active sink raises `ValueError`; a non-integer
  raises `TypeError`.
- `logger.remove()` without an id removes the sinks added through that logger
  and the loggers derived from it with `bind()` or `opt()`.
- A `logging.Handler` keeps its own level and formatter. Its formatter wraps the
  rendered line, only `level` decides what it receives, and `remove()` closes
  it. Each handler can have one active registration across all loggers;
  registering it again raises `ValueError` until it has been removed.
- A file added this way is created owner-only (`0600`) on Unix, like the
  rotating file.

### Third-party records at `logger.add()` sinks

Sinks added with `logger.add()` are also attached to the standard-library root
logger, so they receive records from libraries that use `logging`. Those arrive
raw, as the library's plain message. To receive them rendered and redacted like
your own records, install the standard-library bridge; while it is installed,
the raw delivery stops, so no record arrives twice. See
[Capture standard-library logging](capture-stdlib-logging.md).

## Make sure lines have been written

Rendering and writing happen on background threads, so a returning logging call
does not mean the line has reached its destination. Call `flush()` when you need
that guarantee:

```python
import structguru
from structguru import logger

logger.info("checkpoint")
structguru.flush()  # returns once the line has reached its sink
```

You do not need `flush()` before exit: `shutdown()`, reconfiguration, fork, and
interpreter exit drain buffered records automatically.

## Check for failing destinations

```python
from structguru import writer_metrics

metrics = writer_metrics()
print(metrics["sink_errors"], metrics["callable_errors"], metrics["stream_errors"])
```

`sink_errors` counts failed file or stdout writes, including a record that one
destination failed to write while another (with `also_stdout=True`) succeeded.
For every counter, see [Metrics](../reference/metrics.md).

## See also

- [Configuration and runtime control](../reference/configuration.md)
- [Settings and environment variables](../reference/settings.md)
- [Logger](../reference/logger.md)
- [Delivery and lifecycle](../explanation/delivery-and-lifecycle.md)
