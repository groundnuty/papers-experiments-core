"""Helpers for the Apptainer sandbox's tests: settings with every file pinned, specs, and options for a fast start.

Every name, address and file here is invented; the pinned files are a few bytes each, written into the test's own
directory, so a pin is checked exactly as on a node.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fake_node import FakeNode
from sandbox_support import TOKEN, SleptClock

from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxSpec
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.apptainer.settings import ApptainerSettings, Pinned
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.sandbox.settings import ImageRef, Limits, MountSettings, SandboxSettings
from expcore_onedata.tls import TlsSettings
from expcore_onedata.tokens import Secret

ZONE = Endpoint("zone.invented.test", 8443, True)
PROVIDER = Endpoint("alpha.invented.test", 8443, True)
LEVELS = {"via-gate": DataPathMode.REST, "isolated": DataPathMode.NONE, "mounted": DataPathMode.MOUNT}
PLACEHOLDER = "placeholder"
LIMITS = Limits(cpus=1.0, memory_bytes=64 << 20, pids=64, nofile=64, tmp_bytes=1 << 20, shm_bytes=1 << 20,
                work_disk_bytes=4 << 20)  # fmt: skip


def pinned(directory: Path, name: str, content: bytes) -> Pinned:
    """A file written into the test's directory, pinned by its SHA-256."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return Pinned(path, hashlib.sha256(content).hexdigest())


def app_settings(files: Path, **changes: Any) -> ApptainerSettings:
    values: dict[str, Any] = {
        "image": pinned(files, "agent.sif", b"an invented SIF image\n"),
        "egress_shim": pinned(files, "egresslog.so", b"an invented egress shim\n"),
        "egress_collector": pinned(files, "egress-collector.py", b"# an invented egress collector\n"),
        "memory_poll_s": 0.02,
        "disk_poll_s": 0.1,
    }
    values.update(changes)
    return ApptainerSettings(**values)


def make_settings(tmp_path: Path, *, limits: Limits = LIMITS, **changes: Any) -> SandboxSettings:
    """The `[sandbox]` of the Apptainer backend, its work root an owner-only directory of the test's."""
    root = tmp_path / "root"
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    app = app_settings(tmp_path / "files", **changes)
    image = ImageRef(str(app.image.path), f"sha256:{app.image.sha256}")
    return SandboxSettings(
        work_root=root, name_prefix="sbx", label_namespace="org.expcore", agent_uid=0, python=app.python, sudo=(),
        agent=image, gate=image, oneclient=None, network=None, limits=limits,
        mount=MountSettings("/data", "proxy"), data_paths=dict(LEVELS), tls=TlsSettings(),
        placeholder_token=PLACEHOLDER, kill_grace_s=1.0, backend="apptainer", apptainer=app,
    )  # fmt: skip


def make_spec(level: str = "via-gate", *, token: str = TOKEN, lease_key: str | None = None,
              run_key: str = "run-key-1") -> SandboxSpec:  # fmt: skip
    mode = LEVELS[level]
    if mode is DataPathMode.NONE:
        return SandboxSpec(run_key=run_key, mode=mode, lease_key=lease_key)
    return SandboxSpec(run_key=run_key, mode=mode, token=Secret(token), zone=ZONE, provider=PROVIDER,
                       lease_key=lease_key)  # fmt: skip


def options(**more: Any) -> dict[str, Any]:
    """No real sleeping in the harness's own waits (a clock the sleeps move), and a registry of the test's own."""
    clock = SleptClock()
    return {"registry": Registry(), "sleep": clock.sleep, "clock": clock, **more}


def start(node: FakeNode, settings: SandboxSettings, spec: SandboxSpec | None = None, **more: Any) -> ApptainerSandbox:
    return ApptainerSandbox.start(node, settings, spec or make_spec(), **options(**more))


__all__ = ["LEVELS", "LIMITS", "PLACEHOLDER", "PROVIDER", "TOKEN", "ZONE", "make_settings", "make_spec", "options",
           "pinned", "start"]  # fmt: skip
