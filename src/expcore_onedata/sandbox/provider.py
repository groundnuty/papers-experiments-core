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
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from expcore.campaign.lease import Lease, process_gone
from expcore.errors import InfraError
from expcore.plugin import CallContext
from expcore_onedata.config import PlatformConfig
from expcore_onedata.credentials import Principal
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxError, SandboxSpec
from expcore_onedata.sandbox.hostops import HostOps, mounted_points
from expcore_onedata.sandbox.registry import DEFAULT, Registry
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.sandbox.settings import SandboxSettings
from expcore_onedata.tokens import MintedToken, TokenIssuer, data_path_spec

log = logging.getLogger(__name__)

WAIT_KIND = "sandbox_lifecycle"
DATA_PATH_FACTOR = "data_path"  # the study's factor whose levels [sandbox.data_paths] maps
DATA_USERS = "data_users"  # the lease group of the platform users a data path leases


class SandboxProbe:
    """The `ResourceProbe` of the sandbox's leases: it proves a key's containers, networks and mounts gone (C19).

    `resource_gone` answers exactly True only when docker lists no container and no network carrying the lease key's
    label, and the mount table shows no mount in a directory of the key. Anything it cannot read or ask is False, so the
    key stays quarantined. It reads and never removes: the sandbox's own stop, the next start's retry and the orphan
    reaper remove.
    """

    def __init__(
        self, host: HostOps, settings: SandboxSettings, *, holder_gone: Callable[[str], bool | None] = process_gone
    ) -> None:
        self._host, self._s, self._holder_gone = host, settings, holder_gone

    def holder_gone(self, owner: str) -> bool | None:
        """Whether the process that held the key is gone (the core's check of its boot, pid and start time)."""
        return self._holder_gone(owner)

    def resource_gone(self, key: str, note: Mapping[str, Any]) -> bool:
        """True only when nothing started with this key is left; False on anything else, a failed read included."""
        label = f"label={self._s.label('lease')}={key}"
        try:
            containers = self._host.docker("ps", "-a", "-q", "--filter", label, check=False, timeout=30)
            networks = self._host.docker("network", "ls", "-q", "--filter", label, check=False, timeout=30)
            if containers.returncode != 0 or networks.returncode != 0:
                return False
            if containers.stdout.strip() or networks.stdout.strip():
                return False
            mounts = mounted_points(self._host.read_text("/proc/self/mountinfo"))
        except (OSError, SandboxError):
            return False
        own = re.compile(re.escape(str(self._s.work_root)) + "/" + re.escape(key) + r"-[0-9a-f]{8}(/|$)")
        return not any(own.match(m) for m in mounts)


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
    ) -> None:
        if config.sandbox is None:
            raise ValueError(f"{config.file}: has no [sandbox] section")
        self._config, self._s, self._issuer, self._host = config, config.sandbox, issuer, host
        self._registry, self._options = registry, dict(sandbox_options or {})

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
        return SandboxProbe(self._host, self._s)

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

    def _start(self, run_key: str, mode: DataPathMode, lease: Lease | None, ceiling_s: float) -> DockerSandbox:
        """Blocking: mint the token as the leased user, then start the sandbox with it."""
        token = None
        if mode is not DataPathMode.NONE:
            spec = data_path_spec(self._config.space.id, mode.interface or "rest")
            token = self._issuer.mint(spec, self._principal(lease), ceiling_s=ceiling_s)
        sandbox = DockerSandbox.start(
            self._host, self._s, self._spec(run_key, mode, token, lease), registry=self._registry, **self._options
        )
        if token is not None:
            sandbox.record(
                data_path_token={"subject": token.subject, "expires_at": token.expires_at}
            )  # never the value
        return sandbox

    @contextlib.asynccontextmanager
    async def launch(
        self, ctx: CallContext, *, run_key: str, level: str, ceiling_s: float, lease: Lease | None = None
    ) -> AsyncIterator[DockerSandbox]:
        """Start the sandbox of a run and yield it; stop it, verified, on leaving.

        The start and the stop are each inside `ctx.infrastructure_wait("sandbox_lifecycle")`; the body is the agent's
        own time. A teardown that leaves something behind raises `InfraError("sandbox")` when nothing else is in
        flight, and the lease's probe then keeps the key quarantined. `ceiling_s` is the run's whole-time ceiling (O06).
        """
        mode = self._s.mode_of(level)
        async with ctx.infrastructure_wait(WAIT_KIND):
            sandbox = await asyncio.to_thread(self._start, run_key, mode, lease, ceiling_s)
        try:
            yield sandbox
        except BaseException:
            await self._stop(ctx, sandbox, quiet=True)
            raise
        await self._stop(ctx, sandbox, quiet=False)

    async def _stop(self, ctx: CallContext, sandbox: DockerSandbox, *, quiet: bool) -> None:
        async with ctx.infrastructure_wait(WAIT_KIND):
            try:
                await asyncio.to_thread(sandbox.stop)
            except SandboxError as error:
                if not quiet:
                    raise InfraError("sandbox", f"the sandbox could not be torn down: {error}"[:600]) from None
                log.error("the sandbox of an ended run could not be torn down: %s", error)


__all__ = ["DATA_PATH_FACTOR", "DATA_USERS", "SandboxPlatform", "SandboxProbe"]
