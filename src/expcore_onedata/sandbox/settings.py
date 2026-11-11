# Provenance: new
"""The `[sandbox]` table of platform.toml: every image, network range, limit, mount option and host path (O12-O14).

Nothing here has a default for a thing a deployment names: an image, a pool of addresses, a host directory. Loading
validates everything and names the file and the key in a message, never the value. The address ranges must be networks
in strict mode (no host bits set), so a typo fails at load and not when the thirtieth sandbox asks for a subnet (O14).

```toml
[sandbox]
work_root = "/path"             # host directory for the per-sandbox directories and the teardown log
name_prefix = "sbx"             # prefix of container, network and directory names; letters, digits and '-'
label_namespace = "org.expcore" # docker labels are <namespace>.sandbox, .owner, .dir, .lease
agent_uid = 10001               # the unprivileged user the agent's code runs as
python = "python"               # the interpreter in the agent image that runs the agent's code
sudo = ["sudo", "-n"]           # the privilege command for host steps; [] when the driver runs as root
earlyoom_comm = "expcore-drv"   # optional: the process name the host's earlyoom is told to spare
trust_paths = []                # optional: paths in the agent image of CA bundles that must also trust the run's CA
content_check_file = "dir/f"    # a provisioned file under the space root, read once before a campaign to confirm
                                # the content type the gate streams on (`precampaign.py`); required with a rest level

[sandbox.images.agent]          # also the gate's image unless [sandbox.images.gate] names another
name = "image:tag"              # a tag or an id, only ever resolved to the id below
id = "sha256:..."               # the pinned id; a tag that now resolves elsewhere refuses the start (O13)
[sandbox.images.gate]           # optional
[sandbox.images.oneclient]      # required when a data path is a mount

[sandbox.network]
pools = ["10.0.0.0/13"]         # one or more IPv4 networks in strict mode, none overlapping another; each sandbox
                                # gets its own small network
prefix = 28                     # size of that network (a sandbox needs two addresses)
max_attempts = 64

[sandbox.limits]
cpus = 2.0
memory_bytes = 8589934592
pids = 512
nofile = 4096
tmp_bytes = 1073741824
shm_bytes = 1073741824
work_disk_bytes = 4294967296    # the quota of the agent's working directory
gate_memory_bytes = 1073741824  # optional

[sandbox.mount]                 # required when a data path is a mount
path = "/data"                  # where the agent sees the space, read-only
io_mode = "direct"              # optional: "direct" or "proxy"
extra_args = []                 # further oneclient arguments, never one that changes the TLS mode

[sandbox.data_paths]            # the levels of the study's data_path factor, each one of mount, rest, none
<level> = "rest"                # the kind alone; a rest level then pages its listings as the server does
<level> = { kind = "rest", listing = "complete" }  # or a table: a rest level's listing, "paged" or "complete"

[sandbox.served_bytes]          # optional: how much a run's tables of the bytes served list (gatelog.Caps, O22)
max_files = 1000                # files, the first read; max_directories = 1000; max_ranges_per_file = 64
```

`listing` is a REST data path's option (O21): `paged`, the default, passes the server's page of a listing as it comes,
and `complete` has the data gate answer every listing with all of the directory's entries (`gatepolicy`). It is refused
on a mount or no data path, where no gate serves a listing. A study selects it per condition through the level the
condition gives its `data_path` factor. The run's metadata records it (`listing`), and so does the campaign fingerprint
(`config.PlatformConfig.fingerprint`, from `data_path_record`), so two modes are never one experiment. A declared
`[sandbox.served_bytes]` is in the fingerprint too (`served_caps_declared`); an absent one leaves it as it was.

The certificate check of the data gate's upstream and of oneclient is not declared here either: it is the platform's one
`[tls]` setting (`tls.py`), which `load_platform` hands to `load_sandbox` and which every client of the federation
takes. A `tls` key in `[sandbox.mount]`, oneclient's own setting in earlier versions, is refused with a pointer to it.

The platform users a run leases are not declared here: a run that needs a data-path token or a tool server leases one
slot of `[slots.<name>]` (`users.py`), which gives it both planes' users, so there is one pool of leased users. The
table `[sandbox.users]` of earlier versions is refused at load, with a message that points to `[slots]`.
"""

import itertools
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType

from expcore.config.loading import ConfigError, Section
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.sandbox.gatelog import DEFAULT_CAPS, Caps
from expcore_onedata.sandbox.gatepolicy import LISTINGS, PAGED
from expcore_onedata.sandbox.hostops import strict_networks
from expcore_onedata.tls import TlsSettings, moved

PREFIX_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
NAMESPACE_RE = re.compile(r"[a-z][a-z0-9.-]{0,63}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
COMMAND_WORD = re.compile(r"[A-Za-z0-9._/-]{1,128}")
SPACE_PART = re.compile(r"[^/\x00-\x1f\\]{1,255}")  # one name of a path under the space root
IO_MODES = ("direct", "proxy")
FORBIDDEN_ARGS = {"-i", "--insecure"}  # TLS is set by the platform's [tls] only


@dataclass(frozen=True)
class ImageRef:
    """An image by the name a deployment gives it and by the id it was pinned to."""

    name: str
    id: str


@dataclass(frozen=True)
class Limits:
    """What one sandbox's containers may use; the same in every condition, so a limit never differs between arms."""

    cpus: float
    memory_bytes: int
    pids: int
    nofile: int
    tmp_bytes: int
    shm_bytes: int
    work_disk_bytes: int
    gate_memory_bytes: int = 1 << 30


@dataclass(frozen=True)
class NetworkSettings:
    """The pool the sandbox networks come from."""

    pools: tuple[str, ...]
    prefix: int = 28
    max_attempts: int = 64


@dataclass(frozen=True)
class MountSettings:
    """How the oneclient sidecar mounts the space and where the agent sees it; its TLS is `SandboxSettings.tls`."""

    path: str
    io_mode: str | None = None
    extra_args: tuple[str, ...] = ()


@dataclass(frozen=True)
class SandboxSettings:
    """Everything the sandbox needs from platform.toml."""

    work_root: Path
    name_prefix: str
    label_namespace: str
    agent_uid: int
    python: str
    sudo: tuple[str, ...]
    agent: ImageRef
    gate: ImageRef
    oneclient: ImageRef | None
    network: NetworkSettings
    limits: Limits
    mount: MountSettings | None
    data_paths: Mapping[str, DataPathMode]
    earlyoom_comm: str | None = None
    trust_paths: tuple[str, ...] = ()
    content_check_file: str | None = None  # the file the pre-campaign check reads (`precampaign.py`)
    tls: TlsSettings = field(default_factory=TlsSettings)  # the platform's [tls]: the gate's upstream and oneclient
    listings: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))  # rest level -> its listing
    served_caps: Caps = DEFAULT_CAPS  # [sandbox.served_bytes]: the bounds of a run's tables of the bytes served (O22)
    served_caps_declared: bool = False  # the table is in the file: its caps are in the campaign fingerprint (C06)

    def mode_of(self, level: str) -> DataPathMode:
        """The data path of a level of the study's `data_path` factor; `ValueError` for a level not mapped."""
        if level not in self.data_paths:
            raise ValueError("[sandbox.data_paths] does not map the level of this run's data_path factor")
        return self.data_paths[level]

    def listing_of(self, level: str) -> str | None:
        """The listing mode of a level (O21): `paged` or `complete` for a REST data path, None for any other."""
        if self.mode_of(level) is not DataPathMode.REST:
            return None
        return self.listings.get(level, PAGED)

    def data_path_record(self) -> dict[str, dict[str, str]]:
        """Every level's data path as the campaign fingerprint holds it: its kind and, for REST, its listing mode."""
        out: dict[str, dict[str, str]] = {}
        for level, mode in self.data_paths.items():
            listing = self.listing_of(level)
            out[level] = {"kind": mode.value} | ({"listing": listing} if listing is not None else {})
        return out

    def label(self, name: str) -> str:
        """A docker label key in this deployment's namespace."""
        return f"{self.label_namespace}.{name}"


def _number(
    section: Section, key: str, *, minimum: float, integer: bool = False, default: float | None = None
) -> float:
    """Read a finite number at or above a minimum; required unless a default is given."""
    if key not in section.data:
        if default is None:
            raise section.fail(key, "is required")
        return default
    value = section.data[key]
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or not math.isfinite(value) or value < minimum:
        raise section.fail(key, f"must be {'an integer' if integer else 'a number'} of at least {minimum:g}")
    return value


def _image(section: Section) -> ImageRef:
    """Read one image table: its name and the id it is pinned to."""
    section.reject_unknown(("name", "id"))
    name, pinned = section.string("name"), section.string("id")
    if not IMAGE_ID.fullmatch(pinned):
        raise section.fail("id", "must be an image id: 'sha256:' and 64 hexadecimal digits, not a tag")
    if not name or any(c.isspace() for c in name):
        raise section.fail("name", "must be an image name or id without spaces")
    return ImageRef(name, pinned)


def _images(section: Section) -> tuple[ImageRef, ImageRef, ImageRef | None]:
    """Read [sandbox.images.*]: the agent's, the gate's (the agent's when absent) and the sidecar's."""
    section.reject_unknown(("agent", "gate", "oneclient"))
    agent = _image(section.section("agent"))
    gate = _image(section.section("gate")) if "gate" in section.data else agent
    oneclient = _image(section.section("oneclient")) if "oneclient" in section.data else None
    return agent, gate, oneclient


def _network(section: Section) -> NetworkSettings:
    """Read [sandbox.network]; every pool must be a network in strict mode, and no two may overlap: a sandbox network
    drawn from one would then be drawn from the other too, and two sandboxes could get the same addresses. Checked at
    load, with the pools named by their position (O14)."""
    section.reject_unknown(("pools", "prefix", "max_attempts"))
    pools = section.strings("pools")
    if not pools or len(set(pools)) != len(pools):
        raise section.fail("pools", "must list at least one network, each once")
    prefix = int(_number(section, "prefix", minimum=8, integer=True, default=28))
    try:
        nets = strict_networks(pools)
    except ValueError as error:
        raise section.fail("pools", str(error)) from None
    for (i, a), (j, b) in itertools.combinations(enumerate(nets), 2):
        if a.overlaps(b):  # by position: a range is a configured value, never echoed
            raise section.fail("pools", f"entries {i} and {j} overlap: each address must belong to one pool only")
    if any(net.prefixlen > prefix for net in nets) or prefix > 30:
        raise section.fail("prefix", "must be at most 30 and no smaller than the prefix of every pool")
    attempts = int(_number(section, "max_attempts", minimum=1, integer=True, default=64))
    return NetworkSettings(pools, prefix, attempts)


def _limits(section: Section) -> Limits:
    """Read [sandbox.limits]: every limit is explicit, so no hidden default decides how arms differ."""
    keys = ("cpus", "memory_bytes", "pids", "nofile", "tmp_bytes", "shm_bytes", "work_disk_bytes", "gate_memory_bytes")
    section.reject_unknown(keys)
    values = {key: int(_number(section, key, minimum=1, integer=True)) for key in keys[1:7]}
    gate = int(_number(section, "gate_memory_bytes", minimum=1 << 20, integer=True, default=1 << 30))
    return Limits(float(_number(section, "cpus", minimum=0.1)), gate_memory_bytes=gate, **values)


def _served_caps(section: Section) -> Caps:
    """Read [sandbox.served_bytes] (O22): each cap a whole number of at least 1, the default where none is declared."""
    keys = tuple(f.name for f in fields(Caps))
    section.reject_unknown(keys)
    return Caps(
        **{k: int(_number(section, k, minimum=1, integer=True, default=getattr(DEFAULT_CAPS, k))) for k in keys}
    )


def _mount(section: Section) -> MountSettings:
    """Read [sandbox.mount]: the path in the agent and the oneclient options; TLS is the platform's [tls]."""
    if "tls" in section.data:
        raise moved(section, "tls")
    section.reject_unknown(("path", "io_mode", "extra_args"))
    path = section.string("path")
    if not path.startswith("/") or path == "/" or ".." in path.split("/") or any(c.isspace() for c in path):
        raise section.fail("path", "must be an absolute path inside the agent, not the root")
    io_mode = section.optional_string("io_mode")
    if io_mode is not None and io_mode not in IO_MODES:
        raise section.fail("io_mode", f"must be one of: {', '.join(IO_MODES)}")
    extra = section.strings("extra_args") if "extra_args" in section.data else ()
    if any(a in FORBIDDEN_ARGS or (a.startswith("-") and not a.startswith("--") and "i" in a[1:]) for a in extra):
        raise section.fail("extra_args", "must not carry the TLS flag: the platform's [tls] alone sets it")
    return MountSettings(path, io_mode, tuple(extra))


def _entry_string(section: Section, key: str, position: int) -> str:
    """The non-empty string at a key that is itself a configured value (a level, a login), named by position on failure.

    `Section.string` would name the key in its message, and here the key is the value to keep out of it (O12)."""
    value = section.data[key]
    if not isinstance(value, str) or not value.strip():
        raise section.fail(None, f"entry {position}: must be a non-empty string, got {type(value).__name__}")
    return value


def _data_path(section: Section, level: str, position: int) -> tuple[DataPathMode, str | None]:
    """One level's data path: its kind, and the listing mode a REST level's table names (None when it names none). The
    entry is a kind, or a table of `kind` and `listing`; it is named by its position, never by the level (O12)."""
    value = section.data[level]
    if isinstance(value, Mapping):
        unknown = sorted(set(value) - {"kind", "listing"})
        if unknown:
            raise section.fail(
                None, f"entry {position}: key {unknown[0]!r} is not a known key; known keys: kind, listing"
            )
        if "kind" not in value:
            raise section.fail(None, f"entry {position}: key 'kind' is required")
        kind, listing = value["kind"], value.get("listing")
    else:
        kind, listing = _entry_string(section, level, position), None
    try:
        mode = DataPathMode(kind) if isinstance(kind, str) else None
    except ValueError:
        mode = None
    if mode is None:
        raise section.fail(None, f"entry {position}: must be one of: {', '.join(m.value for m in DataPathMode)}")
    if listing is not None and listing not in LISTINGS:
        raise section.fail(None, f"entry {position}: key 'listing' must be one of: {', '.join(LISTINGS)}")
    if listing is not None and mode is not DataPathMode.REST:
        raise section.fail(
            None, f"entry {position}: key 'listing' applies to a rest data path only: no gate lists here"
        )
    return mode, listing


def _data_paths(section: Section) -> tuple[Mapping[str, DataPathMode], Mapping[str, str]]:
    """Read [sandbox.data_paths]: level of the study's factor -> mount, rest or none, and each REST level's listing mode
    (O21), `paged` unless its entry names `complete`."""
    modes, listings = {}, {}
    for position, level in enumerate(section.data):  # a level is the study's word: it is named by position, not echoed
        modes[level], listing = _data_path(section, level, position)
        if modes[level] is DataPathMode.REST:
            listings[level] = listing or PAGED
    if not modes:
        raise section.fail(None, "must map at least one level of the data_path factor")
    return MappingProxyType(modes), MappingProxyType(listings)


def _space_path(section: Section, key: str) -> str | None:
    """Read an optional path under the space root: plain names joined by '/', none of them '.' or '..'."""
    if key not in section.data:
        return None
    value = section.data[key]
    parts = value.split("/") if isinstance(value, str) and value else []
    if not parts or any(part in (".", "..") or not SPACE_PART.fullmatch(part) for part in parts):
        raise section.fail(key, "must be a path under the space root: plain names joined by '/', no '.' or '..'")
    return value


def _words(section: Section, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Read an optional list of command words (a path or an option), with no space or control character."""
    words = section.strings(key) if key in section.data else default
    if any(not COMMAND_WORD.fullmatch(word) for word in words):
        raise section.fail(key, "must hold plain command words")
    return tuple(words)


def load_sandbox(section: Section, base: Path, *, tls: TlsSettings | None = None) -> SandboxSettings:
    """Validate the `[sandbox]` table; raise ConfigError naming the file and key, never a value.

    `tls` is the platform's `[tls]`, already loaded; without it the sandbox's clients verify against their defaults."""
    keys = (
        "work_root",
        "name_prefix",
        "label_namespace",
        "agent_uid",
        "python",
        "sudo",
        "earlyoom_comm",
        "trust_paths",
        "content_check_file",
    )
    if "users" in section.data:  # one pool of leased users: a second one here would lease a user the slots also hold
        raise section.fail(
            "users",
            "is no longer read: declare the platform users a run leases as [slots.<name>], each with a data_path and "
            "a toolserver user, so one lease key gives a run both planes' users (C19)",
        )
    section.reject_unknown((*keys, "images", "network", "limits", "mount", "data_paths", "served_bytes"))
    work_root = (base / section.string("work_root")).resolve()
    prefix = section.optional_string("name_prefix") or "sbx"
    namespace = section.optional_string("label_namespace") or "org.expcore"
    if not PREFIX_RE.fullmatch(prefix):
        raise section.fail("name_prefix", "must be lower-case letters, digits and '-', starting with a letter")
    if not NAMESPACE_RE.fullmatch(namespace):
        raise section.fail("label_namespace", "must be lower-case letters, digits, '.' and '-'")
    agent, gate, oneclient = _images(section.section("images"))
    data_paths, listings = _data_paths(section.section("data_paths"))
    mount = _mount(section.section("mount")) if "mount" in section.data else None
    if DataPathMode.MOUNT in data_paths.values() and (mount is None or oneclient is None):
        raise section.fail("mount", "and [sandbox.images.oneclient] are required when a data path is a mount")
    check_file = _space_path(section, "content_check_file")
    if DataPathMode.REST in data_paths.values() and check_file is None:
        raise section.fail(
            "content_check_file",
            "is required when a data path is rest: the provisioned file read before a campaign to confirm the content "
            "type the data gate streams on",
        )
    comm = section.optional_string("earlyoom_comm")
    if comm is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,15}", comm):
        raise section.fail("earlyoom_comm", "must be 1 to 15 letters, digits, '_' and '-' (the kernel's comm limit)")
    return SandboxSettings(
        work_root=work_root,
        name_prefix=prefix,
        label_namespace=namespace,
        agent_uid=int(_number(section, "agent_uid", minimum=1000, integer=True, default=10001)),
        python=section.optional_string("python") or "python",
        sudo=_words(section, "sudo", ("sudo", "-n")),
        agent=agent,
        gate=gate,
        oneclient=oneclient,
        network=_network(section.section("network")),
        limits=_limits(section.section("limits")),
        mount=mount,
        data_paths=data_paths,
        earlyoom_comm=comm,
        trust_paths=_words(section, "trust_paths", ()),
        content_check_file=check_file,
        tls=tls if tls is not None else TlsSettings(),
        listings=listings,
        served_caps=_served_caps(section.section("served_bytes")) if "served_bytes" in section.data else DEFAULT_CAPS,
        served_caps_declared="served_bytes" in section.data,
    )


__all__ = ["ConfigError", "ImageRef", "Limits", "MountSettings", "NetworkSettings", "SandboxSettings", "load_sandbox"]
