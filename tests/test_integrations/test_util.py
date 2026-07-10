"""Tests for shared integration helpers."""

from __future__ import annotations

import pytest

from structguru.integrations._util import coerce_request_id, sanitize_url


@pytest.mark.parametrize(
    "raw,expected",
    [
        # userinfo credentials are dropped
        ("https://user:pass@host/path", "https://host/path"),
        # query string values (tokens/keys) are dropped, marker kept
        ("https://host/p?api_key=SECRET&x=1", "https://host/p?"),
        ("https://user:pass@api.example.com:8443/v1?token=t", "https://api.example.com:8443/v1?"),
        # fragment dropped
        ("https://host/p#frag", "https://host/p"),
        # no query → no trailing marker
        ("http://host/path", "http://host/path"),
    ],
)
def test_sanitize_url_strips_secrets(raw: str, expected: str) -> None:
    result = sanitize_url(raw)
    assert result == expected
    assert "SECRET" not in result
    assert "pass" not in result
    assert "token=t" not in result


def test_sanitize_url_accepts_non_string() -> None:
    class _Url:
        def __str__(self) -> str:
            return "https://user:pw@host/x?k=v"

    assert sanitize_url(_Url()) == "https://host/x?"


def test_coerce_request_id_rejects_control_chars() -> None:
    # A CRLF-injection attempt is replaced with a generated UUID.
    coerced = coerce_request_id("abc\r\nSet-Cookie: evil")
    assert "\r" not in coerced and "\n" not in coerced
    assert coerced != "abc\r\nSet-Cookie: evil"
