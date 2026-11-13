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
trust_paths = []                # optional: paths in the agent image of CA bundles that must also trust the run's CA;
                                # on a rest data path the image's certifi bundle is added to them, found at the start
content_check_file = "dir/f"    # a provisioned file under the space root, read once before a campaign to confirm
                                # the content type the gate streams on (`precampaign.py`); required with a rest level
placeholder_token = "placeholder"  # optional: the agent's ONEDATA_TOKEN on a rest data path, never a credential (O23)
kill_grace_s = 5                # optional, 1 to 60: how long a cell's processes have to end after their kill at its
                                # deadline before the mount is aborted or the sandbox stopped (deadline.py, O34); the
                                # whole escalation lasts at most two graces and 20 s past the cap (deadline.margin_s)

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
io_mode = "proxy"               # required, no default: "proxy" (O36); "direct" is refused until #223 maps the storage
extra_args = []                 # further oneclient arguments, never one that sets the TLS mode, the I/O mode, the
                                # config file or oneclient's environment

[sandbox.data_paths]            # the levels of the study's data_path factor, each one of mount, rest, none
<level> = "rest"                # the kind alone; a rest level then pages its listings as the server does
<level> = { kind = "rest", listing = "complete" }  # or a table: a rest level's listing, "paged" or "complete"

[sandbox.served_bytes]          # optional: how much a run's tables of the bytes served list (gatelog.Caps, O22)
max_files = 1000                # files, the first read; max_directories = 1000; max_ranges_per_file = 64

[sandbox.federation]            # optional, with a mount or rest level: the federation on a docker network of its own
network = "fed_net"             # an existing network of the federation's deployment (a Compose project's); the
                                # oneclient sidecar and the data gate start on it instead of docker's default bridge;
                                # the agent never
zone = "host"                   # the zone's host name on that network, the one its certificate holds; the gate's
                                # alone: required with a rest level, refused without one
providers.<local> = "host"      # the local provider's host name on that network, the one its certificate holds;
                                # keyed by [support] local, the only provider accepted (the agent is pinned to it)
```

`io_mode` is declared, never detected (O36). oneclient 25.0 started with neither force option detects each storage's
access type at the first read of each space, and for a second space on a storage already detected as proxied it takes a
plain mutex it already holds (`src/cache/helpersCache.h`, `performAutoIOStorageDetection`, the recursion at the end of
its `!forceProxyIO` branch), so the read waits for ever and nothing is logged. `proxy` (`--force-proxy-io`) reads every
space through the provider. `direct` (`--force-direct-io`) reads the storage itself and so needs the providers' storage
mapped into the sidecar, which the sandbox does not do yet: it is refused at load (#223), and the command's code for it
stays for the mapping to lift the refusal. There is no third word for oneclient's own detection, and the extra
arguments may not carry either force option. The sidecar's oneclient reads no configuration file (`--config
/dev/null`) and its environment holds the token alone (`env -u` of `ONECLIENT_INSECURE`, `ONECLIENT_OVERRIDE` and
`ONECLIENT_CONFIG`), so its effective options are the argv and the token. The I/O mode is in the campaign fingerprint,
for each mount level, as the listing is for a REST level.

`listing` is a REST data path's option (O21): `paged`, the default, passes the server's page of a listing as it comes,
and `complete` has the data gate answer every listing with all of the directory's entries (`gatepolicy`). It is refused
on a mount or no data path, where no gate serves a listing. A study selects it per condition through the level the
condition gives its `data_path` factor. The run's metadata records it (`listing`), and so does the campaign fingerprint
(`config.PlatformConfig.fingerprint`, from `data_path_record`), so two modes are never one experiment. A declared
`[sandbox.served_bytes]` is in the fingerprint too (`served_caps_declared`); an absent one leaves it as it was.

Without `[sandbox.federation]` the sidecar and the REST data path's gate start on docker's default bridge and dial the
hosts of the zone's and the local provider's URLs, which suits a federation with public addresses. A federation the
host reaches at addresses no container can use, such as a Compose project whose ports are published on the host's
loopback, needs the table: the sidecar and the gate join the federation's network, where the names resolve, and dial
them on port 443 (oneclient's default, and the port a Compose service listens on inside its network). The gate serves
the agent those same names, so a client that follows the zone to the provider's registered domain (its name on that
network, in a Compose federation) comes back to the gate. The network must exist when a mount or REST sandbox starts
(it is never made, changed or removed by the sandbox).

`placeholder_token` is what the agent's environment holds as `ONEDATA_TOKEN` on a REST data path (O23): the published
client (`onedatarestfsspec`) refuses to start without a token, and the agent holds none, since the gate adds its own to
every request and drops every token the agent sends. It is not a credential and goes in no fingerprint; the agent sees
it, so a study whose check of the words its agent sees refuses the default names another. A value equal to the run's
token refuses the start.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from pathlib import Path
from types import MappingProxyType

from expcore.config.loading import ConfigError, Section
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.sandbox.gatelog import DEFAULT_CAPS, Caps
from expcore_onedata.sandbox.gatepolicy import LISTINGS, PAGED
from expcore_onedata.sandbox.hostops import strict_networks
from expcore_onedata.sandbox.oneclient_args import (
    IO_MODES,
    direct_keys_problem,
    mode_arg_problem,
    mount_problem,
    nested,
    storages_problem,
)
from expcore_onedata.tls import TlsSettings, moved

PREFIX_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
NAMESPACE_RE = re.compile(r"[a-z][a-z0-9.-]{0,63}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
COMMAND_WORD = re.compile(r"[A-Za-z0-9._/-]{1,128}")
SPACE_PART = re.compile(r"[^/\x00-\x1f\\]{1,255}")  # one name of a path under the space root
# The agent's ONEDATA_TOKEN on a REST data path (O23): the published client sends it as a header value and the gate
# drops it, so it is printable ASCII without white space; the default is a plain word no one takes for a real token.
PLACEHOLDER_TOKEN = "placeholder"
PLACEHOLDER_RE = re.compile(r"[!-~]{1,128}")
NETWORK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")  # a docker network's name; no `container:<id>`
NOT_A_FEDERATION_NETWORK = ("bridge", "default", "host", "none")  # docker's own: no name resolves, or no isolation
HOST_NAME = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")
KILL_GRACE_S = 5.0  # [sandbox] kill_grace_s unless declared: a few seconds, so a dying process is not taken for a wedge
# a cell's whole escalation past its cap is bounded by `deadline.margin_s`: two graces (after the kill, after an
# abort) and `deadline.COMMANDS_S` for its commands; 30 s at the default grace, 140 s at this maximum
KILL_GRACE_MAX_S = 60.0


IO_MODE_REFUSAL = (
    'is required and must be "proxy" or "direct": a mount never leaves the storage access to oneclient\'s own '
    "detection, which hangs the first read of a second space backed by the same storage (oneclient 25.0, "
    'src/cache/helpersCache.h, performAutoIOStorageDetection takes its access-type mutex twice). "proxy" reads every '
    'space through the provider; "direct" reads the local provider\'s storage itself and needs it mapped in '
    "[sandbox.mount.storages] (#223)"
)


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
    io_mode: str  # "proxy" or "direct", declared, never left to oneclient's detection (O36)
    extra_args: tuple[str, ...] = ()
    # direct I/O only (O37): (storage id, the directory that holds it on the host), in the file's order, by which a
    # message or a record names one; bound read-only into the sidecar alone, each named to oneclient by an override
    # of its POSIX mount point
    storages: tuple[tuple[str, str], ...] = ()
    direct_check_file: str | None = None  # direct I/O only: `<space>/<path>` under the mount, read at every start

    def __post_init__(self) -> None:
        """Settings built in code are held to what the loader holds a file to: a known mode, no extra argument that
        sets what the core declares (O36), and a direct mount's storage mapping and check file, which a proxy mount
        may not have (O37). `ValueError`; the message never holds an argument, an id or a path."""
        problem = mount_problem(self.io_mode, self.extra_args, self.storages, self.direct_check_file)
        if problem is not None:
            raise ValueError(problem)


@dataclass(frozen=True)
class FederationNetwork:
    """The docker network the oneclient sidecar and the REST data path's gate reach the federation on, and the names
    they dial there."""

    network: str  # an existing network of the federation's deployment; the sidecar or gate joins it, the agent never
    local_provider: str  # the host name the sidecar and the gate dial on it: the local provider's, its certificate's
    zone: str | None = None  # the zone's host name there, which the gate dials; required with a rest level, else None


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
    federation: FederationNetwork | None = None  # [sandbox.federation]: None puts the sidecar on the default bridge
    placeholder_token: str = PLACEHOLDER_TOKEN  # the REST agent's ONEDATA_TOKEN: no credential, the gate drops it (O23)
    kill_grace_s: float = KILL_GRACE_S  # a cell's processes' time to end after the kill at its deadline (O34)

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

    def io_mode_of(self, level: str) -> str | None:
        """The I/O mode of a level (O36): the mount's `io_mode` for a mount data path, None for any other."""
        if self.mode_of(level) is not DataPathMode.MOUNT or self.mount is None:
            return None
        return self.mount.io_mode

    def data_path_record(self) -> dict[str, dict[str, str]]:
        """Every level's data path as the campaign fingerprint holds it: its kind, for REST its listing mode and for a
        mount the I/O mode it runs with."""
        out: dict[str, dict[str, str]] = {}
        for level, mode in self.data_paths.items():
            listing, io_mode = self.listing_of(level), self.io_mode_of(level)
            out[level] = (
                {"kind": mode.value}
                | ({"listing": listing} if listing is not None else {})
                | ({"io_mode": io_mode} if io_mode is not None else {})
            )
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


def _storages(section: Section) -> tuple[tuple[str, str], ...] | None:
    """Read [sandbox.mount.storages] (O37), in the file's order, or None when the mount declares none. Each entry is a
    configured id and path, so a message names it by its position and never echoes either (O12)."""
    if "storages" not in section.data:
        return None
    table = section.section("storages")
    declared = tuple(table.data.items())
    problem = storages_problem(declared)
    if problem is not None:
        raise table.fail(None, problem)
    return declared


def _mount(section: Section) -> MountSettings:
    """Read [sandbox.mount]: the path in the agent and the oneclient options; TLS is the platform's [tls]. A direct
    mount also maps its storages and names its check file (O37), which a proxy mount may not."""
    if "tls" in section.data:
        raise moved(section, "tls")
    section.reject_unknown(("path", "io_mode", "extra_args", "storages", "direct_check_file"))
    path = section.string("path")
    if not path.startswith("/") or path == "/" or ".." in path.split("/") or any(c.isspace() for c in path):
        raise section.fail("path", "must be an absolute path inside the agent, not the root")
    io_mode = section.data.get("io_mode")
    if io_mode not in IO_MODES:
        raise section.fail("io_mode", IO_MODE_REFUSAL)
    extra = section.strings("extra_args") if "extra_args" in section.data else ()
    for argument in extra:
        problem = mode_arg_problem(io_mode, argument)
        if problem is not None:
            raise section.fail("extra_args", problem)
    storages, check = _storages(section), section.data.get("direct_check_file")
    found = direct_keys_problem(io_mode, storages, check)
    if found is not None:
        raise section.fail(*found)
    return MountSettings(path, io_mode, tuple(extra), storages or (), check)


def _storages_clear_of(section: Section, mount: MountSettings, work_root: Path) -> None:
    """Refuse a storage whose directory is, holds or lies under the work root (O37): the sandbox makes, unmounts and
    removes directories there, and the reaper removes what a killed run left, so a provider's files could go."""
    table = section.section("mount").section("storages") if mount.storages else None
    for position, (_, path) in enumerate(mount.storages):
        if table is not None and nested(str(Path(path).resolve()), str(work_root)):
            why = "is, holds or lies under [sandbox] work_root, where the sandbox makes and removes directories"
            raise table.fail(None, f"entry {position}: {why}")


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


def _federation_zone(section: Section, rest: bool) -> str | None:
    """The zone's name on the federation's network, which the REST data path's gate alone dials (O03, #203): required
    with a rest level, refused without one, where it would do nothing. A configured value: a message names the key."""
    if not rest:
        if "zone" in section.data:
            raise section.fail("zone", "is read by the REST data path's gate alone, and no data path is rest")
        return None
    zone = section.string("zone")
    if not HOST_NAME.fullmatch(zone):
        raise section.fail("zone", "must be a host name on that network, with no scheme or port")
    return zone


def _federation(section: Section, providers: Sequence[str], local: str | None, *, rest: bool) -> FederationNetwork:
    """Read [sandbox.federation]: the docker network the sidecar and the gate join, and the names they dial there: the
    local provider's, and with a rest level the zone's (O03).

    The provider's name is keyed by the provider it belongs to, and only the local one's is accepted, so a `[support]
    local` changed without this table is refused here and never mounts or reads through another provider. Every name
    is configured, so a message names a provider entry by its position and never echoes the network or a name."""
    section.reject_unknown(("network", "zone", "providers"))
    network = section.string("network")
    if not NETWORK_NAME.fullmatch(network) or network in NOT_A_FEDERATION_NETWORK:
        raise section.fail(
            "network",
            "must name the federation's own docker network: not docker's default bridge, the host's network, none or "
            "a container's",
        )
    table = section.section("providers")
    address = None
    for position, name in enumerate(table.data):
        if name not in providers:
            raise table.fail(None, f"entry {position}: is not a provider declared in [providers]")
        if name != local:
            raise table.fail(None, f"entry {position}: only the local provider's name is read, the agent's pinned one")
        address = _entry_string(table, name, position)
        if not HOST_NAME.fullmatch(address):
            raise table.fail(None, f"entry {position}: must be a host name on that network, with no scheme or port")
    if address is None:
        raise table.fail(None, "must name the local provider (the support's `local`), which the agent is pinned to")
    return FederationNetwork(network, address, _federation_zone(section, rest))


def _space_path(section: Section, key: str) -> str | None:
    """Read an optional path under the space root: plain names joined by '/', none of them '.' or '..'."""
    if key not in section.data:
        return None
    value = section.data[key]
    parts = value.split("/") if isinstance(value, str) and value else []
    if not parts or any(part in (".", "..") or not SPACE_PART.fullmatch(part) for part in parts):
        raise section.fail(key, "must be a path under the space root: plain names joined by '/', no '.' or '..'")
    return value


def _placeholder(section: Section) -> str:
    """Read the optional placeholder token (O23), a value never echoed: the key is named, as for any value."""
    value = section.data.get("placeholder_token", PLACEHOLDER_TOKEN)
    if not isinstance(value, str) or not PLACEHOLDER_RE.fullmatch(value):
        raise section.fail(
            "placeholder_token", "must be 1 to 128 printable ASCII characters without white space: a header value"
        )
    return value


def _kill_grace(section: Section) -> float:
    """Read the optional grace of a cell's kill at its deadline (O34): a number of seconds from 1 to 60."""
    grace = float(_number(section, "kill_grace_s", minimum=1, default=KILL_GRACE_S))
    if grace > KILL_GRACE_MAX_S:
        raise section.fail("kill_grace_s", f"must be at most {KILL_GRACE_MAX_S:g} seconds")
    return grace


def _words(section: Section, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """Read an optional list of command words (a path or an option), with no space or control character."""
    words = section.strings(key) if key in section.data else default
    if any(not COMMAND_WORD.fullmatch(word) for word in words):
        raise section.fail(key, "must hold plain command words")
    return tuple(words)


def load_sandbox(
    section: Section,
    base: Path,
    *,
    tls: TlsSettings | None = None,
    providers: Sequence[str] = (),
    local: str | None = None,
) -> SandboxSettings:
    """Validate the `[sandbox]` table; raise ConfigError naming the file and key, never a value.

    `tls` is the platform's `[tls]`, already loaded; without it the sandbox's clients verify against their defaults.
    `providers` and `local` are the platform's declared providers and its local provider (`[support] local`, or the
    `local` every `[[spaces]]` entry shares), which `[sandbox.federation]` is checked against."""
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
        "placeholder_token",
        "kill_grace_s",
    )
    if "users" in section.data:  # one pool of leased users: a second one here would lease a user the slots also hold
        raise section.fail(
            "users",
            "is no longer read: declare the platform users a run leases as [slots.<name>], each with a data_path and "
            "a toolserver user, so one lease key gives a run both planes' users (C19)",
        )
    section.reject_unknown((*keys, "images", "network", "limits", "mount", "data_paths", "served_bytes", "federation"))
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
    if mount is not None:
        _storages_clear_of(section, mount, work_root)
    federation = None
    if "federation" in section.data:
        if not {DataPathMode.MOUNT, DataPathMode.REST} & set(data_paths.values()):
            raise section.fail(
                "federation", "is read by the mount sidecar and the data gate alone, and no data path is mount or rest"
            )
        rest = DataPathMode.REST in data_paths.values()
        federation = _federation(section.section("federation"), providers, local, rest=rest)
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
        federation=federation,
        placeholder_token=_placeholder(section),
        kill_grace_s=_kill_grace(section),
    )


__all__ = [
    "ConfigError",
    "FederationNetwork",
    "ImageRef",
    "Limits",
    "MountSettings",
    "NetworkSettings",
    "SandboxSettings",
    "load_sandbox",
]
