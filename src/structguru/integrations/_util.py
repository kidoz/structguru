"""Shared helpers for framework integrations."""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit, urlunsplit

REQUEST_ID_MAX_LEN = 128


def sanitize_url(raw: object) -> str:
    """Return *raw* as a string with credentials and query string removed.

    Outbound-request URLs routinely carry secrets in two places that must not
    reach logs: userinfo (``https://user:pass@host``) and the query string
    (``?api_key=...``, ``?token=...``). Both are stripped here, leaving
    scheme/host/path plus a ``?`` marker when a query was present, so operators
    can still see that parameters existed without their values. A URL that does
    not parse is coerced to ``"<unparsable-url>"`` rather than logged raw.
    """
    text = str(raw)
    try:
        parts = urlsplit(text)
    except ValueError:
        return "<unparsable-url>"
    # netloc without userinfo: keep host[:port], drop anything before '@'.
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    # Drop the fragment too; it is client-side and occasionally sensitive.
    sanitized = urlunsplit((parts.scheme, host, parts.path, "", ""))
    # Preserve a bare '?' so operators can see parameters were present.
    return f"{sanitized}?" if parts.query else sanitized


def coerce_request_id(raw: str | None, *, max_len: int = REQUEST_ID_MAX_LEN) -> str:
    """Return a safe request ID: *raw* if it passes validation, else a new UUID.

    An incoming value is accepted only when it is non-empty, at most
    *max_len* characters long, and contains no non-printable characters.
    Otherwise a fresh :func:`uuid.uuid4` is returned so upstream callers
    cannot inject control characters or unbounded strings into logs or
    response headers.
    """
    if raw and len(raw) <= max_len and raw.isprintable():
        return raw
    return str(uuid.uuid4())
