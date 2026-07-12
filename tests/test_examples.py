"""Smoke tests that the shipped examples run against the current public API.

The examples are documentation that users copy verbatim, so they must stay in
sync with the API — a rename or signature change that breaks an example should
fail here rather than silently rot.

Each example runs in a *subprocess*: importing an example has process-global
side effects (Celery signal handlers, native-mode configuration, contextvars),
so running it in-process would contaminate the rest of the suite.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# The examples import these; skip cleanly if any is absent (e.g. bare `pytest`
# without the integration extras installed).
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("celery")
pytest.importorskip("sqlalchemy")

_EXAMPLES = Path(__file__).resolve().parent.parent / "examples"

_FASTAPI_DRIVER = textwrap.dedent(
    """
    import asyncio, sys
    sys.path.insert(0, {example_dir!r})
    import main  # configure(format="console") + ASGI/Celery/SQLAlchemy wiring
    import httpx

    async def go():
        transport = httpx.ASGITransport(app=main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/users/alice")

    resp = asyncio.run(go())
    assert resp.status_code == 200, resp.status_code
    assert resp.json() == {{"id": 1, "name": "alice"}}, resp.json()
    print("EXAMPLE_OK")
    """
)


def test_full_stack_fastapi_example_serves_a_request() -> None:
    driver = _FASTAPI_DRIVER.format(example_dir=str(_EXAMPLES / "full_stack_fastapi"))
    result = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "EXAMPLE_OK" in result.stdout, result.stdout
