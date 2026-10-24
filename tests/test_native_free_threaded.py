"""Functional correctness checks specific to free-threaded CPython."""

from __future__ import annotations

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


def test_list_snapshot_survives_concurrent_mutation_during_conversion() -> None:
    class MutatingClock:
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
