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


@pytest.mark.parametrize(
    "raw, expected",
    [
        (b"https://user:pass@host/path?token=VALUE#fragment", "https://host/path?"),
        ("https://user:pass@[::1]:8443/path?token=VALUE", "https://[::1]:8443/path?"),
        ("https://[::1]/path", "https://[::1]/path"),
        ("https://host:invalid/path", "<unparsable-url>"),
        ("https://host:99999/path", "<unparsable-url>"),
        ("https://[invalid/path", "<unparsable-url>"),
        (b"https://host/\xff", "<unparsable-url>"),
    ],
)
def test_sanitize_url_handles_bytes_ipv6_and_malformed_authorities(
    raw: object, expected: str
) -> None:
    assert sanitize_url(raw) == expected


def test_sanitize_url_does_not_propagate_conversion_failure() -> None:
    class BrokenURL:
        def __str__(self) -> str:
            raise RuntimeError("conversion failed")

    assert sanitize_url(BrokenURL()) == "<unparsable-url>"


@pytest.mark.parametrize(
    "raw",
    [
        "https:/user:password@host/path",
        "https:user:password@host/path",
        "https:///user:password@host/path",
        "user:password@host/path",
        "/user:password@host/path",
    ],
)
def test_sanitize_url_rejects_credentials_outside_a_parsed_authority(raw: str) -> None:
    # urlsplit() leaves userinfo in the path when the authority is malformed,
    # where stripping userinfo cannot reach it. The path must not be retained.
    result = sanitize_url(raw)
    assert result == "<unparsable-url>"
    assert "password" not in result


def test_sanitize_url_keeps_relative_paths_without_userinfo() -> None:
    assert sanitize_url("/relative/path?x=1") == "/relative/path?"
