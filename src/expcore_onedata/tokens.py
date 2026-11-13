# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/scripted/tokens.py, blob 502f83b763e6c4750b2a826866780be1155e3a9e
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:342,345 (privilege reads, direct and effective)
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/harness/slot_users.py:197-204 (the temporary token of a user)
#   changes: see git history of this file from its import commit
"""Mint the tokens of a run's two planes, each for its own principal, and examine each before it is handed out.

The data path and the tool server are separate planes (architecture.md, "Tokens and permissions"): each has its own
platform user and its own token, and nothing is shared. The data path is read-only on the study space and its user holds
exactly view and read. The tool server's user holds the condition's declared rights, no more and no less, and its token
carries no data-access caveat, whether the condition declares a write right or none (`tool_server_spec`, O39).

What `TokenIssuer.mint` does, in this order, and refuses at the first failure:
1. derives the lifetime from the run's whole-time ceiling and checks it against the platform's limit, before anything is minted
   (O06: `token_window`);
2. reads the principal's privileges back, direct and effective, and requires exactly the plane's set (O05), and, for a
   token without a data-access caveat (every tool-server token), no Onezone admin privilege (`tokenscope`);
3. mints with the plane's exact caveats (`CaveatSpec`, shared with the examination);
4. examines the token fail-closed, against that exact set and the lifetime window, and, for a token without a
   data-access caveat, requires Onezone's scope of it to be exactly the plane's spaces and the clusters it acts in to
   be none (`tokenscope`). A token that fails is dropped.

A principal mints its own token with its own credential (`OnezoneClient.mint_own_token`). A token ends with its
lifetime, which derives from the whole-time ceiling (O06), unless its user revokes it: a leased slot's users revoke all
their temporary tokens, each with its own credential, when the run that held the slot ends (`users.SlotTokens.revoke`,
C19). `revoke_own` sends that revocation and requires the user's generation to have risen, and `require_revoked`
requires a token to verify as 400 `tokenRevoked` and nothing else: an examination only decodes a token, and a
verification refuses a token with a data-access caveat for that caveat whether or not it is revoked.

The minted value is registered with `expcore.records.redact.register_secret` inside the client, before it comes back
here (R12), and it is wrapped in a `Secret` whose repr and str never show it. A caller reads it with `.reveal()` at the
one place it is needed, such as the environment of a child process.

`ceiling_s` is the whole-time ceiling of the attempt (`AgentConfig.ceiling_s`), not the agent's time cap: the agent's
clock stops during declared waits on infrastructure, so an attempt can last longer than its cap by the length of those
waits, and a token that expired at the cap plus a margin could end during one.
"""

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from urllib.parse import quote

from expcore_onedata.client import VERIFY_TOKEN_PATH, MintNotSent, OnezoneClient, PlatformError
from expcore_onedata.config import Condition, PlatformConfig, TokenSettings
from expcore_onedata.credentials import Principal
from expcore_onedata.readretry import ONE_ATTEMPT
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
from expcore_onedata.tokenscope import (
    admin_privileges,
    effective_clusters,
    require_no_admin,
    require_no_clusters,
    require_scope,
    scope_spaces,
)

TOKEN_REVOKED = "tokenRevoked"  # Onezone's error id for a token older than its user's generation (ctool, HTTP 400)


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
    """A token was minted and then refused; it is dropped, and revoked only with all its user's temporary tokens when
    the run that leased the slot ends. `expires_at` is when it stops working unless revoked, or None when its
    examination could not read that, in which case nothing may assume it has ended."""

    def __init__(self, message: str, expires_at: float | None) -> None:
        super().__init__(message)
        self.expires_at = expires_at


class RevocationUnconfirmed(RuntimeError):
    """A revocation that could not be shown to have taken effect: the user's generation did not rise, or a token did
    not verify as revoked. The message names what failed, never a token, a user or a value of the configuration."""


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
    """The tokens of one run: the data path's when it has a data path, the tool server's when it has a tool server."""

    data_path: MintedToken | None
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


def _scope(spaces: str | Sequence[str]) -> str | tuple[str, ...]:
    """One space's id as it is, or several as a tuple; a sequence of one is that one id, so a one-space token is the
    same whichever way its space was named."""
    if isinstance(spaces, str):
        return spaces
    ids = tuple(spaces)
    return ids[0] if len(ids) == 1 else ids


def data_path_spec(space_id: str | Sequence[str], interface: str = "rest") -> PlaneSpec:
    """The data path: read-only and scoped to its spaces (`PlatformConfig.space_ids`); its principal holds exactly
    view and read in each."""
    return PlaneSpec(Plane.DATA_PATH, BASELINE_RIGHTS, CaveatSpec(_scope(space_id), readonly=True, interface=interface))


def tool_server_spec(space_id: str | Sequence[str], condition: Condition) -> PlaneSpec:
    """The tool server of a condition, reaching its spaces with its declared rights in each.

    Every condition's token carries no data-access caveat, only its expiry and the `rest` interface, whether the
    condition declares a write right or none. On Onedata 25.0 a token with any data-access caveat (`data.path`,
    `data.objectid`, `data.readonly`) is a data-access token, usable for data access alone: a writing condition's could
    neither schedule a transfer nor add a QoS requirement (#177: `POST /transfers` answered `tokenCaveatUnverified`),
    and a read-only condition's was refused its views, `GET /transfers/{id}`, `GET /spaces/{id}/transfers` and `GET
    /qos_requirements/{id}`, with the same 401, as was the tool server's own examination of it (#181, the live run of
    12 Nov). Only reads under `/data/{id}` served on it.

    **The boundary of the token is therefore its principal**, the slot's leased tool-server user (C19, O09), and four
    reads hold it, before the token is minted (the scope and the clusters just after) and again at the tool server's
    launch and teardown: the user's rights, direct and effective, in each of the plane's spaces must be exactly the
    condition's; Onezone's scope of the token, which lists every space the user is an effective member of, must be
    exactly the plane's spaces, so a space of another study or one declared for the data path alone is refused; the
    user must hold no Onezone admin privilege; and the clusters the token acts in, read as the token, must be none,
    since a member of a provider's cluster can act through that provider (`tokenscope`).

    **A read-only condition stays read-only through its user (O39).** Its declared rights hold no write right
    (`rights.declares_write`), and the user must hold exactly those, so it holds none of `rights.WRITE_RIGHTS` and,
    since the set is exact, no other privilege either, `space_write_data` among them: Onedata refuses its replication,
    eviction and QoS requirement for the missing privilege, where it refused them for the caveat before. The data path
    keeps its caveats; only the tool server's token changes.

    Onedata's `api` caveat is not a data-access caveat and could narrow the token to named operations, but which
    operations the tool server's calls map to is not in the source its format was read from, and one missing entry
    would refuse a tool as #177 did, so this token has none. Within those spaces it is a full REST session for every
    call the tool server makes, and what the agent can ask for is the condition's tool allow-list (its frozen manifest).
    """
    return PlaneSpec(
        Plane.TOOL_SERVER, condition.rights, CaveatSpec(_scope(space_id), readonly=False, interface="rest")
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

    def _read_privileges(self, user_id: str, space_id: str) -> tuple[Any, Any]:
        """Read a user's privileges in one space, direct and effective.

        Both paths and the `privileges` key of the answer are confirmed by the source named in this file's header.
        """
        space, user = quote(space_id, safe=""), quote(user_id, safe="")
        answers = [
            self._client.get(f"/spaces/{space}/{kind}/{user}/privileges", retry=ONE_ATTEMPT)
            for kind in ("users", "effective_users")
        ]
        try:
            return answers[0]["privileges"], answers[1]["privileges"]
        except (KeyError, TypeError):
            raise TokenRefused("the principal's privileges could not be read") from None

    def check_user(self, spec: PlaneSpec, user_id: str) -> None:
        """Require the user to hold exactly the plane's rights, direct and effective, in every space the plane's token
        reaches, as Onezone reports them now; with several spaces a refusal names the space by its position. For a
        token without a data-access caveat, also require the user to hold no Onezone admin privilege
        (`tokenscope.AdminPrivileged`), which such a token would carry whatever its spaces.

        An answer that cannot be had raises `ExaminationFailed` (a `TokenRefused` that keeps the cause, so a caller can
        tell an outage from a refusal); one that differs from the plane's set raises `TokenRefused`.
        """
        space_ids = spec.caveats.space_ids
        if len(space_ids) == 1:
            self._check_in(spec, user_id, space_ids[0])
        else:
            for space_id in space_ids:
                try:
                    self._check_in(spec, user_id, space_id)
                except ExaminationFailed as failed:
                    raise ExaminationFailed(f"{self._position(space_id)}: {failed}", failed.error) from None
                except TokenRefused as refused:  # PrivilegesMismatch or TokenRefused, each with a message alone
                    raise type(refused)(f"{self._position(space_id)}: {refused}") from None
        if not spec.caveats.data_access:
            require_no_admin(admin_privileges(self._client, user_id))

    def check_scope(self, spec: PlaneSpec, value: str) -> None:
        """For a token without a data-access caveat (every tool-server token), require Onezone's scope of it to be
        exactly the plane's spaces (`tokenscope.require_scope`); the data path's scope is its `data.path` caveat, so it
        is not read.

        A scope read that gets no answer raises `ExaminationFailed`; an answer that cannot be read raises `TokenRefused`;
        a scope with another space, or without one of the plane's, raises `tokenscope.ScopeMismatch`.
        """
        if spec.caveats.data_access:
            return
        require_scope(scope_spaces(self._client, value), spec.caveats.space_ids, self._config)

    def check_clusters(self, spec: PlaneSpec, value: str) -> None:
        """For a token without a data-access caveat (every tool-server token), require its user to be an effective
        member of no cluster, read as the token itself (`GET /user/effective_clusters` answers for the user it is sent
        as); the data path's boundary is its `data.path` caveat, so it is not read.

        A read that gets no answer raises `ExaminationFailed`; an answer that cannot be read raises `TokenRefused`; a
        member of a cluster raises `tokenscope.ClusterMember`, counting the clusters and never naming one.
        """
        if spec.caveats.data_access:
            return
        require_no_clusters(effective_clusters(self._client, value))

    def _check_in(self, spec: PlaneSpec, user_id: str, space_id: str) -> None:
        """Require the plane's exact rights of the user in one space; raise as `check_user` says."""
        try:
            direct, effective = self._read_privileges(user_id, space_id)
        except PlatformError as error:
            raise ExaminationFailed(f"the principal's privileges could not be read ({error})", error) from None
        require_privileges(spec.rights, direct=direct, effective=effective)

    def _position(self, space_id: str) -> str:
        """Name a space for a message by its position in the file, `spaces[i]`, never by its id."""
        return next((e.position for e in self._config.spaces if e.space.id == space_id), "an undeclared space")

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

        A token that fails examination is dropped and never returned. It ends with its lifetime, which derives from
        `ceiling_s` (O06), or when its user's temporary tokens are revoked (`revoke_own`).
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
        try:
            self.check_scope(spec, value)
            self.check_clusters(spec, value)
        except TokenRefused as refused:
            raise DroppedToken(str(refused), examined.valid_until) from refused
        return MintedToken(spec.plane, principal.user_id, examined.token_id, examined.valid_until, Secret(value))

    def revoke_own(self, principal: Principal) -> tuple[int, int]:
        """Revoke every temporary token of the principal, as the principal, and return its generation before and after.

        The generation is read, the principal's own revocation sent, and the generation read again; one that did not
        rise raises `RevocationUnconfirmed`, and a failed request raises `PlatformError`. Every token of the principal
        then has a generation at or below the one read first, so every one of them is revoked (C19).
        """
        before = self._client.own_temporary_generation(principal)
        self._client.revoke_own_temporary_tokens(principal)
        after = self._client.own_temporary_generation(principal)
        if after <= before:
            raise RevocationUnconfirmed(
                f"the generation of a user's temporary tokens did not rise when they were revoked ({before}, {after})"
            )
        return before, after

    def require_revoked(self, value: str) -> None:
        """Require a token to verify as revoked: Onezone's verification must answer 400 `tokenRevoked`.

        Any other answer raises `RevocationUnconfirmed`: a token that verifies, and every other error, since the
        verification refuses a token with a data-access caveat for that caveat whatever its generation.
        """
        try:
            self._client.query(VERIFY_TOKEN_PATH, {"token": value})
        except PlatformError as error:
            if (error.status, error.code) == (400, TOKEN_REVOKED):
                return
            raise RevocationUnconfirmed(f"a token did not verify as revoked: {error}") from None
        raise RevocationUnconfirmed("a token still verifies after its user's revocation")

    def mint_run_tokens(
        self,
        *,
        data: Principal | None,
        ceiling_s: float,
        interface: str = "rest",
        tool_server: tuple[Principal, str] | None = None,
        valid_until: int | None = None,
    ) -> RunTokens:
        """Mint, given its principal, the data path's token and, given `(principal, condition name)`, the tool server's,
        on separate users; a run with neither plane is refused, since it needs no token.

        The two users must differ by name and by id; the two tokens are checked to be different after minting. A
        refusal returns neither. With `valid_until` both tokens are minted with exactly that expiry (see `mint`).
        """
        data_spaces, tool_spaces = (self._config.space_ids(plane) for plane in (Plane.DATA_PATH, Plane.TOOL_SERVER))
        if data is None and tool_server is None:
            raise ValueError("a run with neither a data path nor a tool server needs no token")
        if (
            data is not None
            and tool_server is not None
            and (tool_server[0].username == data.username or tool_server[0].user_id == data.user_id)
        ):
            raise TokenRefused("the two planes share a principal")
        data_token = (
            None
            if data is None
            else self.mint(data_path_spec(data_spaces, interface), data, ceiling_s=ceiling_s, valid_until=valid_until)
        )
        if tool_server is None:
            return RunTokens(data_token, None)
        principal, condition = tool_server
        tool = self.mint(
            tool_server_spec(tool_spaces, self._config.condition(condition)),
            principal,
            ceiling_s=ceiling_s,
            valid_until=valid_until,
        )
        if data_token is None:
            return RunTokens(None, tool)
        require_distinct(
            Examined(data_token.token_id, data_token.subject, data_token.expires_at, ()),
            Examined(tool.token_id, tool.subject, tool.expires_at, ()),
        )
        return RunTokens(data_token, tool)
