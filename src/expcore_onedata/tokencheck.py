# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/knowledge/tokencheck.py, blob 80f29a7b310adef07c5baf41fcb60aa05b133869
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/knowledge/live.py:36,52 (the examination: id, caveats)
#   changes: see git history of this file from its import commit
"""Examine a token, and the principal that holds it, before either is used. Fail closed (O05, O06).

A token is examined against the exact caveat set its plane allows (`CaveatSpec`): no caveat missing, none added, no
caveat with a field it should not have. Any doubt is a refusal, including an examination that fails or cannot be
parsed ("unknown is not a pass"). The same `CaveatSpec` builds the caveats at mint time (tokens.py), so what is asked for
and what is accepted have one source.

The two planes differ only in their spec: the data path is read-only on the space and its principal holds exactly view
and read; the tool-server plane's principal privileges equal the condition's declared rights, its token is read-only
with the data path's caveats when the condition declares no write right, and otherwise it carries no data-access caveat
at all (`CaveatSpec`).

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
# The caveats that make a token a data-access token on Onedata 25.0, usable for data access alone; the interface caveat
# is one too, but only as `oneclient` (onedata/ctool, release/25.0 at 019fee12a1638ca516da97a931a2d6138fa25279,
# src/auth/data_access_caveats.erl, `?DATA_ACCESS_CAVEATS` and `filter/1`).
DATA_ACCESS_CAVEATS = ("data.path", "data.objectid", "data.readonly")
_TYPE_LIMIT = 40


class TokenRefused(RuntimeError):
    """A token or its principal failed examination; the message holds no value.

    `expires_at` is the refused token's expiry when the examination could read its time caveat, else None. A token
    is dropped, not revoked on its own (only with all its user's temporary tokens, when a slot's run ends), so a caller
    holding a resource for it needs that expiry (or must assume the worst).
    """

    expires_at: float | None = None


class PrivilegesMismatch(TokenRefused):
    """A principal's privileges, read back, differ from the declared set: the one refusal that says the principal is
    set up for other rights, as opposed to a token, an answer or a read that is wrong."""


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
    """The exact caveat set of one plane's token for the spaces its plane reaches: one space's id, or the ids of
    several (`[[spaces]]`).

    A read-only token is a data-access token: its caveats are the expiry, `data.readonly`, the interface and a
    `data.path` whitelist with each space's subtree as one entry. A writable token carries no data-access caveat, only
    the expiry and the interface: on Onedata 25.0 a token with any of `DATA_ACCESS_CAVEATS` is usable for data access
    alone, and its request to schedule a transfer is refused (`tokenCaveatUnverified`, #177). Its spaces are then not a
    caveat but the scope its principal's rights are read in (`tokens.TokenIssuer.check_user`).
    """

    space_id: str | tuple[str, ...]
    readonly: bool
    interface: str

    def __post_init__(self) -> None:
        """Refuse an interface the platform does not have, a set of spaces that is empty or names one twice, and a
        writable token on the oneclient interface, which 25.0 counts as a data-access caveat."""
        if self.interface not in INTERFACES:
            raise ValueError(f"unknown interface {self.interface!r}; known: {', '.join(INTERFACES)}")
        if not self.space_ids or len(set(self.space_ids)) != len(self.space_ids):
            raise ValueError("a token's caveats reach at least one space, each once")
        if not self.readonly and self.interface == "oneclient":
            raise ValueError("a writable token cannot have the oneclient interface, a data-access caveat on 25.0")

    @property
    def data_access(self) -> bool:
        """Whether the token carries the data-access caveats, `data.readonly` and `data.path`: exactly when it is
        read-only."""
        return self.readonly

    @property
    def space_ids(self) -> tuple[str, ...]:
        """The ids of the spaces the plane reaches, in the order given."""
        return (self.space_id,) if isinstance(self.space_id, str) else tuple(self.space_id)

    def paths(self) -> list[str]:
        """The `data.path` whitelist of a read-only token, decoded: each space's subtree, `/{space id}`."""
        return [f"/{space_id}" for space_id in self.space_ids]

    def caveats(self, valid_until: int) -> list[dict[str, Any]]:
        """Return the caveats to mint: the expiry, read-only when the plane is, the interface, and the spaces' subtrees
        when the token is read-only."""
        return [
            {"type": "time", "validUntil": valid_until},
            *([{"type": "data.readonly"}] if self.data_access else []),
            {"type": "interface", "interface": self.interface},
            *(
                [{"type": "data.path", "whitelist": [base64.b64encode(p.encode()).decode() for p in self.paths()]}]
                if self.data_access
                else []
            ),
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


def _unparsed(field_name: str, wanted: str) -> TokenRefused:
    """Refuse an examination that lacks a field or has it in another shape, naming the field and never guessing."""
    return TokenRefused(f"the examination of the token could not be parsed: {field_name} is not {wanted}")


def _parse(info: Any) -> tuple[str, str, list[Mapping[str, Any]]]:
    """Return the token id, the subject's id and the caveats of an examination answer; refuse if any is not as read.

    `id` and `caveats` are the fields the earlier code read from a real examination. The subject's shape (an `id`
    under `subject`) is confirmed only by the live run against Onedata 25.0 (`[L1]` in `client.py`), so a subject that
    is not exactly that is a refusal.
    """
    if not isinstance(info, Mapping):
        raise _unparsed("the answer", "an object")
    token_id, subject, caveats = info.get("id"), info.get("subject"), info.get("caveats")
    if not isinstance(token_id, str) or not token_id:
        raise _unparsed("'id'", "a non-empty string")
    subject_id = subject.get("id") if isinstance(subject, Mapping) else None
    if not isinstance(subject_id, str) or not subject_id:
        raise _unparsed("'subject.id'", "a non-empty string")
    if not isinstance(caveats, list):
        raise _unparsed("'caveats'", "a list")
    if not all(isinstance(c, Mapping) and isinstance(c.get("type"), str) for c in caveats):
        raise _unparsed("'caveats'", "a list of objects each with a string 'type'")
    return token_id, subject_id, caveats


def _check_shape(caveats: list[Mapping[str, Any]], spec: CaveatSpec) -> None:
    """Require exactly the caveat types of the spec, each once, and no field a caveat should not have; a writable
    token with a data-access caveat is refused as such, since that caveat would leave it unable to write."""
    found, wanted = Counter(c["type"] for c in caveats), spec.shape()
    for name in sorted(wanted):
        if found[name] == 0:
            raise TokenRefused(f"the token has no {name} caveat")
    if not spec.data_access:
        carried = sorted(name for name in found if name in DATA_ACCESS_CAVEATS)
        if carried:
            raise TokenRefused(
                f"the writable token carries a data-access caveat ({_shown(carried[0])}), which limits it to data access"
            )
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
    """Check the path, when the spec has one, and the interface against the spec; return the expiry the time caveat
    sets."""
    by_type = {c["type"]: c for c in caveats}
    if spec.data_access:
        _check_paths(by_type["data.path"], spec)
    if by_type["interface"]["interface"] != spec.interface:
        raise TokenRefused("the interface caveat is not the one the plane uses")
    expiry = by_type["time"]["validUntil"]
    if isinstance(expiry, bool) or not isinstance(expiry, int | float) or not math.isfinite(expiry):
        raise TokenRefused("the time caveat is malformed")
    return float(expiry)


def _check_paths(caveat: Mapping[str, Any], spec: CaveatSpec) -> None:
    """Require the `data.path` whitelist to be each of the spec's spaces once, in canonical base64."""
    whitelist = caveat["whitelist"]
    if not isinstance(whitelist, list) or not all(isinstance(w, str) for w in whitelist):
        raise TokenRefused("the data.path caveat is malformed")
    try:
        decoded = [base64.b64decode(w, validate=True) for w in whitelist]
        paths = [d.decode() for d in decoded]
    except (binascii.Error, UnicodeDecodeError):
        raise TokenRefused("the data.path caveat is malformed") from None
    # A strict decode still accepts text whose spare trailing bits are not zero ("L3N=" decodes as "L3M=" does), so
    # the same path could be written many ways. Only the one canonical text of each path is accepted.
    if any(base64.b64encode(d).decode() != w for d, w in zip(decoded, whitelist, strict=True)):
        raise TokenRefused("the data.path caveat is not canonical base64")
    if sorted(paths) != sorted(spec.paths()):  # each declared space once, in any order, and nothing else
        raise TokenRefused("the data.path caveat is not the study space(s) alone")


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
    spec's caveats, and expire no sooner than `min_ttl_s` (a run's whole-time ceiling plus a margin, O06) and no later than
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
    declared = _declared_expiry(info)
    try:
        return _require(info, spec, subject=subject, min_ttl_s=min_ttl_s, max_ttl_s=max_ttl_s, now=now)
    except TokenRefused as refused:
        refused.expires_at = declared
        raise
    except Exception as error:  # an answer the checks could not even read is a refusal, never a crash
        refused = TokenRefused(f"the examination answer could not be checked ({type(error).__name__})")
        refused.expires_at = declared
        raise refused from None


def _declared_expiry(info: Any) -> float | None:
    """Read the time caveat's expiry leniently, for a token about to be refused; None when it cannot be read.

    Never raises: an expiry too large for a float, or any answer it cannot read, is None (unknown).
    """
    try:
        return _read_expiry(info)
    except Exception:
        return None


def _read_expiry(info: Any) -> float | None:
    """The body of `_declared_expiry`."""
    caveats = info.get("caveats") if isinstance(info, Mapping) else None
    if not isinstance(caveats, list):
        return None
    times = [c.get("validUntil") for c in caveats if isinstance(c, Mapping) and c.get("type") == "time"]
    if len(times) != 1:
        return None
    value = times[0]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _require(info: Any, spec: CaveatSpec, *, subject: str, min_ttl_s: float, max_ttl_s: float, now: float) -> Examined:
    """The checks of `require_examined` on an examination answer."""
    token_id, found_subject, caveats = _parse(info)
    if found_subject != subject:
        raise TokenRefused("the token belongs to another principal than the one it was minted for")
    _check_shape(caveats, spec)
    expiry = _check_values(caveats, spec)
    left = expiry - now
    if left <= 0:
        raise TokenRefused("the token has already expired")
    if left < min_ttl_s:
        raise TokenRefused(f"the token lives {left:.0f} s, shorter than the {min_ttl_s:.0f} s the run needs")
    if left > max_ttl_s:
        raise TokenRefused(f"the token lives {left:.0f} s, longer than the {max_ttl_s:.0f} s allowed")
    return Examined(token_id, found_subject, expiry, tuple(sorted(c["type"] for c in caveats)))


def _rights(values: Any, what: str) -> set[str]:
    """Read a list of privileges; raise TokenRefused naming the field when the answer is not a list of strings.

    Only a list will do. A mapping such as `{"space_view": true}` is a collection whose iteration yields its keys, so
    taking it for a list would read a set of flags, including false ones, as the rights held.
    """
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise TokenRefused(f"the {what} privileges could not be read: 'privileges' is not a list of strings")
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
            raise PrivilegesMismatch(
                f"the principal's {what} privileges differ from the declared set: {'; '.join(parts)}"
            )


def require_distinct(first: Examined, second: Examined) -> None:
    """Require two planes to have different principals and different tokens: nothing is shared between them."""
    if first.subject == second.subject:
        raise TokenRefused("the two planes share a principal")
    if first.token_id == second.token_id:
        raise TokenRefused("the two planes share a token")
