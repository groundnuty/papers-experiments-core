# Provenance: new
#   merged: www-2027-experiments @ 46a5828d, harness/agent/providers.py:760-823
#   merged: www-2027-experiments @ 46a5828d, harness/agent/providers.py:691-728
"""Classify provider errors, decide what is retried, and wait (B01, B02, B03, B09, B13).

Classification reads a status code and class names, never a module path, so a test double raising an error of the
same shape is treated like the SDK's own. What each class of error means for a request:

- 429, 408, 409, 5xx, a timeout before a reply, a connection error: transient, retried with capped exponential
  backoff. Jitter only lengthens a delay, and a `Retry-After` is never shortened: the wait is at least the server's.
- a read timeout (the request went out and the reply is slow): never resent; `RequestAbandoned`.
- 401 and 403: `key_rejected` after one request. The SDK's text can echo part of a key, so it is never kept.
- 400 or 413 naming the context: `ContextOverflow`, the agent's outcome, not retried.
- anything else with a status (a plain 400, a 404): not retried, and passed on unchanged.

The SDKs' own retries are switched off by the adapters, so every retry is counted and recorded here.
"""

import asyncio
import math
import random
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from expcore.config.models import ClientSettings
from expcore.errors import ContextOverflow, InfraError, RequestAbandoned

MESSAGE_LIMIT = 300
_TRANSIENT_STATUSES = frozenset({408, 409, 429})
_OVERFLOW_STATUSES = frozenset({400, 413})
_OVERFLOW_CODES = frozenset({"context_length_exceeded"})
# (row id, the provider's wording as an example, pattern); the examples are tested against their own row.
CONTEXT_OVERFLOW_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "prompt_too_long",
        "prompt is too long: 1000001 tokens > 1000000 maximum",
        re.compile(r"prompt is too long", re.IGNORECASE),
    ),
    (
        "maximum_context_length",
        "This model's maximum context length is 131072 tokens. However, your request has 140211 input tokens.",
        re.compile(r"maximum context length", re.IGNORECASE),
    ),
    (
        "maximum_model_length",
        "The decoder prompt (length 40000) is longer than the maximum model length of 32768.",
        re.compile(r"maximum model length|max_model_len", re.IGNORECASE),
    ),
    (
        "context_limit",
        "input length and `max_tokens` exceed context limit: 188240 + 21333 > 200000",
        re.compile(r"context (?:limit|window|length)|context_length_exceeded", re.IGNORECASE),
    ),
)


@dataclass(frozen=True)
class ErrorInfo:
    """What the core needs to know about one provider error."""

    kind: str  # rate_limit, timeout, read_timeout, connection, conflict, server, auth, context_overflow, ...
    transient: bool
    error_class: str
    status: int | None = None
    code: str | None = None
    message: str = ""
    retry_after_s: float | None = None
    overflow: str | None = None  # the matched pattern row when kind is context_overflow


def _error_code(exc: BaseException) -> str | None:
    """Read the provider's error code from the exception or its parsed body, when there is one."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    body = getattr(exc, "body", None)
    if isinstance(body, Mapping):
        inner = body.get("error", body)
        found = inner.get("code") if isinstance(inner, Mapping) else None
        if isinstance(found, str) and found:
            return found
    return None


def retry_after_seconds(exc: BaseException, now: Callable[[], datetime] | None = None) -> float | None:
    """Read `Retry-After` (seconds, or an HTTP date) or `Retry-After-Ms` from the response; None when absent."""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return None
    lowered = {str(k).lower(): v for k, v in headers.items()}
    try:
        if lowered.get("retry-after-ms") is not None:
            seconds = float(lowered["retry-after-ms"]) / 1000.0
        elif lowered.get("retry-after") is not None:
            seconds = _seconds_from(str(lowered["retry-after"]).strip(), now or (lambda: datetime.now(UTC)))
        else:
            return None
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _seconds_from(value: str, now: Callable[[], datetime]) -> float:
    """Parse a number of seconds, or an HTTP date as the seconds from now until it."""
    try:
        return float(value)
    except ValueError:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return (when - now()).total_seconds()


def context_overflow_match(status: int | None, code: str | None, message: str) -> str | None:
    """Return the id of the pattern row (or "code" or "http_413") that makes this error a context overflow."""
    if status not in _OVERFLOW_STATUSES:
        return None
    if code in _OVERFLOW_CODES:
        return "code"
    for row_id, _example, pattern in CONTEXT_OVERFLOW_PATTERNS:
        if pattern.search(message):
            return row_id
    return "http_413" if status == 413 else None


def _is_read_timeout(exc: BaseException) -> bool:
    """Tell whether a timeout came after the request was sent: the transport error underneath is a ReadTimeout.

    Matched by class name, so it holds for whichever HTTP package the SDKs use. Connect, write and pool timeouts
    are failures to reach the endpoint, not a slow reply.
    """
    cause = exc.__cause__ or exc.__context__
    names = {c.__name__ for c in type(cause).__mro__} if cause is not None else set()
    return "ReadTimeout" in names


def classify(exc: BaseException, now: Callable[[], datetime] | None = None) -> ErrorInfo | None:
    """Classify a provider error by its status code and class names; None when it is not one (a harness bug)."""
    names = {c.__name__ for c in type(exc).__mro__}
    status = getattr(exc, "status_code", None)
    status = status if isinstance(status, int) and not isinstance(status, bool) else None
    error_class = type(exc).__name__
    if status is None:
        if "APITimeoutError" in names:
            kind = "read_timeout" if _is_read_timeout(exc) else "timeout"
            return ErrorInfo(kind, kind == "timeout", error_class)
        if "APIConnectionError" in names:
            return ErrorInfo("connection", True, error_class)
        return None
    message = str(exc)[:MESSAGE_LIMIT]
    code = _error_code(exc)
    wait = retry_after_seconds(exc, now)
    common = {"status": status, "code": code, "message": message}
    if status in (401, 403):
        return ErrorInfo("auth", False, error_class, status=status)  # no code, no text: they can echo a key
    overflow = context_overflow_match(status, code, message)
    if overflow is not None:
        return ErrorInfo("context_overflow", False, error_class, **common, overflow=overflow)
    transient = {429: "rate_limit", 408: "timeout", 409: "conflict"}.get(status, "server" if status >= 500 else None)
    if transient is not None:
        return ErrorInfo(transient, True, error_class, **common, retry_after_s=wait)
    return ErrorInfo("not_found" if status == 404 else "bad_request", False, error_class, **common)


@dataclass(frozen=True)
class RetryPolicy:
    """How long to wait between tries, and how many retries to make."""

    max_retries: int = 8
    base_delay_s: float = 5.0
    max_delay_s: float = 120.0
    jitter: float = 0.25  # the delay grows by a random share of up to this much, never shrinks

    @classmethod
    def from_settings(cls, settings: ClientSettings) -> "RetryPolicy":
        """Take the policy from the client settings."""
        return cls(settings.max_retries, settings.retry_base_s, settings.retry_max_s, settings.retry_jitter)

    def delay(self, retry: int, retry_after_s: float | None, rng: "SupportsUniform") -> float:
        """Return the wait before retry number `retry` (0-based).

        The backoff doubles from the base up to the cap. A server's `Retry-After` is not capped and sets a floor.
        Jitter is added to the larger of the two, so the result is never below either.
        """
        backoff = min(self.base_delay_s * 2**retry, self.max_delay_s)
        floor = max(backoff, retry_after_s or 0.0)
        return floor * (1 + rng.uniform(0.0, self.jitter))


class SupportsUniform(Protocol):
    """The part of `random.Random` the policy uses, so a test can fix the jitter."""

    def uniform(self, a: float, b: float) -> float:
        """Return a number between a and b."""
        ...


@dataclass
class RetryStats:
    """Totals over the life of one backend, reported with the run's route."""

    requests: int = 0
    retries: int = 0
    rate_limited: int = 0
    read_timeouts: int = 0


def _no_deadline() -> None:
    """The default `time_left`: the run has no deadline."""
    return None


@dataclass
class Retrier:
    """Sends requests one at a time and retries transient errors; one per backend, so its counters are the run's.

    `sleep`, `rng` and `time_left` are injected, so a test never waits and a run's deadline is the loop's.
    `time_left` returns the seconds the run has left, or None when it has no deadline.
    """

    policy: RetryPolicy
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    rng: SupportsUniform = field(default_factory=random.Random)
    time_left: Callable[[], float | None] = _no_deadline
    stats: RetryStats = field(default_factory=RetryStats)
    events: list[dict[str, Any]] = field(default_factory=list)
    abandoned: dict[str, str] | None = None

    def abandon(self, reason: str) -> None:
        """Record that a request in flight was given up; its usage is unknown."""
        self.abandoned = {"reason": reason, "usage": "unknown"}

    async def call[T](self, send: Callable[[], Awaitable[T]]) -> tuple[T, tuple[dict[str, Any], ...]]:
        """Send until it answers; return the reply and the retries this call made, each with its error.

        Raises InfraError "endpoint" when the retries run out or the run's time cannot cover the next wait,
        InfraError "key_rejected" on 401 or 403, RequestAbandoned on a read timeout, ContextOverflow on a context
        error; any other error is raised unchanged.
        """
        made: list[dict[str, Any]] = []
        while True:
            self.stats.requests += 1
            try:
                return await send(), tuple(made)
            except asyncio.CancelledError:
                self.abandon("deadline")
                raise
            except Exception as exc:
                info = classify(exc)
                if info is None:
                    raise
                self._refuse_final(exc, info)
                wait = self.policy.delay(len(made), info.retry_after_s, self.rng)
                self._refuse_exhausted(exc, info, wait, len(made))
                made.append(self._record(info, wait, len(made)))
                await self.sleep(wait)

    def _refuse_final(self, exc: Exception, info: ErrorInfo) -> None:
        """Raise the right error for an error that is never retried; return when the error is transient."""
        if info.kind == "auth":
            raise InfraError("key_rejected", f"HTTP {info.status} {info.error_class}") from None
        if info.kind == "read_timeout":
            self.stats.read_timeouts += 1
            self.abandoned = {"reason": "read_timeout", "usage": "unknown"}
            raise RequestAbandoned("read timeout") from exc
        if info.kind == "context_overflow":
            raise ContextOverflow(info.status, info.overflow or "context") from None
        if not info.transient:
            raise exc
        if info.kind == "rate_limit":
            self.stats.rate_limited += 1

    def _refuse_exhausted(self, exc: Exception, info: ErrorInfo, wait: float, retries_made: int) -> None:
        """Raise InfraError "endpoint" when the run's time cannot cover the wait (checked first) or retries ran out."""
        left = self.time_left()
        if left is not None and wait > left:
            seconds = max(left, 0.0)
            raise InfraError(
                "endpoint", f"{_describe(info)}; {seconds:.0f} s left in the run, no time to retry"
            ) from exc
        if retries_made >= self.policy.max_retries:
            raise InfraError("endpoint", f"{_describe(info)} after {retries_made} retries") from exc

    def _record(self, info: ErrorInfo, wait: float, retries_made: int) -> dict[str, Any]:
        """Count one retry and return its event, with the error that caused it."""
        self.stats.retries += 1
        event = {
            "retry": retries_made + 1,
            "kind": info.kind,
            "error": info.error_class,
            "status": info.status,
            "retry_after_s": info.retry_after_s,
            "wait_s": wait,
            "message": info.message,
        }
        self.events.append(event)
        return event


def _describe(info: ErrorInfo) -> str:
    """Name an error for a detail line: class, status and a short message."""
    status = f" HTTP {info.status}" if info.status is not None else ""
    text = f": {info.message}" if info.message else ""
    return f"{info.error_class}{status}{text}"
