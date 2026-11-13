# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/real_sandbox.py, blob 021a144aa05994ecd37086bcab409a40a8615e15
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see the history of this file from its import commit
"""The platform's sandbox for a run: a leased slot, its tokens, a sandbox, and the proof that it is gone.

Per run (never per campaign) the run leases one slot of `[slots.<name>]` (`users.py`) when it needs a data-path token or
a tool server, and none when it needs neither. A slot gives the run both planes' users, so there is one pool of leased
users and one lease key per run. `users.SlotTokens` mints the run's tokens on the slot's users at once, with one
journalled expiry: the data path's (read-only and scoped to the study space, with the interface of the run's data path)
and, for a condition with a tool server, the tool server's, each with a lifetime from the run's whole-time ceiling. The
sandbox starts with the first; the tool server (`tool_server`) starts with the second, on the same slot's tool-server
user. The core's lease pool (C19) holds the key for the attempt. The `ResourceProbe` (`probe`) is
`users.SlotProbe(journal, sandbox_gone=SandboxProbe(...).resource_gone)`: a key is freed only when no container,
network or mount of the sandbox is left and the slot's tokens have ended, by their journalled expiry plus a margin.

`SandboxPlatform` takes a required `events` (a change of interface): the platform's event sink, the one its
tool-server launcher records through, so that the slots' minting can record `token_expiry_beyond_request`
(`users.py`). It has no default, because a silent no-op would lose the event.

Waits. Start and teardown (the token mint, the image checks, the containers, the stop) are each inside
`ctx.infrastructure_wait("sandbox_lifecycle")`. What the agent's code does in the sandbox and its reads of data are the
agent's own time and are never waits (see `codetool.py`).

The study's `data_path` factor names its levels in the study's words; `[sandbox.data_paths]` of platform.toml maps each to
mount, rest or none. Which runs have a tool server, and of which condition, is the study's to say (`tool_condition`).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import threading
import time
import weakref
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from expcore.campaign.lease import Lease, process_gone
from expcore.errors import InfraError
from expcore.plugin import CallContext
from expcore_onedata.admission import require_admission, retain_admission
from expcore_onedata.config import PlatformConfig
from expcore_onedata.credentials import Principal
from expcore_onedata.sandbox import gatelog, kinds
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, ReadOnlyInput, SandboxError, SandboxSpec, StartRefused
from expcore_onedata.sandbox.gatepolicy import PAGED
from expcore_onedata.sandbox.hostops import HostOps, describe_error, mount_identity, parse_mountinfo
from expcore_onedata.sandbox.inputs import InputsRecord
from expcore_onedata.sandbox.registry import DEFAULT, Registry
from expcore_onedata.sandbox.sandbox import MOUNT_RECORDS, DockerSandbox
from expcore_onedata.sandbox.settings import SandboxSettings
from expcore_onedata.tokens import MintedToken, Plane, RunTokens, TokenIssuer, data_path_spec
from expcore_onedata.users import GROUP, SlotProbe, Slots, SlotTokens, TokenJournal

if TYPE_CHECKING:
    from expcore.tools.mcp_client import McpToolProvider
    from expcore_onedata.toolserver import EventSink, ToolServerLauncher

log = logging.getLogger(__name__)

WAIT_KIND = "sandbox_lifecycle"
CANCEL_WAIT_S = 120.0  # how long a cancelled launch waits for its start thread to notice and clean up, before it stops
DATA_PATH_FACTOR = "data_path"  # the study's factor whose levels [sandbox.data_paths] maps


def _well_formed(document: Any) -> bool:
    """Whether a decoded mount record has the shape `DockerSandbox` writes."""
    return (
        isinstance(document, dict)
        and "lease" in document
        and isinstance(document.get("sid"), str)
        and isinstance(document.get("mounts"), list)
        and all(isinstance(m, dict) and isinstance(m.get("path"), str) for m in document["mounts"])
    )


@dataclasses.dataclass(frozen=True)
class _Recorded:
    """What the records say a lease's sandboxes mounted, and the question of whether a mount is one of them."""

    paths: tuple[str, ...]
    identities: frozenset[tuple[str, str, str, str]]  # `hostops.mount_identity`: unchanged by a rename or a move
    sids: frozenset[str]
    key_dir: re.Pattern[str]

    @classmethod
    def of(cls, records: Sequence[Mapping[str, Any]], key: str) -> _Recorded:
        mounts = [m for r in records for m in r["mounts"]]
        return cls(
            tuple(m["path"] for m in mounts),
            frozenset(i for m in mounts if (i := mount_identity(m)) is not None),  # all of them: the probe checks first
            frozenset(r["sid"] for r in records),
            re.compile(re.escape(key) + r"-[0-9a-f]{8}"),
        )

    def owns(self, mount: Mapping[str, str]) -> bool:
        path = mount["mountpoint"]
        parts = path.split("/")
        return (
            any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.paths)
            or mount_identity(mount) in self.identities
            or any(part in self.sids or self.key_dir.fullmatch(part) for part in parts)
        )


class _Share:
    """A start thread's share of the run's admission (`admission.retain_admission`): the thread gives it up when it
    ends, never the launch that may stop waiting for it; a thread that never began has it given up for it."""

    def __init__(self, give_up: Callable[[], None]) -> None:
        self._give_up, self._state, self._lock = give_up, "pending", threading.Lock()

    def begin(self) -> bool:
        """In the thread, first: whether it may run, holding its share."""
        with self._lock:
            if self._state != "pending":
                return False
            self._state = "running"
            return True

    def end(self) -> None:
        """In the thread, last: give the share up."""
        with self._lock:
            self._state = "ended"
        self._give_up()

    def never_ran(self) -> None:
        """When the thread's task is done: give the share up if the thread never began; one that runs gives up its own."""
        with self._lock:
            if self._state != "pending":
                return
            self._state = "ended"
        self._give_up()


class _StartThreads:
    """The mint and start threads of the platform that have not ended, by lease key: the probe says nothing is gone
    while one runs."""

    def __init__(self) -> None:
        self._lock, self._running = threading.Lock(), {}

    def began(self, key: str | None) -> Callable[[], None]:
        """Note a start thread of `key`; the function returned notes its end, once, however often it is called."""
        done = [key is None]
        if key is not None:
            with self._lock:
                self._running[key] = self._running.get(key, 0) + 1

        def ended() -> None:
            with self._lock:
                if not done[0]:
                    done[0] = True
                    self._running[key] -= 1

        return ended

    def active(self, key: str) -> bool:
        with self._lock:
            return self._running.get(key, 0) > 0


class SandboxProbe:
    """The `ResourceProbe` of the sandbox's leases: it proves a key's containers, networks and mounts gone (C19).

    `resource_gone` answers exactly True only when docker lists no container and no network carrying the lease key's
    label, and the whole mount table shows no mount of the lease. A mount is the lease's by identity, not by where it is
    now: the sandbox records the mount id, device, source and type of each mount it makes (`hostops.mount_identity`),
    with its path, in a file under the work
    root (`sandbox.MOUNT_RECORDS`), and the probe matches the table against those records, so a directory that was
    relocated or kept as evidence is still seen. A directory of the lease's sandbox id anywhere in a mount's path counts
    too. Anything it cannot read or ask, a record included, is False, so the key stays quarantined; so is a recorded
    mount of the lease without its full identity, which could be anywhere: such a record holds the key until an
    operator has verified that nothing of it is mounted and cleared it (`mountrecords`, its procedure). It reads and never
    removes: the sandbox's own stop, the next start's retry and the orphan reaper remove. While a mint or start thread of
    the key is still running (a cancelled launch that gave up waiting for it) nothing is gone yet, whatever the reads
    show.
    """

    def __init__(
        self,
        host: HostOps,
        settings: SandboxSettings,
        *,
        holder_gone: Callable[[str], bool | None] = process_gone,
        starting: Callable[[str], bool] = lambda key: False,
    ) -> None:
        self._host, self._s, self._holder_gone, self._starting = host, settings, holder_gone, starting

    def holder_gone(self, owner: str) -> bool | None:
        """Whether the process that held the key is gone (the core's check of its boot, pid and start time)."""
        return self._holder_gone(owner)

    def resource_gone(self, key: str, note: Mapping[str, Any]) -> bool:
        """True only when nothing started with this key is left; False on anything else, a failed read included."""
        if self._starting(key):  # a start thread of this key still runs: it may yet create what none of the reads show
            return False
        label = f"label={self._s.label('lease')}={key}"
        try:
            containers = self._host.docker("ps", "-a", "-q", "--filter", label, check=False, timeout=30)
            networks = self._host.docker("network", "ls", "-q", "--filter", label, check=False, timeout=30)
            if containers.returncode != 0 or networks.returncode != 0:
                return False
            if containers.stdout.strip() or networks.stdout.strip():
                return False
            mounts = parse_mountinfo(self._host.read_text("/proc/self/mountinfo"))
            records = self._records(key)
        except (OSError, SandboxError):
            return False
        if records is None:
            return False
        partial = sum(1 for r in records for m in r["mounts"] if mount_identity(m) is None)
        if partial:  # never left out: a mount without its identity could be anywhere now, so nothing is shown gone
            log.error("%d recorded mount(s) of the lease have no full identity (id, device, source, type)", partial)
            return False
        recorded = _Recorded.of(records, key)
        return not any(recorded.owns(m) for m in mounts)

    def _records(self, key: str) -> list[dict[str, Any]] | None:
        """The mount records of the lease's sandboxes; None when any record cannot be read or is not one (unknown)."""
        directory = self._s.work_root / MOUNT_RECORDS
        try:
            files = sorted(directory.glob("*.json")) if directory.exists() else []
            documents = [json.loads(file.read_text(encoding="utf-8")) for file in files]
        except (OSError, ValueError):
            return None
        if not all(_well_formed(d) for d in documents):
            return None
        return [d for d in documents if d["lease"] == key]


@dataclasses.dataclass(frozen=True)
class _ToolPlane:
    """What a run's tool server starts with: its condition, its token, and the slot user the token must be."""

    condition: str
    token: MintedToken
    principal: Principal


class SandboxPlatform:
    """The sandbox half of the platform for a study: which data path a run has, which slot it leases, and the launch.

    `slots` are the leasable slots of platform.toml (`users.load_slots`) and `journal` the expiry journal beside the
    leases. `events` is the platform's event sink, the one the tool-server launcher is given: the slots' minting records
    `token_expiry_beyond_request` through it (`users.SlotTokens`). `tool_condition` tells from a run's factors the
    tool-server condition it has, or None; `tool_server` starts that server (`toolserver.ToolServerLauncher`) when a
    run has one.
    """

    def __init__(
        self,
        config: PlatformConfig,
        issuer: TokenIssuer,
        host: HostOps,
        *,
        slots: Slots,
        journal: TokenJournal,
        events: EventSink,
        tool_condition: Callable[[Mapping[str, Any]], str | None] = lambda factors: None,
        tool_server: ToolServerLauncher | None = None,
        registry: Registry = DEFAULT,
        sandbox_options: Mapping[str, Any] | None = None,
        cancel_wait_s: float = CANCEL_WAIT_S,
        clock: Callable[[], float] = time.time,
        read_only_inputs: Mapping[str, Sequence[ReadOnlyInput]] | None = None,
    ) -> None:
        if config.sandbox is None:
            raise ValueError(f"{config.file}: has no [sandbox] section")
        self._config, self._s, self._issuer, self._host = config, config.sandbox, issuer, host
        self._slots, self._journal, self._clock = slots, journal, clock
        self._tokens = SlotTokens(issuer, config, slots, journal, events=events, clock=clock)
        self._tool_condition, self._launcher = tool_condition, tool_server
        self._registry, self._options = registry, dict(sandbox_options or {})
        self._cancel_wait_s = cancel_wait_s
        self._threads = _StartThreads()
        self._declared = {item for items in (read_only_inputs or {}).values() for item in items}
        self._planes: weakref.WeakKeyDictionary[DockerSandbox, _ToolPlane] = weakref.WeakKeyDictionary()

    # ------------------------------------------------------------------------------------------------ the plugin's hooks
    def mode(self, factors: Mapping[str, Any]) -> DataPathMode:
        """The data path of a run, from its `data_path` factor."""
        return self._s.mode_of(str(factors[DATA_PATH_FACTOR]))

    def lease_group(self, factors: Mapping[str, Any]) -> str | None:
        """The lease group of a run: the slots, when it needs a data-path token or a tool server; None when neither."""
        needs = self.mode(factors) is not DataPathMode.NONE or self._tool_condition(factors) is not None
        return GROUP if needs else None

    def lease_keys(self, group: str) -> Sequence[str]:
        """The keys of a lease group: the names of `[slots]`; a group this layer does not declare is refused."""
        return self._slots.keys_of(group)

    def sandbox_probe(self) -> SandboxProbe:
        """The probe that proves a key's sandbox gone: no container, network or mount of it is left."""
        return SandboxProbe(self._host, self._s, starting=self._threads.active)

    def probe(self) -> SlotProbe:
        """The `ResourceProbe` for the plugin: a key is free once its sandbox is proven gone and its tokens have ended."""
        return SlotProbe(self._journal, sandbox_gone=self.sandbox_probe().resource_gone, clock=self._clock)

    @staticmethod
    def _begin(sandbox: DockerSandbox, ended: Callable[[], None], share: _Share) -> None:
        """Blocking, in the start thread: begin the sandbox, and note that the thread has ended, however it ended; then
        give up the thread's share of the run's admission, last, so the federation stays held while it runs (O11)."""
        running = share.begin()
        try:
            if running:  # a thread whose share was already given up for it never starts anything
                sandbox.begin()
        finally:
            ended()
            if running:
                share.end()

    # ------------------------------------------------------------------------------------------------ the launch
    def _fixed(self) -> Principal:
        """The user whose credential mints the data-path token of a run that leases no slot (a live check)."""
        fixed = self._config.principals.data_path
        if fixed is None:
            raise ValueError("a run with a data path needs a leased slot, or [principals.data_path] for a fixed user")
        return fixed

    def _spec(
        self,
        run_key: str,
        level: str,
        token: MintedToken | None,
        lease: Lease | None,
        inputs: Sequence[ReadOnlyInput] = (),
    ) -> SandboxSpec:
        mode = self._s.mode_of(level)
        lease_key, given = (lease.key if lease else None), tuple(inputs)
        if mode is DataPathMode.NONE:
            return SandboxSpec(run_key=run_key, mode=mode, lease_key=lease_key, inputs=given)
        return SandboxSpec(
            run_key=run_key,
            mode=mode,
            token=token,
            zone=Endpoint.from_url(self._config.zone.url),
            provider=Endpoint.from_url(self._config.local_provider.url),  # the agent is pinned to its local provider
            lease_key=lease_key,
            listing=self._s.listing_of(level) or PAGED,  # the level's listing mode (O21)
            inputs=given,
        )

    def _mint(
        self, mode: DataPathMode, lease: Lease | None, ceiling_s: float, condition: str | None
    ) -> RunTokens | None:
        """Blocking: mint the run's tokens. With a lease, both planes' through the slot (`SlotTokens`), with one
        journalled expiry; without one, only a data path's, as the fixed user. Nothing exists yet, so nothing needs
        undoing."""
        if lease is None:
            if condition is not None:
                raise ValueError("a run with a tool server needs a leased slot: its tool-server user is the slot's")
            if mode is DataPathMode.NONE:
                return None
            spec = data_path_spec(self._config.space_ids(Plane.DATA_PATH), mode.interface or "rest")
            return RunTokens(self._issuer.mint(spec, self._fixed(), ceiling_s=ceiling_s), None)
        if mode is DataPathMode.NONE and condition is None:
            return None  # a run that needs neither plane mints nothing, whatever it holds
        return self._tokens.mint(lease, ceiling_s=ceiling_s, condition=condition, interface=mode.interface)

    def _mint_tracked(
        self,
        mode: DataPathMode,
        lease: Lease | None,
        ceiling_s: float,
        condition: str | None,
        ended: Callable[[], None],
    ) -> RunTokens | None:
        """Blocking, in the mint thread: mint the run's tokens, and note that the thread has ended, however it ended."""
        try:
            return self._mint(mode, lease, ceiling_s, condition)
        finally:
            ended()

    def _prepare(
        self,
        run_key: str,
        level: str,
        lease: Lease | None,
        tokens: RunTokens | None,
        condition: str | None,
        inputs: Sequence[ReadOnlyInput] = (),
    ) -> DockerSandbox:
        """The sandbox of a run, built and not started: it has no effect on the host, so a launch holds it, and can stop
        it, from before the first resource exists."""
        data = tokens.data_path if tokens is not None else None
        sandbox = DockerSandbox(
            self._host,
            self._s,
            self._spec(run_key, level, data, lease, inputs),
            registry=self._registry,
            config_sha256=self._config.sha256,
            **self._options,
        )
        # never a value, and the slot by its place in the file, never by its name (O12)
        slot = self._slots.keys_of(GROUP).index(lease.key) if lease is not None else "principals.data_path"
        if data is not None:
            sandbox.record(data_path_token={"slot": slot, "expires_at": data.expires_at})
        if tokens is not None and tokens.tool_server is not None and lease is not None:
            sandbox.record(tool_server_token={"slot": slot, "expires_at": tokens.tool_server.expires_at})
            principal = self._slots.users_of(lease).tool_server
            self._planes[sandbox] = _ToolPlane(str(condition), tokens.tool_server, principal)
        return sandbox

    @contextlib.asynccontextmanager
    async def launch(
        self,
        ctx: CallContext,
        *,
        run_key: str,
        level: str,
        ceiling_s: float,
        lease: Lease | None = None,
        condition: str | None = None,
        inputs: Sequence[ReadOnlyInput] = (),
        inputs_record: InputsRecord | None = None,
    ) -> AsyncIterator[DockerSandbox]:
        """Start the sandbox of a run and yield it; stop it, verified, on leaving.

        `inputs` are the run's read-only inputs (`ReadOnlyInput`): files the study supplies, such as scripts the agent
        may run but not change, which the executor passes, from the run's condition or its own, as the platform was
        built with them (`read_only_inputs`, from `inputs.declare`): any other is refused, as is a run with inputs and
        no `inputs_record`, before anything is minted or created. Each is hashed again and refused when its content is
        no longer the declared one, copied into the run's directory, mounted read-only in the agent from that copy,
        proved read-only from inside the agent, and recorded by its hash in the sandbox's metadata and, through
        `inputs_record` (`inputs.InputsRecord`), in the run's record itself (`sandbox.inputs`, O20).

        A run with a lease has its tokens minted through its slot (`users.SlotTokens`): the data path's, and the tool
        server's when `condition` names the run's tool-server condition, which `tool_server` then starts the server
        with. A run with a tool server needs a lease; one without a lease (a live check) mints its data-path token as
        the fixed `[principals.data_path]` user and is never journalled.

        The start and the stop are each inside `ctx.infrastructure_wait("sandbox_lifecycle")`; the body is the agent's
        own time. A teardown that leaves something behind raises `InfraError("sandbox")` when nothing else is in
        flight, and the lease's probe then keeps the key quarantined. `ceiling_s` is the run's whole-time ceiling (O06).
        When the data gate refused an answer for its own integrity during the run, the run ends in `InfraError` of
        kind `data_gate_integrity` (`kinds.gate_error`), whatever its body raised or returned (O05, C01).

        The mint runs in a thread too, which a cancellation does not stop either: it is tracked as a start thread of
        the key from before it is dispatched, so the probe shows nothing gone while it runs, and a launch cancelled or
        failed during the mint waits for it, however often it is cancelled meanwhile, with the same deadline, before it
        re-raises. Its tokens are journalled by `SlotTokens` as they are minted, so a slot whose run was cancelled
        stays held until they have ended (C19).

        The start runs in a thread, and cancelling the launch does not stop a thread: it goes on creating the sandbox.
        So the sandbox is built before the thread begins, and the launch holds it. On cancellation, or any failure of
        the start, the launch asks the thread to stop creating things, waits for it with a deadline however often it is
        cancelled meanwhile, and then tears down everything the sandbox registered; what a thread that outlives the
        deadline creates afterwards carries the lease labels from the moment it exists, and the thread removes it
        itself on its way out, or the probe and the orphan reaper find it (O15, C19). The start thread holds a share of
        the run's admission and gives it up only when it ends, so a launch that stops waiting for it leaves the
        federation held until the thread and its cleanup are done (O11).

        On a platform whose platform.toml has `[faults]`, the run must hold the federation's admission
        (`expcore_onedata.admission`): the campaign takes it through the platform's run admission before the attempt,
        shared for a baseline condition and exclusive for an outage run, and lets it go after the attempt's post-run
        check, so it covers the start, the run, the teardown and the check. Its wait is before the agent's clock starts. Without it `launch` raises `AdmissionRefused` before
        it mints a token or enters a wait (O11). An injector's own admission, taken when its run holds none, is not
        the run's: the run must be admitted before it launches.
        """
        require_admission(self._config)  # first: a refused run mints, journals and starts nothing (O11)
        mode = self._s.mode_of(level)
        if any(item not in self._declared for item in inputs):
            raise StartRefused("a read-only input passed to launch was not declared to the platform (inputs.declare)")
        if inputs and inputs_record is None:
            raise ValueError("a run with read-only inputs needs the instrument that keeps them in its record")
        sandbox: DockerSandbox | None = None
        started: asyncio.Future[None] | None = None
        minting: asyncio.Future[RunTokens | None] | None = None
        try:
            async with ctx.infrastructure_wait(WAIT_KIND):
                minted = self._threads.began(lease.key if lease is not None else None)  # before the thread exists
                minting = asyncio.ensure_future(
                    asyncio.to_thread(self._mint_tracked, mode, lease, ceiling_s, condition, minted)
                )
                minting.add_done_callback(lambda _: minted())  # a task that never got to run has no thread to end
                tokens = await asyncio.shield(minting)
                sandbox = self._prepare(run_key, level, lease, tokens, condition, inputs)
                ended = self._threads.began(lease.key if lease is not None else None)
                share = _Share(retain_admission())  # the thread, not this coroutine, lets the federation go (O11)
                started = asyncio.ensure_future(asyncio.to_thread(self._begin, sandbox, ended, share))
                started.add_done_callback(lambda _: ended())  # a task that never got to run has no thread to end
                started.add_done_callback(lambda _: share.never_ran())
                await asyncio.shield(started)
                if inputs_record is not None:
                    inputs_record.attach(sandbox.read_only_inputs)
        except BaseException:
            if minting is not None and not minting.done():  # cancelled mid-mint: the worker goes on, so wait for it
                minting.add_done_callback(lambda f: f.cancelled() or f.exception())  # its failure is not unretrieved
                await _wait_through_cancellation(minting, self._cancel_wait_s)
            if sandbox is not None and started is not None:
                await self._settle_start(sandbox, started)
                await self._stop(ctx, sandbox, quiet=True)
            raise
        try:
            yield sandbox
        except Exception as error:
            refused = await self._gate_refused(sandbox)
            await self._stop(ctx, sandbox, quiet=True)
            unkillable = isinstance(error, InfraError) and error.kind == kinds.CELL_UNKILLABLE
            # the gate cut the condition's read: that, not the body's failure, ends the run; unless a cell outlived its
            # kill (O30), after which the gate's log, gone with a stopped sandbox, is not the run's evidence
            if refused is not None and not unkillable:
                raise refused from None
            raise
        except BaseException:
            await self._stop(ctx, sandbox, quiet=True)
            raise
        refused = await self._gate_refused(sandbox)
        if refused is not None:  # a refusal no cell reported (a read the agent's code left running): the same end
            await self._stop(ctx, sandbox, quiet=True)
            raise refused
        await self._stop(ctx, sandbox, quiet=False)

    @contextlib.asynccontextmanager
    async def tool_server(
        self, ctx: CallContext, sandbox: DockerSandbox, *, ceiling_s: float, extra_env: Mapping[str, str] | None = None
    ) -> AsyncIterator[McpToolProvider]:
        """Start the tool server of a run whose sandbox `launch` yielded, and yield its tool provider; stop it on leaving.

        The server takes the tool-server token minted with the run's data-path token on the same leased slot, and the
        launcher requires that token's subject to be the slot's tool-server user; nothing else can give it a token.
        """
        plane = self._planes.get(sandbox)
        if plane is None:
            raise ValueError(
                "this sandbox's run was launched without a tool-server condition, so it has no tool server"
            )
        if self._launcher is None:
            raise ValueError("the platform was built without a tool-server launcher")
        async with self._launcher.launch(
            ctx, plane.condition, plane.token, ceiling_s=ceiling_s, extra_env=extra_env, principal=plane.principal
        ) as provider:
            yield provider

    @staticmethod
    async def _gate_refused(sandbox: DockerSandbox) -> InfraError | None:
        """The error the data gate's refusals make of the run, or None; never raises, so the stop always follows. A
        check that fails is unknown, never none: it stands for an unreadable gate log (O16)."""
        try:
            return kinds.gate_error(await asyncio.to_thread(sandbox.gate_refusals))
        except Exception as e:
            log.error("the gate's refusals could not be read: %s", describe_error(e))
            return kinds.gate_error([gatelog.UNREADABLE])

    async def _settle_start(self, sandbox: DockerSandbox, started: asyncio.Future[None]) -> None:
        """Ask the start thread to stop and wait for it, at most `cancel_wait_s`, through any further cancellation."""
        sandbox.abandon()
        started.add_done_callback(
            lambda f: f.cancelled() or f.exception()
        )  # its failure is the caller's, not unretrieved
        await _wait_through_cancellation(started, self._cancel_wait_s)

    async def _stop(self, ctx: CallContext, sandbox: DockerSandbox, *, quiet: bool) -> None:
        async with ctx.infrastructure_wait(WAIT_KIND):
            try:
                await _complete(asyncio.to_thread(sandbox.stop))
            except SandboxError as error:
                if not quiet:
                    raise InfraError("sandbox", f"the sandbox could not be torn down: {error}"[:600]) from None
                log.error("the sandbox of an ended run could not be torn down: %s", error)


async def _wait_through_cancellation(future: asyncio.Future[Any], seconds: float) -> bool:
    """Wait until `future` is done, for at most `seconds`, however often this task is cancelled meanwhile; whether it is."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while not future.done() and (remaining := deadline - loop.time()) > 0:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({future}, timeout=remaining)
    return future.done()


async def _complete(work: Any) -> None:
    """Run `work` (a coroutine) to its end even if this task is cancelled meanwhile, then raise the cancellation: a
    teardown that is cut short leaves what it exists to remove. An error of the work is raised in preference."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            cancelled = True
    task.result()
    if cancelled:
        raise asyncio.CancelledError


__all__ = ["DATA_PATH_FACTOR", "SandboxPlatform", "SandboxProbe"]
