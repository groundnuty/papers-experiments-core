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
cannot start the plane: every condition's token carries the same caveats, a read-only condition's as a writing one's
(O88), so only the privileges tell them apart, and only they keep a read-only condition from writing. The token carries
no data-access caveat, so its user is its boundary: the launcher also reads Onezone's scope of the token, which must be
exactly the tool-server plane's spaces, the user's Onezone admin privileges, which must be none, and the clusters the
user is an effective member of, read as the token, which must be none (`tokenscope`), and reads all three again at
teardown.

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

The checks (`_prepare` before the spawn, `_recheck` after the stop) read Onezone in worker threads, and each holds a share
of the run's federation admission while it runs (`expcore_onedata.admission_share`, O11): a launch cancelled while one
runs stops waiting for it, and its run may leave its admission, but the federation stays held until the thread has ended,
so nothing of a run acts on the federation after its admission ends.

`ctx` is anything with `infrastructure_wait(kind)`: the `CallContext` of a tool call, or the attempt's timer, which an
executor has before the loop starts.

The server's process. The MCP client spawns it as a child of the driver, in a session of its own, with the client's
inherited variables of the driver's environment (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`) beside the settings
here, in the driver's working directory: on Docker that child is a process on the host, outside every container. A
backend may name where it runs instead (`process`, an `api.ServerProcess`; the Apptainer backend's
`tool_server_process`, O56): then its `HOME` and working directory are the backend's directory, its settings include
the backend's (the run's tag), and its environment is named whole: the client's inherited variables that the settings do
not set are unset by `env -u ... --` before the command, so nothing of the driver's reaches it but `PATH`, and no value,
the token least of all, is ever an argument (`env -i` would need every value as one).
"""

import asyncio
import contextlib
import dataclasses
import os
import sys
import tempfile
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from pathlib import Path

from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS

from expcore.config.loading import ConfigError
from expcore.errors import TOOL_PLANE_INTEGRITY, InfraError
from expcore.plugin import CallContext
from expcore.tools.mcp_client import McpToolProvider
from expcore_onedata.addresses import AddressMapError, launch_argv
from expcore_onedata.admission import (
    TOOL_SERVER_STEP,
    TokenStepRefused,
    begin_token_step,
    current_admission,
    require_admission,
    token_step,
)
from expcore_onedata.admission_share import ThreadShare
from expcore_onedata.client import PlatformError
from expcore_onedata.config import Condition, PlatformConfig, ToolServer
from expcore_onedata.credentials import Principal
from expcore_onedata.pin import verify_configured
from expcore_onedata.sandbox.api import ServerProcess
from expcore_onedata.tls import CA_BUNDLE_ENV, write_private_copy
from expcore_onedata.tokencheck import ExaminationFailed, PrivilegesMismatch, TokenRefused
from expcore_onedata.tokens import MintedToken, Plane, TokenIssuer, token_window, tool_server_spec

WAIT_KIND = "toolserver_lifecycle"
INTEGRITY_KIND = TOOL_PLANE_INTEGRITY  # the kind of every failure of the plane's integrity checks; not treatable
EVENT_RIGHTS_CHANGED = "tool_plane_rights_changed"
# the plane's two checks, each a step that uses the run's token (`admission.begin_token_step`), as a refusal names them
CHECK_BEFORE_START, CHECK_AT_STOP = "the tool server's check before its start", "the tool server's check at its stop"

# Where the platform records an event: called with the event's kind and a detail that holds no configured name, token or
# id (a condition or a slot is named by its position). The launcher and the slots' minting (`users.SlotTokens`) share
# one. The campaign binds it to its event log (with the time and the model); a test collects the pairs.
EventSink = Callable[[str, str], None]

# The settings of the published tool server (its environment), not names of any federation.
ENV_ZONE_HOST = "ONEDATA_ONEZONE_HOST"
ENV_ZONE_TOKEN = "ONEDATA_ONEZONE_TOKEN"
ENV_PROVIDER_HOST = "ONEDATA_ONEPROVIDER_HOST"
ENV_PROVIDER_TOKEN = "ONEDATA_ONEPROVIDER_TOKEN"
ENV_TOOLS = "ONEDATA_MCP_TOOLS"
ENV_INSECURE_TLS = "ONEDATA_ALLOW_INSECURE_TLS"
MANAGED_ENV = (
    ENV_ZONE_HOST,
    ENV_ZONE_TOKEN,
    ENV_PROVIDER_HOST,
    ENV_PROVIDER_TOKEN,
    ENV_TOOLS,
    ENV_INSECURE_TLS,
    *CA_BUNDLE_ENV,
)


def server_argv(config: PlatformConfig) -> tuple[str, list[str]]:
    """The server's command and arguments (O73): `[toolserver]`'s as declared, or, for a platform whose names resolve
    nowhere (`[addresses] resolver`), started through the resolve launcher (`addresses.launch_argv`): the declared
    interpreter, `-I -B`, the launcher, the private map as an argument, `--`, and the declared arguments. The server
    then resolves exactly the map's names, to their addresses, and every other host as before; nothing is put in its
    environment for it (no `PYTHONPATH`, no setting naming the map), every Python of the driver's running `-I -B`; it
    covers Python servers only."""
    server = _server(config)
    if config.addresses_resolver is None or config.addresses_file is None:
        return server.command, list(server.args)
    try:
        wrapped = launch_argv(server.command, server.args, config.addresses_resolver, config.addresses_file)
    except AddressMapError as error:
        raise ConfigError(f"{config.file}: [toolserver]: {error}") from None
    return wrapped[0], wrapped[1:]


def server_environment(
    config: PlatformConfig,
    condition: Condition,
    token: MintedToken,
    extra: Mapping[str, str] | None = None,
    *,
    ca_copy: Path | None = None,
) -> dict[str, str]:
    """Return the environment of the server child.

    The one token serves the server's Onezone and Oneprovider settings. The provider is the agent's pinned local one.
    The allow-list is the condition's, and a caller cannot set it or any other managed setting.

    It sets no `ONEDATA_MCP_NODE_NAMES`, on any platform: once the services register the declared names there is no
    node name to map, so onedata-mcp runs unset and its text is a Compose federation's. A platform whose names resolve
    nowhere starts the server through the resolve launcher instead (`server_argv`).

    Its certificate check is the platform's `[tls]`: by default the server's own (it verifies with httpx's CAs); with
    `ca_file`, `ca_copy`, the run's own read-only copy of the verified bundle (`tls.write_private_copy`), never the
    configured file, is named by the settings a client reads for its CA file (`CA_BUNDLE_ENV`, of which the server's
    httpx reads `SSL_CERT_FILE`); with `insecure_tls` the server's own flag turns verification off.
    """
    _server(config)
    clash = sorted(set(extra or {}) & set(MANAGED_ENV))
    if clash:
        raise ValueError(f"the tool server's own settings are fixed by the condition: {', '.join(clash)}")
    if config.tls.ca_file is not None and ca_copy is None:
        raise ValueError("the platform's ca_file needs the run's own copy of the verified bundle (ca_copy)")
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
    if config.tls.insecure:
        env[ENV_INSECURE_TLS] = "true"
    elif ca_copy is not None:
        env.update(dict.fromkeys(CA_BUNDLE_ENV, str(ca_copy)))
    return env


def inherited_by_the_client() -> tuple[str, ...]:
    """The variables of the driver's environment the MCP client gives every server it spawns, beside the ones it is
    given (`mcp.client.stdio.get_default_environment`)."""
    return tuple(DEFAULT_INHERITED_ENV_VARS)


def _placed(extra: Mapping[str, str] | None, process: ServerProcess | None) -> Mapping[str, str] | None:
    """The caller's settings with a backend's placement added: its `HOME` and its own settings, which a caller cannot
    set, as it cannot set the managed ones."""
    if process is None:
        return extra
    ours = {**process.env, "HOME": str(process.home)}
    clash = sorted(set(extra or {}) & set(ours))
    if clash:
        raise ValueError(f"the tool server's {', '.join(clash)} is the backend's, not the caller's to set")
    return {**(extra or {}), **ours}


def _spawned(
    command: str, args: list[str], env: Mapping[str, str], process: ServerProcess | None
) -> tuple[str, list[str]]:
    """The command and arguments the client spawns: `command` and `args` (the server's own, or through the resolve
    launcher, `server_argv`), or, with a backend's placement, the same behind `env -u <each inherited variable the
    settings do not set> --`, so that the server's environment is `env` alone."""
    if process is None:
        return command, list(args)
    unset = [word for name in inherited_by_the_client() if name not in env for word in ("-u", name)]
    return "env", [*unset, "--", command, *args]


@contextlib.contextmanager
def _private_bundle(config: PlatformConfig) -> Iterator[Path | None]:
    """With the platform's `ca_file`: the verified bytes, read once and written to a read-only copy in a directory of
    this launch's own (mode 0700), removed with it when the server has stopped. Nothing without it."""
    if config.tls.ca_file is None:
        yield None
        return
    content = config.tls.bundle()
    with tempfile.TemporaryDirectory(prefix="expcore-tls-") as directory:
        yield write_private_copy(content, Path(directory))


def _server(config: PlatformConfig) -> ToolServer:
    """Return the tool-server section; raise ValueError naming the file when the configuration has none."""
    if config.toolserver is None:
        raise ValueError(f"{config.file}: has no [toolserver] section")
    return config.toolserver


def _prepare(
    issuer: TokenIssuer, config: PlatformConfig, condition: Condition, token: MintedToken, ceiling_s: float
) -> None:
    """The checks before a spawn: the pinned checkout, the token examined again against the condition's caveats, and
    the principal's privileges read again against the condition's rights. Each must equal the condition's set exactly.
    The token, which has no data-access caveat whatever the condition's rights, also has its scope read again, which
    must be the plane's spaces, its principal must still hold no Onezone admin privilege, and be a member of no cluster
    (`tokenscope`)."""
    if token.plane is not Plane.TOOL_SERVER:
        raise ValueError("the tool server takes the tool-server plane's token, not another plane's")
    verify_configured(_server(config))
    spec = tool_server_spec(config.space_ids(Plane.TOOL_SERVER), condition)
    issuer.examine(token.reveal(), spec, token.subject, token_window(ceiling_s, config.tokens))
    issuer.check_user(spec, token.subject)
    issuer.check_scope(spec, token.reveal())
    issuer.check_clusters(spec, token.reveal())


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
    condition's exact set, and the token's scope, its principal's admin privileges and its clusters, as at launch.
    The run is over, so the token only has to be unexpired, not to outlast a run."""
    spec = tool_server_spec(config.space_ids(Plane.TOOL_SERVER), condition)
    window = dataclasses.replace(token_window(ceiling_s, config.tokens), min_ttl_s=0)
    issuer.examine(token.reveal(), spec, token.subject, window)
    issuer.check_user(spec, token.subject)
    issuer.check_scope(spec, token.reveal())
    issuer.check_clusters(spec, token.reveal())


async def _in_counted_thread(config: PlatformConfig, what: str, check: Callable[..., None], *args: object) -> None:
    """Run one of the plane's checks (`what`, in words), which examine the run's token and read Onezone, in a worker
    thread that, for its whole life, is counted as the tool server's step on the run's admission
    (`admission.begin_token_step`) and holds a share of that admission (`admission_share.ThreadShare`). Both are taken
    here, before the thread exists, the share first, so the admission the count is taken on stays held, and the
    thread gives both up when the check ends, on success or an exception, the share last; a thread that never began
    has its share given up when its task is done.

    On a platform with `[faults]` a check is refused before its thread starts, holding nothing, when the run's admission
    is not held or no share of it can be taken (it is being let go, or the run's shortcut section has sealed it):
    `TokenStepRefused`, naming it, failing closed (O35, O11). A cancellation of the await does not stop the thread, so
    the count and the share outlive a launch that leaves without it: no restore heals while it still examines the
    token (O35), and the federation stays held until it has ended (O11); the thread's failure is then never
    unretrieved. A thread the event loop's shutdown kept from ever starting leaves the count open, so the restore fails
    closed. Without `[faults]` there is no admission: nothing is counted, shared or refused."""
    admission = current_admission()  # read once: the share is taken from it before the count, so it stays held
    give_up = admission.try_retain() if admission is not None else None
    try:  # refuses when the run's admission is not held, and then any share taken is given back
        end = begin_token_step(TOOL_SERVER_STEP, config, what)
    except BaseException:
        if give_up is not None:
            give_up()
        raise
    if admission is not None and give_up is None:  # held, but being let go or sealed: no share, so it never starts
        end()
        raise TokenStepRefused(config, TOOL_SERVER_STEP, what)
    share = ThreadShare(give_up or (lambda: None))  # the thread, not this coroutine, lets the federation go (O11)

    def work() -> None:
        if not share.begin():  # its share was given up for it already: the check never runs
            end()
            return
        try:
            check(*args)
        finally:
            try:
                end()
            finally:
                share.end()  # last: the federation stays held until the thread has ended

    running = asyncio.ensure_future(asyncio.to_thread(work))
    running.add_done_callback(lambda done: done.cancelled() or done.exception())
    running.add_done_callback(lambda _: share.never_ran())
    await asyncio.shield(running)


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
            await _in_counted_thread(
                self._config, CHECK_AT_STOP, _recheck, self._issuer, self._config, chosen, token, ceiling_s
            )
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
        process: ServerProcess | None = None,
    ) -> AsyncIterator[McpToolProvider]:
        """Start the server for a condition and yield its tool provider; stop it on leaving.

        `principal` is the leased slot's tool-server user (`SandboxPlatform.tool_server` passes it): a token minted for
        any other user is refused before anything starts, so the plane's principal is the run's own slot's (C19).

        `process` is where a backend runs the server (`SandboxPlatform.tool_server` passes the sandbox's
        `tool_server_process()` when it has one, O56): its home and working directory, and its settings, which
        `extra_env` may not set (`ValueError`); the server's environment is then named whole (the module's
        docstring). Without it the server's process is as it always was.

        The start and the stop are each inside `ctx.infrastructure_wait("toolserver_lifecycle")`. A token that fails its
        examination, a principal whose privileges differ from the condition's rights, and a read of either that fails
        (an outage included) raise `InfraError` of kind `tool_plane_integrity` before the server is started; a server
        that fails to start is the server's own. `ceiling_s` is the same value the token was minted for, so a token that
        cannot last the run fails here; the platform's `tool_server` passes the ceiling its launch minted for and
        refuses another.

        On leaving, after the server has stopped, the principal's privileges and the token are checked again: a
        change from the condition's declared rights records `tool_plane_rights_changed` and raises `InfraError` of kind
        `tool_plane_integrity`, which replaces any exception of the run's body, because the run is set aside whatever
        it did.

        From before its first check to the end of that last one, the launch is a step of the run that uses its token,
        counted on the run's admission (`admission.token_step`), so the fault injector's restore can tell that it
        still runs and that a heal might outlast the token (O35). On a platform with `[faults]` each check starts only
        under the run's admission, held then: one that is not held refuses the check before it starts, naming it
        (`admission.TokenStepRefused`), and that refusal is raised, at the start or at the stop.
        """
        if principal is not None and token.subject != principal.user_id:
            raise ValueError("the tool-server token was not minted for the leased slot's tool-server user")
        require_admission(self._config)  # a platform with a fault injector: the run holds the federation (O11)
        server, chosen = _server(self._config), self._config.condition(condition)
        with (
            token_step(TOOL_SERVER_STEP),  # ended only once the re-verification at teardown has ended
            _private_bundle(self._config) as ca_copy,  # the run's copy of the verified CA bytes, gone with the server
        ):
            env = server_environment(self._config, chosen, token, _placed(extra_env, process), ca_copy=ca_copy)
            command, args = _spawned(*server_argv(self._config), env, process)
            provider = McpToolProvider(
                command,
                args,
                frozen=chosen.manifest,
                env=env,
                cwd=None if process is None else process.home,
                allowed_tools=chosen.allow,
                start_timeout_s=server.start_timeout_s,
            )
            async with ctx.infrastructure_wait(WAIT_KIND):
                try:
                    await _in_counted_thread(
                        self._config, CHECK_BEFORE_START, _prepare, self._issuer, self._config, chosen, token, ceiling_s
                    )
                except TokenRefused as refused:
                    label = self._config.condition_label(condition)
                    raise _integrity_failure(label, "before the start", refused) from None
                await provider.__aenter__()
            try:
                yield provider
            finally:
                async with ctx.infrastructure_wait(WAIT_KIND):
                    try:
                        await provider.__aexit__(*sys.exc_info())
                    finally:
                        await self._verify_plane(condition, chosen, token, ceiling_s)
