"""Helpers for the Apptainer sandbox's tests: settings with every file pinned, specs, and options for a fast start.

Every name, address and file here is invented; the pinned files are a few bytes each, written into the test's own
directory, so a pin is checked exactly as on a node.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fake_elf import bundle_members, write_tar
from fake_node import FakeNode
from sandbox_support import TOKEN, SleptClock

from expcore_onedata.readlog import paths as readlog_paths
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxSpec
from expcore_onedata.sandbox.apptainer import readlogrun
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
DEFAULT_RUN_KEY = "run-key-1"
# What `short_dir` allows a directory: a Unix socket's path is at most 107 bytes, and the collector's is
# `<dir>/root` + `/<run id, at most 40 bytes here>` + `/readlog-sock/sock` (18 bytes): 107 - 5 - 41 - 18.
SHORT_DIR_BYTES = 43
LIMITS = Limits(cpus=1.0, memory_bytes=64 << 20, pids=64, nofile=64, tmp_bytes=1 << 20, shm_bytes=1 << 20,
                work_disk_bytes=4 << 20)  # fmt: skip


def pinned(directory: Path, name: str, content: bytes) -> Pinned:
    """A file written into the test's directory, pinned by its SHA-256."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(content)
    return Pinned(path, hashlib.sha256(content).hexdigest())


def pinned_bundle(directory: Path) -> Pinned:
    """oneclient's bundle as the build writes it (an invented tar: fake_elf), pinned by its SHA-256."""
    path = directory / "oneclient-bundle.tar"
    return Pinned(path, write_tar(path, bundle_members()))


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


def short_dir(prefix: str = "rl") -> Path:
    """A private directory of the test's whose path leaves room for a run's collector socket in a Unix socket's address.

    The socket is `<dir>/root/<run id>/readlog-sock/sock` (`make_settings` puts the work root at `<dir>/root`), at most
    107 bytes. A pytest path is too long on a batch node, where the test runner's TMPDIR is a per-run directory, so the
    default temporary directory is used when the path fits, else `/tmp`, else `/dev/shm`. When none fits the test fails
    (never skips: a skipped test on the nodes would be a false pass)."""
    refused: list[str] = []
    for base in (tempfile.gettempdir(), "/tmp", "/dev/shm"):
        try:
            made = Path(tempfile.mkdtemp(prefix=prefix, dir=base))
        except OSError as error:
            refused.append(f"{base}: {error.strerror}")
            continue
        if len(os.fsencode(made)) <= SHORT_DIR_BYTES:
            return made
        shutil.rmtree(made)
        refused.append(f"{base}: the path is too long")
    raise RuntimeError(f"no temporary directory is short enough for a collector's socket ({'; '.join(refused)})")


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


READLOG_SOCKET = f"{readlogrun.SOCK_DIR}/{readlog_paths.SOCKET_NAME}"  # in the run's directory
EGRESS_SOCKET = "egress-sock/sock"  # in the run's directory


def settings_with_socket_bytes(
    base: Path, size: int, *, socket: str = READLOG_SOCKET, **changes: Any
) -> SandboxSettings:
    """`make_settings` under a directory of `base` named so that a collector's socket path (`socket`, in the run's
    directory), for a run of the default spec (`make_spec`), is exactly `size` bytes:
    `<base>/<fill>/root/<run id>/<socket>`."""
    tail = len(f"/root/{DEFAULT_RUN_KEY}-{'0' * 8}/{socket}")
    fill = size - tail - len(os.fsencode(base)) - 1
    if fill < 1:
        raise ValueError(f"{base} leaves no room for a socket path of {size} bytes")
    directory = base / ("p" * fill)
    directory.mkdir()
    return make_settings(directory, **changes)


def make_spec(level: str = "via-gate", *, token: str = TOKEN, lease_key: str | None = None,
              run_key: str = DEFAULT_RUN_KEY) -> SandboxSpec:  # fmt: skip
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


__all__ = ["DEFAULT_RUN_KEY", "EGRESS_SOCKET", "LEVELS", "LIMITS", "PLACEHOLDER", "PROVIDER", "READLOG_SOCKET", "TOKEN",
           "ZONE", "make_settings", "make_spec", "options", "pinned", "pinned_bundle", "settings_with_socket_bytes",
           "short_dir", "start"]  # fmt: skip
