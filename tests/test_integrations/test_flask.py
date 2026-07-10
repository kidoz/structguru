"""Tests for structguru.integrations.flask."""

from __future__ import annotations

import io

import pytest

from structguru._contextvars import get_contextvars
from structguru.config import configure_structlog
from structguru.integrations.flask import setup_flask_logging

flask = pytest.importorskip("flask")


def _build_app(**kwargs: object) -> flask.Flask:
    app = flask.Flask(__name__)
    app.config["TESTING"] = True
    setup_flask_logging(app, **kwargs)  # type: ignore[arg-type]

    seen: dict[str, dict[str, object]] = {}

    @app.route("/ping")
    def ping() -> str:
        seen["ping"] = dict(get_contextvars())
        return "pong"

    @app.route("/boom")
    def boom() -> str:
        raise RuntimeError("boom")

    app.extensions["_structguru_seen"] = seen
    return app


class TestSetupFlaskLogging:
    def test_generates_request_id_and_binds_context(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        app = _build_app()
        client = app.test_client()

        response = client.get("/ping")

        assert response.status_code == 200
        assert response.data == b"pong"
        header_id = response.headers.get("X-Request-ID")
        assert header_id and len(header_id) > 0

        seen = app.extensions["_structguru_seen"]["ping"]
        assert seen["method"] == "GET"
        assert seen["path"] == "/ping"
        assert seen["request_id"] == header_id
        assert "client_ip" in seen

        output = buf.getvalue()
        assert "Request completed" in output
        assert header_id in output

    def test_reuses_inbound_request_id_header(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        app = _build_app()
        response = app.test_client().get("/ping", headers={"X-Request-ID": "caller-123"})

        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == "caller-123"

        seen = app.extensions["_structguru_seen"]["ping"]
        assert seen["request_id"] == "caller-123"
        assert "caller-123" in buf.getvalue()

    def test_rejects_malformed_inbound_id(self) -> None:
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=io.StringIO())

        app = _build_app()
        response = app.test_client().get(
            "/ping",
            headers={"X-Request-ID": "bad\x00value"},
        )

        assert response.status_code == 200
        issued = response.headers["X-Request-ID"]
        assert issued != "bad\x00value"
        assert "\x00" not in issued

    def test_rejects_oversized_inbound_id(self) -> None:
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=io.StringIO())

        app = _build_app()
        oversized = "a" * 129
        response = app.test_client().get("/ping", headers={"X-Request-ID": oversized})

        assert response.headers["X-Request-ID"] != oversized

    def test_honors_custom_request_id_header(self) -> None:
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=io.StringIO())

        app = _build_app(request_id_header="X-Trace-Id")
        response = app.test_client().get("/ping", headers={"X-Trace-Id": "trace-xyz"})

        assert response.headers.get("X-Trace-Id") == "trace-xyz"
        assert response.headers.get("X-Request-ID") is None

    def test_log_request_false_skips_completion_log(self) -> None:
        buf = io.StringIO()
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=buf)

        app = _build_app(log_request=False)
        response = app.test_client().get("/ping")

        assert response.status_code == 200
        # Header still propagated, but no completion log line emitted.
        assert response.headers.get("X-Request-ID")
        assert "Request completed" not in buf.getvalue()

    def test_teardown_clears_contextvars(self) -> None:
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=io.StringIO())

        app = _build_app()
        client = app.test_client()

        client.get("/ping")
        # After the request cycle completes, contextvars must be clean so
        # subsequent non-request logging doesn't leak request state.
        assert get_contextvars() == {}

    def test_teardown_clears_context_after_exception(self) -> None:
        configure_structlog(service="test", level="DEBUG", json_logs=True, stream=io.StringIO())

        app = _build_app()
        # Let the test client surface the RuntimeError so the teardown_request
        # hook is still exercised in the error path.
        app.config["PROPAGATE_EXCEPTIONS"] = True
        with pytest.raises(RuntimeError, match="boom"):
            app.test_client().get("/boom")

        assert get_contextvars() == {}
