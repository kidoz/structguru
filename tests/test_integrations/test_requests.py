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
    configure(service="test", level="DEBUG", stream=buf)
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
def test_requests_logged_url_strips_query_and_credentials() -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", stream=buf)
    responses.add(responses.GET, "http://user:pass@test.local/charge", json={}, status=200)

    session = get_logging_session()
    # Credentials and a token embedded directly in the URL string are what the
    # integration sees (params= are merged later, during request preparation).
    session.get("http://user:pass@test.local/charge?token=SECRET")

    rec = _records(buf)[0]
    assert "SECRET" not in rec["http_url"]
    assert "pass" not in rec["http_url"]
    assert rec["http_url"] == "http://test.local/charge?"


@responses.activate
def test_requests_logging_session_error() -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", stream=buf)
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
    configure(service="test", level="DEBUG", stream=buf)
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


@responses.activate
def test_requests_bytes_url_is_sanitized() -> None:
    buf = io.StringIO()
    configure(stream=buf)
    responses.add(responses.GET, "https://user:pass@test.local/path", json={}, status=200)
    get_logging_session().get(b"https://user:pass@test.local/path?token=VALUE")
    assert _records(buf)[0]["http_url"] == "https://test.local/path?"
    assert "pass" not in buf.getvalue() and "VALUE" not in buf.getvalue()


def test_requests_invalid_port_preserves_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    original = requests.exceptions.InvalidURL("original failure")

    def fail(*args: object, **kwargs: object) -> None:
        raise original

    monkeypatch.setattr(requests.Session, "request", fail)
    buf = io.StringIO()
    configure(stream=buf)
    with pytest.raises(requests.exceptions.InvalidURL) as captured:
        get_logging_session().get("https://host:invalid/path")
    assert captured.value is original
    record = _records(buf)[0]
    assert record["http_url"] == "<unparsable-url>"
    assert record["message"] == "Outbound HTTP Request Failed"
