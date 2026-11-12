# Provenance: new
"""What the agent of an outage meets, started for the day-0 outage check as a run starts it (O28, issue #108).

For one space's scenario `AgentPlane.opened` leases a slot of `[slots]` (`expcore.campaign.lease.LeasePool`, over the
platform's own probe), and through the platform's `SandboxPlatform`:

- **the sandbox** (`launch`): the sandbox package's `DockerSandbox` at the first level of `[sandbox.data_paths]` that
  maps to the mount, with its oneclient sidecar and the slot's data-path token; without a mount level, the first that
  maps to none (else the first level), so the tool server still has a run to belong to;
- **the tool server** (`tool_server`): the condition's server, with the slot's tool-server user and the token minted
  for it with the data path's, examined and checked against the condition's rights before it starts (C19).

The condition is `Context.tool_condition`, or else the first `[toolserver]` condition whose allow-list has the four
knowledge tools. Both start under the federation's exclusive admission the check holds (`require_admission`).

Starting containers is the caller's consent: the check is given a host (`Context.agent_host`, a `DockerHost` when
`EXPCORE_LIVE_DOCKER=1`) or starts nothing. Whatever cannot start is recorded with its reason (`facts`), never raised,
and the scenario goes on without it: what it would have measured is then unmeasured, which makes the check unknown.
A teardown that cannot show the sandbox gone, or a tool plane that cannot be re-verified at its stop, is a problem of
the check (`problems`). The lease's journal and the token journal are in the report's directory (`day0-leases`).

**Tokens.** Both tokens are minted for `ceiling_s`: `Context.outage_ceiling_s` when the caller names one, else the
longest the scenario can need them (`outage_budget.scenario_s`). `tokens_until` is when, on the check's clock, they
surely still hold (the sandbox's start plus the ceiling; each is minted later, for at least that), so `outage_run` can
tell a step the tokens could no longer make from one the provider failed.
"""

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from expcore.campaign.lease import LeasePool, LeaseRefused
from expcore.config.loading import ConfigError, load_toml
from expcore.errors import InfraError
from expcore.plugin import NO_WAITS
from expcore_onedata.config import StudySpace
from expcore_onedata.day0.outage_budget import scenario_s
from expcore_onedata.day0.outage_tools import KNOWLEDGE_TOOLS
from expcore_onedata.day0.registry import Context
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.toolserver import ToolServerLauncher
from expcore_onedata.users import GROUP, TokenJournal, load_slots

LEASES = "day0-leases"


def mount_levels(ctx: Context) -> list[str]:
    """The levels of `[sandbox.data_paths]` that map to the mount: the platform declares a mount data path."""
    sandbox = ctx.config.sandbox
    return [] if sandbox is None else [lvl for lvl in sandbox.data_paths if sandbox.mode_of(lvl) is DataPathMode.MOUNT]


def knowledge_condition(ctx: Context) -> str | None:
    """The tool-server condition the check calls the tools of: the one named, else the first that allows all four."""
    server = ctx.config.toolserver
    if server is None:
        return None
    if ctx.tool_condition is not None:
        return ctx.tool_condition if ctx.tool_condition in server.conditions else None
    return next((name for name, cond in server.conditions.items() if set(KNOWLEDGE_TOOLS) <= set(cond.allow)), None)


def refusal(ctx: Context) -> str | None:
    """What stands between the check and starting the sandbox and the tool server, or None. A platform without a
    `[sandbox]` table is named as such, whether or not containers were allowed."""
    config = ctx.config
    if config.sandbox is None:
        return "platform.toml has no [sandbox] table, so no sandbox or tool server was started"
    if ctx.agent_host is None:
        return "EXPCORE_LIVE_DOCKER=1 is not set, so no sandbox or tool server was started"
    if "slots" not in load_toml(config.file):
        return "platform.toml has no [slots] table, so no user could be leased for the sandbox and the tool server"
    if config.toolserver is None:
        return "platform.toml has no [toolserver] table, so the agent's tools cannot be started"
    if knowledge_condition(ctx) is None:
        return "no [toolserver] condition allows the four knowledge tools (or the named one is not declared)"
    return None


def unallowed_tools(ctx: Context) -> list[str]:
    """The knowledge tools the chosen condition does not allow (all of them when no condition is chosen)."""
    server, condition = ctx.config.toolserver, knowledge_condition(ctx)
    if server is None or condition is None:
        return list(KNOWLEDGE_TOOLS)
    return [name for name in KNOWLEDGE_TOOLS if name not in server.conditions[condition].allow]


def _level(ctx: Context) -> str:
    sandbox = ctx.config.sandbox
    assert sandbox is not None
    levels = list(sandbox.data_paths)
    mounts = mount_levels(ctx)
    nones = [lvl for lvl in levels if sandbox.mode_of(lvl) is DataPathMode.NONE]
    return (mounts or nones or levels)[0]


class AgentPlane:
    """The sandbox and the tool server of one space's scenario; `sandbox` and `tools` are None when not started."""

    def __init__(self, ctx: Context, entry: StudySpace, run_key: str) -> None:
        self._ctx, self._entry, self._run_key = ctx, entry, run_key
        self.sandbox: Any = None
        self.tools: Any = None
        self.mount_path: str | None = None  # the space's directory as the agent sees it through the mount
        self.ceiling_s = ctx.outage_ceiling_s  # what the tokens are minted for, the scenario's worst case by default
        self.tokens_until: float | None = None  # on the check's clock, the latest moment the tokens surely hold
        self.facts: dict[str, Any] = {"sandbox": {"started": False}, "tool_server": {"started": False}}
        self.problems: list[str] = []
        self.events: list[tuple[str, str]] = []

    def _event(self, kind: str, detail: str) -> None:
        self.events.append((kind, detail))

    def _platform(self) -> SandboxPlatform:
        ctx = self._ctx
        slots = load_slots(ctx.config.file, ctx.config)
        journal = TokenJournal(ctx.out_dir / LEASES)
        launcher = ToolServerLauncher(ctx.config, ctx.issuer, events=self._event)
        options = dict(ctx.agent_options)
        return SandboxPlatform(
            ctx.config,
            ctx.issuer,
            ctx.agent_host,
            slots=slots,
            journal=journal,
            events=self._event,
            tool_server=launcher,
            clock=ctx.now,
            **options,
        )

    @contextlib.asynccontextmanager
    async def opened(self) -> AsyncIterator["AgentPlane"]:
        """Lease a slot, start the sandbox and then the tool server, yield, and stop them in reverse order."""
        refused = refusal(self._ctx)
        if refused is not None:
            self.facts = {"sandbox": {"started": False, "reason": refused}, "tool_server": {"started": False}}
            self.facts["tool_server"]["reason"] = refused
            yield self
            return
        ctx, condition = self._ctx, knowledge_condition(self._ctx)
        if self.ceiling_s is None:
            self.ceiling_s = scenario_s(ctx, mount=bool(mount_levels(ctx)))
        self.facts["tokens"] = {"ceiling_s": self.ceiling_s}
        try:
            platform = self._platform()
            pool = LeasePool(
                ctx.out_dir / LEASES, platform.lease_keys, platform.probe(), clock=ctx.clock, sleep=ctx.sleep
            )
            lease = pool.acquire(GROUP, run_key=self._run_key, timeout_s=ctx.outage_bound_s)
        except (ConfigError, LeaseRefused, OSError, ValueError) as error:
            reason = f"no slot could be leased for the sandbox and the tool server ({type(error).__name__})"
            self.facts["sandbox"]["reason"] = self.facts["tool_server"]["reason"] = reason
            yield self
            return
        try:
            async with self._sandbox(platform, lease, condition), self._tool_server(platform):
                yield self
        finally:
            lease.release()

    @contextlib.asynccontextmanager
    async def _sandbox(self, platform: SandboxPlatform, lease: Any, condition: str | None) -> AsyncIterator[None]:
        ctx, level = self._ctx, _level(self._ctx)
        mode = platform.mode({"data_path": level})
        self.facts["sandbox"]["data_path"] = mode.value
        assert self.ceiling_s is not None
        self.tokens_until = ctx.clock() + self.ceiling_s  # minted after this, for at least the ceiling (O06)
        launch = platform.launch(
            NO_WAITS, run_key=self._run_key, level=level, ceiling_s=self.ceiling_s, lease=lease, condition=condition,
        )  # fmt: skip
        try:
            self.sandbox = await launch.__aenter__()
        except Exception as error:  # a start that failed: nothing of it is left (launch tears it down)
            self.facts["sandbox"]["reason"] = f"the sandbox did not start ({type(error).__name__})"
            self.facts["tool_server"]["reason"] = "the tool server belongs to a sandbox that did not start"
            yield
            return
        self.facts["sandbox"]["started"] = True
        settings = ctx.config.sandbox
        if mode is DataPathMode.MOUNT and settings is not None:
            self.mount_path = f"{settings.mount.path}/{self._entry.space.name}"
        try:
            yield
        finally:
            self.facts["sandbox"]["stopped"] = await self._leave(launch, "the sandbox's teardown")

    @contextlib.asynccontextmanager
    async def _tool_server(self, platform: SandboxPlatform) -> AsyncIterator[None]:
        if self.sandbox is None:
            yield
            return
        assert self.ceiling_s is not None
        server = platform.tool_server(NO_WAITS, self.sandbox, ceiling_s=self.ceiling_s)
        try:
            self.tools = await server.__aenter__()
        except Exception as error:  # refused before the start (its plane), or a server that did not start
            self.facts["tool_server"]["reason"] = f"the tool server did not start ({_why(error)})"
            yield
            return
        self.facts["tool_server"]["started"] = True
        try:
            yield
        finally:
            self.facts["tool_server"]["stopped"] = await self._leave(server, "the tool plane's check at its stop")
            self.tools = None

    async def _leave(self, manager: Any, what: str) -> bool:
        """Leave a started context; a failure of its own teardown or check is a problem of the check, recorded."""
        try:
            await manager.__aexit__(None, None, None)
        except Exception as error:
            self.problems.append(f"{what} failed ({_why(error)})")
            return False
        return True


def _why(error: BaseException) -> str:
    """An error by its kind when it has one (an infrastructure failure), else by its class; never its message."""
    return error.kind if isinstance(error, InfraError) else type(error).__name__


__all__ = ["LEASES", "AgentPlane", "knowledge_condition", "mount_levels", "refusal", "unallowed_tools"]
