# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/config.py, blob b04e62a429fa4faf8c6c093e0f5a8dd5e08791b3
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Sandbox configuration: limits, per-option disk, data configuration.

All numbers here are config, not constants of the design. The defaults are
PLACEHOLDERS until the science agent fixes them (see sandbox/README.md).
"""
from __future__ import annotations

import dataclasses
import ipaddress
import math
import os
import re
import tomllib
from pathlib import Path

from dataset_76.paths import DATA_ROOT
from dataset_76.records import Option
from dataset_76.sandbox.netpool import default_pool

GiB = 1024 ** 3
# the work filesystem is built larger than its quota so the agent can really write `quota` bytes
IMAGE_OVERHEAD_FACTOR = 1.03
IMAGE_OVERHEAD_BYTES = 32 * 1024 ** 2


def usable_image_bytes(quota: int) -> int:
    """Size of the work filesystem image that offers `quota` usable bytes (ext4 overhead included)."""
    return int(quota * IMAGE_OVERHEAD_FACTOR) + IMAGE_OVERHEAD_BYTES
D_DISK_FACTOR = 1.2


ALIASES_ENV = "STUDY_VAR_57"
# Per host and outside every synced tree (vmsync replaces config/ on each sync), on the root disk (the data volume may be
# absent): a driver that forgets an environment variable must not silently take the public address.
HOST_ALIASES_FILE = Path("/etc/dataset-80/host_aliases.toml")
_HOSTNAME = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


def validate_host_aliases(aliases: dict) -> dict[str, str]:
    """name -> IPv4 address, checked before any container is started (docker's --add-host splits at the first colon, so an
    IPv6 address or a stray colon would silently map the wrong thing)."""
    out: dict[str, str] = {}
    for name, ip in dict(aliases).items():
        if not isinstance(name, str) or not _HOSTNAME.match(name):
            raise ValueError(f"host alias name {name!r} is not a host name")
        try:
            a = ipaddress.IPv4Address(ip)
        except (ValueError, TypeError):
            raise ValueError(f"host alias {name!r}: {ip!r} is not an IPv4 address") from None
        out[name] = str(a)
    return out


def default_host_aliases() -> dict[str, str]:
    """Per-host: fr's and de's addresses on fr's LAN, set only on a VM that can reach them; EMPTY on every other host.
    First of: the environment (`STUDY_VAR_57=name=ip,name=ip`, an empty value means none), the host's file
    (HOST_ALIASES_FILE, `"name" = "ip"` lines), `[network] host_aliases` in config/harness.toml (shipped empty). A host
    file that exists but cannot be read raises: it never falls through to an empty map."""
    env = os.environ.get(ALIASES_ENV)
    if env is not None:
        pairs = [x for x in env.split(",") if x.strip()]
        if any("=" not in x for x in pairs):
            raise ValueError(f"{ALIASES_ENV} must be name=ip,name=ip")
        return validate_host_aliases(dict(x.strip().split("=", 1) for x in pairs))
    try:
        os.lstat(HOST_ALIASES_FILE)             # absent is the only way through: a dangling link, a refused search and a file for the directory all raise
    except FileNotFoundError:
        pass
    else:
        with open(HOST_ALIASES_FILE, "rb") as fh:
            return validate_host_aliases(tomllib.load(fh))
    d = Path(os.environ.get("STUDY_VAR_56") or Path(__file__).resolve().parents[2] / "config")
    with open(d / "harness.toml", "rb") as fh:
        return validate_host_aliases(tomllib.load(fh).get("network", {}).get("host_aliases", {}))


def host_alias_args(aliases: dict[str, str], skip=()) -> list[str]:
    """`--add-host name:ip` for each alias (sorted, so the command is stable); `skip` names are left out."""
    return [a for n, ip in sorted(aliases.items()) if n not in skip for a in ("--add-host", f"{n}:{ip}")]


@dataclasses.dataclass
class Limits:
    """Fixed per-run resources, identical for every option except disk (see disk_bytes).

    The dataset sizes have no default: build with `Limits.for_dataset(s1_bytes, s2_bytes)` (the real sizes
    from #3). `Sandbox.start` refuses limits whose small quota could hold a wholesale copy of a layout.
    """

    cpus: float = 4.0
    # Memory by rule, identical in every option: the agent's ALLOWANCE (what the task prompt's run may use) plus the
    # measured headroom of the tracer that runs inside the agent's process. The container limit is the sum. The headroom has no
    # default: it is a measurement (#9, at real size), and the real factory refuses to start without it.
    agent_memory_bytes: int = 16 * GiB
    tracer_headroom_bytes: int | None = None
    memory_bytes: int | None = None       # TESTS ONLY: an explicit container limit that overrides the rule
    pids: int = 512
    # open files (RLIMIT_NOFILE), soft = hard, same in every option: M and D hold a descriptor per open file and T's file-like objects
    # do not, so docker's default soft limit of 1,024 would fail open_mfdataset on 1,544 files under M and D only (science ruling, 1 Nov)
    nofile: int = 1048576
    s1_size_bytes: int | None = None
    s2_size_bytes: int | None = None
    small_disk_bytes: int | None = None   # "small" quota of T, condition_a, M, M+ (the work disk)
    d_disk_factor: float = D_DISK_FACTOR  # D: disk >= factor * S2
    d_disk_bytes: int | None = None       # explicit override; never below the factor * S2 floor
    trace_bytes: int = 256 * 1024 ** 2    # the /trace ALLOWANCE: what the writable-storage budget below counts and the scripted controls' /trace tmpfs holds (scripted/control.py)
    # The /trace volume of an AGENT sandbox (science, #34, 18:31Z): two long runs filled the 256 MiB volume, the tracer dropped start markers and the
    # runs were retried. 1 GiB, the same sparse loop file, the same for every option. It is NOT part of the budget or of `writable_small_bytes`: counting it
    # there would shrink every option's work disk from 957,996,528 B to 176,145,685 B, and the agent must see nothing new. The measured guard in
    # Sandbox._check_capacity sees the real size (the writable total is then about 2.70 GB against the smaller layout's 3.79 GB). Scripted runs
    # set it back to `trace_bytes` (scripted/federation.py): they keep the 256 MiB volume.
    agent_trace_bytes: int = 1024 ** 3
    tmp_bytes: int = 512 * 1024 ** 2
    shm_bytes: int = 64 * 1024 ** 2       # /dev/shm of the agent container (set explicitly, counted as writable)

    @property
    def container_memory_bytes(self) -> int:
        """The agent container's memory limit (also its swap limit, so no swap): allowance + tracer headroom."""
        if self.memory_bytes is not None:
            return int(self.memory_bytes)
        if self.tracer_headroom_bytes is None:
            raise ValueError("Limits.tracer_headroom_bytes is not set: the container memory limit is the agent's allowance "
                             "plus the tracer's measured headroom, and there is no default for the measurement")
        return int(self.agent_memory_bytes + self.tracer_headroom_bytes)

    @property
    def writable_small_bytes(self) -> int:
        """All storage the agent can write outside D: the work filesystem as actually built (the quota plus
        the filesystem overhead added by `usable_image_bytes`), /tmp, trace and /dev/shm."""
        return usable_image_bytes(self.small_disk_bytes or 0) + self.tmp_bytes + self.trace_bytes + self.shm_bytes

    @classmethod
    def for_dataset(cls, s1_bytes: int, s2_bytes: int, writable_fraction: float = 0.5, **kw) -> "Limits":
        """Limits computed from the real layout sizes (#3): D disk = 1.2 x S2; all small-option writable
        storage (work disk + /tmp + trace) at most `writable_fraction` of the smaller layout."""
        if not 0 < writable_fraction < 1:
            raise ValueError("writable_fraction must be in (0, 1)")
        lim = cls(s1_size_bytes=s1_bytes, s2_size_bytes=s2_bytes, **kw)
        budget = writable_fraction * min(s1_bytes, s2_bytes) - lim.tmp_bytes - lim.trace_bytes - lim.shm_bytes
        small = int((budget - IMAGE_OVERHEAD_BYTES) / IMAGE_OVERHEAD_FACTOR)
        if small < 64 * 1024 ** 2:
            raise ValueError("layouts too small for the tmp/trace allowances; lower tmp_bytes and trace_bytes")
        lim.small_disk_bytes = small
        return lim

    def validate(self) -> None:
        """Refuse to start with limits that cannot enforce the design."""
        if None in (self.s1_size_bytes, self.s2_size_bytes, self.small_disk_bytes):
            raise ValueError("Limits need the real dataset sizes: use Limits.for_dataset(s1_bytes, s2_bytes)")
        smallest = min(self.s1_size_bytes, self.s2_size_bytes)
        if self.writable_small_bytes >= smallest:
            raise ValueError(f"small-option writable storage ({self.writable_small_bytes} B: work disk + tmp + trace) "
                             f"could hold a wholesale copy of the smaller layout ({smallest} B)")
        if self.disk_bytes(Option.D) < D_DISK_FACTOR * self.s2_size_bytes:  # d_disk_factor below 1.2 is refused
            raise ValueError("D disk below 1.2 x S2")

    def disk_bytes(self, option: Option) -> int:
        if option == Option.D:
            floor = math.ceil(max(self.d_disk_factor, D_DISK_FACTOR) * self.s2_size_bytes)
            return max(self.d_disk_bytes or 0, floor)
        return self.small_disk_bytes


@dataclasses.dataclass
class SandboxConfig:
    # The sandbox image is pinned by id, never trusted by tag: a tag can be moved under a running campaign (another
    # branch's build, a rebuild), so `image` is only a reference to resolve and `pinned_image_id` is what must come out.
    # `Sandbox.start` refuses when they disagree and runs every container by the resolved id.
    image: str = ""                 # tag or id of the image to run; no default tag
    pinned_image_id: str = ""       # "sha256:<hex>", fixed once (SandboxConfig.pin) and recorded by the campaign
    oneclient_image: str = "onedata/oneclient:25.0"
    work_root: Path = DATA_ROOT / "sandboxes"   # sandbox disks live on /mnt/volume2
    limits: Limits | None = None    # required: Limits.for_dataset(...)
    drop_host_caches: bool = False  # opt-in host-wide sync + drop_caches (single-run measurements, #12); evicts concurrent runs' caches
    instruments_dir: Path | None = None  # #9 mounts its instrumentation module directory here
    instrument_module: str = "dataset_179"
    agent_uid: int = 10001
    # each sandbox network is a /28 of this range, never of Docker's default pools (see netpool); env STUDY_VAR_89
    subnet_pool: str = dataclasses.field(default_factory=default_pool)
    page_limit: int = 1000          # condition_a: page size of the gate's internal pagination
    # name -> IPv4, per host (see default_host_aliases): --add-host on every container this config starts
    host_aliases: dict[str, str] = dataclasses.field(default_factory=default_host_aliases)


@dataclasses.dataclass
class DataConfig:
    """Where the data is and how to reach it. Secrets are values, never logged or recorded."""

    onezone_host: str = ""                 # e.g. zone.example.org
    space_name: str = ""
    token: str = ""                        # read-only token scoped to the dataset (T, condition_a, D)
    provider_hosts: list[str] | None = None  # None: discover from the token scope at start
    # name -> {"addr", "port", "tls"}: upstream override (mock Onedata in tests)
    upstream: dict[str, dict] = dataclasses.field(default_factory=dict)
    # dataset locations inside the space (placeholders until #3 has laid the data out)
    s1_path: str = "state_a"                    # S1: the directory of 15,340 daily files
    s2_path: str = "s2/dataset-2.nc"          # S2: the one file
    # M / M+
    mount_path: str = "/data"              # fixed path of the mount inside the sandbox
    io_mode: str | None = None             # "direct" | "proxy" (#10); recorded per run
    oneclient_provider_host: str = ""    # empty: first of provider_hosts (the one place to change the data provider)
    oneclient_token: str = ""              # held by the mount sidecar only, never by the sandbox
    oneclient_tls: str = "verify"          # "verify" | "insecure" (oneclient -i): one value, recorded per run (pending operator decision on the SNI problem)
    oneclient_perf_log: bool = False       # M/M+: oneclient --log-read-write-perf; CSV kept out of the agent's sight, off by default
    oneclient_extra_args: list[str] = dataclasses.field(default_factory=list)
    mount_stub_dir: Path | None = None     # TESTS ONLY: bind a plain directory instead of oneclient
    replicate: object = None               # M+: callable run before the mount (scripted replication, #12)
    # knowledge plane: reachable through the gate by name; [{"name","host","port"}]
    knowledge: list[dict] = dataclasses.field(default_factory=list)
    verify_ssl: bool = True                # TLS between gate and the real upstream


def validate_knowledge(knowledge: list[dict]) -> None:
    """Knowledge-plane endpoints are plain TCP pass-through on their own port of the gate.

    Port 443 belongs to the gate's HTTPS listener and two endpoints cannot share a port; a TLS
    knowledge plane on 443 would need hostname routing (not built: the knowledge plane is not yet specified).
    """
    seen = set()
    for k in knowledge:
        port = k["port"]
        if port == 443 or port in seen:
            raise ValueError(f"knowledge endpoint {k['name']!r}: port {port} is taken (443 is the gate's HTTPS port; ports are unique)")
        seen.add(port)


def dataset_location(dc: "DataConfig", option: Option, layout) -> str:
    """{DATASET_LOCATION} (T, condition_a, D: a path for the fsspec client) or {MOUNT_LOCATION} (M, M+)."""
    rel = dc.s1_path if getattr(layout, "value", layout) == "state_a" else dc.s2_path
    rel = rel.strip("/")
    if option in (Option.M, Option.condition_d):
        return f"{dc.mount_path.rstrip('/')}/{dc.space_name}/{rel}"
    return f"{dc.space_name}/{rel}"


AGENT_OOM_SCORE_ADJ = 1000     # science, #34, 2 Nov: earlyoom's and the kernel's victim is the sandbox's own process. Every process of the agent's container takes this; nothing else on the host has it


def study_env(dc: "DataConfig", option: Option, layout) -> dict[str, str]:
    """What a process in the study needs to tell the study's data path from anything else. The tracer reads it: without it, a file under the mount is an unknown local file, and
    naming that means hashing it, one whole read of the file through FUSE. So the enforced arm and the control set it from here, and cannot differ."""
    env = {"STUDY_VAR_71": option.value, "STUDY_VAR_42": dataset_location(dc, option, layout)}
    if option in (Option.M, Option.condition_d):
        env["STUDY_VAR_68"] = dc.mount_path
    return env


# Campaign-wide setting (operator decision, 1 Nov 2026): oneclient runs --insecure for the whole campaign,
# pilot and calibration included. The library default of DataConfig.oneclient_tls stays "verify"; the harness (#8)
# and campaign (#16) build their DataConfig through `campaign_data_config`, the one place this is set.
CAMPAIGN_ONECLIENT_TLS = "insecure"


def campaign_data_config(*, via: str | None = None, **kw) -> DataConfig:
    """DataConfig for campaign runs: as DataConfig(**kw) with the campaign's oneclient TLS mode and the one provider route
    (route.apply_provider_route: the configured port for the gate's upstream and oneclient's -P; `via="traefik"` only for fault
    calibration)."""
    from dataset_76.sandbox.route import apply_provider_route
    kw.setdefault("oneclient_tls", CAMPAIGN_ONECLIENT_TLS)
    if kw["oneclient_tls"] != CAMPAIGN_ONECLIENT_TLS:
        raise ValueError(f"the campaign runs oneclient with oneclient_tls={CAMPAIGN_ONECLIENT_TLS!r}; it is not changed per run")
    kw = apply_provider_route(kw, via=via)
    return DataConfig(**kw)


def pin_image(ref: str) -> str:
    """Resolve a tag (or id) to its immutable image id. A campaign calls this once at its start and stores the result."""
    from dataset_76.sandbox import hostops
    return hostops.image_id(ref)
