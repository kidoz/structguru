"""Tests for httpx integration."""

import io
import json

import httpx
from pytest_httpserver import HTTPServer

from structguru.config import configure_structlog
from structguru.integrations.httpx import StructguruHTTPXLoggingHooks


def _records(buf: io.StringIO) -> list[dict]:
    records = [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]
    # Ignore httpcore's own DEBUG chatter; keep only our outbound-request lines.
    return [r for r in records if r.get("logger") == "structguru.httpx"]


def test_httpx_logging_hooks_success(httpserver: HTTPServer) -> None:
    buf = io.StringIO()
    configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)
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
    configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)
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


def test_get_hooks_returns_fresh_lists() -> None:
    first = StructguruHTTPXLoggingHooks.get_hooks()
    first["request"].append(lambda req: None)
    second = StructguruHTTPXLoggingHooks.get_hooks()
    assert len(second["request"]) == 1
