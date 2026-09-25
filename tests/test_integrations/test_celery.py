"""Tests for structguru.integrations.celery."""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from structguru._contextvars import bind_contextvars, clear_contextvars, get_contextvars


@pytest.fixture(autouse=True)
def reset_celery_setup(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from structguru.integrations import celery as integration

    monkeypatch.setattr(integration, "_setup_done", False)
    clear_contextvars()
    yield
    clear_contextvars()


def test_eager_context_stacks_are_isolated_between_threads() -> None:
    pytest.importorskip("celery")
    from celery.utils.dispatch import Signal

    from structguru.integrations import celery as integration

    signals = SimpleNamespace(
        before_task_publish=Signal(),
        task_prerun=Signal(),
        task_postrun=Signal(),
    )
    barrier = threading.Barrier(2)
    with (
        patch.dict("sys.modules", {"celery.signals": signals}),
        patch.object(integration, "_setup_done", False),
    ):
        integration.setup_celery_logging(context_keys=["request_id"])
        # Repeated setup must not connect duplicate handlers or context stacks.
        with pytest.warns(RuntimeWarning, match="different options"):
            integration.setup_celery_logging()

        def run(identity: str) -> None:
            bind_contextvars(request_id=identity, private="caller only")
            caller = get_contextvars()
            headers: dict[str, Any] = {}
            signals.before_task_publish.send(sender=None, headers=None)
            signals.before_task_publish.send(sender=None, headers=headers)
            assert headers == {"structguru_context": {"request_id": identity}}
            task = SimpleNamespace(
                name="eager",
                request=SimpleNamespace(is_eager=True, headers=headers),
            )
            results = signals.task_prerun.send(sender=None, task=task, task_id=identity)
            assert len(results) == 1 and results[0][1] is None
            barrier.wait(timeout=3)
            assert get_contextvars() == {
                "request_id": identity,
                "task_name": "eager",
                "task_id": identity,
            }
            signals.task_postrun.send(sender=None)
            assert get_contextvars() == caller
            clear_contextvars()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, identity) for identity in ("first", "second")]
            for future in futures:
                future.result(timeout=5)


@pytest.mark.parametrize("propagate", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_real_eager_tasks_restore_parent_and_caller_context(propagate: bool, fail: bool) -> None:
    pytest.importorskip("celery")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
        from celery import Celery
        from structguru.integrations.celery import setup_celery_logging
        from structguru._contextvars import bind_contextvars, get_contextvars
        setup_celery_logging(propagate_context={propagate}, context_keys=['request_id'])
        app = Celery('test', broker='memory://', backend='cache+memory://')
        observed = []
        @app.task
        def child():
            context = get_contextvars()
            observed.append(context)
            assert 'task_id' in context
            assert 'parent_only' not in context
            assert 'private' not in context
            assert context.get('request_id') == ('child-request' if {propagate} else None)
            if {fail}:
                raise ValueError('child failed')
        @app.task
        def parent():
            bind_contextvars(parent_only='retained')
            before = get_contextvars()
            assert before.get('request_id') == ('caller-request' if {propagate} else None)
            try:
                    child.apply(throw=True, headers={{
                        'structguru_context': {{'request_id': 'child-request'}}}})
            except ValueError:
                assert {fail}
            assert observed[-1]['task_id'] != before['task_id']
            assert get_contextvars() == before
            if {fail}:
                raise RuntimeError('parent failed')
        bind_contextvars(request_id='caller-request', private='caller-only')
        before = get_contextvars()
        for _ in range(2):
            try:
                parent.apply(throw=True)
            except RuntimeError:
                assert {fail}
            assert get_contextvars() == before
        assert len(observed) == 2
        app.close()
    """),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_real_worker_signals_clear_stale_context_and_restore_nested_tasks() -> None:
    pytest.importorskip("celery")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent("""
        from types import SimpleNamespace
        from celery.signals import task_prerun, task_postrun
        from structguru.integrations.celery import setup_celery_logging
        from structguru._contextvars import bind_contextvars, get_contextvars
        setup_celery_logging()
        task = SimpleNamespace(name='worker', request=SimpleNamespace(is_eager=False))
        bind_contextvars(stale='previous request')
        task_prerun.send(sender=object(), task=task, task_id='parent')
        assert get_contextvars() == {'task_id': 'parent', 'task_name': 'worker'}
        bind_contextvars(parent_only=True)
        parent = get_contextvars()
        task_prerun.send(sender=object(), task=task, task_id='child')
        assert get_contextvars() == {'task_id': 'child', 'task_name': 'worker'}
        task_postrun.send(sender=object())
        assert get_contextvars() == parent
        task_postrun.send(sender=object())
        assert get_contextvars() == {}
        task_prerun.send(sender=object(), task=task, task_id='next')
        assert get_contextvars() == {'task_id': 'next', 'task_name': 'worker'}
        task_postrun.send(sender=object())
        assert get_contextvars() == {}
    """),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


class TestSetupCeleryLogging:
    def test_binds_task_context_on_prerun(self) -> None:
        clear_contextvars()

        # Mock celery signals
        handlers: dict[str, Any] = {}

        def make_signal(name: str) -> MagicMock:
            sig = MagicMock()

            def connect(fn: Any = None, weak: bool = True) -> Any:
                if fn is None:

                    def decorator(f: Any) -> Any:
                        handlers[name] = f
                        return f

                    return decorator
                handlers[name] = fn
                return fn

            sig.connect = MagicMock(side_effect=connect)
            return sig

        mock_signals = MagicMock()
        mock_signals.before_task_publish = make_signal("before_task_publish")
        mock_signals.task_prerun = make_signal("task_prerun")
        mock_signals.task_postrun = make_signal("task_postrun")

        with patch.dict("sys.modules", {"celery": MagicMock(), "celery.signals": mock_signals}):
            from structguru.integrations.celery import setup_celery_logging

            setup_celery_logging()

        # Simulate task_prerun
        mock_task = MagicMock()
        mock_task.name = "my_app.tasks.send_email"
        mock_task.request = MagicMock()
        mock_task.request.structguru_context = None

        handlers["task_prerun"](task_id="abc-123", task=mock_task)

        ctx = get_contextvars()
        assert ctx["task_id"] == "abc-123"
        assert ctx["task_name"] == "my_app.tasks.send_email"

    def test_clears_context_on_postrun(self) -> None:
        clear_contextvars()
        bind_contextvars(task_id="old")

        handlers: dict[str, Any] = {}

        def make_signal(name: str) -> MagicMock:
            sig = MagicMock()

            def connect(fn: Any = None, weak: bool = True) -> Any:
                if fn is None:

                    def decorator(f: Any) -> Any:
                        handlers[name] = f
                        return f

                    return decorator
                handlers[name] = fn
                return fn

            sig.connect = MagicMock(side_effect=connect)
            return sig

        mock_signals = MagicMock()
        mock_signals.before_task_publish = make_signal("before_task_publish")
        mock_signals.task_prerun = make_signal("task_prerun")
        mock_signals.task_postrun = make_signal("task_postrun")

        with patch.dict("sys.modules", {"celery": MagicMock(), "celery.signals": mock_signals}):
            from structguru.integrations.celery import setup_celery_logging

            setup_celery_logging()

        handlers["task_postrun"]()
        assert get_contextvars() == {}

    def test_context_propagation_via_headers(self) -> None:
        clear_contextvars()
        bind_contextvars(request_id="req-999", task_id="parent-id", task_name="parent-task")

        handlers: dict[str, Any] = {}

        def make_signal(name: str) -> MagicMock:
            sig = MagicMock()

            def connect(fn: Any = None, weak: bool = True) -> Any:
                if fn is None:

                    def decorator(f: Any) -> Any:
                        handlers[name] = f
                        return f

                    return decorator
                handlers[name] = fn
                return fn

            sig.connect = MagicMock(side_effect=connect)
            return sig

        mock_signals = MagicMock()
        mock_signals.before_task_publish = make_signal("before_task_publish")
        mock_signals.task_prerun = make_signal("task_prerun")
        mock_signals.task_postrun = make_signal("task_postrun")

        with patch.dict("sys.modules", {"celery": MagicMock(), "celery.signals": mock_signals}):
            from structguru.integrations.celery import setup_celery_logging

            setup_celery_logging(propagate_context=True)

        # Simulate before_task_publish
        task_headers: dict[str, Any] = {}
        handlers["before_task_publish"](headers=task_headers)

        assert "structguru_context" in task_headers
        assert task_headers["structguru_context"]["request_id"] == "req-999"

        child = MagicMock()
        child.name = "child-task"
        child.request = task_headers
        handlers["task_prerun"](task_id="child-id", task=child)
        assert get_contextvars() == {
            "request_id": "req-999",
            "task_id": "child-id",
            "task_name": "child-task",
        }
        handlers["task_postrun"]()
        assert get_contextvars() == {}


def _dispatch_signals(**options: Any) -> SimpleNamespace:
    """Run setup against real Celery dispatch signals isolated from the global ones."""
    pytest.importorskip("celery")
    from celery.utils.dispatch import Signal

    from structguru.integrations import celery as integration

    signals = SimpleNamespace(
        before_task_publish=Signal(),
        task_prerun=Signal(),
        task_postrun=Signal(),
    )
    with patch.dict("sys.modules", {"celery.signals": signals}):
        integration.setup_celery_logging(**options)
    return signals


class _Opaque:
    def __repr__(self) -> str:
        return "Opaque(secret-repr)"

    def __str__(self) -> str:
        return "opaque-secret-str"


def test_propagated_context_is_transport_safe_bounded_and_redacted() -> None:
    import datetime
    import decimal
    import enum
    import json
    import uuid
    from pathlib import PurePosixPath

    class Color(enum.Enum):
        RED = "red"

    signals = _dispatch_signals()
    request_uuid = uuid.UUID(int=1)
    bind_contextvars(
        request_id="req-1",
        path=PurePosixPath("/srv/app"),
        tags={"only"},
        pair=(1, 2),
        user=_Opaque(),
        uid=request_uuid,
        amount=decimal.Decimal("1.50"),
        when=datetime.datetime(2020, 1, 2, 3, 4, 5),
        color=Color.RED,
        huge=10**30,
        ratio=float("nan"),
        raw=b"\x00bytes",
        Password="hunter2",
        nested={"Token": "t0k", "items": [1, (2, 3)], 7: "int key", ("a",): "dropped"},
        blob="x" * 5000,
    )
    headers: dict[str, Any] = {}
    signals.before_task_publish.send(sender=None, headers=headers)

    context = headers["structguru_context"]
    # Serializable by strict JSON (virtual transports) and AMQP header tables.
    encoded = json.dumps(headers, allow_nan=False)
    amqp_serialization = pytest.importorskip("amqp.serialization")
    amqp_serialization.dumps("F", [headers])

    assert context["request_id"] == "req-1"
    assert context["path"] == "/srv/app"
    assert context["tags"] == ["only"]
    assert context["pair"] == [1, 2]
    assert context["user"] == "<unsupported: _Opaque>"
    assert context["uid"] == str(request_uuid)
    assert context["amount"] == "1.50"
    assert context["when"] == "2020-01-02T03:04:05"
    assert context["color"] == "red"
    assert context["huge"] == str(10**30)
    assert context["ratio"] is None
    assert context["raw"] == "<unsupported: bytes>"
    assert context["Password"] == "[REDACTED]"
    assert context["nested"] == {"Token": "[REDACTED]", "items": [1, [2, 3]], "7": "int key"}
    assert context["blob"].startswith("x" * 100)
    assert len(context["blob"]) < 1100
    for leaked in ("hunter2", "t0k", "secret-repr", "opaque-secret-str"):
        assert leaked not in encoded


def test_propagated_context_redacts_the_active_runtime_keys() -> None:
    from structguru import configure

    configure(target="null", sensitive_keys=["Tenant_Secret"])
    signals = _dispatch_signals()
    bind_contextvars(tenant_secret="s3cret", password="kept by custom keys", request_id="r")
    headers: dict[str, Any] = {}
    signals.before_task_publish.send(sender=None, headers=headers)
    # Custom keys replace the defaults, exactly as when the record is rendered.
    assert headers["structguru_context"] == {
        "tenant_secret": "[REDACTED]",
        "password": "kept by custom keys",
        "request_id": "r",
    }


def test_propagated_context_honours_the_allow_list() -> None:
    signals = _dispatch_signals(context_keys=["request_id", "password"])
    bind_contextvars(request_id="r", password="p", private="not exported")
    headers: dict[str, Any] = {}
    signals.before_task_publish.send(sender=None, headers=headers)
    assert headers["structguru_context"] == {"request_id": "r", "password": "[REDACTED]"}


def test_context_export_failure_drops_the_header_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from structguru.integrations import celery as integration

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("export failed")

    signals = _dispatch_signals()
    monkeypatch.setattr(integration, "_export_context", explode)
    bind_contextvars(request_id="r")
    headers: dict[str, Any] = {"other": "kept"}
    [(_receiver, result)] = signals.before_task_publish.send(sender=None, headers=headers)
    # The receiver itself swallowed the failure; Celery did not have to.
    assert result is None
    assert headers == {"other": "kept"}


def test_real_publish_survives_unserializable_context(tmp_path: Any) -> None:
    pytest.importorskip("celery")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(f"""
        import glob, json, pathlib, uuid
        from celery import Celery
        from structguru.integrations.celery import setup_celery_logging
        from structguru._contextvars import bind_contextvars, clear_contextvars
        folder = {str(tmp_path)!r}
        app = Celery('t', broker='filesystem://', broker_transport_options={{
            'data_folder_in': folder, 'data_folder_out': folder,
            'control_folder': folder + '/control'}})
        @app.task
        def add(x, y):
            return x + y
        setup_celery_logging()
        class User:
            pass
        values = [uuid.uuid4(), pathlib.Path('/x'), User(), {{1, 2}}, 10**40]
        for value in values:
            clear_contextvars()
            bind_contextvars(request_id='r1', thing=value, password='hunter2')
            add.delay(1, 2)
        messages = [json.load(open(f)) for f in glob.glob(folder + '/*.msg')]
        assert len(messages) == len(values), messages
        for message in messages:
            context = message['headers']['structguru_context']
            assert context['request_id'] == 'r1'
            assert context['password'] == '[REDACTED]'
            assert isinstance(context['thing'], (str, list)), context
        """),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr


def test_worker_ignores_non_string_keys_in_propagated_context() -> None:
    signals = _dispatch_signals()
    task = SimpleNamespace(
        name="worker",
        request=SimpleNamespace(
            is_eager=False, headers={"structguru_context": {"request_id": "r", 1: "x"}}
        ),
    )
    [(_receiver, result)] = signals.task_prerun.send(sender=None, task=task, task_id="t1")
    assert result is None
    assert get_contextvars() == {"request_id": "r", "task_id": "t1", "task_name": "worker"}
    signals.task_postrun.send(sender=None)


def test_failed_setup_import_does_not_latch() -> None:
    pytest.importorskip("celery")
    from structguru.integrations import celery as integration

    with (
        patch.dict("sys.modules", {"celery.signals": None}),
        pytest.raises(ImportError),
    ):
        integration.setup_celery_logging()
    assert integration._setup_done is False

    signals = _dispatch_signals(context_keys=["request_id"])
    bind_contextvars(request_id="r", private="p")
    headers: dict[str, Any] = {}
    signals.before_task_publish.send(sender=None, headers=headers)
    assert headers == {"structguru_context": {"request_id": "r"}}


def test_repeated_setup_warns_only_when_options_differ() -> None:
    import warnings

    from structguru.integrations import celery as integration

    signals = _dispatch_signals(context_keys=["request_id"])
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        integration.setup_celery_logging(context_keys=("request_id",))
    with pytest.warns(RuntimeWarning, match="different options"):
        integration.setup_celery_logging(context_keys=["request_id", "user_id"])
    with pytest.warns(RuntimeWarning, match="different options"):
        integration.setup_celery_logging(propagate_context=False, context_keys=["request_id"])

    # The first configuration stays active and handlers are not duplicated.
    bind_contextvars(request_id="r", user_id="u")
    headers: dict[str, Any] = {}
    assert len(signals.before_task_publish.send(sender=None, headers=headers)) == 1
    assert headers == {"structguru_context": {"request_id": "r"}}
