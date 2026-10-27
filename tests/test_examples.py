"""Smoke tests that the shipped examples run against the current public API.

The examples are documentation that users copy verbatim, so they must stay in
sync with the API — a rename or signature change that breaks an example should
fail here rather than silently rot.

Each example runs in a *subprocess*: importing an example has process-global
side effects (Celery signal handlers, native-mode configuration, contextvars),
so running it in-process would contaminate the rest of the suite.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import textwrap
import time
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

_GRANIAN_FASTAPI_DRIVER = textwrap.dedent(
    """
    import asyncio, sys
    sys.path.insert(0, {example_dir!r})
    import app
    import httpx

    async def go():
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/health", headers={{"X-Request-ID": "demo-123"}})

    resp = asyncio.run(go())
    assert resp.status_code == 200, resp.status_code
    assert resp.json() == {{"ok": True}}, resp.json()
    assert resp.headers["x-request-id"] == "demo-123"
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
    assert "Background task complete for user 1" in result.stdout, result.stdout


def test_granian_fastapi_example_serves_a_request() -> None:
    driver = _GRANIAN_FASTAPI_DRIVER.format(example_dir=str(_EXAMPLES / "granian_fastapi"))
    result = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "EXAMPLE_OK" in result.stdout, result.stdout
    assert '"message":"Health checked"' in result.stdout, result.stdout
    assert '"message":"Request completed"' in result.stdout, result.stdout
    assert '"request_id":"demo-123"' in result.stdout, result.stdout


def test_granian_fastapi_example_launches_from_python() -> None:
    pytest.importorskip("granian", reason="Granian is not installed")
    import httpx

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    env = os.environ.copy()
    env.update(PYTHONDONTWRITEBYTECODE="1", PORT=str(port), WEB_CONCURRENCY="1")
    process = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=_EXAMPLES / "granian_fastapi",
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    response = None
    try:
        deadline = time.monotonic() + 20
        with httpx.Client(timeout=0.2) as client:
            while response is None and process.poll() is None and time.monotonic() < deadline:
                try:
                    response = client.get(
                        f"http://127.0.0.1:{port}/health",
                        headers={"X-Request-ID": "granian-demo"},
                    )
                except httpx.TransportError:
                    time.sleep(0.05)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            output, _ = process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()

    assert response is not None, output
    assert response.status_code == 200, output
    assert response.json() == {"ok": True}, output
    assert response.headers["x-request-id"] == "granian-demo", output
    assert '"logger":"_granian"' in output, output
    assert '"service":"granian-fastapi-example"' in output, output
    assert '"request_id":"granian-demo"' in output, output
    assert output.count('"message":"Request completed"') == 1, output
