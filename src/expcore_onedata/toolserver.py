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

Waits. The only infrastructure wait kind this module uses is `toolserver_lifecycle`, around the start (the pre-checks
and the spawn) and around the stop. What the agent triggers through the tools, a federation operation or a read of data,
is the agent's own time and is never an infrastructure wait. Health polling before dispatch is the scheduler's
(C11, `PlatformHealth`) and is not a wait either.

`ctx` is anything with `infrastructure_wait(kind)`: the `CallContext` of a tool call, or the attempt's timer, which an
executor has before the loop starts.
"""

import asyncio
import contextlib
import os
import sys
from collections.abc import AsyncIterator, Mapping

from expcore.errors import InfraError
from expcore.plugin import CallContext
from expcore.tools.mcp_client import McpToolProvider
from expcore_onedata.client import PlatformError
from expcore_onedata.config import Condition, PlatformConfig, ToolServer
from expcore_onedata.pin import verify_configured
from expcore_onedata.tokencheck import ExaminationFailed
from expcore_onedata.tokens import MintedToken, Plane, TokenIssuer, token_window, tool_server_spec

WAIT_KIND = "toolserver_lifecycle"

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


class ToolServerLauncher:
    """Starts the tool server of a condition for a run and stops it again."""

    def __init__(self, config: PlatformConfig, issuer: TokenIssuer) -> None:
        self._config = config
        self._issuer = issuer

    @contextlib.asynccontextmanager
    async def launch(
        self,
        ctx: CallContext,
        condition: str,
        token: MintedToken,
        *,
        ceiling_s: float,
        extra_env: Mapping[str, str] | None = None,
    ) -> AsyncIterator[McpToolProvider]:
        """Start the server for a condition and yield its tool provider; stop it on leaving.

        The start and the stop are each inside `ctx.infrastructure_wait("toolserver_lifecycle")`. An outage of Onezone
        while the token is examined or the principal's privileges are read is infrastructure of kind `knowledge`; a
        token or principal it refuses is a `TokenRefused`. `ceiling_s` is the same value the token was minted for, so a
        token that cannot last the run is refused here.
        """
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
            except ExaminationFailed as failed:
                if _is_outage(failed.error):
                    raise InfraError(
                        "knowledge",
                        f"the token or its principal could not be examined before the start: {failed.error}",
                    ) from None
                raise
            await provider.__aenter__()
        try:
            yield provider
        finally:
            async with ctx.infrastructure_wait(WAIT_KIND):
                await provider.__aexit__(*sys.exc_info())
