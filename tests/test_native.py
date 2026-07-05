from __future__ import annotations

import structguru._rust as rust

from structguru import _native


def test_native_module_exports_core_version() -> None:
    assert rust.version() == "0.2.0"
    assert _native.native_available()


def test_native_level_helpers_match_processor_contract() -> None:
    assert rust.normalize_level("warning") == "WARN"
    assert rust.normalize_level("exception") == "ERROR"
    assert rust.normalize_level("notice") == "NOTICE"

    assert rust.syslog_severity("WARN") == 4
    assert rust.normalized_syslog_severity("fatal") == 2
    assert rust.normalized_syslog_severity("notice") == 6
