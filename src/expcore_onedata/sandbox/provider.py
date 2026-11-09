# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/real_sandbox.py, blob 021a144aa05994ecd37086bcab409a40a8615e15
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see the history of this file from its import commit
"""The platform's sandbox for a run: a leased user, its data-path token, a sandbox, and the proof that it is gone.

Per run (never per campaign): the run's leased platform user mints its own data-path token (`tokens.TokenIssuer`, read-only
and scoped to the study space, with the interface of the run's data path, a lifetime from the run's whole-time ceiling),
and the sandbox starts with it. The core's lease pool (C19) holds the user and the sandbox's resources for the attempt;
`SandboxProbe` is the `ResourceProbe` that proves them gone, so a key is freed only when no container, network or mount
of the sandbox is left.

Waits. Start and teardown (the token mint, the image checks, the containers, the stop) are each inside
`ctx.infrastructure_wait("sandbox_lifecycle")`. What the agent's code does in the sandbox and its reads of data are the
agent's own time and are never waits (see `codetool.py`).

The study's `data_path` factor names its levels in the study's words; `[sandbox.data_paths]` of platform.toml maps each to
mount, rest or none, and `[sandbox.users]` lists the platform users a run may lease.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from expcore.campaign.lease import Lease, process_gone
from expcore.errors import InfraError
from expcore.plugin import CallContext
from expcore_onedata.config import PlatformConfig
from expcore_onedata.credentials import Principal
from expcore_onedata.sandbox import kinds
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxError, SandboxSpec
from expcore_onedata.sandbox.hostops import HostOps, mount_identity, parse_mountinfo
from expcore_onedata.sandbox.registry import DEFAULT, Registry
from expcore_onedata.sandbox.sandbox import MOUNT_RECORDS, DockerSandbox
from expcore_onedata.sandbox.settings import SandboxSettings
from expcore_onedata.tokens import MintedToken, TokenIssuer, data_path_spec

log = logging.getLogger(__name__)

WAIT_KIND = "sandbox_lifecycle"
CANCEL_WAIT_S = 120.0  # how long a cancelled launch waits for its start thread to notice and clean up, before it stops
DATA_PATH_FACTOR = "data_path"  # the study's factor whose levels [sandbox.data_paths] maps
DATA_USERS = "data_users"  # the lease group of the platform users a data path leases


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


class _StartThreads:
    """The start threads of the platform that have not ended, by lease key: the probe says nothing is gone while one runs."""

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
    mount of the lease without its full identity, which could be anywhere. It reads and never
    removes: the sandbox's own stop, the next start's retry and the orphan reaper remove. While a start thread of the key
    is still running (a cancelled launch that gave up waiting for it) nothing is gone yet, whatever the reads show.
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


class SandboxPlatform:
    """The sandbox half of the platform for a study: which data path a condition has, who may be leased, and the launch."""

    def __init__(
        self,
        config: PlatformConfig,
        issuer: TokenIssuer,
        host: HostOps,
        *,
        registry: Registry = DEFAULT,
        sandbox_options: Mapping[str, Any] | None = None,
        cancel_wait_s: float = CANCEL_WAIT_S,
    ) -> None:
        if config.sandbox is None:
            raise ValueError(f"{config.file}: has no [sandbox] section")
        self._config, self._s, self._issuer, self._host = config, config.sandbox, issuer, host
        self._registry, self._options = registry, dict(sandbox_options or {})
        self._cancel_wait_s = cancel_wait_s
        self._threads = _StartThreads()

    # ------------------------------------------------------------------------------------------------ the plugin's hooks
    def mode(self, factors: Mapping[str, Any]) -> DataPathMode:
        """The data path of a run, from its `data_path` factor."""
        return self._s.mode_of(str(factors[DATA_PATH_FACTOR]))

    def lease_group(self, factors: Mapping[str, Any]) -> str | None:
        """The lease group of a run: the platform users, when its data path takes a token; None when it takes none."""
        return None if self.mode(factors) is DataPathMode.NONE else DATA_USERS

    def lease_keys(self, group: str) -> Sequence[str]:
        """The keys of a lease group: the logins of `[sandbox.users]`."""
        return sorted(self._s.users) if group == DATA_USERS else ()

    def probe(self) -> SandboxProbe:
        """The `ResourceProbe` for the plugin."""
        return SandboxProbe(self._host, self._s, starting=self._threads.active)

    @staticmethod
    def _begin(sandbox: DockerSandbox, ended: Callable[[], None]) -> None:
        """Blocking, in the start thread: begin the sandbox, and note that the thread has ended, however it ended."""
        try:
            sandbox.begin()
        finally:
            ended()

    # ------------------------------------------------------------------------------------------------ the launch
    def _principal(self, lease: Lease | None) -> Principal:
        """The user whose credential mints the data-path token: the leased one, else the configured fixed one."""
        if lease is not None:
            if lease.key not in self._s.users:
                raise ValueError("the lease key is not a login of [sandbox.users]")
            return Principal(lease.key, self._s.users[lease.key])
        fixed = self._config.principals.data_path
        if fixed is None:
            raise ValueError("a run with a data path needs a leased user, or [principals.data_path] for a fixed one")
        return fixed

    def _spec(self, run_key: str, mode: DataPathMode, token: MintedToken | None, lease: Lease | None) -> SandboxSpec:
        if mode is DataPathMode.NONE:
            return SandboxSpec(run_key=run_key, mode=mode, lease_key=lease.key if lease else None)
        return SandboxSpec(
            run_key=run_key,
            mode=mode,
            token=token,
            zone=Endpoint.from_url(self._config.zone.url),
            provider=Endpoint.from_url(self._config.local_provider.url),  # the agent is pinned to its local provider
            lease_key=lease.key if lease else None,
        )

    def _mint(self, mode: DataPathMode, lease: Lease | None, ceiling_s: float) -> MintedToken | None:
        """Blocking: mint the data-path token as the leased user. Nothing exists yet, so nothing needs undoing."""
        if mode is DataPathMode.NONE:
            return None
        spec = data_path_spec(self._config.space.id, mode.interface or "rest")
        return self._issuer.mint(spec, self._principal(lease), ceiling_s=ceiling_s)

    def _prepare(
        self, run_key: str, mode: DataPathMode, lease: Lease | None, token: MintedToken | None
    ) -> DockerSandbox:
        """The sandbox of a run, built and not started: it has no effect on the host, so a launch holds it, and can stop
        it, from before the first resource exists."""
        sandbox = DockerSandbox(
            self._host,
            self._s,
            self._spec(run_key, mode, token, lease),
            registry=self._registry,
            config_sha256=self._config.sha256,
            **self._options,
        )
        if token is not None:  # never the value, and the user by its place in the file, not by its id (O12)
            user = list(self._s.users).index(lease.key) if lease is not None else "principals.data_path"
            sandbox.record(data_path_token={"user": user, "expires_at": token.expires_at})
        return sandbox

    @contextlib.asynccontextmanager
    async def launch(
        self, ctx: CallContext, *, run_key: str, level: str, ceiling_s: float, lease: Lease | None = None
    ) -> AsyncIterator[DockerSandbox]:
        """Start the sandbox of a run and yield it; stop it, verified, on leaving.

        The start and the stop are each inside `ctx.infrastructure_wait("sandbox_lifecycle")`; the body is the agent's
        own time. A teardown that leaves something behind raises `InfraError("sandbox")` when nothing else is in
        flight, and the lease's probe then keeps the key quarantined. `ceiling_s` is the run's whole-time ceiling (O06).
        When the data gate refused an answer for its own integrity during the run, the run ends in `InfraError` of
        kind `data_gate_integrity` (`kinds.gate_error`), whatever its body raised or returned (O05, C01).

        The start runs in a thread, and cancelling the launch does not stop a thread: it goes on creating the sandbox.
        So the sandbox is built before the thread begins, and the launch holds it. On cancellation, or any failure of
        the start, the launch asks the thread to stop creating things, waits for it with a deadline however often it is
        cancelled meanwhile, and then tears down everything the sandbox registered; what a thread that outlives the
        deadline creates afterwards carries the lease labels from the moment it exists, and the thread removes it
        itself on its way out, or the probe and the orphan reaper find it (O15, C19).
        """
        mode = self._s.mode_of(level)
        sandbox: DockerSandbox | None = None
        started: asyncio.Future[None] | None = None
        try:
            async with ctx.infrastructure_wait(WAIT_KIND):
                token = await asyncio.to_thread(self._mint, mode, lease, ceiling_s)
                sandbox = self._prepare(run_key, mode, lease, token)
                ended = self._threads.began(lease.key if lease is not None else None)
                started = asyncio.ensure_future(asyncio.to_thread(self._begin, sandbox, ended))
                started.add_done_callback(lambda _: ended())  # a task that never got to run has no thread to end
                await asyncio.shield(started)
        except BaseException:
            if sandbox is not None and started is not None:
                await self._settle_start(sandbox, started)
                await self._stop(ctx, sandbox, quiet=True)
            raise
        try:
            yield sandbox
        except Exception:
            refused = kinds.gate_error(await asyncio.to_thread(sandbox.gate_refusals))
            await self._stop(ctx, sandbox, quiet=True)
            if refused is not None:  # the gate cut the condition's read: that, not the body's failure, ends the run
                raise refused from None
            raise
        except BaseException:
            await self._stop(ctx, sandbox, quiet=True)
            raise
        refused = kinds.gate_error(await asyncio.to_thread(sandbox.gate_refusals))
        if refused is not None:  # a refusal no cell reported (a read the agent's code left running): the same end
            await self._stop(ctx, sandbox, quiet=True)
            raise refused
        await self._stop(ctx, sandbox, quiet=False)

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


__all__ = ["DATA_PATH_FACTOR", "DATA_USERS", "SandboxPlatform", "SandboxProbe"]
