"""Tests for structguru.integrations.flask."""

from __future__ import annotations

import io

import pytest
from conftest import configure

from structguru._contextvars import get_contextvars
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
        configure(service="test", level="DEBUG", stream=buf)

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
        configure(service="test", level="DEBUG", stream=buf)

        app = _build_app()
        response = app.test_client().get("/ping", headers={"X-Request-ID": "caller-123"})

        assert response.status_code == 200
        assert response.headers["X-Request-ID"] == "caller-123"

        seen = app.extensions["_structguru_seen"]["ping"]
        assert seen["request_id"] == "caller-123"
        assert "caller-123" in buf.getvalue()

    def test_rejects_malformed_inbound_id(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())

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
        configure(service="test", level="DEBUG", stream=io.StringIO())

        app = _build_app()
        oversized = "a" * 129
        response = app.test_client().get("/ping", headers={"X-Request-ID": oversized})

        assert response.headers["X-Request-ID"] != oversized

    def test_honors_custom_request_id_header(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())

        app = _build_app(request_id_header="X-Trace-Id")
        response = app.test_client().get("/ping", headers={"X-Trace-Id": "trace-xyz"})

        assert response.headers.get("X-Trace-Id") == "trace-xyz"
        assert response.headers.get("X-Request-ID") is None

    def test_log_request_false_skips_completion_log(self) -> None:
        buf = io.StringIO()
        configure(service="test", level="DEBUG", stream=buf)

        app = _build_app(log_request=False)
        response = app.test_client().get("/ping")

        assert response.status_code == 200
        # Header still propagated, but no completion log line emitted.
        assert response.headers.get("X-Request-ID")
        assert "Request completed" not in buf.getvalue()

    def test_teardown_clears_contextvars(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())

        app = _build_app()
        client = app.test_client()

        client.get("/ping")
        # After the request cycle completes, contextvars must be clean so
        # subsequent non-request logging doesn't leak request state.
        assert get_contextvars() == {}

    def test_teardown_clears_context_after_exception(self) -> None:
        configure(service="test", level="DEBUG", stream=io.StringIO())

        app = _build_app()
        # Let the test client surface the RuntimeError so the teardown_request
        # hook is still exercised in the error path.
        app.config["PROPAGATE_EXCEPTIONS"] = True
        with pytest.raises(RuntimeError, match="boom"):
            app.test_client().get("/boom")

        assert get_contextvars() == {}


def _records(buf: io.StringIO) -> list[dict[str, object]]:
    import json

    return [json.loads(line) for line in buf.getvalue().splitlines() if line.strip()]


def _fake_clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    from types import SimpleNamespace

    from structguru.integrations import flask as flask_module

    clock = [0.0]
    monkeypatch.setattr(flask_module, "time", SimpleNamespace(perf_counter=lambda: clock[0]))
    return clock


@pytest.mark.parametrize("with_context", [False, True])
def test_streamed_body_runs_with_request_context_and_is_summarized_at_the_end(
    monkeypatch: pytest.MonkeyPatch, with_context: bool
) -> None:
    from structguru import logger
    from structguru._contextvars import bind_contextvars

    clock = _fake_clock(monkeypatch)
    buf = io.StringIO()
    configure(service="test", stream=buf)
    app = flask.Flask(__name__)
    setup_flask_logging(app)

    @app.route("/stream")
    def stream() -> flask.Response:
        bind_contextvars(user_id="u-1")

        def generate():  # type: ignore[no-untyped-def]
            for i in range(2):
                clock[0] += 1.0
                logger.info("chunk", i=i)
                bind_contextvars(chunks=i + 1)
                yield f"part-{i}"

        body = flask.stream_with_context(generate()) if with_context else generate()
        return flask.Response(body)

    response = app.test_client().get("/stream", headers={"X-Request-ID": "rid-stream"})
    # Werkzeug's test client pulls the first chunk eagerly; the summary waits
    # for the rest, and nothing stays bound between chunks.
    assert get_contextvars() == {}
    assert [record["message"] for record in _records(buf)] == ["chunk"]
    assert response.get_data() == b"part-0part-1"
    assert get_contextvars() == {}

    first, second, summary = _records(buf)
    for record in (first, second):
        assert record["message"] == "chunk"
        assert record["request_id"] == "rid-stream"
        assert record["path"] == "/stream"
        assert record["user_id"] == "u-1"
    assert second["chunks"] == 1
    assert summary["message"] == "Request completed"
    assert summary["request_id"] == "rid-stream"
    assert summary["status_code"] == 200
    assert summary["duration_ms"] == 2000.0
    assert summary["chunks"] == 2
    assert response.headers["X-Request-ID"] == "rid-stream"


def test_streamed_body_summary_is_logged_for_head_and_early_close() -> None:
    buf = io.StringIO()
    configure(service="test", stream=buf)
    app = flask.Flask(__name__)
    setup_flask_logging(app)
    closed: list[dict[str, object]] = []

    @app.route("/stream", methods=["GET", "HEAD"])
    def stream() -> flask.Response:
        def generate():  # type: ignore[no-untyped-def]
            try:
                yield "a"
                yield "b"
            finally:
                closed.append(get_contextvars())

        return flask.Response(generate())

    client = app.test_client()
    head = client.head("/stream", headers={"X-Request-ID": "rid-head"})
    head.close()
    [summary] = _records(buf)
    assert summary["message"] == "Request completed"
    assert summary["request_id"] == "rid-head"

    buf.truncate(0)
    buf.seek(0)
    response = client.get("/stream", headers={"X-Request-ID": "rid-partial"}, buffered=False)
    chunks = iter(response.response)
    assert next(chunks) == b"a"
    response.close()
    assert closed[-1]["request_id"] == "rid-partial"
    [summary] = _records(buf)
    assert summary["request_id"] == "rid-partial"
    assert get_contextvars() == {}


def test_streamed_body_failure_is_logged_as_failed() -> None:
    buf = io.StringIO()
    configure(service="test", stream=buf)
    app = flask.Flask(__name__)
    setup_flask_logging(app)

    @app.route("/stream")
    def stream() -> flask.Response:
        def generate():  # type: ignore[no-untyped-def]
            yield "a"
            raise RuntimeError("stream broke")

        return flask.Response(generate())

    response = app.test_client().get("/stream", headers={"X-Request-ID": "rid-broken"})
    with pytest.raises(RuntimeError, match="stream broke"):
        response.get_data()
    [summary] = _records(buf)
    assert summary["message"] == "Request failed"
    assert summary["level"] == "ERROR"
    assert summary["request_id"] == "rid-broken"
    assert "RuntimeError: stream broke" in str(summary["exception"])
    assert get_contextvars() == {}


def test_binds_before_hooks_registered_earlier() -> None:
    from structguru import logger
    from structguru._contextvars import bind_contextvars

    buf = io.StringIO()
    configure(service="test", stream=buf)
    app = flask.Flask(__name__)
    seen: list[dict[str, object]] = []

    @app.before_request
    def authenticate() -> tuple[str, int] | None:
        seen.append(get_contextvars())
        bind_contextvars(user_id="u-7")
        if flask.request.path == "/denied":
            logger.info("denied")
            return ("nope", 401)
        return None

    setup_flask_logging(app)

    @app.route("/ok")
    def ok() -> str:
        logger.info("in view")
        return "ok"

    client = app.test_client()
    denied = client.get("/denied", headers={"X-Request-ID": "rid-denied"})
    assert denied.status_code == 401
    assert denied.headers["X-Request-ID"] == "rid-denied"
    assert seen[-1]["request_id"] == "rid-denied"
    denied_log, denied_summary = _records(buf)
    assert denied_log["request_id"] == "rid-denied"
    assert denied_summary["message"] == "Request completed"
    assert denied_summary["status_code"] == 401
    assert denied_summary["request_id"] == "rid-denied"
    assert denied_summary["user_id"] == "u-7"

    buf.truncate(0)
    buf.seek(0)
    assert client.get("/ok").status_code == 200
    view_log, summary = _records(buf)
    # Context bound by an earlier hook survives into the view and summary.
    assert view_log["user_id"] == "u-7"
    assert summary["user_id"] == "u-7"
    assert get_contextvars() == {}


def test_propagated_exception_is_summarized_from_teardown() -> None:
    buf = io.StringIO()
    configure(service="test", stream=buf)
    app = _build_app()
    app.config["PROPAGATE_EXCEPTIONS"] = True

    with pytest.raises(RuntimeError, match="boom"):
        app.test_client().get("/boom", headers={"X-Request-ID": "rid-boom"})

    [summary] = _records(buf)
    assert summary["message"] == "Request failed"
    assert summary["level"] == "ERROR"
    assert summary["status_code"] == 500
    assert summary["request_id"] == "rid-boom"
    assert "RuntimeError: boom" in str(summary["exception"])
    assert get_contextvars() == {}


def test_handled_exception_is_summarized_once() -> None:
    buf = io.StringIO()
    configure(service="test", stream=buf)
    app = _build_app()
    app.config["TESTING"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = False

    response = app.test_client().get("/boom")
    assert response.status_code == 500
    [summary] = [r for r in _records(buf) if r["logger"] == "structguru.flask"]
    assert summary["message"] == "Request completed"
    assert summary["status_code"] == 500


def test_body_consumed_inside_the_request_keeps_the_request_context() -> None:
    from structguru import logger

    buf = io.StringIO()
    configure(service="test", stream=buf)
    app = flask.Flask(__name__)
    after: list[dict[str, object]] = []

    @app.after_request
    def buffer_body(response: flask.Response) -> flask.Response:
        # Registered before setup, so it runs after the structguru hook.
        response.get_data()
        after.append(get_contextvars())
        return response

    @app.teardown_request
    def teardown(exc: BaseException | None) -> None:
        logger.info("teardown")

    setup_flask_logging(app)

    @app.route("/stream")
    def stream() -> flask.Response:
        return flask.Response(iter(["a", "b"]))

    response = app.test_client().get("/stream", headers={"X-Request-ID": "rid-buffered"})
    assert response.get_data() == b"ab"
    assert after[0]["request_id"] == "rid-buffered"
    summary, teardown_record = _records(buf)
    assert summary["message"] == "Request completed"
    assert teardown_record["request_id"] == "rid-buffered"
    assert get_contextvars() == {}


@pytest.mark.parametrize("streamed", [False, True])
@pytest.mark.parametrize("fails_again", [False, True])
def test_failing_later_after_request_hook_logs_one_summary_with_the_final_status(
    streamed: bool, fails_again: bool
) -> None:
    buf = io.StringIO()
    configure(service="test", stream=buf)
    app = flask.Flask(__name__)
    app.config["PROPAGATE_EXCEPTIONS"] = False

    @app.after_request
    def later_hook(response: flask.Response) -> flask.Response:
        # Registered before setup, so it runs after the structguru hook.
        if fails_again or response.status_code == 200:
            raise RuntimeError("after hook failed")
        return response

    setup_flask_logging(app)

    @app.route("/ok")
    def ok() -> flask.Response:
        return flask.Response(iter(["a", "b"]) if streamed else "ok")

    app.logger.disabled = True
    response = app.test_client().get("/ok", headers={"X-Request-ID": "rid-after"})
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "rid-after"
    response.close()
    [summary] = [r for r in _records(buf) if r["logger"] == "structguru.flask"]
    assert summary["message"] == ("Request failed" if fails_again else "Request completed")
    assert summary["status_code"] == 500
    assert summary["request_id"] == "rid-after"
    assert get_contextvars() == {}
