# Provenance: new
"""`[sandbox.apptainer]`: what the Apptainer backend runs on an HPC batch node, every file pinned by its SHA-256 (O56).

```toml
[sandbox]
backend = "apptainer"            # the Docker backend when absent
work_root = "/node/tmpfs/dir"    # an existing 0700 directory of the driver's uid, not a link, on the node's tmpfs
[sandbox.limits]                 # as for Docker; on this backend memory_bytes, tmp_bytes, shm_bytes and
                                 # work_disk_bytes are the harness's watch's bounds, and cpus, pids, nofile and
                                 # gate_memory_bytes are recorded, not enforced (facts.py)
[sandbox.data_paths]             # as for Docker; a mount level is refused at start until the mount lands

[sandbox.apptainer]
apptainer = ["apptainer"]        # optional: the command words
image = "agent.sif"              # the agent's SIF (relative to the platform file, or absolute)
image_sha256 = "<64 hex>"        # its pinned SHA-256: a file with another refuses the start (O13)
python = "python3"               # optional: the interpreter in the image that runs a cell
host_python = "python3.11"       # optional: the node's interpreter for the gate and the collector (default: the
                                 # driver's own)
egress_shim = "egresslog.so"     # the bundle's egress shim (egresslog.c), built for this image
egress_shim_sha256 = "<64 hex>"
egress_collector = "egress-collector.py"   # the bundle's collector
egress_collector_sha256 = "<64 hex>"
memory_poll_s = 0.1              # optional, 0.02 to 1: the memory watch's period
disk_poll_s = 1.0                # optional, 0.1 to 10: the period of its walk of /work, /tmp and /dev/shm
preload = [{ path = "x.so", sha256 = "<64 hex>" }]   # optional (hook b): libraries the cell preloads BEFORE the egress
                                 # shim, so a connect hook's rewrite reaches the shim through RTLD_NEXT
binds = [{ path = "f", sha256 = "<64 hex>", target = "/etc/x" }]   # optional (hook b): files bound read-only
[sandbox.apptainer.gate_options] # optional (hook a): further keys of the gate's configuration, never one it sets
```

Every file is copied into the run's directory at the start, its copy hashed and compared with the pin, and only the
copy is used; the SIF, too large to copy, is hashed once per identity (device, inode, size, times) and its identity
checked again before every exec (`sandbox.py`). A path is a configured value: a message names the key, never the path,
and a record holds the hashes alone (`record`).
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from expcore.config.loading import ConfigError, Section

SHA256 = re.compile(r"[0-9a-f]{64}")
COMMAND_WORD = re.compile(r"[A-Za-z0-9._/-]{1,128}")
CELL_PATH = re.compile(r"/[A-Za-z0-9._+/-]{1,1023}")
# the targets the sandbox binds itself, and those under which it binds: an extra bind may be none of them
OWN_TARGETS = ("/work", "/tmp", "/dev/shm", "/etc/hosts", "/etc/ld.so.preload", "/etc/sandbox", "/etc/expcore",
               "/hpcop-egress", "/hpcop-shim", "/expcore-preload")  # fmt: skip
# the keys of the gate's configuration the sandbox writes itself (`gaterun.config`): gate_options may set none of them
GATE_OWN_KEYS = frozenset(
    {"run_key", "verify_ssl", "log", "tls_dir", "hosts", "listing", "token_file", "token", "upstream_ca_file",
     "listen_host", "listen_port", "tcp_forwards"}
)  # fmt: skip


@dataclass(frozen=True)
class Pinned:
    """A file by its path on the node and the SHA-256 it is pinned to."""

    path: Path
    sha256: str


@dataclass(frozen=True)
class PinnedBind:
    """A file bound read-only into every cell at `target` (hook b)."""

    file: Pinned
    target: str


@dataclass(frozen=True)
class ApptainerSettings:
    """`[sandbox.apptainer]`, validated."""

    image: Pinned
    egress_shim: Pinned
    egress_collector: Pinned
    apptainer: tuple[str, ...] = ("apptainer",)
    python: str = "python3"
    host_python: str | None = None  # None: the driver's own interpreter
    memory_poll_s: float = 0.1
    disk_poll_s: float = 1.0
    preload: tuple[Pinned, ...] = ()
    binds: tuple[PinnedBind, ...] = ()
    gate_options: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        clash = sorted(set(self.gate_options) & GATE_OWN_KEYS)
        if clash:
            raise ValueError(f"gate_options may not set {clash[0]!r}: the sandbox writes it itself")
        for bind in self.binds:
            if _own_target(bind.target):
                raise ValueError("a bind's target is one of the sandbox's own, or lies under one")

    def record(self) -> dict[str, Any]:
        """What the campaign fingerprint and the run's record hold: the backend and every pin, never a path (O12)."""
        return {
            "backend": "apptainer",
            "image_sha256": self.image.sha256,
            "egress_shim_sha256": self.egress_shim.sha256,
            "egress_collector_sha256": self.egress_collector.sha256,
            "memory_poll_s": self.memory_poll_s,
            "disk_poll_s": self.disk_poll_s,
            "preload_sha256": [p.sha256 for p in self.preload],
            "binds": [{"target": b.target, "sha256": b.file.sha256} for b in self.binds],
            "gate_options": sorted(self.gate_options),
        }


def _own_target(target: str) -> bool:
    return any(target == own or target.startswith(own + "/") for own in OWN_TARGETS) or target == "/"


def _pinned(section: Section, base: Path, key: str) -> Pinned:
    """A required file and its pin, `<key>` and `<key>_sha256`."""
    path = section.string(key)
    digest = section.string(f"{key}_sha256")
    if not SHA256.fullmatch(digest):
        raise section.fail(f"{key}_sha256", "must be 64 lower-case hexadecimal digits")
    return Pinned((base / path).resolve(), digest)


def _entry_pin(section: Section, key: str, position: int, entry: Any, base: Path, *, target: bool) -> PinnedBind:
    """One entry of `preload` or `binds`, named by its position, never by its path."""
    known = {"path", "sha256", *(["target"] if target else [])}
    if not isinstance(entry, Mapping) or set(entry) != known:
        raise section.fail(key, f"entry {position}: must be a table of exactly {', '.join(sorted(known))}")
    path, digest = entry["path"], entry["sha256"]
    if not isinstance(path, str) or not path.strip():
        raise section.fail(key, f"entry {position}: path must be a non-empty string")
    if not isinstance(digest, str) or not SHA256.fullmatch(digest):
        raise section.fail(key, f"entry {position}: sha256 must be 64 lower-case hexadecimal digits")
    where = entry.get("target", "/")
    if target and (not isinstance(where, str) or not CELL_PATH.fullmatch(where) or ".." in where.split("/")):
        raise section.fail(key, f"entry {position}: target must be an absolute path in the cell, without '..'")
    if target and _own_target(where):
        raise section.fail(key, f"entry {position}: target is one of the sandbox's own, or lies under one")
    return PinnedBind(Pinned((base / path).resolve(), digest), where)


def _period(section: Section, key: str, default: float, low: float, high: float) -> float:
    value = section.data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise section.fail(key, "must be a number of seconds")
    if not low <= value <= high:
        raise section.fail(key, f"must be from {low:g} to {high:g} seconds")
    return float(value)


def _gate_options(section: Section) -> Mapping[str, Any]:
    options = section.table("gate_options")
    clash = sorted(set(options) & GATE_OWN_KEYS)
    if clash:
        raise section.fail("gate_options", f"may not set {clash[0]!r}: the sandbox writes that key itself")
    return MappingProxyType(options)


def load_apptainer(section: Section, base: Path) -> ApptainerSettings:
    """Validate `[sandbox.apptainer]`; raise ConfigError naming the file and key, never a value or a path."""
    section.reject_unknown(
        ("apptainer", "image", "image_sha256", "python", "host_python", "egress_shim", "egress_shim_sha256",
         "egress_collector", "egress_collector_sha256", "memory_poll_s", "disk_poll_s", "preload", "binds",
         "gate_options")
    )  # fmt: skip
    words = section.strings("apptainer") or ("apptainer",)
    if any(not COMMAND_WORD.fullmatch(word) for word in words):
        raise section.fail("apptainer", "must hold plain command words")
    for key in ("python", "host_python"):
        value = section.optional_string(key)
        if value is not None and not COMMAND_WORD.fullmatch(value):
            raise section.fail(key, "must be a plain command word or path")
    lists = {}
    for key in ("preload", "binds"):
        value = section.data.get(key, [])
        if not isinstance(value, list):
            raise section.fail(key, "must be an array of tables")
        lists[key] = [_entry_pin(section, key, n, e, base, target=key == "binds") for n, e in enumerate(value)]
    targets = [b.target for b in lists["binds"]]
    if len(set(targets)) != len(targets):
        raise section.fail("binds", "names a target twice")
    try:
        return ApptainerSettings(
            image=_pinned(section, base, "image"),
            egress_shim=_pinned(section, base, "egress_shim"),
            egress_collector=_pinned(section, base, "egress_collector"),
            apptainer=tuple(words),
            python=section.optional_string("python") or "python3",
            host_python=section.optional_string("host_python"),
            memory_poll_s=_period(section, "memory_poll_s", 0.1, 0.02, 1.0),
            disk_poll_s=_period(section, "disk_poll_s", 1.0, 0.1, 10.0),
            preload=tuple(b.file for b in lists["preload"]),
            binds=tuple(lists["binds"]),
            gate_options=_gate_options(section),
        )
    except ValueError as error:  # the dataclass's own checks, which the loader already made: never a value
        raise ConfigError(f"{section.file}: {section.where}: {error}") from None


__all__ = ["ApptainerSettings", "Pinned", "PinnedBind", "load_apptainer"]
