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
    not parse, or whose authority is malformed so that credentials would survive
    in the path, is coerced to ``"<unparsable-url>"`` rather than logged raw.
    """
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        parts = urlsplit(text)
        # urlsplit() only recognizes userinfo inside an authority ("//user:pw@host").
        # A URL missing its slashes ("https:/user:pw@host/path") or its scheme
        # ("user:pw@host/path" parses as scheme "user") carries the credentials in
        # the path instead, where stripping userinfo cannot reach them. Retain the
        # path only when the authority was actually parsed.
        if not parts.netloc and (parts.scheme or "@" in parts.path):
            return "<unparsable-url>"
        # Accessing hostname/port also validates parts of the authority.
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = parts.port
        if port is not None:
            host = f"{host}:{port}"
        sanitized = urlunsplit((parts.scheme, host, parts.path, "", ""))
    except Exception:  # noqa: BLE001 - diagnostics must not mask request failures
        return "<unparsable-url>"
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
