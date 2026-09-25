# Tutorials

The tutorials are lessons. Each one builds a small, working program step by
step and shows the output you should see after every change. Follow them in
order; the second lesson assumes you have finished the first.

1. [Get started with structguru](getting-started.md) builds a short script
   that logs an order being processed. You will write your first log line,
   add structured fields, attach context with `bind()` and `contextualize()`,
   switch between JSON and console output, set the service name and level,
   log an exception, and write the same records to a file.
2. [Log a web service](web-service.md) builds a small FastAPI service. You
   will add the structguru middleware, read the request summary line, follow
   one request through its log lines with a request ID, add your own fields in
   a handler, and route uvicorn's own logs through structguru.

## Before you start

You need Python 3.11 or newer, a terminal, and a text editor. The lessons
use `pip` in a virtual environment and `curl` to send requests to the web
service.

## After the tutorials

When you know the basics and want to solve a specific problem, go to the
[how-to guides](../how-to/index.md). To look up an exact signature or setting,
use the [reference](../reference/index.md).
