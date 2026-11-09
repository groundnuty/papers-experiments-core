# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/tokens.py, blob 502f83b763e6c4750b2a826866780be1155e3a9e
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:342,345 (privilege reads, direct and effective)
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:197-204 (the temporary token of a user)
#   changes: see git history of this file from its import commit
"""Mint the tokens of a run's two planes, each for its own principal, and examine each before it is handed out.

The data path and the tool server are separate planes (architecture.md, "Tokens and permissions"): each has its own
platform user and its own token, and nothing is shared. The data path is read-only on the study space and its user holds
exactly view and read. The tool server's token and user match the condition's declared rights, no more and no less, and
its token is read-only only when the condition declares no write right.

What `TokenIssuer.mint` does, in this order, and refuses at the first failure:
1. derives the lifetime from the run's whole-time ceiling and checks it against the platform's limit, before anything is minted
   (O06: `token_window`);
2. reads the principal's privileges back, direct and effective, and requires exactly the plane's set (O05);
3. mints with the plane's exact caveats (`CaveatSpec`, shared with the examination);
4. examines the token fail-closed, against that exact set and the lifetime window. A token that fails is dropped.

A principal mints its own token with its own credential (`OnezoneClient.mint_own_token`). Tokens are never revoked: no
source revokes one, so each ends with its lifetime, which derives from the whole-time ceiling (O06).

The minted value is registered with `expcore.records.redact.register_secret` inside the client, before it comes back
here (R12), and it is wrapped in a `Secret` whose repr and str never show it. A caller reads it with `.reveal()` at the
one place it is needed, such as the environment of a child process.

`ceiling_s` is the whole-time ceiling of the attempt (`AgentConfig.ceiling_s`), not the agent's time cap: the agent's
clock stops during declared waits on infrastructure, so an attempt can last longer than its cap by the length of those
waits, and a token that expired at the cap plus a margin could end during one.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from expcore_onedata.client import MintNotSent, OnezoneClient, PlatformError
from expcore_onedata.config import Condition, PlatformConfig, TokenSettings
from expcore_onedata.credentials import Principal
from expcore_onedata.rights import BASELINE_RIGHTS
from expcore_onedata.tokencheck import (
    CaveatSpec,
    ExaminationFailed,
    Examined,
    TokenRefused,
    require_distinct,
    require_examined,
    require_privileges,
)


class Plane(StrEnum):
    """The two planes a run holds a token on."""

    DATA_PATH = "data_path"
    TOOL_SERVER = "tool_server"


class Secret:
    """A secret value that does not show itself: repr and str are fixed text, and `reveal` is the one way out."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """Return the value; call it only where the value is used."""
        return self._value

    def __repr__(self) -> str:
        return "Secret(<hidden>)"

    __str__ = __repr__


class DroppedToken(TokenRefused):
    """A token was minted and then refused; it is dropped, never revoked. `expires_at` is when it stops working,
    or None when its examination could not read that, in which case nothing may assume it has ended."""

    def __init__(self, message: str, expires_at: float | None) -> None:
        super().__init__(message)
        self.expires_at = expires_at


class ExpiryMismatch(DroppedToken):
    """A token's examined expiry is not the one it was minted with; `expires_at` is the one it has."""

    def __init__(self, message: str, expires_at: float) -> None:
        super().__init__(message, expires_at)


@dataclass(frozen=True)
class MintedToken:
    """A token that passed examination, with what the examination established; the value is in `secret`."""

    plane: Plane
    subject: str
    token_id: str
    expires_at: float
    secret: Secret = field(repr=False)

    def reveal(self) -> str:
        """Return the token value."""
        return self.secret.reveal()


@dataclass(frozen=True)
class RunTokens:
    """The tokens of one run: the data path's, and the tool server's when the condition has a tool server."""

    data_path: MintedToken
    tool_server: MintedToken | None


@dataclass(frozen=True)
class PlaneSpec:
    """What one plane's principal and token must be: the rights the principal holds and the exact caveats."""

    plane: Plane
    rights: tuple[str, ...]
    caveats: CaveatSpec


@dataclass(frozen=True)
class TokenWindow:
    """The lifetimes of a token for one run, in seconds: the least it may have, the most, and what is asked for."""

    min_ttl_s: int
    max_ttl_s: int
    mint_ttl_s: int


def data_path_spec(space_id: str, interface: str = "rest") -> PlaneSpec:
    """The data path: read-only and scoped to the space; its principal holds exactly view and read."""
    return PlaneSpec(Plane.DATA_PATH, BASELINE_RIGHTS, CaveatSpec(space_id, readonly=True, interface=interface))


def tool_server_spec(space_id: str, condition: Condition) -> PlaneSpec:
    """The tool server of a condition: its declared rights, read-only exactly when none of them writes."""
    return PlaneSpec(
        Plane.TOOL_SERVER, condition.rights, CaveatSpec(space_id, readonly=not condition.writes, interface="rest")
    )


def token_window(ceiling_s: float, settings: TokenSettings) -> TokenWindow:
    """Derive a token's lifetimes from the run's whole-time ceiling, and refuse what the platform cannot grant (O06).

    The least is the cap plus the margin; the most adds the allowed excess; the lifetime asked for adds the headroom
    that covers the seconds between minting and examination.
    """
    if isinstance(ceiling_s, bool) or not isinstance(ceiling_s, int | float) or not math.isfinite(ceiling_s):
        raise TokenRefused("the run's ceiling is not a finite number")
    if ceiling_s <= 0:
        raise TokenRefused("the run's ceiling must be positive")
    least = math.ceil(ceiling_s + settings.margin_s)
    window = TokenWindow(least, least + settings.max_excess_s, least + settings.mint_headroom_s)
    if settings.max_ttl_s is not None and window.mint_ttl_s > settings.max_ttl_s:
        raise TokenRefused("the platform does not grant a token as long as this run needs")
    return window


class TokenIssuer:
    """Mints and examines the tokens of both planes through one client."""

    def __init__(
        self, client: OnezoneClient, config: PlatformConfig, *, clock: Callable[[], float] = time.time
    ) -> None:
        self._client = client
        self._config = config
        self._clock = clock

    def _read_privileges(self, user_id: str) -> tuple[Any, Any]:
        """Read a user's privileges in the study space, direct and effective.

        Both paths and the `privileges` key of the answer are confirmed by the source named in this file's header.
        """
        space, user = quote(self._config.space.id, safe=""), quote(user_id, safe="")
        answers = [
            self._client.get(f"/spaces/{space}/{kind}/{user}/privileges") for kind in ("users", "effective_users")
        ]
        try:
            return answers[0]["privileges"], answers[1]["privileges"]
        except (KeyError, TypeError):
            raise TokenRefused("the principal's privileges could not be read") from None

    def check_user(self, spec: PlaneSpec, user_id: str) -> None:
        """Require the user to hold exactly the plane's rights, direct and effective, as Onezone reports them now.

        An answer that cannot be had raises `ExaminationFailed` (a `TokenRefused` that keeps the cause, so a caller can
        tell an outage from a refusal); one that differs from the plane's set raises `TokenRefused`.
        """
        try:
            direct, effective = self._read_privileges(user_id)
        except PlatformError as error:
            raise ExaminationFailed(f"the principal's privileges could not be read ({error})", error) from None
        require_privileges(spec.rights, direct=direct, effective=effective)

    def check_principal(self, spec: PlaneSpec, principal: Principal) -> None:
        """Require the principal to hold exactly the plane's rights, direct and effective; refuse when unreadable."""
        self.check_user(spec, principal.user_id)

    def examine(self, value: str, spec: PlaneSpec, user_id: str, window: TokenWindow) -> Examined:
        """Examine a token value against a plane's exact set and the lifetime window; raise TokenRefused otherwise."""
        return require_examined(
            value,
            spec.caveats,
            subject=user_id,
            examine=lambda token: self._client.query("/tokens/examine", {"token": token}),
            min_ttl_s=window.min_ttl_s,
            max_ttl_s=window.max_ttl_s,
            now=self._clock(),
        )

    def mint(
        self, spec: PlaneSpec, principal: Principal, *, ceiling_s: float, valid_until: int | None = None
    ) -> MintedToken:
        """Mint a plane's token as its principal, with the principal's own credential, and return it only if it passes.

        The token's time caveat is `validUntil`, an absolute time in whole seconds. By default it is the clock at this
        moment plus the window's lifetime, taken after the privileges are read. A caller that must know the expiry
        before minting (a journal line that outlasts every token) passes `valid_until`: the token is minted with
        exactly that, and refused with `ExpiryMismatch` if its examination shows another expiry.

        A token that fails examination is dropped and never returned. Nothing revokes it: no source revokes a token,
        and it ends with its lifetime, which derives from `ceiling_s` (O06).
        """
        window = token_window(ceiling_s, self._config.tokens)
        self.check_principal(spec, principal)
        asked = valid_until if valid_until is not None else int(self._clock()) + window.mint_ttl_s
        try:
            value = self._client.mint_own_token(principal, spec.caveats.caveats(asked))
        except MintNotSent:
            raise  # nothing was sent, so no token exists
        except Exception as error:  # the request was sent: a token may exist whose expiry nothing told us
            raise DroppedToken(f"the token request had no usable answer ({type(error).__name__})", None) from None
        try:  # from here a token exists: any failure drops it with the expiry it is known to have, or none
            examined = self.examine(value, spec, principal.user_id, window)
        except TokenRefused as refused:
            raise DroppedToken(str(refused), refused.expires_at) from refused
        except Exception as error:
            raise DroppedToken(f"the token could not be examined ({type(error).__name__})", None) from None
        if valid_until is not None and examined.valid_until != valid_until:
            raise ExpiryMismatch("the token's expiry is not the one it was minted with", examined.valid_until)
        return MintedToken(spec.plane, principal.user_id, examined.token_id, examined.valid_until, Secret(value))

    def mint_run_tokens(
        self,
        *,
        data: Principal,
        ceiling_s: float,
        interface: str = "rest",
        tool_server: tuple[Principal, str] | None = None,
        valid_until: int | None = None,
    ) -> RunTokens:
        """Mint the data path's token and, given `(principal, condition name)`, the tool server's, on separate users.

        The two users must differ by name and by id; the two tokens are checked to be different after minting. A
        refusal returns neither. With `valid_until` both tokens are minted with exactly that expiry (see `mint`).
        """
        space = self._config.space.id
        if tool_server is not None and (
            tool_server[0].username == data.username or tool_server[0].user_id == data.user_id
        ):
            raise TokenRefused("the two planes share a principal")
        data_token = self.mint(data_path_spec(space, interface), data, ceiling_s=ceiling_s, valid_until=valid_until)
        if tool_server is None:
            return RunTokens(data_token, None)
        principal, condition = tool_server
        tool = self.mint(
            tool_server_spec(space, self._config.condition(condition)),
            principal,
            ceiling_s=ceiling_s,
            valid_until=valid_until,
        )
        require_distinct(
            Examined(data_token.token_id, data_token.subject, data_token.expires_at, ()),
            Examined(tool.token_id, tool.subject, tool.expires_at, ()),
        )
        return RunTokens(data_token, tool)
