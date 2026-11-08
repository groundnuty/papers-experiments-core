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
1. derives the lifetime from the run's time cap and checks it against the platform's limit, before anything is minted
   (O06: `token_window`);
2. reads the principal's privileges back, direct and effective, and requires exactly the plane's set (O05);
3. mints with the plane's exact caveats (`CaveatSpec`, shared with the examination);
4. examines the token fail-closed, against that exact set and the lifetime window. A token that fails is revoked.

The minted value is registered with `expcore.records.redact.register_secret` inside the client, before it comes back
here (R12), and it is wrapped in a `Secret` whose repr and str never show it. A caller reads it with `.reveal()` at the
one place it is needed, such as the environment of a child process.

`time_cap_s` is the longest the attempt can last. The agent's clock stops during waits on infrastructure, so a caller
that has a whole-time ceiling (`AgentConfig.ceiling_s`) passes that.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import Condition, PlatformConfig, TokenSettings
from expcore_onedata.rights import BASELINE_RIGHTS
from expcore_onedata.tokencheck import (
    CaveatSpec,
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


def token_window(time_cap_s: float, settings: TokenSettings) -> TokenWindow:
    """Derive a token's lifetimes from the run's time cap, and refuse what the platform cannot grant (O06).

    The least is the cap plus the margin; the most adds the allowed excess; the lifetime asked for adds the headroom
    that covers the seconds between minting and examination.
    """
    if isinstance(time_cap_s, bool) or not isinstance(time_cap_s, int | float) or not math.isfinite(time_cap_s):
        raise TokenRefused("the run's time cap is not a finite number")
    if time_cap_s <= 0:
        raise TokenRefused("the run's time cap must be positive")
    least = math.ceil(time_cap_s + settings.margin_s)
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

    def _read_privileges(self, subject: str) -> tuple[Any, Any]:
        """Read a principal's privileges in the study space, direct and effective.

        Both paths and the `privileges` key of the answer are confirmed by the source named in this file's header.
        """
        space, user = quote(self._config.space.id, safe=""), quote(subject, safe="")
        answers = [
            self._client.get(f"/spaces/{space}/{kind}/{user}/privileges") for kind in ("users", "effective_users")
        ]
        try:
            return answers[0]["privileges"], answers[1]["privileges"]
        except (KeyError, TypeError):
            raise TokenRefused("the principal's privileges could not be read") from None

    def check_principal(self, spec: PlaneSpec, subject: str) -> None:
        """Require the principal to hold exactly the plane's rights, direct and effective; refuse when unreadable."""
        try:
            direct, effective = self._read_privileges(subject)
        except PlatformError as error:
            raise TokenRefused(f"the principal's privileges could not be read ({error})") from None
        require_privileges(spec.rights, direct=direct, effective=effective)

    def examine(self, value: str, spec: PlaneSpec, subject: str, window: TokenWindow) -> Examined:
        """Examine a token value against a plane's exact set and the lifetime window; raise TokenRefused otherwise."""
        return require_examined(
            value,
            spec.caveats,
            subject=subject,
            examine=lambda token: self._client.query("/tokens/examine", {"token": token}),
            min_ttl_s=window.min_ttl_s,
            max_ttl_s=window.max_ttl_s,
            now=self._clock(),
        )

    def mint(self, spec: PlaneSpec, subject: str, *, time_cap_s: float) -> MintedToken:
        """Mint a plane's token for a principal and return it only if it passes examination."""
        window = token_window(time_cap_s, self._config.tokens)
        self.check_principal(spec, subject)
        valid_until = int(self._clock()) + window.mint_ttl_s
        value = self._client.mint_temporary_token(subject, spec.caveats.caveats(valid_until))
        try:
            examined = self.examine(value, spec, subject, window)
        except TokenRefused as refused:
            if not self._revoke_best_effort(subject):
                refused.add_note("the refused token could not be revoked and stays valid until it expires")
            raise
        return MintedToken(spec.plane, subject, examined.token_id, examined.valid_until, Secret(value))

    def _revoke_best_effort(self, subject: str) -> bool:
        """Revoke a principal's tokens while an error is already on its way; say whether it worked."""
        try:
            self._client.revoke_temporary_tokens(subject)
        except PlatformError:
            return False
        return True

    def revoke(self, token: MintedToken) -> None:
        """Revoke the tokens of a principal when its run ends."""
        self._client.revoke_temporary_tokens(token.subject)

    def mint_run_tokens(
        self,
        *,
        data_user: str,
        time_cap_s: float,
        interface: str = "rest",
        tool_server: tuple[str, str] | None = None,
    ) -> RunTokens:
        """Mint the data path's token and, given `(user, condition name)`, the tool server's, on separate principals.

        The two users must differ; the tokens are checked to be different after minting. If anything fails, what was
        already minted is revoked.
        """
        space = self._config.space.id
        if tool_server is not None and tool_server[0] == data_user:
            raise TokenRefused("the two planes share a principal")
        data = self.mint(data_path_spec(space, interface), data_user, time_cap_s=time_cap_s)
        if tool_server is None:
            return RunTokens(data, None)
        user, condition = tool_server
        try:
            tool = self.mint(tool_server_spec(space, self._config.condition(condition)), user, time_cap_s=time_cap_s)
            require_distinct(
                Examined(data.token_id, data.subject, data.expires_at, ()),
                Examined(tool.token_id, tool.subject, tool.expires_at, ()),
            )
        except BaseException as error:
            for plane, subject in (("data path's", data_user), ("tool server's", user)):
                if not self._revoke_best_effort(subject):
                    error.add_note(f"the {plane} token could not be revoked and stays valid until it expires")
            raise
        return RunTokens(data, tool)
