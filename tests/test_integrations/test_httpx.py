"""Tests for httpx integration."""

import io
import json

import httpx
from conftest import configure
from pytest_httpserver import HTTPServer

from structguru.integrations.httpx import StructguruHTTPXLoggingHooks


def _records(buf: io.StringIO) -> list[dict]:
    records = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    # Ignore httpcore's own DEBUG chatter; keep only our outbound-request lines.
    return [r for r in records if r.get("logger") == "structguru.httpx"]


def test_httpx_logging_hooks_success(httpserver: HTTPServer) -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", json=True, stream=buf)
    httpserver.expect_request("/test").respond_with_data("OK", status=200)

    client = httpx.Client(event_hooks=StructguruHTTPXLoggingHooks.get_hooks())
    client.get(httpserver.url_for("/test"), headers={"x-request-id": "abc-123"})

    records = _records(buf)
    assert len(records) == 1
    rec = records[0]
    assert rec["message"] == "Outbound HTTP Request Completed"
    assert rec["level"] == "INFO"
    assert rec["http_method"] == "GET"
    assert rec["status_code"] == 200
    assert rec["request_id"] == "abc-123"
    assert isinstance(rec["duration_ms"], (int, float))


def test_httpx_logging_hooks_error(httpserver: HTTPServer) -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", json=True, stream=buf)
    httpserver.expect_request("/error").respond_with_data("Error", status=500)

    client = httpx.Client(event_hooks=StructguruHTTPXLoggingHooks.get_hooks())
    response = client.get(httpserver.url_for("/error"))

    assert response.status_code == 500
    records = _records(buf)
    assert len(records) == 1
    rec = records[0]
    assert rec["message"] == "Outbound HTTP Request Failed"
    assert rec["level"] == "ERROR"
    assert rec["status_code"] == 500


def test_httpx_logged_url_strips_query_and_credentials(httpserver: HTTPServer) -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", json=True, stream=buf)
    httpserver.expect_request("/charge").respond_with_data("OK", status=200)

    client = httpx.Client(event_hooks=StructguruHTTPXLoggingHooks.get_hooks())
    # Query string carries a token; it must not reach the log.
    client.get(httpserver.url_for("/charge"), params={"api_key": "SECRET"})

    rec = _records(buf)[0]
    assert "SECRET" not in rec["http_url"]
    assert "api_key" not in rec["http_url"]
    assert rec["http_url"].endswith("/charge?")


def test_get_hooks_returns_fresh_lists() -> None:
    first = StructguruHTTPXLoggingHooks.get_hooks()
    first["request"].append(lambda req: None)
    second = StructguruHTTPXLoggingHooks.get_hooks()
    assert len(second["request"]) == 1


async def test_httpx_async_logging_hooks_success() -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", json=True, stream=buf)

    async def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond),
        event_hooks=StructguruHTTPXLoggingHooks.get_async_hooks(),
    ) as client:
        response = await client.get(
            "https://example.test/async",
            headers={"x-request-id": "async-123"},
        )

    assert response.status_code == 200
    records = _records(buf)
    assert len(records) == 1
    assert records[0]["request_id"] == "async-123"
    assert records[0]["message"] == "Outbound HTTP Request Completed"


def test_get_async_hooks_returns_fresh_lists() -> None:
    first = StructguruHTTPXLoggingHooks.get_async_hooks()
    first["request"].append(lambda req: None)
    second = StructguruHTTPXLoggingHooks.get_async_hooks()
    assert len(second["request"]) == 1
