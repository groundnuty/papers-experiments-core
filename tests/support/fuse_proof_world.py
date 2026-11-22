"""The live world of the O97 proof: the platform's real objects over a rendered federation, built once per process.

The registered attempt runs through `SandboxPlatform.launch` inside the campaign's attempt (the minimal study's
executor), with the `FaultInjector` started by `FaultInjector.running` in the same attempt: the attempt's liveness
token, the injector's hold and the sandbox's `held_reader` are the campaign's own, as production keys them. The
control and the unregistered sandboxes (K and N) are started directly, with a token of their own, as the Docker live
test starts its sandboxes: outside any attempt, so no run's admission is held for them and an outage's exclusive
admission never waits on them. Nothing here is a fake; the dry run's world is `fuse_proof_fake.FakeWorld`.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mount_live import SpaceFiles

from expcore.plugin import NO_WAITS
from expcore_onedata import fault_relay
from expcore_onedata import linkrecord as LR
from expcore_onedata.admission import fault_settings_of, run_admission
from expcore_onedata.client import MappedTransport, OnezoneClient, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.fault_serves import provider_serves
from expcore_onedata.fault_settings import hpc_driver
from expcore_onedata.faults import FaultInjector
from expcore_onedata.health import PlatformHealth
from expcore_onedata.placement import held_bytes, provider_blocks
from expcore_onedata.sandbox.api import DataPathMode, SandboxSpec
from expcore_onedata.sandbox.apptainer.hostmount import host_mount_factory
from expcore_onedata.sandbox.apptainer.node import LocalNode
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.tokens import TokenIssuer

log = logging.getLogger("fuse_proof")
TOKEN_CEILING_S = 7200.0  # the direct sandboxes' tokens: the whole step, with room


def mount_level(config: Any) -> str:
    """The first level of `[sandbox.data_paths]` that is a mount."""
    for level, mode in config.sandbox.data_paths.items():
        if mode is DataPathMode.MOUNT:
            return level
    raise SystemExit("[sandbox.data_paths] maps no level to a mount: the proof needs one")


def pick_space(config: Any) -> tuple[Any, str]:
    """A space whose data provider is not the agent's local one for that space, and that provider's label: the target
    the outage takes down. The first such space, in the file's order. Each entry's own support set names its local
    provider: `config.support` is the one-space form's alone, and a file of several spaces refuses it."""
    holders = config.space_holders()
    for entry in config.spaces:
        label = holders.get(entry.space.name)
        if label is not None and label != config.provider_label(entry.support.local):
            return entry, label
    raise SystemExit("no space has a data provider other than the agent's local one: nothing to take down")


def space_files(config: Any) -> SpaceFiles:
    """The platform's REST file calls, dialling each declared name at its mapped address when the file has `[addresses]`
    (O73, as the platform's own client does): an HPC federation's names resolve nowhere else."""
    if config.addresses is None:
        return SpaceFiles(config)
    return SpaceFiles(config, transport=MappedTransport(config.addresses, verify=config.tls.client_verify()))


def direct_spec(platform: SandboxPlatform, run_key: str, level: str, ceiling_s: float) -> SandboxSpec:
    """The spec of a sandbox started outside any attempt: the platform's own token and spec for `level`, with no
    held-down set (no injection of this run explains a block in it)."""
    tokens = platform._mint(DataPathMode.MOUNT, None, ceiling_s, None)
    spec = platform._spec(run_key, level, tokens.data_path if tokens is not None else None, None)
    return dataclasses.replace(spec, held_down=lambda: frozenset())


def make_work_root(path: Path) -> None:
    """Make the sandboxes' work root on this node, 0700, before any sandbox starts (the sandbox refuses a start without
    it), as the HPC worker does before its dispatcher (`deploy/hpc-federation/shards.py`, `make_work_root`); an existing
    one is left for the sandbox's own check (`layout.check_work_root`: this user's directory, no link, 0700)."""
    try:
        os.mkdir(path, 0o700)
        os.chmod(path, 0o700)
    except FileExistsError:
        pass


class LiveWorld:
    """The platform's objects over the federation whose platform file is `platform_file`."""

    def __init__(self, platform_file: Path) -> None:
        self.config = config = load_platform(platform_file)
        self.settings = fault_settings_of(config)
        if self.settings is None or self.settings.backend != "hpc" or self.settings.sanctioned is not True:
            raise SystemExit("the platform file has no sanctioned [faults] of the hpc backend")
        self.client = OnezoneClient(config)
        self.issuer = TokenIssuer(self.client, config)
        probe = provider_serves(config, self.settings, self.client)
        transport = fault_relay.RelayClient(
            LR.federation_root(self.settings.journal), wait_s=self.settings.relay_wait_s
        )
        self.driver = hpc_driver(self.settings, probe=probe, transport=transport)
        self.node = LocalNode()
        make_work_root(Path(config.sandbox.work_root))  # the worker's first step on a node: the H3 try 1 lesson
        self.level = mount_level(config)
        self.entry, self.target = pick_space(config)
        self.local_name = self.entry.support.local  # the picked space's own local provider
        self.space_id, self.space_name = self.entry.space.id, self.entry.space.name
        self.mount_path = str(config.sandbox.mount.path)
        self.owner_path: str | None = None
        self.platform = SandboxPlatform(
            config,
            self.issuer,
            self.node,
            events=lambda kind, detail: log.info("platform event %s", kind),
            sandbox_options={"mount_helper": host_mount_factory()},
        )

    # --- the campaign's path (the minimal study's executor)
    def launch(self, run_key: str, ceiling_s: float) -> Any:
        """`SandboxPlatform.launch` of a mount run, inside the attempt: the sandbox's `held_reader` is the attempt's."""
        return self.platform.launch(NO_WAITS, run_key=run_key, level=self.level, ceiling_s=ceiling_s)

    def injector(self, target: str) -> FaultInjector:
        return FaultInjector(self.config, self.settings, target, sanctioned=True, driver=self.driver)

    def health(self) -> Callable[[], Any]:
        return PlatformHealth(self.config, self.client, self.issuer, fault_driver=self.driver)

    def admission(self, schema: Any) -> Any:
        return run_admission(self.config, schema)

    def fingerprint(self) -> Mapping[str, Any]:
        return self.config.fingerprint()

    # --- outside the campaign: the control (K) and the unregistered sandbox (N)
    def direct(self, run_key: str) -> ApptainerSandbox:
        """A mount sandbox started directly, outside any attempt, with no injection registered for it (`direct_spec`).
        A launch is refused outside an attempt on a platform with `[faults]`, so the platform's own token and spec are
        used, and only its held-down set is emptied: a block in it is never the agent's."""
        return ApptainerSandbox.start(
            self.node, self.config.sandbox, direct_spec(self.platform, run_key, self.level, TOKEN_CEILING_S),
            mount_helper=host_mount_factory(),
        )  # fmt: skip

    def files(self) -> SpaceFiles:
        return space_files(self.config)

    def local_held(self, file_id: str) -> int | None:
        """How many bytes of a file the agent's local provider holds, by its own block map (`/distribution` read
        through it, as the link check reads one); None when that cannot be told: unknown is never zero."""
        try:
            answer = self.client.get(f"/data/{file_id}/distribution", provider=self.local_name)
            return held_bytes(provider_blocks(answer, self.config.providers[self.local_name].id or ""))
        except (PlatformError, ValueError):
            return None

    def provider_of(self, label: str) -> str:
        """A provider's configured name from its label, for the platform's own REST calls; never recorded."""
        return next(name for name in self.config.providers if self.config.provider_label(name) == label)

    def mounts_left(self) -> int:
        """FUSE mounts left under the sandboxes' work root, from this process's own mount table."""
        from expcore_onedata.sandbox.hostops import fuse_mounts

        root = str(self.config.sandbox.work_root).rstrip("/") + "/"
        table = Path("/proc/self/mountinfo").read_text(encoding="utf-8", errors="replace")
        return sum(1 for m in fuse_mounts(table) if m["mountpoint"].startswith(root))

    def close(self) -> None:
        self.client.close()


__all__ = ["LiveWorld", "direct_spec", "make_work_root", "mount_level", "pick_space", "space_files"]
