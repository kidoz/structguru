"""Shared helpers for framework integrations."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable, Iterator
from typing import Any, Generic, TypeVar
from urllib.parse import urlsplit, urlunsplit

from structguru._contextvars import bind_contextvars, clear_contextvars, get_contextvars
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

_T = TypeVar("_T")


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


class StreamState:
    """Request state shared by a lazily produced (streamed) response body.

    Holds the request context to re-bind while the body is produced, and a
    one-shot completion callback that runs when the body is exhausted, fails,
    or is closed — whichever comes first.

    Parameters
    ----------
    context:
        Context snapshot taken when the handler returned the response.
    on_finish:
        Called once with the exception that ended the stream, or ``None``.
    """

    __slots__ = ("_on_finish", "context", "finished")

    def __init__(
        self,
        context: dict[str, Any],
        on_finish: Callable[[BaseException | None], None],
    ) -> None:
        self.context = context
        self.finished = False
        self._on_finish = on_finish

    def enter(self) -> None:
        """Bind the request context (replacing whatever is bound)."""
        replace_contextvars(self.context)

    def save(self) -> None:
        """Keep enrichment the body bound while producing a chunk."""
        self.context = get_contextvars()

    def finish(
        self,
        error: BaseException | None = None,
        caller_context: dict[str, Any] | None = None,
    ) -> None:
        """Run the completion callback once, then hand the context back.

        The callback runs with the request context bound. Afterwards
        *caller_context* is restored — unless the framework ended the request
        (cleared the context) while the body ran, as Flask's
        ``stream_with_context`` does during the last chunk, or no caller
        context is given. Then the context is left clear, so a finished
        request is never bound again.
        """
        request_ended = not get_contextvars()
        try:
            if not self.finished:
                self.finished = True
                replace_contextvars(self.context)
                self._on_finish(error)
        finally:
            if caller_context is not None and not request_ended:
                replace_contextvars(caller_context)
            else:
                clear_contextvars()


class ContextBoundIterator(Generic[_T]):
    """Iterate a lazy response body with its request context bound.

    Each step binds the request context while the body produces a chunk (so
    its logs carry it), keeps enrichment the body made for the next step, and
    then restores the caller's context, so nothing leaks into the server's own
    work before, between, or after chunks. Exhaustion, a failure, or
    :meth:`close` finishes the stream: the completion callback runs with the
    request context (see :meth:`StreamState.finish`).
    """

    def __init__(self, iterable: Iterable[_T], state: StreamState) -> None:
        self._iterable = iterable
        self._iterator: Iterator[_T] | None = None
        self._state = state

    def __iter__(self) -> ContextBoundIterator[_T]:
        return self

    def __next__(self) -> _T:
        state = self._state
        if state.finished:
            raise StopIteration
        caller_context = get_contextvars()
        state.enter()
        try:
            if self._iterator is None:
                self._iterator = iter(self._iterable)
            item = next(self._iterator)
        except StopIteration:
            state.finish(None, caller_context)
            raise
        except BaseException as exc:
            state.finish(exc, caller_context)
            raise
        state.save()
        replace_contextvars(caller_context)
        return item

    def close(self) -> None:
        """Close the wrapped body (with its context bound) and finish the stream."""
        state = self._state
        closers = [getattr(self._iterable, "close", None)]
        if self._iterator is not None and self._iterator is not self._iterable:
            closers.append(getattr(self._iterator, "close", None))
        if state.finished:
            for close in closers:
                if close is not None:
                    close()
            return
        caller_context = get_contextvars()
        state.enter()
        error: BaseException | None = None
        try:
            for close in closers:
                if close is not None:
                    close()
        except BaseException as exc:
            error = exc
            raise
        finally:
            state.finish(error, caller_context)


class ContextBoundAsyncIterator(Generic[_T]):
    """Asynchronous counterpart of :class:`ContextBoundIterator`.

    It deliberately has no ``__iter__`` so frameworks that detect async bodies
    by the absence of synchronous iteration (Django) keep treating it as one.
    The synchronous :meth:`close` finishes the stream without awaiting the
    wrapped iterator, for response-level ``close()`` hooks.
    """

    def __init__(self, iterable: AsyncIterable[_T], state: StreamState) -> None:
        self._iterable = iterable
        self._iterator: AsyncIterator[_T] | None = None
        self._state = state

    def __aiter__(self) -> ContextBoundAsyncIterator[_T]:
        return self

    async def __anext__(self) -> _T:
        state = self._state
        if state.finished:
            raise StopAsyncIteration
        caller_context = get_contextvars()
        state.enter()
        try:
            if self._iterator is None:
                self._iterator = aiter(self._iterable)
            item = await anext(self._iterator)
        except StopAsyncIteration:
            state.finish(None, caller_context)
            raise
        except BaseException as exc:
            state.finish(exc, caller_context)
            raise
        state.save()
        replace_contextvars(caller_context)
        return item

    async def aclose(self) -> None:
        """Close the wrapped body (with its context bound) and finish the stream."""
        state = self._state
        target = self._iterator if self._iterator is not None else self._iterable
        aclose = getattr(target, "aclose", None)
        if state.finished:
            if aclose is not None:
                await aclose()
            return
        caller_context = get_contextvars()
        state.enter()
        error: BaseException | None = None
        try:
            if aclose is not None:
                await aclose()
        except BaseException as exc:
            error = exc
            raise
        finally:
            state.finish(error, caller_context)

    def close(self) -> None:
        """Finish the stream without awaiting the wrapped iterator."""
        if not self._state.finished:
            self._state.finish(None, get_contextvars())
