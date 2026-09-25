"""Shared helpers for framework integrations."""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from structguru._contextvars import bind_contextvars, clear_contextvars
from structguru.redaction import DEFAULT_SENSITIVE_KEYS

REQUEST_ID_MAX_LEN = 128
HEADER_VALUE_MAX_LEN = 256
REDACTED = "[REDACTED]"
TRUNCATED_SUFFIX = "...[truncated]"

# Headers that carry credentials whatever redaction keys are configured. Other
# names are matched against the active sensitive keys by ``is_credential_header``.
CREDENTIAL_HEADERS: frozenset[str] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
        "x-access-token",
        "x-csrf-token",
        "x-csrftoken",
        "x-xsrf-token",
        "x-amz-security-token",
    }
)


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


def coerce_request_id(raw: object, *, max_len: int = REQUEST_ID_MAX_LEN) -> str:
    """Return a safe request ID: *raw* if it passes validation, else a new UUID.

    An incoming value is accepted only when it is non-empty, at most
    *max_len* characters long, and contains no non-printable characters.
    ``bytes`` (binary gRPC metadata, raw ASGI headers) are accepted when they
    decode as strict UTF-8. Anything else — including values of other types —
    yields a fresh :func:`uuid.uuid4`, so upstream callers cannot inject
    control characters or unbounded strings into logs or response headers, and
    a malformed value never fails the request.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = None
    if isinstance(raw, str) and raw and len(raw) <= max_len and raw.isprintable():
        return raw
    return str(uuid.uuid4())


def coerce_header_value(raw: object, *, max_len: int = HEADER_VALUE_MAX_LEN) -> str | None:
    """Return a bounded, printable header value for binding, or ``None`` to drop it.

    ``bytes`` must decode as strict UTF-8. A value longer than *max_len*
    characters is truncated and marked with :data:`TRUNCATED_SUFFIX`; a value
    containing non-printable characters (CR/LF, NUL, other controls) is dropped.
    """
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    if len(raw) > max_len:
        raw = raw[:max_len] + TRUNCATED_SUFFIX
    return raw if raw.isprintable() else None


def active_sensitive_keys() -> frozenset[str]:
    """Return the lower-cased keys the active runtime redacts when rendering.

    Custom ``sensitive_keys`` replace the defaults, exactly as in the native
    renderer. Without a configured runtime (or if it cannot be read) the
    defaults from :data:`structguru.redaction.DEFAULT_SENSITIVE_KEYS` apply.
    """
    try:
        from structguru import _runtime

        keys = _runtime.sensitive_keys()
    except Exception:  # noqa: BLE001 - fall back to the defaults, never fail the caller
        keys = None
    return frozenset(key.lower() for key in (DEFAULT_SENSITIVE_KEYS if keys is None else keys))


def is_credential_header(name: str, sensitive_keys: frozenset[str]) -> bool:
    """Return whether a header *name* carries credentials and must be redacted.

    The renderer redacts a field only when its whole name equals a sensitive
    key, which misses header spellings such as ``x-api-key`` or
    ``x-auth-token``. Here a header matches when it is a well-known credential
    header (:data:`CREDENTIAL_HEADERS`), or when any contiguous run of its
    ``-``/``_``-separated words, joined by ``_`` or directly, equals one of
    *sensitive_keys* (``x-api-key`` → ``api_key``, ``set-cookie`` → ``cookie``).
    """
    lowered = name.lower()
    if lowered in CREDENTIAL_HEADERS or lowered in sensitive_keys:
        return True
    words = [word for word in lowered.replace("_", "-").split("-") if word]
    for start in range(len(words)):
        for end in range(start + 1, len(words) + 1):
            run = words[start:end]
            if "_".join(run) in sensitive_keys or "".join(run) in sensitive_keys:
                return True
    return False


def replace_contextvars(fields: dict[str, Any]) -> None:
    """Make *fields* the entire bound context (a clear followed by a bind)."""
    clear_contextvars()
    if fields:
        bind_contextvars(**fields)
