"""The `[hpc]` table of a layout and the site's settings, checked: the sizes of every step, the held job's shape and
what it bills, and the site values the driver reads from the environment (DESIGN.md, sections 2 and 3).

The layout file is the Compose federation's (`deploy/compose-federation/fedspec.py`), which keeps `[hpc]` unread; this
module reads it. The site's values (`site.env`) come from the environment at `up` and are recorded in the state file,
so a later command (`down`, a service verb) needs no environment.
"""

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

import tomllib
from hpcslurm import valid_network

SIZE = re.compile(r"([1-9][0-9]{0,6})([mg])")
TIME = re.compile(r"(?:([0-9]{1,2})-)?([0-9]{1,2}):([0-9]{2}):([0-9]{2})")
SLURM_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # an account or a partition
EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")
HPC_KEYS = (
    "time",
    "stop_seconds",
    "step_launch_wait_s",
    "core",
    "zone",
    "provider",
    "control",
    "sandbox",
    "sandbox_placement",
)
SIZE_KEYS = ("cpus", "memory")
CORE_PLACES = ("login", "step")  # where the bootstrap and the link check run (DESIGN.md, section 9)
# Where the agent's node is held (DESIGN.md, section 1): "het", the last component of a heterogeneous job, sized on its
# own; "separate", a held job of its own, the fallback should a site refuse the first.
SANDBOX_PLACEMENTS = ("het", "separate")
DEFAULT_STEP_LAUNCH_WAIT_S = 240  # `[hpc] step_launch_wait_s`: see DEFAULTS
VERB_WORK_S = 60  # what a relay verb does beside waiting for its step to start: its step's own bound, a status's 60 s
KILL_TAIL_S = 30  # supervise.py's SIGKILL tail after the graceful stop's bound (lib.sh, stop_service)
STEP_END_S = 60  # the longest the driver polls for a stopped service's step to leave squeue -s
SETTLE_SLACK_S = 70  # what settle_timeout_s keeps beyond the stop's bound and the launch wait: the stop verb's
# own tail past its control step (the wait for the step to leave the list and after a cancel: 40 s) and a 30 s margin
STEP_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin"  # HPC_STEP_PATH's default
SANDBOX_SPARE_CPUS = 2  # beside the agent's cores on its node: the gate, the dispatcher and the egress collector
SANDBOX_OVERHEAD_MIB = 4096  # beside the agent's bytes: the gate, the dispatcher, the collector, the store, the SIF
PLACEHOLDER = re.compile(r"@[A-Za-z0-9_.-]+@")  # a value the platform template leaves to up (fedfacts)
SHA256 = re.compile(r"[0-9a-f]{64}")
# The site's variables of the agent's Apptainer sandbox (M5), by the Site field each fills: the files' paths and the
# work root, and the SHA-256 each file is declared to have (oneclient's bundle: the host mount's oneclient; the
# gateport: the cell's 443 mapper, `bundle/gateport.c`, the preload of a layout with a REST level).
AGENT_PATHS = {
    "HPC_AGENT_SIF": "agent_sif",
    "HPC_EGRESS_SHIM": "egress_shim",
    "HPC_ONECLIENT_BUNDLE": "oneclient_bundle",
    "HPC_GATEPORT": "gateport",
    "HPC_AGENT_WORK_ROOT": "agent_work_root",
}
AGENT_PINS = {
    "HPC_AGENT_SIF_SHA256": "agent_sif_sha256",
    "HPC_EGRESS_SHIM_SHA256": "egress_shim_sha256",
    "HPC_ONECLIENT_BUNDLE_SHA256": "oneclient_bundle_sha256",
    "HPC_GATEPORT_SHA256": "gateport_sha256",
}
# The study's own artifacts (its tool server's venv, clone and manifests) on the filesystem the login node and the
# agent's node see: the directory a platform template (or a space's `data_manifest`) names as `@study_artifacts@`;
# `up` digests the directory's tree into the federation's render identity (O74, hpcidentity).
STUDY_PATHS = {"HPC_STUDY_ARTIFACTS": "study_artifacts"}
OPTIONAL_VARIABLES = {**AGENT_PATHS, **AGENT_PINS, **STUDY_PATHS}  # each may be left out of site.env
# HPC_QUOTA_CHECK's one value. It never applies to a federation bound onto scratch (a site that sets it with
# HPC_STORAGE_ROOT is refused), and without a scratch root there is no check to skip (README, "Provider storage").
# HPC_HOME_QUOTA_CHECK takes the same one value, and skips the home guard wherever it is set.
QUOTA_CHECK_OFF = "off"
DEFAULTS = {
    # From the live peaks: the zone about 4.0 GB of RSS and its 1.9 GB rootfs on tmpfs; a provider about 4.8 GB and its
    # 2.5 GB rootfs, plus its posix storage on the same tmpfs; the largest control step (a token's delivery) about
    # 180 MB, killed under 100 MB.
    "time": "02:00:00",
    # Three provider outages on one HPC federation (17 Nov): the largest service-side stop after SIGTERM was 33.1 s
    # (the others 27.1 s and 26.1 s), plus 50 %.
    "stop_seconds": 50,
    # How long a control step may wait to start before its own work begins, which every bound that waits on a step's
    # client from its start adds. A step's creation can wait on the scheduler's controller, up to its message timeout
    # (200 s on a site that sets it so, with no error); relay verb steps were measured starting 47 to 103 s after they
    # were issued during a heal. 240 s is that 200 s plus 20 %. It widens timeouts only.
    "step_launch_wait_s": DEFAULT_STEP_LAUNCH_WAIT_S,
    "core": "login",
    "zone": {"cpus": 4, "memory": "8g"},
    "provider": {"cpus": 4, "memory": "10g"},
    "control": {"cpus": 1, "memory": "1g"},
    # The coordinator's decision (16 Nov 2026): the agent's node is its own component of a heterogeneous job, so only
    # it bills the agent's size; a site that refuses one takes "separate"; a uniform larger shape is the last resort.
    "sandbox_placement": "het",
}


class SettingsError(ValueError):
    """A layout's `[hpc]` table or a site setting that cannot be used; the message names the key."""


@dataclass(frozen=True)
class Size:
    """One step's cores and memory."""

    cpus: int
    memory_mib: int

    @property
    def mem(self) -> str:
        """The memory as Slurm's `--mem` takes it."""
        return f"{self.memory_mib}M"


@dataclass(frozen=True)
class Role:
    """One node of the held job: the service it runs (or `sandbox`, the agent's node) and its kind."""

    name: str
    kind: str  # "oz", "op" or "sandbox"


@dataclass(frozen=True)
class AgentLimits:
    """The agent's limits as the layout's platform template declares them for the Apptainer sandbox (M5): what its
    node must hold beside the gate, the dispatcher, the collector, the store and the staged image."""

    cpus: float
    memory_bytes: int
    work_disk_bytes: int
    tmp_bytes: int

    def least(self) -> Size:
        """The least sandbox step for these limits: the agent's cores plus the spare ones, and its memory, work and tmp
        bytes (the last two on the node's tmpfs, charged to the step) plus the overhead."""
        mib = -(-(self.memory_bytes + self.work_disk_bytes + self.tmp_bytes) // (1 << 20))
        return Size(math.ceil(self.cpus) + SANDBOX_SPARE_CPUS, mib + SANDBOX_OVERHEAD_MIB)


@dataclass(frozen=True)
class HpcSettings:
    """A layout's `[hpc]` table, checked, with its defaults."""

    time_s: int
    stop_seconds: int
    step_launch_wait_s: int
    core: str
    zone: Size
    provider: Size
    control: Size
    sandbox: Optional[Size] = None
    sandbox_placement: str = "het"

    @property
    def time(self) -> str:
        """The held job's `--time`, as Slurm prints a limit."""
        days, rest = divmod(self.time_s, 86400)
        clock = f"{rest // 3600:02d}:{rest % 3600 // 60:02d}:{rest % 60:02d}"
        return f"{days}-{clock}" if days else clock

    def size(self, kind: str) -> Size:
        sizes = {"oz": self.zone, "op": self.provider, "sandbox": self.sandbox}
        found = sizes.get(kind)
        if found is None:
            raise SettingsError(f"no size for a node of kind {kind}")
        return found

    def roles(self, providers: Tuple[str, ...]) -> Tuple[Role, ...]:
        """The held job's nodes in order: the zone, each provider in the layout's order, then the agent's node."""
        roles = [Role("zone", "oz"), *(Role(name, "op") for name in providers)]
        if self.sandbox is not None and "sandbox" in providers:
            raise SettingsError('a provider named "sandbox" would lose its node to the agent\'s: rename the provider')
        if self.sandbox is not None:
            roles.append(Role("sandbox", "sandbox"))
        return tuple(roles)

    def node(self) -> Size:
        """Every service node's shape (the zone's and each provider's): the larger step, plus the control reserve. The
        agent's node has a shape of its own (`sandbox_node`), never this one."""
        return Size(
            max(self.zone.cpus, self.provider.cpus) + self.control.cpus,
            max(self.zone.memory_mib, self.provider.memory_mib) + self.control.memory_mib,
        )

    def sandbox_node(self) -> Optional[Size]:
        """The agent's node's shape: its step plus the control reserve; None when the layout declares no such node."""
        if self.sandbox is None:
            return None
        return Size(self.sandbox.cpus + self.control.cpus, self.sandbox.memory_mib + self.control.memory_mib)

    def billing_per_hour(self, nodes: int, memory_weight: float) -> float:
        """What the federation bills per hour where each node bills the larger of its cores and the weight times its GB:
        `nodes` counts every node held, the agent's node included, which bills at its own shape."""

        def bill(size: Size) -> float:
            return max(float(size.cpus), memory_weight * size.memory_mib / 1024)

        sandbox = self.sandbox_node()
        services = nodes - (1 if sandbox is not None else 0)
        return services * bill(self.node()) + (bill(sandbox) if sandbox is not None else 0.0)

    @property
    def stop_bound_s(self) -> int:
        """The longest the service's own stop takes (DESIGN.md, section 7e): the graceful bound, supervise.py's SIGKILL
        tail, and the poll for the step's end. It holds no launch wait: that is `step_launch_wait_s`'s."""
        return self.stop_seconds + KILL_TAIL_S + STEP_END_S

    @property
    def stop_verb_bound_s(self) -> int:
        """The longest a stop verb takes in all: the control step's launch wait and then the stop's own bound."""
        return self.step_launch_wait_s + self.stop_bound_s

    @property
    def command_timeout_s(self) -> int:
        """The `[faults] command_timeout_s` an HPC platform declares: the longest one relay verb may take, a control
        step's launch wait and the verb's own work (`VERB_WORK_S`)."""
        return self.step_launch_wait_s + VERB_WORK_S

    @property
    def settle_timeout_s(self) -> int:
        """The least `[faults] settle_timeout_s` an HPC platform declares: above the stop verb's whole bound."""
        return self.stop_verb_bound_s + SETTLE_SLACK_S


def _size(value: Any, key: str) -> Size:
    if not isinstance(value, Mapping):
        raise SettingsError(f"hpc.{key}: must be a table with cpus and memory")
    unknown = sorted(set(value) - set(SIZE_KEYS))
    if unknown:
        raise SettingsError(f"hpc.{key}.{unknown[0]}: is not a key of an [hpc] size")
    missing = sorted(set(SIZE_KEYS) - set(value))
    if missing:
        raise SettingsError(f"hpc.{key}.{missing[0]}: is required")
    cpus = value["cpus"]
    if isinstance(cpus, bool) or not isinstance(cpus, int) or not 1 <= cpus <= 256:
        raise SettingsError(f"hpc.{key}.cpus: must be a whole number of cores from 1 to 256")
    memory = value["memory"]
    match = SIZE.fullmatch(memory) if isinstance(memory, str) else None
    if match is None:
        raise SettingsError(f"hpc.{key}.memory: must be a size such as 10g or 512m")
    number, unit = int(match.group(1)), match.group(2)
    return Size(cpus, number * (1024 if unit == "g" else 1))


def _seconds(value: Any) -> int:
    match = TIME.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise SettingsError("hpc.time: must be a Slurm time limit such as 02:00:00 or 1-00:00:00")
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    if minutes > 59 or seconds > 59 or hours > 23:
        raise SettingsError("hpc.time: must be a Slurm time limit such as 02:00:00 or 1-00:00:00")
    total = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    if not 600 <= total <= 7 * 86400:
        raise SettingsError("hpc.time: must be from 00:10:00 to 7 days")
    return total


def _sandbox(table: Mapping[str, Any], agent: Optional[AgentLimits]) -> Optional[Size]:
    """The agent's node's step: as declared, never below what the agent's limits need when the layout's template
    declares them; derived from those limits when the table declares none; else no agent's node."""
    declared = _size(table["sandbox"], "sandbox") if "sandbox" in table else None
    if agent is None:
        return declared
    least = agent.least()
    if declared is None:
        return least
    if declared.cpus < least.cpus or declared.memory_mib < least.memory_mib:
        raise SettingsError(
            f"hpc.sandbox: is below what the agent's limits in the platform template need: {least.cpus} cores (its "
            f"cores + {SANDBOX_SPARE_CPUS}) and {least.memory_mib} MiB (its memory, work and tmp bytes + "
            f"{SANDBOX_OVERHEAD_MIB} MiB)"
        )
    return declared


def load_settings(table: Optional[Mapping[str, Any]], agent: Optional[AgentLimits] = None) -> HpcSettings:
    """The `[hpc]` table of a layout (None: every default), checked, with the agent's limits when its platform template
    declares them (`agent_limits`); SettingsError names the key."""
    table = {} if table is None else table
    if not isinstance(table, Mapping):
        raise SettingsError("hpc: must be a table")
    unknown = sorted(set(table) - set(HPC_KEYS))
    if unknown:
        raise SettingsError(f"hpc.{unknown[0]}: is not a key of the [hpc] table")
    merged = {**DEFAULTS, **table}
    stop = merged["stop_seconds"]
    if isinstance(stop, bool) or not isinstance(stop, int) or not 30 <= stop <= 3600:
        raise SettingsError("hpc.stop_seconds: must be a whole number of seconds from 30 to 3600")
    launch_wait = merged["step_launch_wait_s"]
    if isinstance(launch_wait, bool) or not isinstance(launch_wait, int) or not 0 <= launch_wait <= 3600:
        raise SettingsError("hpc.step_launch_wait_s: must be a whole number of seconds from 0 to 3600")
    core = merged["core"]
    if core not in CORE_PLACES:
        raise SettingsError("hpc.core: must be " + " or ".join(f'"{place}"' for place in CORE_PLACES))
    if merged["sandbox_placement"] not in SANDBOX_PLACEMENTS:
        raise SettingsError("hpc.sandbox_placement: must be " + " or ".join(f'"{p}"' for p in SANDBOX_PLACEMENTS))
    settings = HpcSettings(
        time_s=_seconds(merged["time"]),
        stop_seconds=stop,
        step_launch_wait_s=launch_wait,
        core=core,
        zone=_size(merged["zone"], "zone"),
        provider=_size(merged["provider"], "provider"),
        control=_size(merged["control"], "control"),
        sandbox=_sandbox(merged, agent),
        sandbox_placement=merged["sandbox_placement"],
    )
    if settings.time_s <= 2 * settings.stop_verb_bound_s:
        raise SettingsError(
            "hpc.time: must exceed twice the stop's bound (step_launch_wait_s + stop_seconds + 90 s), or down cannot "
            "finish"
        )
    return settings


def _whole(limits: Mapping[str, Any], key: str) -> int:
    value = limits.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SettingsError(f"the platform template's sandbox.limits.{key}: must be a whole number of bytes")
    return value


def agent_limits(template: Optional[Path]) -> Optional[AgentLimits]:
    """The agent's limits from the layout's platform template, when it declares the Apptainer sandbox (`[sandbox]
    backend = "apptainer"`, M5) and its `[sandbox.limits]`; None for any other template. Each `@...@` placeholder is
    read as 0, as up has not filled it yet; a template that is not TOML even so is refused, naming the file."""
    if template is None:
        return None
    try:
        document = tomllib.loads(PLACEHOLDER.sub("0", template.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        why = type(error).__name__
        raise SettingsError(f"{template.name}: the platform template could not be read ({why})") from None
    sandbox = document.get("sandbox")
    if not isinstance(sandbox, Mapping) or sandbox.get("backend") != "apptainer":
        return None
    limits = sandbox.get("limits")
    if not isinstance(limits, Mapping):
        raise SettingsError("the platform template's [sandbox.limits]: is required for the Apptainer sandbox")
    cpus = limits.get("cpus")
    if isinstance(cpus, bool) or not isinstance(cpus, (int, float)) or not 0 < cpus <= 256:
        raise SettingsError("the platform template's sandbox.limits.cpus: must be a number of cores up to 256")
    return AgentLimits(
        cpus=float(cpus),
        memory_bytes=_whole(limits, "memory_bytes"),
        work_disk_bytes=_whole(limits, "work_disk_bytes"),
        tmp_bytes=_whole(limits, "tmp_bytes"),
    )


# -- the site -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Site:
    """The site's values, from `site.env` through the environment at `up`; recorded in the state file after."""

    account: str
    partition: str
    images: str  # SH: the rootfs tarballs and their .provenance
    logs: str  # L: the service steps' logs, on a filesystem the login node and the nodes see
    # PATH inside every step. The sbin directories hold ss (the bundle's port checks) on the site's nodes: without them
    # the zone's step failed in its first second (the first live slot).
    step_path: str = STEP_PATH
    node_tmp: str = "/tmp"  # the node's tmpfs: TMPDIR inside every step, never Slurm's per-job TMPDIR
    node_python: str = "python3.11"  # the nodes' Python for the stdlib helpers (the bundle's HPCOP_PY)
    core_python: str = ""  # the core's Python on the nodes (a venv's), for [hpc] core = "step"
    admin_mail: str = "admin@example.org"
    billing_weight: Optional[float] = None  # the site's billing weight per GB of memory, for describe
    # The agent's Apptainer sandbox (M5), for a platform template that names them (`hpcpins`): the SIF and the egress
    # shim built for it on the filesystem the nodes see, oneclient's bundle (oneclient_bundle.py) and the 443 mapper
    # (the gateport, built for the SIF: the preload of a layout with a REST level), each with the SHA-256 it is
    # declared to have, and the sandbox's work_root on the agent's node's tmpfs. Empty when the site declares none.
    agent_sif: str = ""
    agent_sif_sha256: str = ""
    egress_shim: str = ""
    egress_shim_sha256: str = ""
    oneclient_bundle: str = ""
    oneclient_bundle_sha256: str = ""
    gateport: str = ""
    gateport_sha256: str = ""
    agent_work_root: str = ""
    # The directory of the study's own artifacts (HPC_STUDY_ARTIFACTS), which `@study_artifacts@` stands for in a
    # template: `up` takes the digest of its tree in its preflight and records it, with this value, in the state file,
    # and the render identity holds the digest (never the path); empty when the site declares none, and then a
    # template that names it is refused.
    study_artifacts: str = ""
    # The providers' storage on the cluster's shared scratch filesystem and the guard of its inode quota (hpcstorage,
    # hpcquota; README, "Provider storage on scratch"). `storage_root` is the directory below which `up` makes the
    # federation's own storage; with none, nothing is made on scratch and no check runs. `quota_cmd` is the site's
    # command that prints the account's inode usage and quota (split as a shell would, run without one),
    # `quota_pattern` the regular expression with the named groups `used` and `quota` that reads its output. With a
    # scratch root the providers' storage is bound onto scratch and the check is mandatory: `quota_check` "off" is
    # refused with it, and changes nothing without it. Only `storage_root` is recorded in the state file: `down` needs
    # it.
    storage_root: str = ""
    quota_cmd: str = ""
    quota_pattern: str = ""
    quota_check: str = ""
    # The guard of the account's home (hpcquota.home_guard; README, "The account's home"). `home_quota_cmd` is the
    # site's command that prints the home's usage and limits, `home_quota_pattern` the regular expression with the
    # named groups `files_used`, `files_limit`, `bytes_used` and `bytes_limit` that reads its output. The check runs
    # whenever either is set, with or without a scratch root, and refuses when the home's files or bytes are past 95%
    # of the limit; `home_quota_check` "off" skips it. None of the three is recorded in the state file: `down` needs
    # none, and the command line names the user and the home's path.
    home_quota_cmd: str = ""
    home_quota_pattern: str = ""
    home_quota_check: str = ""
    # srun's `--network=<value>` on every step the driver launches, when not empty (HPC_STEP_NETWORK). On a site whose
    # switch plugin gives each job a virtual network identifier, a heterogeneous job's steps on component 0 were refused
    # ("Error configuring interconnect") from a point on while the same steps with `no_vni` ran; the services talk over
    # TCP and need none. Empty: no option, as before.
    step_network: str = ""

    def to_json(self) -> dict:
        return {
            "account": self.account,
            "partition": self.partition,
            "images": self.images,
            "logs": self.logs,
            "step_path": self.step_path,
            "node_tmp": self.node_tmp,
            "node_python": self.node_python,
            "core_python": self.core_python,
            "admin_mail": self.admin_mail,
            "billing_weight": self.billing_weight,
            "step_network": self.step_network,
            **{attribute: getattr(self, attribute) for attribute in OPTIONAL_VARIABLES.values()},
            "storage_root": self.storage_root,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Site":
        return checked_site(cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data}))


def _absolute(value: str, key: str) -> str:
    if (
        not value
        or not os.path.isabs(value)
        or ".." in value.split("/")
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", value)
    ):
        raise SettingsError(f"{key}: must be an absolute path of letters, digits, '_', '.', '/' and '-', with no '..'")
    return os.path.normpath(value)


def checked_site(site: Site) -> Site:
    """Refuse a site value the driver cannot pass safely to a step; the message names the variable, never its value."""
    if not SLURM_NAME.fullmatch(site.account):
        raise SettingsError("HPC_ACCOUNT: must be a Slurm account name")
    if not SLURM_NAME.fullmatch(site.partition):
        raise SettingsError("HPC_PARTITION: must be a Slurm partition name")
    _absolute(site.images, "SH")
    _absolute(site.logs, "L")
    _absolute(site.node_tmp, "HPC_NODE_TMP")
    for entry in site.step_path.split(":"):
        _absolute(entry, "HPC_STEP_PATH")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", site.node_python) or (
        "/" in site.node_python and not (os.path.isabs(site.node_python) and ".." not in site.node_python.split("/"))
    ):  # every step starts in / (`--chdir=/`): a relative path would name another interpreter there
        raise SettingsError("HPCOP_PY: must be a command name or an absolute path")
    if site.core_python:
        _absolute(site.core_python, "HPC_CORE_PYTHON")
    if not EMAIL.fullmatch(site.admin_mail):
        raise SettingsError("HPC_ADMIN_MAIL: must be an e-mail address")
    weight = site.billing_weight
    if weight is not None and (isinstance(weight, bool) or not isinstance(weight, (int, float)) or weight <= 0):
        raise SettingsError("HPC_BILLING_MEM_WEIGHT: must be a positive number")
    if not valid_network(site.step_network):
        raise SettingsError(
            "HPC_STEP_NETWORK: must be an option of srun's --network (for example 'no_vni'): up to 128 letters, "
            "digits and _ . , = : + -, with no space"
        )
    for variable, attribute in {**AGENT_PATHS, **STUDY_PATHS}.items():
        if getattr(site, attribute):
            _absolute(getattr(site, attribute), variable)
    for variable, attribute in AGENT_PINS.items():
        if getattr(site, attribute) and not SHA256.fullmatch(getattr(site, attribute)):
            raise SettingsError(f"{variable}: must be 64 lower-case hexadecimal digits")
    if site.storage_root:
        _absolute(site.storage_root, "HPC_STORAGE_ROOT")
    if site.quota_check not in ("", QUOTA_CHECK_OFF):
        raise SettingsError(f"HPC_QUOTA_CHECK: must be '{QUOTA_CHECK_OFF}' or unset (unset is not off)")
    if site.home_quota_check not in ("", QUOTA_CHECK_OFF):
        raise SettingsError(f"HPC_HOME_QUOTA_CHECK: must be '{QUOTA_CHECK_OFF}' or unset (unset is not off)")
    if site.storage_root and site.quota_check == QUOTA_CHECK_OFF:
        raise SettingsError(
            f"HPC_QUOTA_CHECK={QUOTA_CHECK_OFF} with HPC_STORAGE_ROOT: a federation whose provider storage is bound "
            "onto scratch always runs the inode check, which cannot be turned off there; unset HPC_QUOTA_CHECK, or "
            "unset HPC_STORAGE_ROOT for storage on the nodes' tmpfs"
        )
    return site


def queue_timeout_from_environment(environ: Mapping[str, str]) -> Optional[float]:
    """`HPC_QUEUE_TIMEOUT_S`: the seconds `up` waits for a held job to start before it cancels it (README, "Site
    configuration"). None when it is unset or empty: the driver's own bound stands. A value that is not a finite number
    above 0 is refused, naming the key and never the value. It is not a `Site` value: only `up` waits, and `down` needs
    no bound."""
    value = environ.get("HPC_QUEUE_TIMEOUT_S")
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        seconds = math.nan
    if not math.isfinite(seconds) or seconds <= 0:
        raise SettingsError("HPC_QUEUE_TIMEOUT_S: must be a number of seconds above 0")
    return seconds


def site_from_environment(environ: Mapping[str, str]) -> Site:
    """The site's values from the environment (`set -a; . site.env; set +a`); SettingsError names a missing one."""
    for key in ("HPC_ACCOUNT", "HPC_PARTITION", "SH", "L"):
        if not environ.get(key) or "<" in environ[key]:
            raise SettingsError(f"{key} is not set: fill in site.env and export it (set -a; . site.env; set +a)")
    weight = environ.get("HPC_BILLING_MEM_WEIGHT")
    try:
        billing = float(weight) if weight else None
    except ValueError:
        raise SettingsError("HPC_BILLING_MEM_WEIGHT: must be a positive number") from None
    if billing is not None and not math.isfinite(billing):
        raise SettingsError("HPC_BILLING_MEM_WEIGHT: must be a positive number")
    defaults = Site("", "", "", "")
    return checked_site(
        Site(
            account=environ["HPC_ACCOUNT"],
            partition=environ["HPC_PARTITION"],
            images=environ["SH"],
            logs=environ["L"],
            step_path=environ.get("HPC_STEP_PATH") or defaults.step_path,
            node_tmp=environ.get("HPC_NODE_TMP") or defaults.node_tmp,
            node_python=environ.get("HPCOP_PY") or defaults.node_python,
            core_python=environ.get("HPC_CORE_PYTHON") or "",
            admin_mail=environ.get("HPC_ADMIN_MAIL") or defaults.admin_mail,
            billing_weight=billing,
            step_network=environ.get("HPC_STEP_NETWORK") or "",
            **{attribute: environ.get(variable) or "" for variable, attribute in OPTIONAL_VARIABLES.items()},
            storage_root=environ.get("HPC_STORAGE_ROOT") or "",
            quota_cmd=environ.get("HPC_QUOTA_CMD") or "",
            quota_pattern=environ.get("HPC_QUOTA_PATTERN") or "",
            quota_check=environ.get("HPC_QUOTA_CHECK") or "",
            home_quota_cmd=environ.get("HPC_HOME_QUOTA_CMD") or "",
            home_quota_pattern=environ.get("HPC_HOME_QUOTA_PATTERN") or "",
            home_quota_check=environ.get("HPC_HOME_QUOTA_CHECK") or "",
        )
    )
