"""Functional correctness checks specific to free-threaded CPython."""

from __future__ import annotations

import datetime as dt
import enum
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest
import structguru._rust as rust

import structguru
from structguru import _runtime
from structguru.exceptions import _format_locals, _redacted_message_repr
from structguru.integrations.stdlib import StructguruHandler
from structguru.redaction import DEFAULT_SENSITIVE_KEYS


def _gil_enabled() -> bool:
    probe = getattr(sys, "_is_gil_enabled", None)
    return True if probe is None else bool(probe())


pytestmark = pytest.mark.skipif(
    _gil_enabled(),
    reason="requires a free-threaded CPython build",
)


def test_concurrent_logging_preserves_every_record() -> None:
    workers = 4
    records_per_worker = 250
    _runtime.configure(service="free-threaded", target="memory", maxsize=32)
    try:

        def emit(worker_id: int) -> None:
            for sequence in range(records_per_worker):
                structguru.logger.info(
                    "concurrent record",
                    worker_id=worker_id,
                    sequence=sequence,
                )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(emit, range(workers)))
        _runtime.flush_native()

        expected = workers * records_per_worker
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == expected
        assert metrics["written"] == expected
        assert metrics["dropped"] == 0
        assert len(_runtime.drain_messages()) == expected
    finally:
        _runtime.shutdown()


def test_concurrent_stdlib_forwarding_isolates_record_fields() -> None:
    workers, records_per_worker = 4, 150
    handler = StructguruHandler()
    _runtime.configure(target="memory", maxsize=32)
    try:

        def emit(worker: int) -> None:
            for sequence in range(records_per_worker):
                record = logging.LogRecord(
                    "shared.bridge", 25, "", 1, "literal {worker}: %s", (sequence,), None
                )
                record.worker = worker
                record.sequence = sequence
                handler.handle(record)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(emit, range(workers)))
        _runtime.flush_native()
        records = [json.loads(line) for line in _runtime.drain_messages()]
        expected = workers * records_per_worker
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == metrics["written"] == len(records) == expected
        assert metrics["dropped"] == 0
        assert {
            (record["worker"], record["sequence"], record["message"]) for record in records
        } == {
            (worker, sequence, f"literal {{worker}}: {sequence}")
            for worker in range(workers)
            for sequence in range(records_per_worker)
        }
        assert all(record["logger"] == "shared.bridge" for record in records)
        assert not _gil_enabled()
    finally:
        _runtime.shutdown()


def test_concurrent_structured_exceptions_preserve_lines_and_redacted_locals() -> None:
    workers, records_per_worker = 4, 50
    _runtime.configure(
        target="memory", maxsize=32, structured_exceptions=True, exception_include_locals=True
    )
    try:

        def emit(worker: int) -> None:
            password = "CANARY"
            try:
                raise ValueError(f"worker {worker}")
            except ValueError:
                for sequence in range(records_per_worker):
                    structguru.logger.exception(
                        "concurrent failure", worker=worker, sequence=sequence
                    )
            assert password == "CANARY"

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(emit, range(workers)))
        _runtime.flush_native()
        lines = _runtime.drain_messages()
        records = [json.loads(line) for line in lines]
        expected = workers * records_per_worker
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == metrics["written"] == len(records) == expected
        assert metrics["dropped"] == 0
        assert all("CANARY" not in line for line in lines)
        assert {(record["worker"], record["sequence"]) for record in records} == {
            (worker, sequence)
            for worker in range(workers)
            for sequence in range(records_per_worker)
        }
        for record in records:
            exception = record["exception"]
            assert exception["message"] == f"worker {record['worker']}"
            [frame] = exception["frames"]
            assert frame["line"] == 'raise ValueError(f"worker {worker}")'
            assert frame["locals"]["worker"] == str(record["worker"])
            assert frame["locals"]["sequence"] == str(record["sequence"])
            assert frame["locals"]["password"] == "[REDACTED]"
        assert not _gil_enabled()
    finally:
        _runtime.shutdown()


def test_concurrent_mapping_key_redaction_keeps_original_enum_identity() -> None:
    class Key(enum.IntEnum):
        password = 1

    _runtime.configure(target="memory", maxsize=32)
    try:

        def emit(worker: int) -> None:
            for sequence in range(50):
                structguru.logger.info(
                    "keys",
                    payload={Key.password: "CANARY", b"authorization": "CANARY", b"safe": 42},
                    worker=worker,
                    sequence=sequence,
                )

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(emit, range(4)))
        _runtime.flush()
        lines = _runtime.drain_messages()
        records = [json.loads(line) for line in lines]
        assert len(records) == 200
        assert all("CANARY" not in line for line in lines)
        assert all(
            record["payload"] == {"1": "[REDACTED]", "authorization": "[REDACTED]", "safe": 42}
            for record in records
        )
        assert {(record["worker"], record["sequence"]) for record in records} == {
            (worker, sequence) for worker in range(4) for sequence in range(50)
        }
        metrics = _runtime.writer_metrics()
        assert metrics is not None and metrics["enqueued"] == metrics["written"] == 200
        assert metrics["dropped"] == 0
        assert not _gil_enabled()
    finally:
        _runtime.shutdown()


def test_list_snapshot_survives_concurrent_mutation_during_conversion() -> None:
    class MutatingClock(dt.date):
        def __new__(cls, values: list[object]) -> MutatingClock:
            return super().__new__(cls, 2026, 1, 1)

        def __init__(self, values: list[object]) -> None:
            self.values = values

        def isoformat(self) -> str:
            with ThreadPoolExecutor(max_workers=1) as executor:

                def mutate() -> None:
                    self.values.clear()
                    self.values.append("replacement")

                executor.submit(mutate).result(timeout=2)
            return "converted"

    for _ in range(50):
        values: list[object] = []
        values.extend([MutatingClock(values), "retained"])
        assert rust._convert_value_debug(values) == ["converted", "retained"]


def test_concurrent_local_repr_keeps_redaction_and_traversal_state_isolated() -> None:
    @dataclass
    class Account:
        user: str
        password: str

    def render(worker_id: int) -> int:
        shared = {"token": "CANARY", "worker": worker_id}
        value = {"left": shared, "right": shared, "account": Account("Ada", "CANARY")}
        for _ in range(100):
            local = _format_locals({"value": value}, DEFAULT_SENSITIVE_KEYS, 2000)["value"]
            message = _redacted_message_repr(value, DEFAULT_SENSITIVE_KEYS)
            assert message is not None
            for text in (local, message):
                assert "CANARY" not in text
                assert text.count("[REDACTED]") == 3
                assert text.count(f"'worker': {worker_id}") == 2
                assert "..." not in text
        return 100

    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sum(executor.map(render, range(4))) == 400


def test_concurrent_template_analysis_preserves_redaction_and_consumed_fields() -> None:
    from structguru import core

    workers, records_per_worker = 4, 300
    core._extract_format_keys.cache_clear()
    _runtime.configure(target="memory", maxsize=32)
    try:

        def emit(worker: int) -> None:
            for sequence in range(records_per_worker):
                template = f"worker-{worker}-{sequence} {{value:0{{width}}d}} {{token}}"
                structguru.logger.info(
                    template,
                    value=sequence,
                    width=4,
                    token="CANARY",
                    worker_id=worker,
                    sequence=sequence,
                )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(emit, range(workers)))
        _runtime.flush()
        lines = _runtime.drain_messages()
        records = [json.loads(line) for line in lines]
        expected = workers * records_per_worker
        metrics = _runtime.writer_metrics()
        assert metrics is not None
        assert metrics["enqueued"] == metrics["written"] == len(records) == expected
        assert metrics["dropped"] == 0
        assert all("CANARY" not in line for line in lines)
        assert all(not {"value", "width", "token"}.intersection(record) for record in records)
        assert {record["message"] for record in records} == {
            f"worker-{worker}-{sequence} {sequence:04d} [REDACTED]"
            for worker in range(workers)
            for sequence in range(records_per_worker)
        }
        assert core._extract_format_keys.cache_info().currsize == 1024
        assert not _gil_enabled()
    finally:
        _runtime.shutdown()
