"""Propagate request context through async tasks and a thread pool.

Run with ``python examples/context_propagation/main.py``. Every work record
carries ``request_id=req-42``; the final record runs outside the context.
"""

from __future__ import annotations

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor

from structguru import configure, flush, get_contextvars, logger


def blocking_work(item: str) -> None:
    logger.info("Thread work", item=item)


async def async_work(item: str) -> None:
    logger.info("Async work", item=item)
    await asyncio.sleep(0)


async def main() -> None:
    with logger.contextualize(request_id="req-42"):
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(async_work("first"))
            tasks.create_task(async_work("second"))

        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor(max_workers=1) as pool:
            await loop.run_in_executor(
                pool,
                contextvars.copy_context().run,
                blocking_work,
                "third",
            )

    assert get_contextvars() == {}
    logger.info("Request finished")


if __name__ == "__main__":
    configure(service="context-example")
    asyncio.run(main())
    flush()
