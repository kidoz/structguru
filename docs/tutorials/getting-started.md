# Get started with structguru

In this tutorial we will build a short script, `shop.py`, that logs one order
as it moves through a small shop. Step by step you will write your first log
line, add structured fields, attach context with `bind()` and
`contextualize()`, switch between JSON and console output, set the service
name and level, log an exception, and write the same records to a file.

It takes about 15 minutes. You need Python 3.11 or newer and a terminal.

## Set up a project

Create a directory for the project, create a virtual environment inside it,
and install structguru:

```bash
mkdir shop
cd shop
python -m venv .venv
source .venv/bin/activate
pip install structguru
```

Check that the package is installed:

```bash
python -c "import structguru; print(structguru.__version__)"
```

You should see the version number:

```text
1.7.1
```

## Write your first log line

Create a file named `shop.py` with this content:

```python
from structguru import logger

logger.info("Shop started")
```

Now run it:

```bash
python shop.py
```

You should see one line of JSON:

```json
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:03.966584Z","service":"app","message":"Shop started"}
```

You did not configure anything: structguru set itself up when you imported it,
writing JSON to standard output for records at level `INFO` and above. Every
record carries the same standard keys: `logger` is the module that logged it,
`level` and `severity` give its level as a name and as a number, `timestamp`
is in UTC, `service` names your application (`app` until you set it), and
`message` is your text.

## Add structured fields

Add a second log call that records an order. Your `shop.py` should now look
like this:

```python
from structguru import logger

logger.info("Shop started")
logger.info("Order {order_id} placed", order_id=1042, items=3, total=59.9)
```

Run it again:

```bash
python shop.py
```

You should see two lines:

```json
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.002700Z","service":"app","message":"Shop started"}
{"items":3,"total":59.9,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.002731Z","service":"app","message":"Order 1042 placed"}
```

Look at the second line. `order_id` filled the `{order_id}` placeholder, so it
appears inside the message, `Order 1042 placed`, and not as a separate key.
`items` and `total` have no placeholder in the message, so each became a field
of its own. Your fields come first on the line, before the standard keys.

## Bind fields to a logger

The next records are all about order 1042. Instead of passing `order_id` to
every call, bind it once. Add these lines to the end of `shop.py`:

```python
from structguru import logger

logger.info("Shop started")
logger.info("Order {order_id} placed", order_id=1042, items=3, total=59.9)

order_log = logger.bind(order_id=1042)
order_log.info("Payment authorized", amount=59.9)
order_log.info("Order packed")
```

Run the script:

```bash
python shop.py
```

You should see four lines:

```json
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.037932Z","service":"app","message":"Shop started"}
{"items":3,"total":59.9,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.037964Z","service":"app","message":"Order 1042 placed"}
{"order_id":1042,"amount":59.9,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.037976Z","service":"app","message":"Payment authorized"}
{"order_id":1042,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.037981Z","service":"app","message":"Order packed"}
```

Both new lines carry `"order_id":1042`, even `Order packed`, which passes no
fields at all. `bind()` returned a new logger, `order_log`, that adds
`order_id` to everything it logs. The original `logger` is unchanged, which is
why the first two lines have no `order_id` key.

## Add context for a block of code

Sometimes a value belongs to a piece of work rather than to one logger, such
as the request that triggered it. `contextualize()` adds fields to every record
logged inside a `with` block, whichever logger logs it. Add these lines to the
end of `shop.py`:

```python
from structguru import logger

logger.info("Shop started")
logger.info("Order {order_id} placed", order_id=1042, items=3, total=59.9)

order_log = logger.bind(order_id=1042)
order_log.info("Payment authorized", amount=59.9)
order_log.info("Order packed")

with logger.contextualize(request_id="req-7"):
    order_log.info("Order shipped", carrier="UPS")
    logger.info("Customer notified")

logger.info("Shop idle")
```

Run the script:

```bash
python shop.py
```

You should see seven lines:

```json
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.073103Z","service":"app","message":"Shop started"}
{"items":3,"total":59.9,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.073137Z","service":"app","message":"Order 1042 placed"}
{"order_id":1042,"amount":59.9,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.073147Z","service":"app","message":"Payment authorized"}
{"order_id":1042,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.073158Z","service":"app","message":"Order packed"}
{"order_id":1042,"carrier":"UPS","request_id":"req-7","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.073171Z","service":"app","message":"Order shipped"}
{"request_id":"req-7","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.073176Z","service":"app","message":"Customer notified"}
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.073182Z","service":"app","message":"Shop idle"}
```

The two records logged inside the block carry `"request_id":"req-7"`, whether
they came from `order_log` or from `logger`. `Shop idle` is logged after the
block ends, so `request_id` is gone again. To learn how this context follows
your code across function calls, threads, and async tasks, read
[Context propagation](../explanation/context-propagation.md).

## Switch to console output

JSON suits log collectors, but it is hard to read while you develop. Import
`configure` and call it right after the import, asking for console output:

```python
from structguru import configure, logger

configure(format="console")

logger.info("Shop started")
logger.info("Order {order_id} placed", order_id=1042, items=3, total=59.9)

order_log = logger.bind(order_id=1042)
order_log.info("Payment authorized", amount=59.9)
order_log.info("Order packed")

with logger.contextualize(request_id="req-7"):
    order_log.info("Order shipped", carrier="UPS")
    logger.info("Customer notified")

logger.info("Shop idle")
```

Run the script:

```bash
python shop.py
```

You should see the same seven records, now as readable lines:

```text
2026-09-25T08:24:04.111614Z [INFO    ] Shop started
2026-09-25T08:24:04.111664Z [INFO    ] Order 1042 placed  items=3  total=59.9
2026-09-25T08:24:04.111681Z [INFO    ] Payment authorized  order_id=1042  amount=59.9
2026-09-25T08:24:04.111684Z [INFO    ] Order packed  order_id=1042
2026-09-25T08:24:04.111696Z [INFO    ] Order shipped  order_id=1042  carrier="UPS"  request_id="req-7"
2026-09-25T08:24:04.111700Z [INFO    ] Customer notified  request_id="req-7"
2026-09-25T08:24:04.111705Z [INFO    ] Shop idle
```

Each line shows the timestamp, the level, the message, and then your fields as
`key=value` pairs. In a terminal, the levels of debug, warning, error, and
critical records are shown in color.

## Set the service name and level

Now switch back to JSON and give your service a name. Replace the
`configure()` line with one that sets `service` and `level`, and add a debug
record after `Shop started`:

```python
from structguru import configure, logger

configure(service="shop", level="DEBUG")

logger.info("Shop started")
logger.debug("Loaded {count} products", count=12)
logger.info("Order {order_id} placed", order_id=1042, items=3, total=59.9)

order_log = logger.bind(order_id=1042)
order_log.info("Payment authorized", amount=59.9)
order_log.info("Order packed")

with logger.contextualize(request_id="req-7"):
    order_log.info("Order shipped", carrier="UPS")
    logger.info("Customer notified")

logger.info("Shop idle")
```

Run the script:

```bash
python shop.py
```

You should see eight lines of JSON:

```json
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.149812Z","service":"shop","message":"Shop started"}
{"logger":"__main__","level":"DEBUG","severity":7,"timestamp":"2026-09-25T08:24:04.149852Z","service":"shop","message":"Loaded 12 products"}
{"items":3,"total":59.9,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.149865Z","service":"shop","message":"Order 1042 placed"}
{"order_id":1042,"amount":59.9,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.149875Z","service":"shop","message":"Payment authorized"}
{"order_id":1042,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.149878Z","service":"shop","message":"Order packed"}
{"order_id":1042,"carrier":"UPS","request_id":"req-7","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.149890Z","service":"shop","message":"Order shipped"}
{"request_id":"req-7","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.149895Z","service":"shop","message":"Customer notified"}
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.149902Z","service":"shop","message":"Shop idle"}
```

The output is JSON again because each `configure()` call replaces the whole
configuration, and JSON is the default format. Every line now says
`"service":"shop"`, and the `Loaded 12 products` record appears because the
level is `DEBUG`; at the default `INFO` level it would be filtered out.

## Log an exception

Add a calculation that fails, and log the failure with `exception()`. Add these
lines to the end of `shop.py`:

```python
from structguru import configure, logger

configure(service="shop", level="DEBUG")

logger.info("Shop started")
logger.debug("Loaded {count} products", count=12)
logger.info("Order {order_id} placed", order_id=1042, items=3, total=59.9)

order_log = logger.bind(order_id=1042)
order_log.info("Payment authorized", amount=59.9)
order_log.info("Order packed")

with logger.contextualize(request_id="req-7"):
    order_log.info("Order shipped", carrier="UPS")
    logger.info("Customer notified")

logger.info("Shop idle")

try:
    discount = 100 / 0
except ZeroDivisionError:
    order_log.exception("Could not apply discount")
```

Run the script:

```bash
python shop.py
```

The output starts with the same eight records as before and ends with a new
one:

```json
{"order_id":1042,"exception":"Traceback (most recent call last):\n  File \"…/shop/shop.py\", line 20, in <module>\n    discount = 100 / 0\n               ~~~~^~~\nZeroDivisionError: division by zero","logger":"__main__","level":"ERROR","severity":3,"timestamp":"2026-09-25T08:24:04.186399Z","service":"shop","message":"Could not apply discount"}
```

`exception()` logged the record at `ERROR` level and put the traceback of the
exception being handled into the `exception` field. Because you called it on
`order_log`, the record also carries `order_id`.

## Write logs to a file

Finally, keep a copy of every record in a file. Import `flush`, add a file
sink with `logger.add()` right after `configure()`, and call `flush()` at the
end of the script. This is the finished `shop.py`:

```python
from structguru import configure, flush, logger

configure(service="shop", level="DEBUG")
logger.add("shop.log")

logger.info("Shop started")
logger.debug("Loaded {count} products", count=12)
logger.info("Order {order_id} placed", order_id=1042, items=3, total=59.9)

order_log = logger.bind(order_id=1042)
order_log.info("Payment authorized", amount=59.9)
order_log.info("Order packed")

with logger.contextualize(request_id="req-7"):
    order_log.info("Order shipped", carrier="UPS")
    logger.info("Customer notified")

logger.info("Shop idle")

try:
    discount = 100 / 0
except ZeroDivisionError:
    order_log.exception("Could not apply discount")

flush()
```

Run the script:

```bash
python shop.py
```

You should see the same nine records on your terminal as in the previous step.
Now look at the new file:

```bash
cat shop.log
```

It holds the same nine records:

```json
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.223478Z","service":"shop","message":"Shop started"}
{"logger":"__main__","level":"DEBUG","severity":7,"timestamp":"2026-09-25T08:24:04.223518Z","service":"shop","message":"Loaded 12 products"}
{"items":3,"total":59.9,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.223535Z","service":"shop","message":"Order 1042 placed"}
{"order_id":1042,"amount":59.9,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.223546Z","service":"shop","message":"Payment authorized"}
{"order_id":1042,"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.223549Z","service":"shop","message":"Order packed"}
{"order_id":1042,"carrier":"UPS","request_id":"req-7","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.223562Z","service":"shop","message":"Order shipped"}
{"request_id":"req-7","logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.223567Z","service":"shop","message":"Customer notified"}
{"logger":"__main__","level":"INFO","severity":6,"timestamp":"2026-09-25T08:24:04.223581Z","service":"shop","message":"Shop idle"}
{"order_id":1042,"exception":"Traceback (most recent call last):\n  File \"…/shop/shop.py\", line 21, in <module>\n    discount = 100 / 0\n               ~~~~^~~\nZeroDivisionError: division by zero","logger":"__main__","level":"ERROR","severity":3,"timestamp":"2026-09-25T08:24:04.223824Z","service":"shop","message":"Could not apply discount"}
```

`logger.add()` added a second destination, so every record still goes to the
terminal and is also appended to `shop.log`. structguru writes records on a
background thread; `flush()` waits until every record logged so far has been
written, so the file is complete at that point in your program. Each time you
run the script, nine more records are appended to the file. To learn more
about the background writer, read
[Delivery and lifecycle](../explanation/delivery-and-lifecycle.md).

## What you learned

You built a script that writes structured logs to your terminal and to a file.
Along the way you:

- logged with no setup, because structguru configures JSON output to standard
  output when you import it;
- saw keyword arguments either fill a `{placeholder}` in the message or become
  fields of their own;
- used `bind()` to create a logger that adds fields to every record, and
  `contextualize()` to add fields to every record inside a `with` block;
- switched to console output with `configure(format="console")`, and set the
  service name and level with `configure(service="shop", level="DEBUG")`;
- logged an exception and its traceback with `exception()`;
- added a file with `logger.add("shop.log")` and waited for pending records
  with `flush()`.

## Next steps

- Continue with the next lesson, [Log a web service](web-service.md).
- [Configure logging](../how-to/configure-logging.md) for production,
  including environment variables.
- [Send logs to files and sinks](../how-to/send-logs-to-sinks.md), such as a
  rotating file or your own function.
- [Log exceptions](../how-to/log-exceptions.md), including structured
  tracebacks.
- [Redact sensitive data](../how-to/redact-sensitive-data.md) such as passwords
  and tokens.
- Look up every method in the [Logger reference](../reference/logger.md) and
  every key in the [record format reference](../reference/record-format.md).
