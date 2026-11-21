"""The live world of the O97 proof: the platform's real objects over a rendered federation, built once per process.

The registered attempt runs through `SandboxPlatform.launch` inside the campaign's attempt (the minimal study's
executor), with the `FaultInjector` started by `FaultInjector.running` in the same attempt: the attempt's liveness
token, the injector's hold and the sandbox's `held_reader` are the campaign's own, as production keys them. The
control and the unregistered sandboxes (K and N) are started directly, with a token of their own, as the Docker live
test starts its sandboxes: outside any attempt, so no run's admission is held for them and an outage's exclusive
admission never waits on them. Nothing here is a fake; the dry run's world is `fuse_proof_fake.FakeWorld`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mount_live import SpaceFiles

from expcore.plugin import NO_WAITS
from expcore_onedata import fault_relay
from expcore_onedata import linkrecord as LR
from expcore_onedata.admission import fault_settings_of, run_admission
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_serves import provider_serves
from expcore_onedata.fault_settings import hpc_driver
from expcore_onedata.faults import FaultInjector
from expcore_onedata.health import PlatformHealth
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxSpec
from expcore_onedata.sandbox.apptainer.hostmount import host_mount_factory
from expcore_onedata.sandbox.apptainer.node import LocalNode
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.provider import SandboxPlatform
from expcore_onedata.tokens import TokenIssuer, data_path_spec

log = logging.getLogger("fuse_proof")
TOKEN_CEILING_S = 7200.0  # the direct sandboxes' tokens: the whole step, with room


def mount_level(config: Any) -> str:
    """The first level of `[sandbox.data_paths]` that is a mount."""
    for level, mode in config.sandbox.data_paths.items():
        if mode is DataPathMode.MOUNT:
            return level
    raise SystemExit("[sandbox.data_paths] maps no level to a mount: the proof needs one")


def pick_space(config: Any) -> tuple[Any, str]:
    """A space whose data provider is not the agent's local one, and that provider's label: the target the outage takes
    down. The first such space, in the file's order."""
    holders = config.space_holders()
    local = config.provider_label(config.support.local)
    for entry in config.spaces:
        label = holders.get(entry.space.name)
        if label is not None and label != local:
            return entry, label
    raise SystemExit("no space has a data provider other than the agent's local one: nothing to take down")


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
        self.level = mount_level(config)
        self.entry, self.target = pick_space(config)
        self.local_name = config.support.local
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
        """A mount sandbox started directly, with a data-path token of its own and no injection registered for it."""
        config = self.config
        token = self.issuer.mint(
            data_path_spec([e.space.id for e in config.spaces], "oneclient"),
            config.principals.data_path,
            ceiling_s=TOKEN_CEILING_S,
        )
        spec = SandboxSpec(
            run_key=run_key,
            mode=DataPathMode.MOUNT,
            token=token,
            zone=Endpoint.from_url(config.zone.url),
            provider=Endpoint.from_url(config.local_provider.url),
            space_holders=config.space_holders(),
        )
        return ApptainerSandbox.start(self.node, config.sandbox, spec, mount_helper=host_mount_factory())

    def files(self) -> SpaceFiles:
        return SpaceFiles(self.config)

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


__all__ = ["LiveWorld", "mount_level", "pick_space"]
