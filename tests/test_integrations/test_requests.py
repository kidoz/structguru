"""Tests for requests integration."""

import io
import json

import pytest
import responses
from conftest import configure

from structguru.integrations.requests import get_logging_session


def _records(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


@responses.activate
def test_requests_logging_session_success() -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", json=True, stream=buf)
    responses.add(responses.GET, "http://test.local/api", json={"status": "ok"}, status=200)

    session = get_logging_session()
    response = session.get("http://test.local/api", headers={"x-request-id": "123"})

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    records = _records(buf)
    assert len(records) == 1
    rec = records[0]
    assert rec["message"] == "Outbound HTTP Request Completed"
    assert rec["level"] == "INFO"
    assert rec["http_method"] == "GET"
    assert rec["status_code"] == 200
    assert rec["request_id"] == "123"
    assert isinstance(rec["duration_ms"], (int, float))


@responses.activate
def test_requests_logging_session_error() -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", json=True, stream=buf)
    responses.add(responses.GET, "http://test.local/api/error", json={"error": "bad"}, status=500)

    session = get_logging_session()
    response = session.get("http://test.local/api/error")

    assert response.status_code == 500
    records = _records(buf)
    assert len(records) == 1
    rec = records[0]
    assert rec["message"] == "Outbound HTTP Request Failed"
    assert rec["level"] == "ERROR"
    assert rec["status_code"] == 500


@responses.activate
def test_requests_logging_session_transport_failure() -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", json=True, stream=buf)
    # No matching response registered -> requests raises, which the session
    # must still log as a failure (without a status_code) and re-raise.
    session = get_logging_session()

    with pytest.raises(Exception):
        session.get("http://unmatched.local/api")

    records = _records(buf)
    assert len(records) == 1
    rec = records[0]
    assert rec["message"] == "Outbound HTTP Request Failed"
    assert rec["level"] == "ERROR"
    assert "status_code" not in rec
