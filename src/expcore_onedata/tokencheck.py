# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/knowledge/tokencheck.py, blob 80f29a7b310adef07c5baf41fcb60aa05b133869
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Examine a token, and the principal that holds it, before either is used. Fail closed (O05, O06).

A token is examined against the exact caveat set its plane allows (`CaveatSpec`): no caveat missing, none added, no
caveat with a field it should not have. Any doubt is a refusal, including an examination that fails or cannot be
parsed ("unknown is not a pass"). The same `CaveatSpec` builds the caveats at mint time (tokens.py), so what is asked for
and what is accepted have one source.

The two planes differ only in their spec: the data path is read-only on the space and its principal holds exactly view
and read; the tool-server plane's caveats and principal privileges equal the condition's declared rights, and it is
read-only only when the condition declares no write right.

A message names the caveat type or the right that was wrong and never a path, an id or a token value.
"""

import base64
import binascii
import math
from collections import Counter
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any

INTERFACES = ("rest", "oneclient")
_TYPE_LIMIT = 40


class TokenRefused(RuntimeError):
    """A token or its principal failed examination; the message holds no value."""


class ExaminationFailed(TokenRefused):
    """The call that examines a token failed; `error` is what it raised, for a caller that tells outages apart."""

    def __init__(self, message: str, error: Exception) -> None:
        super().__init__(message)
        self.error = error


def _shown(name: Any) -> str:
    """Return a caveat type for a message: short, and quoted only when it is a plain string."""
    return repr(name[:_TYPE_LIMIT]) if isinstance(name, str) else type(name).__name__


@dataclass(frozen=True)
class CaveatSpec:
    """The exact caveat set of one plane's token for one space."""

    space_id: str
    readonly: bool
    interface: str

    def __post_init__(self) -> None:
        """Refuse an interface the platform does not have."""
        if self.interface not in INTERFACES:
            raise ValueError(f"unknown interface {self.interface!r}; known: {', '.join(INTERFACES)}")

    def caveats(self, valid_until: int) -> list[dict[str, Any]]:
        """Return the caveats to mint: the expiry, read-only when the plane is, the interface, the space's subtree."""
        path = base64.b64encode(f"/{self.space_id}".encode()).decode()
        return [
            {"type": "time", "validUntil": valid_until},
            *([{"type": "data.readonly"}] if self.readonly else []),
            {"type": "interface", "interface": self.interface},
            {"type": "data.path", "whitelist": [path]},
        ]

    def shape(self) -> Counter[str]:
        """Return the caveat types the token must have, once each."""
        return Counter(c["type"] for c in self.caveats(0))


@dataclass(frozen=True)
class Examined:
    """What an examination established about a token that passed: ids and types only, never the value."""

    token_id: str
    subject: str
    valid_until: float
    caveat_types: tuple[str, ...]


def _parse(info: Any) -> tuple[str, str, list[Mapping[str, Any]]]:
    """Return the token id, the subject and the caveats of an examination answer; raise TokenRefused if unreadable."""
    try:
        token_id, subject, caveats = info["id"], info["subject"]["id"], info["caveats"]
    except (KeyError, TypeError):
        raise TokenRefused("the examination of the token could not be parsed") from None
    ok = isinstance(token_id, str) and token_id and isinstance(subject, str) and subject and isinstance(caveats, list)
    if not (ok and all(isinstance(c, Mapping) and isinstance(c.get("type"), str) for c in caveats)):
        raise TokenRefused("the examination of the token could not be parsed")
    return token_id, subject, caveats


def _check_shape(caveats: list[Mapping[str, Any]], spec: CaveatSpec) -> None:
    """Require exactly the caveat types of the spec, each once, and no field a caveat should not have."""
    found, wanted = Counter(c["type"] for c in caveats), spec.shape()
    for name in sorted(wanted):
        if found[name] == 0:
            raise TokenRefused(f"the token has no {name} caveat")
    for name in sorted(found):
        if name not in wanted:
            raise TokenRefused(f"the token has a caveat that was not asked for: {_shown(name)}")
        if found[name] != 1:
            raise TokenRefused(f"the token has {found[name]} {name} caveats, expected exactly one")
    fields = {c["type"]: set(c) for c in spec.caveats(0)}
    for caveat in caveats:
        if set(caveat) != fields[caveat["type"]]:
            raise TokenRefused(f"the {caveat['type']} caveat has fields it should not have, or lacks some")


def _check_values(caveats: list[Mapping[str, Any]], spec: CaveatSpec) -> float:
    """Check the path and the interface against the spec; return the expiry the time caveat sets."""
    by_type = {c["type"]: c for c in caveats}
    whitelist = by_type["data.path"]["whitelist"]
    if not isinstance(whitelist, list) or not all(isinstance(w, str) for w in whitelist):
        raise TokenRefused("the data.path caveat is malformed")
    try:
        paths = [base64.b64decode(w, validate=True).decode() for w in whitelist]
    except (binascii.Error, UnicodeDecodeError):
        raise TokenRefused("the data.path caveat is malformed") from None
    if paths != [f"/{spec.space_id}"]:
        raise TokenRefused("the data.path caveat is not the study space alone")
    if by_type["interface"]["interface"] != spec.interface:
        raise TokenRefused("the interface caveat is not the one the plane uses")
    expiry = by_type["time"]["validUntil"]
    if isinstance(expiry, bool) or not isinstance(expiry, int | float) or not math.isfinite(expiry):
        raise TokenRefused("the time caveat is malformed")
    return float(expiry)


def require_examined(
    token: str,
    spec: CaveatSpec,
    *,
    subject: str,
    examine: Callable[[str], Any],
    min_ttl_s: float,
    max_ttl_s: float,
    now: float,
) -> Examined:
    """Examine a token and return what was established, or raise TokenRefused.

    `examine` asks Onezone (a test passes a fake). The token must be the principal's (`subject`), carry exactly the
    spec's caveats, and expire no sooner than `min_ttl_s` (a run's time cap plus a margin, O06) and no later than
    `max_ttl_s` from `now`.
    """
    if not token:
        raise TokenRefused("empty token")
    try:
        info = examine(token)
    except Exception as error:
        raise ExaminationFailed(
            f"the token could not be examined ({type(error).__name__}); refusing it", error
        ) from None
    token_id, found_subject, caveats = _parse(info)
    if found_subject != subject:
        raise TokenRefused("the token belongs to another principal than the one it was minted for")
    _check_shape(caveats, spec)
    left = _check_values(caveats, spec) - now
    if left <= 0:
        raise TokenRefused("the token has already expired")
    if left < min_ttl_s:
        raise TokenRefused(f"the token lives {left:.0f} s, shorter than the {min_ttl_s:.0f} s the run needs")
    if left > max_ttl_s:
        raise TokenRefused(f"the token lives {left:.0f} s, longer than the {max_ttl_s:.0f} s allowed")
    return Examined(token_id, found_subject, now + left, tuple(sorted(c["type"] for c in caveats)))


def _rights(values: Any, what: str) -> set[str]:
    """Read a list of privileges; raise TokenRefused when the answer is not one."""
    if not isinstance(values, Collection) or isinstance(values, str) or not all(isinstance(v, str) for v in values):
        raise TokenRefused(f"the {what} privileges could not be read")
    return set(values)


def require_privileges(declared: Collection[str], *, direct: Any, effective: Any) -> None:
    """Require that a principal's privileges, read back direct and effective, equal the declared set exactly."""
    want = set(declared)
    for what, got in (("direct", _rights(direct, "direct")), ("effective", _rights(effective, "effective"))):
        missing, extra = sorted(want - got), sorted(got - want)
        if missing or extra:
            parts = ([f"lacks {', '.join(missing)}"] if missing else []) + (
                [f"has also {', '.join(extra)}"] if extra else []
            )
            raise TokenRefused(f"the principal's {what} privileges differ from the declared set: {'; '.join(parts)}")


def require_distinct(first: Examined, second: Examined) -> None:
    """Require two planes to have different principals and different tokens: nothing is shared between them."""
    if first.subject == second.subject:
        raise TokenRefused("the two planes share a principal")
    if first.token_id == second.token_id:
        raise TokenRefused("the two planes share a token")
