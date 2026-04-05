"""Memory benchmark for queued logging."""
import gc
import io
import time
import tracemalloc

from structguru import configure_structlog, logger
from structguru.queued import configure_queued_logging


def run_benchmark(num_logs: int = 100_000):
    stream = io.StringIO()
    configure_structlog(json_logs=True, stream=stream)
    listener = configure_queued_logging()

    gc.collect()
    tracemalloc.start()

    start_time = time.perf_counter()
    for i in range(num_logs):
        logger.info("Test log", id=i, somedata="x" * 100)

    duration = time.perf_counter() - start_time

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    listener.stop()

    print(f"Logged {num_logs} messages in {duration:.2f}s")
    print(f"Current memory usage: {current / 10**6:.2f} MB")
    print(f"Peak memory usage: {peak / 10**6:.2f} MB")

if __name__ == "__main__":
    run_benchmark()
