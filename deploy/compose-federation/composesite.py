"""composesite.py: a host's Compose federations as one site of the elastic fleet (README.md, "A Compose site of the
elastic fleet"). The controller (`deploy/hpc-federation/fleetcontrol.py`, a `kind = "compose"` site) runs each
command here over ssh, from the site's directory (`--fleet`, 0700, one per campaign) with its env file sourced.

    <python> -I -B composesite.py observe --fleet F --site S --channel C [--horizon-s N] --member NAME=ROOT ...
    <python> -I -B composesite.py launch --fleet F --member NAME --root ROOT --federation LABEL --channel DIR
                                         --study DIR --home DIR
    <python> -I -B composesite.py stop --fleet F --member NAME
    <python> -I -B composesite.py identity --fleet F --root ROOT --study DIR
    <python> -I -B composesite.py collect --root ROOT --into DIR
    <python> -I -B composesite.py retired --fleet F --member NAME --channel DIR [--drained]
    <python> -I -B composesite.py undrain --fleet F --member NAME

Each takes `--engine` after its name (the site's setting, `EXPCORE_FED_ENGINE`; docker by default). The members are
federations the operator brought up with `federation.py up` (static members: none is ever brought up or taken down
here).

- **observe** reads each member, and writes nothing: live when every service its `facts.json` names has a running
  container of the root's project (the root's mark names it) bound to that root; linked when its `link-check.json` says
  ok; verified when linked and every space its `platform.toml` declares data for (`data_manifest`) has a marker in
  `data-verified.json` naming its id and its manifest's SHA-256 (`data_verified`: fail closed; no Compose command writes
  one yet, so a member with declared data never joins); its worker, by the pidfile `launch` wrote for this campaign's
  channel. A member is `up` and `RUNNING` while live and not yet launched, or launched with its worker running and its
  project there (a service merely stopped, as an injected outage stops one, keeps it so); `GONE` (proven ended) once
  launched and no process of its worker is left (`worker_gone`), whether the project is up or not; `BROKEN` when its
  project is down (a service with no container) and the worker still runs (drained, then retired, which stops it);
  `down` when it is not live and was never launched, or was retired for this channel. Its job id is `compose-<project>`,
  the same at every read, and its end `--horizon-s` from now: a member has no walltime. The site reports no node, so the
  controller never adds to it.
- **launch** starts the elastic worker once, detached in a session of its own, with an environment of the allowlist
  alone and HOME the site's `--home` (a 0700 directory of this user holding the study's key tree at its models.toml
  paths, placed by the operator: never copied, never read here), in `<fleet>/members/<name>/run/` (the study copied
  with the root's `platform.toml` beside it, `work`, `tmp`). Every process it starts carries `WORKER_TAG=<tag>` in its
  environment, the tag in the pidfile (`worker.json`, with the pid, its start tick and the channel). A worker that
  ends within `SETTLE_S` is no launch: the pidfile goes, and the join waits.
- **stop** ends every process carrying the member's tag (TERM, then after `--grace-s` KILL), and exits 0 only when none
  is left, then removes the worker's `run/` directory. **The proof of a worker's end** is that no process of this
  user carries its tag: a worker killed with SIGKILL leaves its lease run (a session of its own) behind, still
  spending, and that run is still found.
- **identity** prints, as one JSON line, the campaign fingerprint's SHA-256 and the environment identity a start at the
  member would pin (`fleetidentity.PROGRAM`, over a private copy of the study with the root's `platform.toml`).
- **collect** archives the root's evidence (`hpcevidence.archive_root`: the same allow-list and secret check as HPC's,
  `facts.json` the one required file) into `--into` as `shards.py collect --roots-only` does, and prints the manifest's
  SHA-256 alone; the copy is pulled and checked with `shards.py verify-copy` on the controller's host. No marker is
  written in the root: no `down` follows.
- **retired** records, after the worker is proven gone, that the member is retired for this channel, so `observe`
  reports it `down` and the controller proves its end. The federation stays up, for the next campaign (a new
  `--fleet` and channel) or the operator's `down`. With `--drained` (a member retired after a drain: an operator's,
  for maintenance say) no later generation of it joins by itself; a member retired for another reason (its worker
  died) is followed by its next generation once its root reads live and linked.
- **undrain** is the operator's step that brings a drained member back: its next generation may join again.
"""

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
HPC = HERE.parent / "hpc-federation"
if str(HERE) not in sys.path:  # `-I` leaves the script's own directory off the path
    sys.path.insert(0, str(HERE))

import rootclaim  # noqa: E402
from feddocker import ENGINE_ENV, ENGINES, project_containers  # noqa: E402
from fedlayout import ROOT_MARKER  # noqa: E402

FORMAT = "expcore-compose-site/1"
WORKER_TAG = "EXPCORE_COMPOSE_WORKER"  # in every process a worker starts: what the proof of its end looks for
WORKER_MODULE = "expcore.campaign.elastic.worker"
PLATFORM, FACTS, LINKS = "platform.toml", "facts.json", "link-check.json"
PIDFILE, RETIRED, RUN = "worker.json", "retired.json", "run"
# A space's data shown to be its declared manifest: what a Compose root holds to read verified (`data_verified`).
DATA_MARKER, DATA_FORMAT = "data-verified.json", "expcore-compose-data-verified/1"
MEMBER = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
GENERATION = "-g"  # a static member's later generations: <member>-g2, <member>-g3, ... (each a new ledger label)
MAX_GENERATIONS = 9
HORIZON_S = 3650 * 86400.0  # a member has no walltime: its recorded end is never reached in a campaign
SETTLE_S = 20.0  # a worker that ends this soon after its start is no launch
GRACE_S = 120.0  # after TERM, before KILL: a lease run's teardown (its sandbox) takes this long at most
KILL_WAIT_S = 15.0
ALLOWLIST = ("PATH", "LC_ALL", "LANG")
IDENTITY_TIMEOUT_S = 600.0
REFUSED = 2


class SiteError(RuntimeError):
    """The site or a member cannot be read or acted on; the message names the member, never a path."""


def _sibling(name: str) -> ModuleType:
    """A module of `deploy/hpc-federation` by its path (`shards`, `hpcevidence`, `fleetidentity`), without putting
    that directory on `sys.path`."""
    loaded = sys.modules.get(f"compose_site_{name}")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location(f"compose_site_{name}", HPC / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # its dataclasses look their module up by name
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------------------------------------------ processes
def start_tick(pid: int) -> int | None:
    """The process's start time in clock ticks (`/proc/<pid>/stat`, field 22), or None when there is no such process
    or it is a zombie (it ended, and only its exit status is left)."""
    try:
        text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    fields = text[text.rfind(")") + 2 :].split()
    if not fields or fields[0] in ("Z", "X") or len(fields) <= 19 or not fields[19].isdigit():
        return None
    return int(fields[19])


def tagged(tag: str, marker: str | None = None) -> list[int]:
    """Every process of this user that is the worker's: its environment carries `WORKER_TAG=<tag>`, or its command
    line holds `marker` (the worker's run directory, which the worker's and every lease run's command lines name). The
    command line is read even where the environment cannot be (a process that is not dumpable), and names the run
    whatever environment a process was given, so neither hides a lease run. A zombie is no process that can spend."""
    needle = f"{WORKER_TAG}={tag}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid() or start_tick(int(entry.name)) is None:
                continue
            line = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if marker is not None and names_run(line, marker):
            found.append(int(entry.name))
            continue
        try:
            words = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if needle in words:
            found.append(int(entry.name))
    return found


def names_run(cmdline: bytes, run: str) -> bool:
    """Whether a command line is the worker's or a lease run's of the run directory `run`: one of its words is exactly
    the worker's study or work directory there, or a lease's study copy (`<run>/work/lease-<id>/study`), never a file
    below them that another program (an editor, a reader of a log) was given."""
    lease = re.compile(re.escape(run) + r"/work/lease-[0-9]{12}/study")
    words = [word.decode(errors="replace") for word in cmdline.split(b"\0")]
    return any(word in (f"{run}/study", f"{run}/work") or lease.fullmatch(word) for word in words)


def workers_on(root: Path) -> list[int]:
    """Every process of this user that is a Compose worker's on the root, of any campaign: its environment carries a
    worker tag and the root (`worker_env`). What a launch reads where the root holds no claim (C53): a worker
    launched before claims existed holds none."""
    tag, here = f"{WORKER_TAG}=".encode(), f"EXPCORE_FED_ROOT={root}".encode()
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != os.getuid() or start_tick(int(entry.name)) is None:
                continue
            words = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if here in words and any(word.startswith(tag) for word in words):
            found.append(int(entry.name))
    return found


def worker_gone(record: dict) -> bool:
    """The proof of a worker's end: its own process (pid and start tick) is not running, and no process of this user
    is the worker's (`tagged`)."""
    pid, start = record.get("pid"), record.get("start")
    alive = isinstance(pid, int) and start is not None and start_tick(pid) == start
    return not alive and not tagged(record["tag"], record.get("run"))


# ------------------------------------------------------------------------------------------------ records
def member_dir(fleet: Path, member: str) -> Path:
    if not MEMBER.fullmatch(member):
        raise SiteError(f"{member!r} is not a member's label ([a-z0-9-])")
    return fleet / "members" / member


def pidfile(here: Path) -> dict | None:
    """A member's pidfile; a record that names no run directory is given the member's own (`<member>/run`)."""
    record = read_record(here / PIDFILE)
    if record is not None:
        record.setdefault("run", str(here / RUN))
    return record


def read_record(path: Path) -> dict | None:
    """A record of ours (a JSON object of this format, in a file that is no link), or None when there is none."""
    try:
        if path.is_symlink():
            raise SiteError(f"{path.name} is a link, never followed")
        found = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise SiteError(f"{path.name} cannot be read ({type(error).__name__})") from None
    if not isinstance(found, dict) or found.get("format") != FORMAT:
        raise SiteError(f"{path.name} is not a record of this site")
    return found


def write_record(path: Path, record: dict) -> None:
    """A record written whole: a draft beside it, mode 0600, renamed over it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, draft = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"format": FORMAT, **record}, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(draft, path)


def worker_of(fleet: Path, member: str, channel: str) -> dict | None:
    """The member's pidfile for this channel; None when it has none. One of another channel whose worker still runs
    is refused: another campaign's worker holds the member."""
    record = pidfile(member_dir(fleet, member))
    if record is None or record.get("channel") == channel:
        return record
    if not worker_gone(record):
        raise SiteError(f"{member}: a worker of another campaign still runs; stop it first")
    return None


# ------------------------------------------------------------------------------------------------ the root
def project_of(root: Path) -> str | None:
    """The Compose project the root's mark names, or None when the root holds no mark."""
    try:
        text = (root / ROOT_MARKER).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def services_of(root: Path) -> list[str]:
    """The services the root's `facts.json` names: the zone and each provider."""
    try:
        facts = json.loads((root / FACTS).read_text(encoding="utf-8"))
        return ["zone", *sorted(facts["providers"])]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def services_state(root: Path, project: str | None) -> tuple[bool, bool]:
    """(every service of the root runs, every service of the root has a container): containers of its project bound
    to this root. A container of the project bound to another root, or none, is another federation of that name, and
    fails the read. A service stopped (a sanctioned outage stops a provider's container) still has its container."""
    services = services_of(root)
    if project is None or not services:
        return False, False
    containers = project_containers(project)
    if any(c.root != str(root) for c in containers):
        raise SiteError(f"project {project}: a container binds another root than the member's")
    running = {c.service for c in containers if c.state == "running"}
    present = {c.service for c in containers}
    return all(s in running for s in services), all(s in present for s in services)


def live(root: Path, project: str | None) -> bool:
    """Every service of the root runs: what a member's join and its worker's launch need."""
    return services_state(root, project)[0]


def linked(root: Path) -> bool:
    try:
        return json.loads((root / LINKS).read_text(encoding="utf-8")).get("ok") is True
    except (OSError, ValueError, AttributeError):
        return False


def declared_data(root: Path) -> dict[str, tuple[Path, Any]] | None:
    """Every space of the root's `platform.toml` that declares its data (`data_manifest`, in `[[spaces]]` or the
    one-space form), by name: (its manifest's path, its id). None when the file cannot be read: nothing is verified."""
    try:
        data = tomllib.loads((root / PLATFORM).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data.get("spaces") if isinstance(data.get("spaces"), list) else [data.get("space")]
    found = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("data_manifest"):
            path = Path(str(entry["data_manifest"]))
            found[str(entry.get("name"))] = (path if path.is_absolute() else root / path, entry.get("id"))
    return found


def data_verified(root: Path) -> bool:
    """FAIL CLOSED, as an HPC member's verify-data markers are (`fleetobserve.verified`): a member's data is verified
    only when every space its platform file declares data for has an entry in the root's `data-verified.json` naming
    that space's id and the SHA-256 of its manifest as it is now. A platform file that declares no data (a probe of the
    baseline files) is verified once linked; one that cannot be read, a marker missing, of another form, of another
    manifest or another space, is not. Nothing here writes the marker: a member with data joins only once its data is
    shown to be the manifest's."""
    declared = declared_data(root)
    if declared is None:
        return False
    if not declared:
        return True
    try:
        marker = json.loads((root / DATA_MARKER).read_text(encoding="utf-8"))
        spaces = marker["spaces"] if marker.get("format") == DATA_FORMAT else None
        for name, (manifest, space_id) in declared.items():
            entry = spaces.get(name)
            digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
            if (
                not isinstance(entry, dict)
                or entry.get("manifest_sha256") != digest
                or entry.get("space_id") != space_id
            ):
                return False
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    return True


# ------------------------------------------------------------------------------------------------ observe
def channel_dir(channel: str, site: str, member: str) -> str:
    """A member's channel directory: `elastic.channel.federation_dir` of its ledger label `<site>-<member>`."""
    return str(Path(channel) / f"{site}-{member}")


def generation(member: str, k: int) -> str:
    return member if k == 1 else f"{member}{GENERATION}{k}"


def is_retired(fleet: Path, label: str, here: str, worker: dict | None) -> bool:
    """Retired for this channel, its worker (if any) proven gone."""
    record = read_record(member_dir(fleet, label) / RETIRED)
    return record is not None and record.get("channel") == here and (worker is None or worker_gone(worker))


def member_view(fleet: Path, site: str, channel: str, member: str, root: Path, now: float, horizon: float) -> dict:
    """One member (one generation of a configured member) as the controller reads it (the module's docstring). A
    member whose worker was launched for this channel stays `RUNNING` while a service of it is merely stopped (an
    injected outage), and is `BROKEN` only once its project is down (a service with no container, or the root's mark
    gone) while its worker lives. `retired` (dropped from what is printed) says it was retired for this channel."""
    here = channel_dir(channel, site, member)
    project = project_of(root)
    view = {"label": member, "root": str(root), "jobs": [f"compose-{project or member}"], "linked": False,
            "verified": False, "relay": "none", "relay_beat_at": None, "submitted_at": None, "started_at": None,
            "ends_at": None, "state": None, "retired": False, "launched": False}  # fmt: skip
    worker = worker_of(fleet, member, here)
    if is_retired(fleet, member, here, worker):
        return {**view, "status": "down", "retired": True}
    if worker is None and rootclaim.elsewhere(root, fleet):
        return {**view, "status": "down"}  # another campaign's member holds the federation (C53)
    running, present = services_state(root, project)
    joined = linked(root)
    verified = joined and data_verified(root)  # fail closed on a declared data manifest
    if worker is None:
        if not running:
            return {**view, "status": "down"}  # not in the pool: never launched, and not up to join
        return {**view, "status": "up", "state": "RUNNING", "linked": joined, "verified": verified,
                "ends_at": now + horizon}  # fmt: skip
    state = "GONE" if worker_gone(worker) else ("RUNNING" if present else "BROKEN")
    return {**view, "status": "up", "state": state, "linked": joined, "verified": verified, "ends_at": now + horizon,
            "launched": True}  # fmt: skip


def has_records(fleet: Path, label: str) -> bool:
    here = member_dir(fleet, label)
    return (here / PIDFILE).exists() or (here / RETIRED).exists()


def drained(fleet: Path, label: str) -> bool:
    """Whether the generation was retired after a drain and not brought back (`undrain`)."""
    record = read_record(member_dir(fleet, label) / RETIRED)
    return bool(record and record.get("drained"))


def chain(fleet: Path, site: str, channel: str, member: str, root: Path, now: float, horizon: float,
          last: bool) -> list[dict]:  # fmt: skip
    """A configured member's generations, oldest first, ending at its current one. Once a generation is retired for
    this channel, the next is read: one already launched or retired, or, for the last configured member naming the
    root, a new one when the root reads live and linked again, so a federation whose worker died (or that was
    retired) joins again under a new ledger label with its own channel, records and copy. An earlier configured
    member naming the same root never starts a new generation: a later one in the sites file takes over the root."""
    views = []
    for k in range(1, MAX_GENERATIONS + 1):
        view = member_view(fleet, site, channel, generation(member, k), root, now, horizon)
        views.append(view)
        if not view["retired"]:
            return views
        following = generation(member, k + 1)
        if has_records(fleet, following):
            continue
        if drained(fleet, view["label"]) or not (last and live(root, project_of(root)) and linked(root)):
            return views  # a drained one stays out until its operator's undrain
    return views


def observe(args: argparse.Namespace) -> dict:
    """One read of the site, as `expcore.campaign.elastic.control.SiteView` takes it (less its label). Members naming
    one root hold it one at a time, in the sites file's order: a later one is `down` (never joined or launched) until
    the earlier one is retired, and one found launched while another holds its root fails the read."""
    fleet, now = Path(args.fleet), time.time()
    pairs = []
    for given in args.member:
        member, _, root = given.partition("=")
        if not root or not os.path.isabs(root):
            raise SiteError(f"--member takes NAME=ROOT with an absolute root, not {member!r}")
        pairs.append((member, Path(root)))
    lasts = {root: member for member, root in pairs}
    held: dict[Path, str] = {}
    members = []
    for member, root in pairs:
        views = chain(fleet, args.site, args.channel, member, root, now, args.horizon_s, lasts[root] == member)
        current = views[-1]
        if not current["retired"] and root in held:
            if current["launched"]:
                raise SiteError(f"{current['label']}: launched while {held[root]} holds its root")
            views[-1] = {**current, "status": "down", "state": None, "linked": False, "verified": False}
        elif not current["retired"]:
            held[root] = current["label"]
        members += [{k: v for k, v in view.items() if k not in ("retired", "launched")} for view in views]
    return {"idle_nodes": 0, "mixed_nodes": 0, "nodes_per_federation": 0, "time_s": args.horizon_s,
            "inodes_used": None, "inodes_quota": None, "inode_estimate": 0, "inodes_error": None,
            "members": members}  # fmt: skip


# ------------------------------------------------------------------------------------------------ the worker
def private_dir(value: str, what: str) -> Path:
    """A directory of this user, mode 0700, no link."""
    path = Path(value)
    try:
        info = path.lstat()
    except OSError:
        raise SiteError(f"{what} is not there") from None
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SiteError(f"{what} must be a directory of this user of mode 0700, not a link")
    return path


def apart(home: Path, study: Path, fleet: Path) -> None:
    """The key home lies neither in the study (copied for each lease and identity read) nor in the site's directory
    (whose run directories are removed), nor holds either: no key is ever copied or removed."""
    key, *others = (Path(os.path.realpath(path)) for path in (home, study, fleet))
    for other in others:
        if key == other or key.is_relative_to(other) or other.is_relative_to(key):
            raise SiteError("--home overlaps the study or the site's directory: its keys would be copied or removed")


def worker_env(home: Path, run: Path, root: Path, tag: str) -> dict[str, str]:
    env = {k: os.environ[k] for k in ALLOWLIST if k in os.environ}
    env.update(
        HOME=str(home),
        TMPDIR=str(run / "tmp"),
        PYTHONDONTWRITEBYTECODE="1",
        EXPCORE_FED_ROOT=str(root),
        EXPCORE_FED_ROOT_PARENT=str(root.parent),
        **{ENGINE_ENV: os.environ[ENGINE_ENV], WORKER_TAG: tag},
    )
    return env


def stage_study(study: Path, root: Path, run: Path) -> Path:
    """A copy of the study in `run` with the root's `platform.toml` beside its study.toml, as each lease copies it."""
    if (study / "out").exists() or os.path.lexists(study / PLATFORM):
        raise SiteError("the study holds an out/ or a platform.toml of its own")
    copy = run / "study"
    shutil.copytree(study, copy, symlinks=True)
    shutil.copyfile(root / PLATFORM, copy / PLATFORM)
    (copy / PLATFORM).chmod(0o600)
    return copy


def worker_words(federation: str, channel: str, study: Path, work: Path) -> list[str]:
    """The elastic worker's command line, on this interpreter."""
    return [sys.executable, "-I", "-B", "-m", WORKER_MODULE, "--federation", federation, "--channel", channel,
            "--study", str(study), "--work", str(work)]  # fmt: skip


@contextlib.contextmanager
def member_lock(here: Path):
    """One launch, stop or retirement of a member at a time (an exclusive lock on `<member>/lock`)."""
    here.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(here / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def root_holder(fleet: Path, root: Path, member: str) -> str | None:
    """Another member of the site's directory whose worker on this root still runs (whatever the sites file names
    now), or None: one worker per federation."""
    for here in sorted((fleet / "members").glob("*")):
        if here.name == member or not (here / PIDFILE).is_file():
            continue
        record = pidfile(here)
        if record is not None and record.get("root") == str(root) and not worker_gone(record):
            return here.name
    return None


def launch(args: argparse.Namespace) -> int:
    fleet, root, study = Path(args.fleet), Path(args.root), Path(args.study)
    here = member_dir(fleet, args.member)
    home = private_dir(args.home, "--home")
    apart(home, study, fleet)
    claim_lock = rootclaim.locked(root) if root.is_dir() and not root.is_symlink() else contextlib.nullcontext()
    with member_lock(fleet / "launch"), member_lock(here), claim_lock:
        record = worker_of(fleet, args.member, args.channel)
        if record is not None and not worker_gone(record):
            print(f"composesite: {args.member}: its worker already runs (pid {record.get('pid')})")
            return 0
        holder = root_holder(fleet, root, args.member)
        if holder is not None:
            raise SiteError(f"{args.member}: the worker of {holder} still runs on its root; one worker per federation")
        if rootclaim.elsewhere(root, fleet):
            raise SiteError(f"{args.member}: another campaign's member holds its federation; one worker per federation")
        if rootclaim.holder(root) is None and workers_on(root):
            raise SiteError(f"{args.member}: a worker with no claim runs on its federation; one worker per federation")
        if record is not None:
            raise SiteError(f"{args.member}: its worker of this campaign ended; it is retired, never started again")
        if not live(root, project_of(root)) or not (root / PLATFORM).is_file():
            raise SiteError(f"{args.member}: its federation is not up (every service running, platform.toml rendered)")
        run = here / RUN
        if run.exists():
            shutil.rmtree(run)  # no worker of this channel holds it: an earlier campaign's, or a launch that failed
        run.mkdir(mode=0o700)
        for name in ("work", "tmp"):
            (run / name).mkdir(mode=0o700)
        copy = stage_study(study, root, run)
        tag = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
        log = fleet / "logs" / f"{args.member}.worker.log"
        log.parent.mkdir(mode=0o700, exist_ok=True)
        fields = {"tag": tag, "run": str(run), "channel": args.channel, "federation": args.federation,
                  "root": str(root)}  # fmt: skip
        write_record(here / PIDFILE, {**fields, "pid": None, "start": None, "at": time.time()})  # the tag first
        rootclaim.take(root, fleet, args.member, args.channel, args.federation)  # no other campaign's from now on
        words = worker_words(args.federation, args.channel, copy, run / "work")
        with open(log, "ab") as sink:
            child = subprocess.Popen(words, env=worker_env(home, run, root, tag), cwd=run, stdin=subprocess.DEVNULL,
                                     stdout=sink, stderr=subprocess.STDOUT, start_new_session=True)  # fmt: skip
        write_record(here / PIDFILE, {**fields, "pid": child.pid, "start": start_tick(child.pid), "at": time.time()})
        deadline = time.monotonic() + SETTLE_S
        while time.monotonic() < deadline and child.poll() is None:
            time.sleep(min(1.0, SETTLE_S))
        if child.poll() is not None:
            if stop_tagged(tag, 0.0, fields["run"]):  # whatever it started goes with it; else the record stays
                rootclaim.release(root, fleet, args.member)
                (here / PIDFILE).unlink()
            raise SiteError(f"{args.member}: its worker ended at once (status {child.returncode}; see {log.name})")
    print(f"composesite: {args.member}: its worker is launched (pid {child.pid}); its log {log.name}")
    return 0


def stop_tagged(tag: str, grace_s: float, marker: str | None = None) -> bool:
    """TERM every process of the worker (`tagged`), KILL what is left after `grace_s`; True when none is left."""
    for sig, wait in ((signal.SIGTERM, grace_s), (signal.SIGKILL, KILL_WAIT_S)):
        for pid in tagged(tag, marker):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
        deadline = time.monotonic() + wait
        while tagged(tag, marker) and time.monotonic() < deadline:
            time.sleep(0.2)
    return not tagged(tag, marker)


def stop(args: argparse.Namespace) -> int:
    """The worker's every process ended; its run directory is kept (a lease store there may be the only one)."""
    here = member_dir(Path(args.fleet), args.member)
    with member_lock(here):
        record = pidfile(here)
        if record is None:
            print(f"composesite: {args.member}: no worker was launched")
            return 0
        pid, start = record.get("pid"), record.get("start")
        if isinstance(pid, int) and start is not None and start_tick(pid) == start:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
        if not stop_tagged(record["tag"], args.grace_s, record.get("run")) or not worker_gone(record):
            raise SiteError(f"{args.member}: a process of its worker is still running")
    print(f"composesite: {args.member}: its worker is stopped; no process of it is left")
    return 0


def retired(args: argparse.Namespace) -> int:
    here = member_dir(Path(args.fleet), args.member)
    with member_lock(here):
        record = pidfile(here)
        if record is not None and not worker_gone(record):
            raise SiteError(f"{args.member}: its worker still runs: stop it before it is retired")
        write_record(here / RETIRED, {"channel": args.channel, "at": time.time(), "drained": args.drained})
        root = Path(record["root"]) if record is not None and record.get("root") else None
        if root is not None and root.is_dir() and not root.is_symlink():
            with rootclaim.locked(root):  # its worker is gone and retired: another campaign's member may take it
                rootclaim.release(root, Path(args.fleet), args.member)
    print(f"composesite: {args.member}: retired; its federation stays up")
    return 0


def undrain(args: argparse.Namespace) -> int:
    """The operator's step: a member retired after a drain may be followed by its next generation again."""
    here = member_dir(Path(args.fleet), args.member)
    with member_lock(here):
        record = read_record(here / RETIRED)
        if record is None or not record.get("drained"):
            raise SiteError(f"{args.member}: not retired after a drain; nothing to undrain")
        write_record(here / RETIRED, {**{k: v for k, v in record.items() if k != "format"}, "drained": False,
                                      "undrained_at": time.time()})  # fmt: skip
    print(f"composesite: {args.member}: undrained; its next generation may join")
    return 0


# ------------------------------------------------------------------------------------------------ identity, collect
def identity(args: argparse.Namespace) -> int:
    """The member's (fingerprint, environment identity), read in a private copy of the study, as one JSON line."""
    root, fleet, study = Path(args.root), Path(args.fleet), Path(args.study)
    apart(private_dir(args.home, "--home"), study, fleet)
    fleet.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="identity-", dir=fleet))
    try:
        for name in ("home", "tmp"):
            (scratch / name).mkdir(mode=0o700)
        copy = stage_study(study, root, scratch)
        env = {k: os.environ[k] for k in ALLOWLIST if k in os.environ}
        env.update(HOME=str(scratch / "home"), TMPDIR=str(scratch / "tmp"), EXPCORE_FED_ROOT=str(root),
                   EXPCORE_FED_ROOT_PARENT=str(root.parent))  # fmt: skip
        done = subprocess.run([sys.executable, "-I", "-B", "-c", _sibling("fleetidentity").PROGRAM, str(copy)],
                              cwd=scratch, env=env, capture_output=True, text=True, timeout=IDENTITY_TIMEOUT_S,
                              check=False)  # fmt: skip
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if done.returncode != 0:
        raise SiteError(f"the identity could not be read (status {done.returncode})")
    print(done.stdout.strip().splitlines()[-1])
    return 0


def collect(args: argparse.Namespace) -> int:
    """The root's evidence into `--into` as a root-only copy; its manifest's SHA-256 alone on stdout."""
    shards = _sibling("shards")
    try:
        return collect_into(args, shards, _sibling("hpcevidence"))
    except shards.ShardsError as error:
        raise SiteError(str(error)) from None


def copy_of(into: Path, root: Path, shards: ModuleType, evidence: ModuleType) -> str:
    """The SHA-256 of the manifest of the whole copy of THIS root that `into` holds (a retry); SiteError for anything
    else there, which is never replaced: another root's copy, a part of one, a pair that is not one another's."""
    manifest = into / shards.ARCHIVES_MANIFEST
    try:
        digest = hashlib.sha256(manifest.read_bytes()).hexdigest()
        whole = not shards.verify_copy(manifest, into, digest).problems
        stem = shards.ROOT_ONLY_STEM
        evidence.verified_pair(into, stem, stem, shards.root_id_for(root), required=(FACTS,))
    except (OSError, ValueError, evidence.EvidenceError, shards.ShardsError):
        whole = False
    if not whole:
        raise SiteError("the directory to collect into holds something that is not this root's whole copy")
    return digest


def collect_into(args: argparse.Namespace, shards: ModuleType, evidence: ModuleType) -> int:
    root = Path(os.path.abspath(args.root))
    if root.is_symlink() or not (root / ROOT_MARKER).is_file() or not (root / FACTS).is_file():
        raise SiteError("the root is not a Compose federation's (its mark and facts.json)")
    into = shards.copy_directory(root, Path(args.into))
    if into.exists() and any(into.iterdir()):
        print(copy_of(into, root, shards, evidence))  # a retry: the copy is there, whole
        return 0
    into.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".collect-", dir=into.parent))
    try:
        held = evidence.archive_root(root, shards.ROOT_ONLY_STEM, shards.root_id_for(root), required=(FACTS,))
        payload = shards.root_only_payload(held)
        digest = hashlib.sha256(payload).hexdigest()
        stem = shards.ROOT_ONLY_STEM
        for name, data in ((f"{stem}.root.tar", held.tar), (f"{stem}.root.json", held.manifest),
                           (shards.ARCHIVES_MANIFEST, payload)):  # fmt: skip
            (stage / name).write_bytes(data)
            (stage / name).chmod(0o600)
        if shards.verify_copy(stage / shards.ARCHIVES_MANIFEST, stage, digest).problems:
            raise SiteError("the copy written does not match its own manifest (nothing is kept)")
        os.rename(stage, into)
    except evidence.EvidenceError as error:
        raise SiteError(str(error)) from None
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    print(digest)
    return 0


# ------------------------------------------------------------------------------------------------ the command line
def parser() -> argparse.ArgumentParser:
    found = argparse.ArgumentParser(prog="composesite.py", description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)  # every command takes --engine, after its name
    common.add_argument("--engine", choices=ENGINES, default="docker")
    sub = found.add_subparsers(dest="command", required=True)
    commands = {"observe": ("fleet", "site", "channel"), "stop": ("fleet", "member"),
                "launch": ("fleet", "member", "root", "federation", "channel", "study", "home"),
                "identity": ("fleet", "root", "study"), "collect": ("root", "into"),
                "retired": ("fleet", "member", "channel"), "undrain": ("fleet", "member")}  # fmt: skip
    made = {}
    for name, keys in commands.items():
        made[name] = sub.add_parser(name, parents=[common])
        for key in keys:
            made[name].add_argument(f"--{key}", required=True)
    made["observe"].add_argument("--horizon-s", dest="horizon_s", type=float, default=HORIZON_S)
    made["observe"].add_argument("--member", action="append", default=[])
    made["stop"].add_argument("--grace-s", dest="grace_s", type=float, default=GRACE_S)
    made["retired"].add_argument("--drained", action="store_true")
    made["identity"].add_argument("--home", required=True)  # checked apart from the study before it is copied
    return found


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    os.environ[ENGINE_ENV] = args.engine  # every call to the engine, here and in feddocker, names this one
    commands: dict[str, Any] = {"launch": launch, "stop": stop, "identity": identity, "collect": collect,
                                "retired": retired, "undrain": undrain}  # fmt: skip
    try:
        if args.command == "observe":
            print(json.dumps(observe(args), sort_keys=True))
            return 0
        return commands[args.command](args)
    except (SiteError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"error: {args.command}: {error}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    sys.exit(main())
