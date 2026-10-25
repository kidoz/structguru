"""Tests for requests integration."""

import io
import json
import socket

import pytest
from conftest import configure
from pytest_httpserver import HTTPServer

from structguru.integrations.requests import get_logging_session


def _records(buf: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _with_credentials(url: str) -> str:
    """Embed userinfo in *url*, as callers sometimes hand it to requests."""
    return url.replace("http://", "http://user:pass@", 1)


def test_requests_logging_session_success(httpserver: HTTPServer) -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", stream=buf)
    httpserver.expect_request("/api").respond_with_json({"status": "ok"})

    session = get_logging_session()
    response = session.get(httpserver.url_for("/api"), headers={"x-request-id": "123"})

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


def test_requests_logged_url_strips_query_and_credentials(httpserver: HTTPServer) -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", stream=buf)
    httpserver.expect_request("/charge").respond_with_json({})

    url = httpserver.url_for("/charge")
    session = get_logging_session()
    # Credentials and a token embedded directly in the URL string are what the
    # integration sees (params= are merged later, during request preparation).
    session.get(_with_credentials(url) + "?token=SECRET")

    rec = _records(buf)[0]
    assert "SECRET" not in rec["http_url"]
    assert "pass" not in rec["http_url"]
    assert rec["http_url"] == f"{url}?"


def test_requests_logging_session_error(httpserver: HTTPServer) -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", stream=buf)
    httpserver.expect_request("/api/error").respond_with_json({"error": "bad"}, status=500)

    session = get_logging_session()
    response = session.get(httpserver.url_for("/api/error"))

    assert response.status_code == 500
    records = _records(buf)
    assert len(records) == 1
    rec = records[0]
    assert rec["message"] == "Outbound HTTP Request Failed"
    assert rec["level"] == "ERROR"
    assert rec["status_code"] == 500


def test_requests_logging_session_transport_failure() -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", stream=buf)
    # Bind and release a port so nothing is listening: requests raises before a
    # response exists, which the session must still log (without a status_code)
    # and re-raise.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    session = get_logging_session()

    with pytest.raises(Exception):
        session.get(f"http://127.0.0.1:{port}/api")

    records = _records(buf)
    assert len(records) == 1
    rec = records[0]
    assert rec["message"] == "Outbound HTTP Request Failed"
    assert rec["level"] == "ERROR"
    assert "status_code" not in rec


def test_requests_bytes_url_is_sanitized(httpserver: HTTPServer) -> None:
    buf = io.StringIO()
    configure(stream=buf)
    httpserver.expect_request("/path").respond_with_json({})
    url = httpserver.url_for("/path")
    get_logging_session().get(_with_credentials(url).encode() + b"?token=VALUE")
    assert _records(buf)[0]["http_url"] == f"{url}?"
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


def test_requests_logged_url_marks_params_supplied_separately(httpserver: HTTPServer) -> None:
    buf = io.StringIO()
    configure(service="test", level="DEBUG", stream=buf)
    httpserver.expect_request("/api").respond_with_json({})

    url = httpserver.url_for("/api")
    session = get_logging_session()
    session.get(url, params={"api_key": "SECRET"})
    session.params = {"token": "SESSION-SECRET"}
    session.get(url)
    session.params = {}
    session.get(url, params={"dropped": None})

    urls = [record["http_url"] for record in _records(buf)]
    assert urls == [f"{url}?", f"{url}?", url]
    assert "SECRET" not in buf.getvalue()


@pytest.mark.parametrize("error_type", [KeyboardInterrupt, SystemExit])
def test_requests_base_exception_is_logged_as_failure(
    monkeypatch: pytest.MonkeyPatch, error_type: type[BaseException]
) -> None:
    import requests

    error = error_type()

    def interrupted(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(requests.Session, "request", interrupted)
    buf = io.StringIO()
    configure(stream=buf)
    with pytest.raises(error_type) as caught:
        get_logging_session().get("https://host/path")
    assert caught.value is error
    [record] = _records(buf)
    assert record["message"] == "Outbound HTTP Request Failed"
    assert record["level"] == "ERROR"


def test_requests_logs_final_response_url_and_prepared_request_id(httpserver: HTTPServer) -> None:
    buf = io.StringIO()
    configure(stream=buf)
    final_url = httpserver.url_for("/final") + "?token=SECRET"
    httpserver.expect_request("/start").respond_with_data(
        status=302, headers={"Location": final_url}
    )
    httpserver.expect_request("/final").respond_with_json({})

    def add_request_id(request: object) -> object:
        request.headers["X-Request-ID"] = "prepared-id"  # type: ignore[attr-defined]
        return request

    response = get_logging_session().get(httpserver.url_for("/start"), auth=add_request_id)

    assert response.url == final_url
    [record] = _records(buf)
    assert record["http_url"] == f"{httpserver.url_for('/final')}?"
    assert record["request_id"] == "prepared-id"
    assert "SECRET" not in buf.getvalue()


@pytest.mark.parametrize("override", [None, ""])
def test_requests_positional_header_removes_or_empties_session_id_case_insensitively(
    httpserver: HTTPServer, override: str | None
) -> None:
    from types import MappingProxyType

    buf = io.StringIO()
    configure(stream=buf)
    httpserver.expect_request("/api").respond_with_json({})
    session = get_logging_session()
    session.headers["X-Request-ID"] = "session-id"

    session.request(
        "GET",
        httpserver.url_for("/api"),
        None,
        None,
        MappingProxyType({"x-ReQuEsT-Id": override}),
    )

    [record] = _records(buf)
    assert "request_id" not in record


def test_requests_transport_failure_reads_request_id_from_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import MappingProxyType

    import requests

    def fail(*args: object, **kwargs: object) -> None:
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests.Session, "request", fail)
    buf = io.StringIO()
    configure(stream=buf)

    with pytest.raises(requests.ConnectionError):
        get_logging_session().get(
            "https://host/path",
            headers=MappingProxyType({"X-Request-ID": "mapping-id"}),
        )

    [record] = _records(buf)
    assert record["request_id"] == "mapping-id"


def test_requests_redirect_failure_uses_exception_prepared_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    prepared = requests.PreparedRequest()
    prepared.prepare_url("https://final.example/after?token=SECRET", None)
    prepared.headers = requests.structures.CaseInsensitiveDict({"X-Request-ID": "redirect-id"})
    error = requests.TooManyRedirects("redirect failed")
    error.request = prepared

    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(requests.Session, "request", fail)
    buf = io.StringIO()
    configure(stream=buf)

    with pytest.raises(requests.TooManyRedirects) as caught:
        get_logging_session().get("https://initial.example/start")

    assert caught.value is error
    [record] = _records(buf)
    assert record["http_url"] == "https://final.example/after?"
    assert record["request_id"] == "redirect-id"
    assert "SECRET" not in buf.getvalue()


@pytest.mark.parametrize("positional", [False, True])
def test_one_shot_params_reach_the_prepared_request(
    httpserver: HTTPServer, positional: bool
) -> None:
    buf = io.StringIO()
    configure(stream=buf)
    httpserver.expect_request("/path", query_string="a=1&b=2").respond_with_json({})
    params = ((key, value) for key, value in [("a", "1"), ("b", "2")])
    session = get_logging_session()
    url = httpserver.url_for("/path")
    response = (
        session.request("GET", url, params) if positional else session.get(url, params=params)
    )
    assert response.status_code == 200
    assert response.request.url == f"{url}?a=1&b=2"
    assert _records(buf)[0]["http_url"] == f"{url}?"
    httpserver.check_assertions()


def test_early_failure_does_not_consume_params(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    def fail(*args: object, **kwargs: object) -> None:
        raise requests.RequestException("before preparation")

    monkeypatch.setattr(requests.Session, "request", fail)
    buf = io.StringIO()
    configure(stream=buf)
    params = iter([("a", "1")])
    with pytest.raises(requests.RequestException):
        get_logging_session().get("https://host/path", params=params)
    assert list(params) == [("a", "1")]
    assert _records(buf)[0]["http_url"] == "https://host/path?"
