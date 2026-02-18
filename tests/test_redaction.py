"""Tests for structguru.redaction."""

from __future__ import annotations

import re

from structguru.redaction import DEFAULT_SENSITIVE_KEYS, RedactingProcessor


class TestRedactingProcessor:
    def test_redacts_sensitive_keys(self) -> None:
        proc = RedactingProcessor()
        ed: dict = {"password": "s3cret", "user": "alice"}
        result = proc(None, "info", ed)
        assert result["password"] == "[REDACTED]"
        assert result["user"] == "alice"

    def test_case_insensitive_key_matching(self) -> None:
        proc = RedactingProcessor()
        ed: dict = {"Password": "s3cret", "TOKEN": "abc"}
        result = proc(None, "info", ed)
        assert result["Password"] == "[REDACTED]"
        assert result["TOKEN"] == "[REDACTED]"

    def test_does_not_redact_non_sensitive(self) -> None:
        proc = RedactingProcessor()
        ed: dict = {"user": "alice", "action": "login"}
        result = proc(None, "info", ed)
        assert result["user"] == "alice"
        assert result["action"] == "login"

    def test_custom_sensitive_keys(self) -> None:
        proc = RedactingProcessor(sensitive_keys=frozenset({"email"}))
        ed: dict = {"email": "a@b.com", "password": "visible"}
        result = proc(None, "info", ed)
        assert result["email"] == "[REDACTED]"
        assert result["password"] == "visible"

    def test_custom_replacement(self) -> None:
        proc = RedactingProcessor(replacement="***")
        ed: dict = {"password": "s3cret"}
        result = proc(None, "info", ed)
        assert result["password"] == "***"

    def test_regex_pattern_on_string_values(self) -> None:
        email_re = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}")
        proc = RedactingProcessor(patterns=[email_re])
        ed: dict = {"msg": "Contact user@example.com for details"}
        result = proc(None, "info", ed)
        assert "[REDACTED]" in result["msg"]
        assert "user@example.com" not in result["msg"]

    def test_non_string_values_not_pattern_matched(self) -> None:
        email_re = re.compile(r"@")
        proc = RedactingProcessor(patterns=[email_re])
        ed: dict = {"count": 42}
        result = proc(None, "info", ed)
        assert result["count"] == 42

    def test_non_string_dict_keys_do_not_crash(self) -> None:
        proc = RedactingProcessor()
        nested: dict = {1: "numeric key", "password": "s3cret", (2, 3): "tuple key"}
        ed: dict = {"event": "test", "data": nested}
        result = proc(None, "info", ed)
        assert result["data"][1] == "numeric key"
        assert result["data"]["password"] == "[REDACTED]"
        assert result["data"][(2, 3)] == "tuple key"

    def test_cyclic_dict_does_not_crash(self) -> None:
        proc = RedactingProcessor()
        d: dict = {"user": "alice", "password": "s3cret"}
        d["self"] = d  # cyclic reference
        result = proc(None, "info", d)
        assert result["password"] == "[REDACTED]"
        assert result["user"] == "alice"
        assert result["self"] is result  # reference preserved, no infinite loop

    def test_cyclic_list_does_not_crash(self) -> None:
        proc = RedactingProcessor()
        lst: list = ["hello"]
        lst.append(lst)  # cyclic reference
        ed: dict = {"data": lst}
        # Must not raise RecursionError
        result = proc(None, "info", ed)
        assert result["data"][0] == "hello"

    def test_default_keys_cover_common_secrets(self) -> None:
        for key in ("password", "token", "api_key", "secret", "authorization", "private_key"):
            assert key in DEFAULT_SENSITIVE_KEYS
