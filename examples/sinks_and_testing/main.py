"""Capture and verify rendered records without third-party test helpers.

Run with ``python examples/sinks_and_testing/main.py``. The example exits
silently on success and raises ``AssertionError`` if logging behavior changes.
"""

from __future__ import annotations

import io
import json

from structguru import configure, flush, get_config, logger, shutdown


def charge(order_id: int) -> None:
    logger.info("Charged order {order_id}", order_id=order_id, amount=10, token="t-1")
    logger.warning("Charge requires verification", order_id=order_id)


def main() -> None:
    previous = get_config()
    sink_id = None
    try:
        stream = io.StringIO()
        configure(service="billing-test", level="DEBUG", target="null", stream_sink=stream)

        callable_lines: list[str] = []
        sink_id = logger.add(callable_lines.append, level="WARNING")
        charge(42)
        flush()

        records = [json.loads(line) for line in stream.getvalue().splitlines()]
        assert len(records) == 2
        assert records[0]["message"] == "Charged order 42"
        assert records[0]["amount"] == 10
        assert records[0]["token"] == "[REDACTED]"
        assert "t-1" not in stream.getvalue()

        assert len(callable_lines) == 1
        assert json.loads(callable_lines[0])["message"] == "Charge requires verification"
    finally:
        if sink_id is not None:
            logger.remove(sink_id)
        if previous is None:
            shutdown()
        else:
            configure(previous)


if __name__ == "__main__":
    main()
