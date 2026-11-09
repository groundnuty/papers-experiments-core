# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/knowledge/launch.py, blob dca6fb5c0ea76ff212b4b898f2cd5459614f3df0
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Launch the MCP stdio tool server of a condition, with that condition's allow-list and the tool-server plane's token.

The command, its arguments, the condition's allow-list, rights and frozen manifest, and the hosts all come from
platform.toml. Only the token differs per run. The server removes the tools outside the allow-list itself (its
`ONEDATA_MCP_TOOLS` setting); the client of `expcore.tools.mcp_client` then requires the tools the server shows to equal
the condition's frozen manifest, and an extra or missing tool fails the start (L13). Nothing here filters tools.

Before it spawns anything the launcher verifies the pinned checkout, when the configuration pins one, examines the token
again against the condition's exact caveat set, and reads the token's principal's privileges again, direct and
effective, requiring exactly the condition's declared rights, no more and no less. An unexamined or drifted token, a
token minted for another condition's rights, and a principal whose privileges changed since the token was minted
cannot start the plane: the caveats of two conditions that both write are the same, so only the privileges tell them
apart.

What the launcher can and cannot prevent. The privileges of the principal can change between the launch-time reread and
the spawn, and again while the run goes on; a client cannot prevent that, so the design detects it:
(a) the principal is a dedicated leased platform user (C19) whose privileges only this harness changes, so a change is
    not expected in the first place;
(b) the launch-time check stays, and refuses a start on any difference;
(c) at teardown, after the server has stopped, the launcher reads the principal's direct and effective privileges again
    and examines the token again. A difference from the condition's declared rights means the run's tool plane was not
    what was declared, so the launcher records a `tool_plane_rights_changed` event through the sink it was given and
    raises `InfraError` of kind `tool_plane_integrity`, which sets the run aside as infrastructure. A teardown that
    cannot re-verify the plane (an outage of Onezone, a token that no longer examines cleanly) is set aside the same
    way, without the event, because unknown is not a pass. The token's remaining life is not held to the run's minimum
    then, because the run is over; it only has to be unexpired.
The event and the error name the condition by its position (`conditions[i]`), never by its name.

The kind of every integrity failure is `tool_plane_integrity`, not `knowledge`. `knowledge` is the core's kind for a tool
server that failed, and a study may declare it as a condition's treatment (a relay that goes down is the manipulation).
A rights change, a token that fails its examination at launch or at teardown, and a privilege read that fails are not
that: they say the plane was not what was declared, so they must be set aside under every condition. The registry
marks `tool_plane_integrity` as not treatable, so no condition can declare it, and the core scores no attempt that
failed before the model's first turn as treatment.

Waits. The only infrastructure wait kind this module uses is `toolserver_lifecycle`, around the start (the pre-checks
and the spawn) and around the stop. What the agent triggers through the tools, a federation operation or a read of data,
is the agent's own time and is never an infrastructure wait. Health polling before dispatch is the scheduler's
(C11, `PlatformHealth`) and is not a wait either.

`ctx` is anything with `infrastructure_wait(kind)`: the `CallContext` of a tool call, or the attempt's timer, which an
executor has before the loop starts.
"""

import asyncio
import contextlib
import dataclasses
import os
import sys
from collections.abc import AsyncIterator, Callable, Mapping

from expcore.errors import TOOL_PLANE_INTEGRITY, InfraError
from expcore.plugin import CallContext
from expcore.tools.mcp_client import McpToolProvider
from expcore_onedata.client import PlatformError
from expcore_onedata.config import Condition, PlatformConfig, ToolServer
from expcore_onedata.credentials import Principal
from expcore_onedata.pin import verify_configured
from expcore_onedata.tokencheck import ExaminationFailed, PrivilegesMismatch, TokenRefused
from expcore_onedata.tokens import MintedToken, Plane, TokenIssuer, token_window, tool_server_spec

WAIT_KIND = "toolserver_lifecycle"
INTEGRITY_KIND = TOOL_PLANE_INTEGRITY  # the kind of every failure of the plane's integrity checks; not treatable
EVENT_RIGHTS_CHANGED = "tool_plane_rights_changed"

# Where the platform records an event: called with the event's kind and a detail that holds no token, and no configured
# name or id except a slot's lease key, which `users.SlotTokens`'s `token_expiry_beyond_request` names. The launcher
# and the slots' minting share one. The campaign binds it to its event log (with the time and the model); a test
# collects the pairs.
EventSink = Callable[[str, str], None]

# The settings of the published tool server (its environment), not names of any federation.
ENV_ZONE_HOST = "ONEDATA_ONEZONE_HOST"
ENV_ZONE_TOKEN = "ONEDATA_ONEZONE_TOKEN"
ENV_PROVIDER_HOST = "ONEDATA_ONEPROVIDER_HOST"
ENV_PROVIDER_TOKEN = "ONEDATA_ONEPROVIDER_TOKEN"
ENV_TOOLS = "ONEDATA_MCP_TOOLS"
ENV_INSECURE_TLS = "ONEDATA_ALLOW_INSECURE_TLS"
MANAGED_ENV = (ENV_ZONE_HOST, ENV_ZONE_TOKEN, ENV_PROVIDER_HOST, ENV_PROVIDER_TOKEN, ENV_TOOLS, ENV_INSECURE_TLS)


def server_environment(
    config: PlatformConfig, condition: Condition, token: MintedToken, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the environment of the server child.

    The one token serves the server's Onezone and Oneprovider settings. The provider is the agent's pinned local one.
    The allow-list is the condition's, and a caller cannot set it or any other managed setting.
    """
    server = _server(config)
    clash = sorted(set(extra or {}) & set(MANAGED_ENV))
    if clash:
        raise ValueError(f"the tool server's own settings are fixed by the condition: {', '.join(clash)}")
    env = {**(extra or {}), "FASTMCP_LOG_LEVEL": "WARNING", "PATH": os.environ.get("PATH", "")}
    env.update(
        {
            ENV_ZONE_HOST: config.zone.url,
            ENV_ZONE_TOKEN: token.reveal(),
            ENV_PROVIDER_HOST: config.local_provider.url,
            ENV_PROVIDER_TOKEN: token.reveal(),
            ENV_TOOLS: ",".join(condition.allow),
        }
    )
    if server.insecure_tls:
        env[ENV_INSECURE_TLS] = "true"
    return env


def _server(config: PlatformConfig) -> ToolServer:
    """Return the tool-server section; raise ValueError naming the file when the configuration has none."""
    if config.toolserver is None:
        raise ValueError(f"{config.file}: has no [toolserver] section")
    return config.toolserver


def _prepare(
    issuer: TokenIssuer, config: PlatformConfig, condition: Condition, token: MintedToken, ceiling_s: float
) -> None:
    """The checks before a spawn: the pinned checkout, the token examined again against the condition's caveats, and
    the principal's privileges read again against the condition's rights. Each must equal the condition's set exactly."""
    if token.plane is not Plane.TOOL_SERVER:
        raise ValueError("the tool server takes the tool-server plane's token, not another plane's")
    verify_configured(_server(config))
    spec = tool_server_spec(config.space.id, condition)
    issuer.examine(token.reveal(), spec, token.subject, token_window(ceiling_s, config.tokens))
    issuer.check_user(spec, token.subject)


def _is_outage(error: Exception) -> bool:
    """Tell an outage of the federation (no answer, or a server error) from a refusal of the token itself."""
    return isinstance(error, PlatformError) and (error.status is None or error.status >= 500)


def _integrity_failure(label: str, when: str, refused: TokenRefused) -> InfraError:
    """The set-aside error for a plane that cannot be shown to be what the condition declared, at launch or teardown.

    Whether Onezone could not be reached or the token or principal was refused, the plane is unverified, which is an
    integrity failure of the same kind either way; only the words differ. The text holds the refusal's own message,
    which names rights and caveat types and never a value.
    """
    outage = isinstance(refused, ExaminationFailed) and _is_outage(refused.error)
    what = "could not be reached" if outage else "failed its check"
    return InfraError(INTEGRITY_KIND, f"{label}: the tool plane {what} {when}: {refused}")


def _recheck(
    issuer: TokenIssuer, config: PlatformConfig, condition: Condition, token: MintedToken, ceiling_s: float
) -> None:
    """The checks at teardown: the token examined again and the principal's privileges read again, against the
    condition's exact set. The run is over, so the token only has to be unexpired, not to outlast a run."""
    spec = tool_server_spec(config.space.id, condition)
    window = dataclasses.replace(token_window(ceiling_s, config.tokens), min_ttl_s=0)
    issuer.examine(token.reveal(), spec, token.subject, window)
    issuer.check_user(spec, token.subject)


class ToolServerLauncher:
    """Starts the tool server of a condition for a run, stops it again, and checks at the end that its plane held.

    `events` receives the `tool_plane_rights_changed` event (kind and detail) when the check at teardown finds the
    principal's privileges changed; it is required so that the event cannot go unrecorded by an omission.
    """

    def __init__(self, config: PlatformConfig, issuer: TokenIssuer, *, events: EventSink) -> None:
        self._config = config
        self._issuer = issuer
        self._events = events

    async def _verify_plane(self, condition: str, chosen: Condition, token: MintedToken, ceiling_s: float) -> None:
        """Re-verify the tool plane after the server stopped; raise InfraError of kind tool_plane_integrity unless it held."""
        label = self._config.condition_label(condition)
        try:
            await asyncio.to_thread(_recheck, self._issuer, self._config, chosen, token, ceiling_s)
        except PrivilegesMismatch as changed:
            detail = f"{label}: the tool plane's rights changed during the run: {changed}"
            error = InfraError(INTEGRITY_KIND, detail)
            try:
                self._events(EVENT_RIGHTS_CHANGED, detail)
            except Exception as sink_error:  # the run is set aside whether or not the event could be written
                raise error from sink_error
            raise error from None
        except TokenRefused as refused:
            raise _integrity_failure(label, "at teardown", refused) from None

    @contextlib.asynccontextmanager
    async def launch(
        self,
        ctx: CallContext,
        condition: str,
        token: MintedToken,
        *,
        ceiling_s: float,
        extra_env: Mapping[str, str] | None = None,
        principal: Principal | None = None,
    ) -> AsyncIterator[McpToolProvider]:
        """Start the server for a condition and yield its tool provider; stop it on leaving.

        `principal` is the leased slot's tool-server user (`SandboxPlatform.tool_server` passes it): a token minted for
        any other user is refused before anything starts, so the plane's principal is the run's own slot's (C19).

        The start and the stop are each inside `ctx.infrastructure_wait("toolserver_lifecycle")`. A token that fails its
        examination, a principal whose privileges differ from the condition's rights, and a read of either that fails
        (an outage included) raise `InfraError` of kind `tool_plane_integrity` before the server is started; a server
        that fails to start is the server's own. `ceiling_s` is the same value the token was minted for, so a token that
        cannot last the run fails here.

        On leaving, after the server has stopped, the principal's privileges and the token are checked again: a
        change from the condition's declared rights records `tool_plane_rights_changed` and raises `InfraError` of kind
        `tool_plane_integrity`, which replaces any exception of the run's body, because the run is set aside whatever
        it did.
        """
        if principal is not None and token.subject != principal.user_id:
            raise ValueError("the tool-server token was not minted for the leased slot's tool-server user")
        server, chosen = _server(self._config), self._config.condition(condition)
        env = server_environment(self._config, chosen, token, extra_env)
        provider = McpToolProvider(
            server.command,
            server.args,
            frozen=chosen.manifest,
            env=env,
            allowed_tools=chosen.allow,
            start_timeout_s=server.start_timeout_s,
        )
        async with ctx.infrastructure_wait(WAIT_KIND):
            try:
                await asyncio.to_thread(_prepare, self._issuer, self._config, chosen, token, ceiling_s)
            except TokenRefused as refused:
                raise _integrity_failure(self._config.condition_label(condition), "before the start", refused) from None
            await provider.__aenter__()
        try:
            yield provider
        finally:
            async with ctx.infrastructure_wait(WAIT_KIND):
                try:
                    await provider.__aexit__(*sys.exc_info())
                finally:
                    await self._verify_plane(condition, chosen, token, ceiling_s)
