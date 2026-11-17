# Provenance: new
"""`[sandbox.apptainer]`: what the Apptainer backend runs on an HPC batch node, every file pinned by its SHA-256 (O56).

```toml
[sandbox]
backend = "apptainer"            # the Docker backend when absent
work_root = "/node/tmpfs/dir"    # an existing 0700 directory of the driver's uid, not a link, on the node's tmpfs
[sandbox.limits]                 # as for Docker; on this backend memory_bytes, tmp_bytes, shm_bytes and
                                 # work_disk_bytes are the harness's watch's bounds, pids and gate_memory_bytes its
                                 # polls', nofile a prlimit and cpus a taskset pin before each cell (limits.py, O70)
[sandbox.data_paths]             # as for Docker; a mount level needs oneclient_bundle below

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
hidden_memory_margin_bytes = 134217728   # optional, 1 MiB to 64 GiB: anonymous memory nothing visible explains that
                                 # a cell may add before it is taken as past its limit (hidden.py)
work_disk = "directory"          # optional (O71): "directory", /work a directory the watch bounds, or "image", /work an
                                 # ext3 image of work_disk_bytes, a write past it refused with ENOSPC (workdisk.py)
work_disk_mkfs = ["mkfs.ext3"]   # optional: the node's mkfs.ext3 (e2fsprogs 1.43 or later, for -d), for "image": ONE
                                 # program word, a bare name or an absolute path (never ./x, ../x, a/b), never an
                                 # option (it is no experiment); the image is ext3 whatever it is called (-t ext3)
preload =[{ path = "x.so", sha256 = "<64 hex>" }]   # optional (hook b): libraries the cell preloads BEFORE the egress
                                 # shim, so a connect hook's rewrite reaches the shim through RTLD_NEXT
binds = [{ path = "f", sha256 = "<64 hex>", target = "/etc/x" }]   # optional (hook b): files bound read-only
oneclient_bundle = "oneclient-bundle.tar"  # the host mount's oneclient (hostmount), required by a mount level: the tar
oneclient_bundle_sha256 = "<64 hex>"      # deploy/hpc-federation/oneclient_bundle.py builds from the sidecar's image
provider_storage = "tmpfs"       # optional: where the federation's providers keep their data, "tmpfs" or
                                 # "shared_filesystem" (the cluster's scratch): the driver renders it (O57)
[sandbox.apptainer.gate_options] # optional (hook a): further keys of the gate's configuration, never one it sets
```

Every file is copied into the run's directory at the start, its copy hashed and compared with the pin, and only the
copy is used; the SIF, too large to copy, is hashed once per identity (device, inode, size, times) and its identity
checked again before every exec (`sandbox.py`); oneclient's bundle is unpacked for each mount run from the bytes that
are hashed (`oneclient_bundle.unpack`). A path is a configured value: a message names the key, never the path, and a
record holds the hashes alone (`record`), the bundle's only when it is declared, so a platform without one keeps its
fingerprint while two bundles are two experiments.

The storage kind (`provider_storage`) is one word the HPC driver renders from `site.env` (`HPC_STORAGE_ROOT` set:
`shared_filesystem`, else `tmpfs`), never the root's path: where a provider stores its data shapes a run's I/O
timing, so shards on tmpfs and on scratch are two experiments. It is in the record (so in the campaign
fingerprint, `sandbox_backend`, and in every run's `backend`) only when declared: a Compose federation, which
has no such table, and a platform written before the key keep the record they had.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from expcore.config.loading import ConfigError, Section
from expcore_onedata.sandbox.apptainer.hidden import DEFAULT_MARGIN_BYTES

SHA256 = re.compile(r"[0-9a-f]{64}")
COMMAND_WORD = re.compile(r"[A-Za-z0-9._/-]{1,128}")
# a command word that is no option: none starts with "-"
MKFS_PROGRAM = re.compile(r"[A-Za-z0-9._/][A-Za-z0-9._/-]{0,127}")
DEFAULT_MKFS = ("mkfs.ext3",)
MKFS_ONE_PROGRAM = (
    "must be exactly one program word: a bare name (no '/') or an absolute path in normal form (no empty, '.' or '..' "
    "part), never an option, a relative path or a second word"
)
PROVIDER_STORAGE_KINDS = ("tmpfs", "shared_filesystem")  # where the providers keep their data (O57)
CELL_PATH = re.compile(r"/[A-Za-z0-9._+/-]{1,1023}")
# the targets the sandbox binds itself, and those under which it binds: an extra bind may be none of them
OWN_TARGETS = ("/work", "/tmp", "/dev/shm", "/etc/hosts", "/etc/resolv.conf", "/etc/nsswitch.conf",
               "/etc/ld.so.preload", "/etc/sandbox", "/etc/expcore", "/hpcop-egress", "/hpcop-shim",
               "/expcore-preload")  # fmt: skip
MARGIN_BOUNDS = (1 << 20, 64 << 30)  # hidden_memory_margin_bytes, in bytes
WORK_DISKS = ("directory", "image")  # work_disk: the first is the default (O71)
# the keys of the gate's configuration the sandbox writes itself (`gaterun.config`): gate_options may set none of them
GATE_OWN_KEYS = frozenset(
    {"run_key", "verify_ssl", "log", "tls_dir", "hosts", "listing", "token_file", "token", "upstream_ca_file",
     "listen_host", "listen_port", "tcp_forwards", "scan_names"}
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
    hidden_memory_margin_bytes: int = DEFAULT_MARGIN_BYTES
    oneclient_bundle: Pinned | None = None  # the host mount's oneclient (`hostmount`), a tar pinned by its SHA-256
    provider_storage: str | None = None  # one of PROVIDER_STORAGE_KINDS, or None: the platform does not say
    work_disk: str = WORK_DISKS[0]  # O71: "directory" (the watch alone) or "image" (`workdisk.py`)
    work_disk_mkfs: tuple[str, ...] = DEFAULT_MKFS

    def __post_init__(self) -> None:
        low, high = MARGIN_BOUNDS
        margin = self.hidden_memory_margin_bytes
        if isinstance(margin, bool) or not isinstance(margin, int) or not low <= margin <= high:
            raise ValueError(f"hidden_memory_margin_bytes must be a whole number of bytes from {low} to {high}")
        if self.provider_storage is not None and self.provider_storage not in PROVIDER_STORAGE_KINDS:
            raise ValueError(f"provider_storage must be one of {', '.join(PROVIDER_STORAGE_KINDS)}")
        if self.work_disk not in WORK_DISKS:
            raise ValueError(f"work_disk must be one of {', '.join(WORK_DISKS)}")
        if not _is_mkfs_program(self.work_disk_mkfs):
            raise ValueError(f"work_disk_mkfs {MKFS_ONE_PROGRAM}")
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
            "gate_options": hashlib.sha256(
                json.dumps(dict(self.gate_options), sort_keys=True, default=str).encode()
            ).hexdigest(),  # by value, never shown: a changed option is another experiment
            # a margin set away from its default only, so a fingerprint made before the key existed is unchanged
            **({} if self.hidden_memory_margin_bytes == DEFAULT_MARGIN_BYTES else
               {"hidden_memory_margin_bytes": self.hidden_memory_margin_bytes}),
            # another oneclient is another experiment; a platform that declares none keeps the record it had
            **({"oneclient_bundle_sha256": self.oneclient_bundle.sha256} if self.oneclient_bundle is not None else {}),
            # the storage kind of the federation's providers, when declared (O57): tmpfs and scratch are two experiments
            **({"provider_storage": self.provider_storage} if self.provider_storage is not None else {}),
            # the image is another experiment; the directory, the default, keeps the record it had (O71); the tool
            # that makes the image is none
            **({} if self.work_disk == WORK_DISKS[0] else {"work_disk": self.work_disk}),
        }  # fmt: skip


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


def _margin(section: Section) -> int:
    value, (low, high) = section.data.get("hidden_memory_margin_bytes", DEFAULT_MARGIN_BYTES), MARGIN_BOUNDS
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise section.fail("hidden_memory_margin_bytes", f"must be a whole number of bytes from {low} to {high}")
    return value


def _is_mkfs_program(words: Sequence[str]) -> bool:
    """Whether `work_disk_mkfs` is the tool alone: exactly one word, a program's bare name or an absolute path, that is
    no option. The file system's type and options shape the image, so no word may carry them (C06: the setting is out
    of the fingerprint because it can name only where the node's mkfs is; the image's type is passed by `workdisk.make`
    itself). A path relative to wherever the driver runs (`./x`, `../x`, `a/b`) names no tool a node can be said to
    have, and an absolute path with an empty, `.` or `..` part (`/`, `/a/`, `/a/../b`) is not in normal form."""
    if len(words) != 1 or not isinstance(words[0], str) or MKFS_PROGRAM.fullmatch(words[0]) is None:
        return False
    word = words[0]
    if not word.startswith("/"):
        return "/" not in word and word not in (".", "..")
    return all(part not in ("", ".", "..") for part in word[1:].split("/"))


def _work_disk(section: Section) -> tuple[str, tuple[str, ...]]:
    mode = section.data.get("work_disk", WORK_DISKS[0])
    if mode not in WORK_DISKS:
        raise section.fail("work_disk", f"must be one of {', '.join(WORK_DISKS)}")
    if "work_disk_mkfs" in section.data and not isinstance(section.data["work_disk_mkfs"], list):
        raise section.fail("work_disk_mkfs", "must be an array holding one program word")
    words = section.strings("work_disk_mkfs") if "work_disk_mkfs" in section.data else DEFAULT_MKFS
    if not _is_mkfs_program(words):
        raise section.fail("work_disk_mkfs", MKFS_ONE_PROGRAM)
    return mode, words


def _gate_options(section: Section) -> Mapping[str, Any]:
    options = section.table("gate_options")
    clash = sorted(set(options) & GATE_OWN_KEYS)
    if clash:
        raise section.fail("gate_options", f"may not set {clash[0]!r}: the sandbox writes that key itself")
    return MappingProxyType(options)


def _provider_storage(section: Section) -> str | None:
    """`provider_storage`: one of the kinds, or absent; the message names the key and the choices, never the value."""
    value = section.optional_string("provider_storage")
    if value is not None and value not in PROVIDER_STORAGE_KINDS:
        raise section.fail("provider_storage", f"must be one of {', '.join(PROVIDER_STORAGE_KINDS)}")
    return value


def load_apptainer(section: Section, base: Path) -> ApptainerSettings:
    """Validate `[sandbox.apptainer]`; raise ConfigError naming the file and key, never a value or a path."""
    section.reject_unknown(
        ("apptainer", "image", "image_sha256", "python", "host_python", "egress_shim", "egress_shim_sha256",
         "egress_collector", "egress_collector_sha256", "memory_poll_s", "disk_poll_s", "preload", "binds",
         "gate_options", "hidden_memory_margin_bytes", "oneclient_bundle", "oneclient_bundle_sha256",
         "provider_storage", "work_disk", "work_disk_mkfs")
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
    declared = "oneclient_bundle" in section.data or "oneclient_bundle_sha256" in section.data
    bundle = _pinned(section, base, "oneclient_bundle") if declared else None
    work_disk, mkfs = _work_disk(section)
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
            hidden_memory_margin_bytes=_margin(section),
            oneclient_bundle=bundle,
            provider_storage=_provider_storage(section),
            work_disk=work_disk,
            work_disk_mkfs=mkfs,
        )
    except ValueError as error:  # the dataclass's own checks, which the loader already made: never a value
        raise ConfigError(f"{section.file}: {section.where}: {error}") from None


__all__ = ["PROVIDER_STORAGE_KINDS", "ApptainerSettings", "Pinned", "PinnedBind", "load_apptainer"]
