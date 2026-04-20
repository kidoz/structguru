"""Shared helpers for framework integrations."""

from __future__ import annotations

import uuid

REQUEST_ID_MAX_LEN = 128


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
