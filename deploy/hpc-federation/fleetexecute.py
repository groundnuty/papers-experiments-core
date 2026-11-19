"""fleetexecute.py: what the elastic fleet's controller does with a decision (#462 step 5, C43; `fleetcontrol.py`).

Every action is the call an operator makes by hand, rendered once (`render`): what the dry run prints is exactly what
the executor runs. On a site's login node: `fleet.py --fleet F add K`; a retirement's `shards.py collect --roots-only
--root R --into DIR`, `shards.py verify-copy DIR/archives.sha256.json DIR --manifest-sha256 <the hash collect
printed>` and `fleet.py --fleet F down --member fed-k`, in that order, each only after the one before succeeded. On the
orchestrating host, in the ledger: `fleetcontrol.py drain`, `ended`, `join` and `settle`, which the executor calls in
process.

- **The ledger first.** Proven ends, settlements, drains and joins run in the ledger at once, in the decision's order.
  A lost lease is settled to the spend its dead federation's call log shows, read through the `spend` reader:
  `dead_store_reader(locate, study)` over step 4's `dead_store_spend`, whose `locate` (where the dead store is) step 6
  gives; until then none is read, and the lease stays charged its grant, logged once and read again every tick. A
  read that is None, of another lease, or without its charged attempts settles nothing: a settle always carries the
  attempts, or the lease's run would not be final. A retirement
  is checked against the ledger again just before it starts: a federation in the ledger must be drained or proven
  ended and hold no lease that is issued or sent; one that is not in the ledger must still not be. A proven end
  `cancelled` comes only on a later tick, once the fleet's record shows the down verified.
- **Never `--discard-evidence`, never `scancel`.** A down goes through `fleet.py`, whose `hpcfed.py down` refuses a
  root with evidence not collected (its gate), and cancels only the jobs that member's up recorded, by id.
- **One operation per site and per member at a time.** An add (one per site) and a retirement (one per member) run in
  the background; until each ends its key is busy, and the decision gives that site no add and that member nothing.
- **A member's prepare** (`fleetprepare.py`, when the site names `prepare`): one call at the site per member, the
  study's own command bounded and logged there; the controller's log keeps its outcome, exit status, times and
  duration, never its output, argv or a path. A retirement of a member found with a prepare started and never ended
  begins with `fleetprepare.py abort`.
- **The copy back.** A local site's commands run on this host (`transport = "local"`, the login node itself), so the
  collected copy is already here and `verify-copy` checks it. A site whose `transport` is `ssh` runs every command over
  ssh (`fleetssh.py`): its retirement is collect (into `<fleet>/collected/<member>` at the site), the pull of that copy
  to `<evidence>/<site>-<member>` here, the pull of the member's channel places (`<channel>/<label>/places/`, when the
  site has a channel: the newest snapshots and journal of each lease's place, into
  `<evidence>/<site>-<member>/places/`, which the settle reads when its channel's place is gone), `verify-copy` on
  this host over the pulled copy against collect's hash, and only then `down`; a place that cannot be pulled stops it
  before `down`, as an evidence pull does. One at a site is refused while the ledger holds a lease of its federation
  that is `reported` (its archive not yet pulled here).
"""

import json
import re
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from fleetssh import SshSiteTransport, redact

HERE = Path(__file__).resolve().parent
DIGEST = "<the hash collect printed>"  # the dry run's stand-in for collect's output, which verify-copy takes
FINGERPRINT, ENVIRONMENT = "<its campaign fingerprint>", "<its environment identity, a JSON file>"
SPEND = "<the spend its call log shows, micro-dollars>"
ATTEMPTS = "<the charged attempts its store shows>"
ARCHIVES_MANIFEST = "archives.sha256.json"  # `shards.ARCHIVES_MANIFEST`
COLLECTED = (
    "collected"  # at an ssh site, in its fleet's directory: <COLLECTED>/<member> holds a member's collected copy
)
SITE, HERE_PLACE, PULL = "site", "here", "pull"  # where a rendered step runs: over the site's transport, here, a pull
PULL_PLACES = "pull-places"  # ... or the pull of the member's channel places (`SshSiteTransport.pull_places`)
HEX64 = re.compile(r"[0-9a-f]{64}")
COMMAND_TIMEOUT_S = 6 * 3600.0  # the fleet's commands bound their own waits (an up's queue wait, a down's steps)
LAUNCH_TIMEOUT_S = 300.0  # a worker's launch only spawns a detached srun client: it never holds the tick for long
PREPARE_GRACE_S = 120.0  # the call's own bound beyond the prepare's timeout, which `fleetprepare.py` enforces itself
PREPARE_TRIES = 3  # calls that fail with no outcome (the wrapper never ran, or never claimed) before the member is lost
PREPARE_OUTCOMES = ("ok", "failed", "timeout", "aborted")


@dataclass(frozen=True)
class SiteConfig:
    """A site as the controller reaches it: its settings, its fleet's directory, how its commands run, and the
    interpreter and deploy directory on its login node."""

    settings: Any  # expcore.campaign.elastic.control.SiteSettings
    fleet: Path
    transport: str = "local"
    python: str = sys.executable
    deploy: Path = HERE
    channel: Optional[Path] = None  # the site's channel root on its shared file system, when the dispatcher serves it
    host: Optional[str] = field(default=None, repr=False)  # an ssh site's destination or alias: never shown or logged
    env_file: Optional[str] = None  # an ssh site's site.env, a path on its login node, sourced before each command
    shard_settings: Optional[Path] = None  # an ssh site's shard settings file, a path on its login node
    prepare: Optional[Tuple[str, ...]] = None  # the study's prepare command (an argv, `{root}` and `{layout}` in it)

    @property
    def label(self) -> str:
        return self.settings.label

    @property
    def remote(self) -> bool:
        return self.transport == "ssh"

    def script(self, name: str, *words: str) -> List[str]:
        return [self.python, "-I", "-B", str(self.deploy / name), *words]


@dataclass(frozen=True)
class Rendered:
    """An action as commands: `where` is `site` (the site's login node) or `ledger` (this host); `steps` in order."""

    where: str
    steps: Tuple[Tuple[str, ...], ...]
    site_steps: Tuple[Tuple[str, ...], ...] = ()  # a join's: the worker's launch on the site, before the ledger's join
    places: Tuple[
        str, ...
    ] = ()  # each step's place (`SITE`, `HERE_PLACE`, `PULL`, `PULL_PLACES`); none given: every step is the site's


def evidence_dir(evidence: Path, site: str, member: str) -> Path:
    """Where a member's root evidence is collected: one directory per member, so a retry finds its own copy."""
    return evidence / f"{site}-{member}"


def render(action: Any, site: SiteConfig, evidence: Path, ledger: Path, digest: str = DIGEST,
           settings: Optional[Path] = None) -> Rendered:  # fmt: skip
    """The commands of one action, exactly as the dry run prints them and the executor runs them. A join also
    launches the federation's worker (`fleetworker.py launch`, on the site) when the site has a channel and the shard
    `settings` are given."""
    kind = type(action).__name__
    # the ledger is this host's: its commands are run here, with this host's interpreter, never the login node's
    control = (
        [sys.executable, "-I", "-B", str(HERE / "fleetcontrol.py")] if site.remote else site.script("fleetcontrol.py")
    )
    if kind == "Add":
        return Rendered("site", (tuple(site.script("fleet.py", "--fleet", str(site.fleet), "add", str(action.count))),))
    if kind == "Prepare":
        if site.prepare is None or site.settings.prepare_timeout_s is None:
            raise ValueError(f"site {site.label} declares no prepare command")
        words = ("run", "--fleet", str(site.fleet), "--member", action.member, "--timeout",
                 repr(float(site.settings.prepare_timeout_s)), "--", *site.prepare)  # fmt: skip
        return Rendered("site", (tuple(site.script("fleetprepare.py", *words)),))
    if kind == "Retire":
        into = evidence_dir(evidence, site.label, action.member)
        if site.remote:
            return retire_over_ssh(action, site, into, digest)
        steps = []
        if action.abort_prepare:
            steps.append(abort_step(action, site))
        if action.collect:
            steps.append(
                site.script("shards.py", "collect", "--roots-only", "--root", action.root, "--into", str(into))
            )
            steps.append(site.script("shards.py", "verify-copy", str(into / ARCHIVES_MANIFEST), str(into),
                                     "--manifest-sha256", digest))  # fmt: skip
        steps.append(site.script("fleet.py", "--fleet", str(site.fleet), "down", "--member", action.member))
        return Rendered("site", tuple(tuple(step) for step in steps))
    if kind == "Drain":
        return Rendered("ledger", ((*control, "drain", "--ledger", str(ledger), "--label", action.label),))
    if kind == "ProveEnded":
        words = ("ended", "--ledger", str(ledger), "--label", action.label, "--proof", action.proof)
        return Rendered("ledger", ((*control, *words),))
    if kind == "Settle":
        words = ("settle", "--ledger", str(ledger), "--lease", str(action.lease_id), "--spend-micro", SPEND,
                 "--source", "root", "--attempts", ATTEMPTS)  # fmt: skip
        return Rendered("ledger", ((*control, *words),))
    if kind == "Join":
        words = ("join", "--ledger", str(ledger), "--label", action.label, "--site", action.site, "--jobs",
                 ",".join(action.job_ids), "--job-end", repr(action.job_end_at), "--kill-wait",
                 repr(action.kill_wait_s), "--fingerprint", FINGERPRINT, "--environment-file", ENVIRONMENT)  # fmt: skip
        return Rendered("ledger", ((*control, *words),), worker_launch(action, site, settings))
    raise ValueError(f"no rendering for an action of kind {kind}")


def retire_over_ssh(action: Any, site: SiteConfig, into: Path, digest: str) -> Rendered:
    """A retirement at an ssh site: collect into the site's `<fleet>/collected/<member>`, the pull of that copy to
    `into` here, the pull of the member's channel places beside it (a member that joined the ledger, at a site with a
    channel), `verify-copy` on this host over the pulled copy, then `down`; each only after the one before."""
    steps: List[Tuple[str, ...]] = []
    places: List[str] = []
    if action.abort_prepare:
        steps.append(abort_step(action, site))
        places.append(SITE)
    if action.collect:
        there = site.fleet / COLLECTED / action.member
        steps.append(tuple(site.script("shards.py", "collect", "--roots-only", "--root", action.root, "--into",
                                       str(there))))  # fmt: skip
        steps.append((PULL, str(there), str(into), digest))
        places += [SITE, PULL]
        if site.channel is not None and action.label is not None:  # a member that never joined held no lease
            from expcore.campaign.elastic import channel as ch

            steps.append((PULL_PLACES, str(ch.federation_dir(str(site.channel), action.label) / ch.PLACES),
                          str(into / ch.PLACES)))  # fmt: skip
            places.append(PULL_PLACES)
        steps.append((sys.executable, "-I", "-B", str(HERE / "shards.py"), "verify-copy", str(into / ARCHIVES_MANIFEST),
                      str(into), "--manifest-sha256", digest))  # fmt: skip
        places.append(HERE_PLACE)
    steps.append(tuple(site.script("fleet.py", "--fleet", str(site.fleet), "down", "--member", action.member)))
    return Rendered("site", tuple(steps), places=(*places, SITE))


def abort_step(action: Any, site: SiteConfig) -> Tuple[str, ...]:
    """The end of a prepare found started and never ended, before its member is collected and taken down."""
    return tuple(site.script("fleetprepare.py", "abort", "--fleet", str(site.fleet), "--member", action.member))


def worker_launch(action: Any, site: SiteConfig, settings: Optional[Path]) -> Tuple[Tuple[str, ...], ...]:
    """A joining federation's worker, launched on its site before the join (idempotent: one already running is not
    launched again): its ledger label, its channel under the site's channel root, and the recorded end of its job."""
    if site.channel is None or settings is None:
        return ()
    from expcore.campaign.elastic import channel as ch

    settings = site.shard_settings if site.remote and site.shard_settings is not None else settings  # the login node's
    words = ("launch", "--fleet", str(site.fleet), "--member", action.member, "--settings", str(settings),
             "--federation", action.label, "--channel", str(ch.federation_dir(str(site.channel), action.label)),
             "--job-end", repr(action.job_end_at))  # fmt: skip
    return (tuple(site.script("fleetworker.py", *words)),)


def shown(site: Optional[SiteConfig], argv: Sequence[str]) -> List[str]:
    """An argv as it is logged: an ssh site's host removed from any word that holds it (not from the words sent)."""
    return [redact(site, word) if site is not None and site.remote else word for word in argv]


def said_of(site: Optional[SiteConfig], err: str, out: str) -> str:
    """The last line of what a command said, as it is logged."""
    said = last_line(err or out)
    return redact(site, said) if site is not None and site.remote else said


def described(action: Any, rendered: Rendered, site: Optional[SiteConfig] = None) -> Dict[str, Any]:
    """An action for the log and the dry run: its kind, site, member, reason and commands (no ssh site's host)."""
    steps = [shown(site, step) for step in rendered.steps]
    if site is not None and type(action).__name__ == "Prepare":  # printed in a dry run, never logged
        steps = [["<the site's prepare command: not logged>"]]
    entry = {"action": type(action).__name__, "site": action.site, "member": getattr(action, "member", None),
             "label": getattr(action, "label", None), "reason": action.reason, "where": rendered.where,
             "site_steps": [shown(site, step) for step in rendered.site_steps], "steps": steps}  # fmt: skip
    if rendered.places:
        entry["places"] = list(rendered.places)
    return entry


def printed(entry: Mapping[str, Any]) -> str:
    """One action as the dry run prints it: where it runs, then each command, one per line."""
    where = f"on {entry['site']}'s login node" if entry["where"] == "site" else "in the ledger, on this host"
    lines = [f"# {entry['action']} {entry['label'] or entry['member'] or entry['site']} ({where}): {entry['reason']}"]
    if entry["site_steps"]:
        lines.append(f"# first, on {entry['site']}'s login node:")
        lines += [shlex.join(step) for step in entry["site_steps"]]
        lines.append("# then, in the ledger:")
    places = entry.get("places") or [SITE] * len(entry["steps"])
    notes = {PULL: "# pulled to this host over ssh (checked against collect's hash):",
             PULL_PLACES: "# its channel's places, pulled to this host over ssh (one that cannot be pulled stops it):",
             HERE_PLACE: "# on this host, over the pulled copy:"}  # fmt: skip
    for step, place in zip(entry["steps"], places):
        lines += ([notes[place]] if place in notes else []) + [shlex.join(step)]
    return "\n".join(lines)


class LocalTransport:
    """Each site command run on this host (the site's login node), with this process's environment (site.env
    exported), from the fleet's own 0700 directory, never a shared one."""

    def __init__(self, environ: Mapping[str, str], run: Callable[..., Any] = subprocess.run) -> None:
        self.environ = dict(environ)
        self._run = run

    def run(self, site: SiteConfig, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        done = self._run(list(argv), env=self.environ, cwd=str(site.fleet), stdin=subprocess.DEVNULL,
                         capture_output=True, text=True, timeout=timeout, check=False)  # fmt: skip
        return done.returncode, done.stdout, done.stderr

    def run_here(self, argv: Sequence[str], cwd: Path, timeout: float) -> Tuple[int, str, str]:
        """A command of this host's own (a retirement's `verify-copy` over a pulled copy), from `cwd`."""
        done = self._run(list(argv), env=self.environ, cwd=str(cwd), stdin=subprocess.DEVNULL, capture_output=True,
                         text=True, timeout=timeout, check=False)  # fmt: skip
        return done.returncode, done.stdout, done.stderr


class SiteTransport:
    """Each site's commands as its `transport` says: run here (`local`) or over ssh (`ssh`, `fleetssh.py`)."""

    def __init__(self, environ: Mapping[str, str], run: Callable[..., Any] = subprocess.run,
                 ssh: Optional[Any] = None) -> None:  # fmt: skip
        self.local = LocalTransport(environ, run)
        self.ssh = ssh or SshSiteTransport(environ)

    def run(self, site: SiteConfig, argv: Sequence[str], timeout: float) -> Tuple[int, str, str]:
        return (self.ssh if site.remote else self.local).run(site, argv, timeout)

    def run_here(self, argv: Sequence[str], cwd: Path, timeout: float) -> Tuple[int, str, str]:
        return self.local.run_here(argv, cwd, timeout)

    def pull(self, site: SiteConfig, remote: str, local: Path, digest: str) -> Tuple[int, str, str]:
        return self.ssh.pull(site, remote, local, digest)

    def exists(self, site: SiteConfig, flag: str, path: str) -> bool:
        """Whether a path is there at an ssh site (`fleetssh.SshSiteTransport.exists`); a call that fails raises."""
        return self.ssh.exists(site, flag, path)

    def names(self, site: SiteConfig, directory: str, pattern: Any) -> List[str]:
        """The names in a directory at an ssh site that `pattern` matches (`fleetssh.SshSiteTransport.names`)."""
        return self.ssh.names(site, directory, pattern)

    def pull_places(self, site: SiteConfig, remote: str, local: Path) -> Tuple[int, str, str]:
        """An ssh site's member's channel places into a new directory here (`fleetssh.SshSiteTransport.pull_places`)."""
        return self.ssh.pull_places(site, remote, local)

    def stream(self, site: SiteConfig, argv: Sequence[str], local: Path, timeout_s: Optional[float] = None) -> None:
        """An ssh site's command's standard output into a new file here (`fleetssh.SshSiteTransport.stream`)."""
        self.ssh.stream(site, argv, local, timeout_s)


def prepare_outcome(out: str) -> Dict[str, Any]:
    """The line `fleetprepare.py run` printed (its outcome, exit status, times and duration), each field checked and
    nothing else kept; empty when there is none or it is not that."""
    lines = [line for line in out.splitlines() if line.strip()]
    try:
        found = json.loads(lines[-1]) if lines else None
    except ValueError:
        return {}
    if not isinstance(found, dict) or found.get("outcome") not in PREPARE_OUTCOMES:
        return {}
    code = found.get("exit")
    keep: Dict[str, Any] = {"outcome": found["outcome"], "exit": code if isinstance(code, int) else None}
    for key in ("started_at", "ended_at", "duration_s"):
        value = found.get(key)
        keep[key] = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    return keep


def last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    said = (lines or ["nothing"])[-1]
    return "".join(c if c.isprintable() else "?" for c in said)[-300:]


def dead_store_reader(locate: Callable[[Any, Any], Optional[Tuple[Path, str]]], study: Any,
                      read: Optional[Callable[[Path, Any], Any]] = None) -> Callable[[Any, Any], Any]:  # fmt: skip
    """The executor's spend reader over step 4's reader of a dead lease store (`expcore.study.merge_by_run.
    dead_store_spend(out, study)`, imported when first used; a core without it reads nothing). `locate(site, settle)`
    gives the dead store's `out/` and where it was read (`root`, the surviving root, or `pulled`), or None: step 6
    knows where a federation's lease stores are. A lease is settled only from a read that is not None and names that
    very lease (a store of another lease never settles it); anything else leaves it charged its grant."""

    def spend(site: Any, action: Any) -> Optional[Tuple[int, str, Optional[int]]]:
        found = locate(site, action)
        if found is None:
            return None
        out, source = found
        reader = read
        if reader is None:
            try:
                from expcore.study.merge_by_run import dead_store_spend as reader
            except ImportError:
                return None
        got = reader(out, study)
        if got is None or got.lease_id != action.lease_id:
            return None
        return int(got.spend_micro), source, int(got.charged_attempts)

    return spend


def drain_refused(ledger: Any, label: str, clock: Callable[[], float]) -> Optional[str]:
    """Why a federation must not be drained now: its recorded job end has passed (the drain comes before it, never
    after); None when it may. The clock is read after the federation, right before the drain."""
    found = {f.label: f for f in ledger.federations()}.get(label)
    if found is not None and clock() >= found.job_end_at:
        return f"{label} is past its recorded job end: it is not drained (its end is proven, not drained)"
    return None


class Log:
    """The controller's JSON log: one object per line, appended and flushed, from the loop and the background."""

    def __init__(self, path: Optional[Path], clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.lock = threading.Lock()
        self.lines: List[Dict[str, Any]] = []

    def write(self, kind: str, **fields: Any) -> None:
        entry = {"at": self.clock(), "kind": kind, **fields}
        with self.lock:
            if self.path is None:
                self.lines.append(entry)  # kept in memory only when there is no file to append to
            else:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, sort_keys=True) + "\n")
                    handle.flush()


@dataclass
class Executor:
    """Runs a decision's actions: the ledger's at once, the sites' in the background, one per site or member."""

    ledger: Any  # an open expcore.campaign.elastic.Ledger
    ledger_path: Path
    sites: Mapping[str, SiteConfig]
    transport: Any
    evidence: Path
    log: Log
    # (site, member) -> (its campaign fingerprint, its environment identity as a lease store pins it), or None
    identity: Callable[[str, str], Optional[Tuple[str, Mapping[str, str]]]] = lambda site, member: None
    # (site, the Settle) -> (spend in micro-dollars, its source "root" or "pulled", charged attempts or None), or None
    spend: Callable[[Any, Any], Optional[Tuple[int, str, Optional[int]]]] = lambda site, action: None
    settings: Optional[Path] = None  # the shard settings a joining federation's worker is launched from
    unread: set = field(default_factory=set)  # lost leases whose log could not be read, said once each
    clock: Callable[[], float] = time.time
    pool: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=8))
    running: Dict[str, Future] = field(default_factory=dict)
    unclaimed: Dict[str, int] = field(default_factory=dict)  # a prepare's calls that failed with no outcome, by member
    counting: threading.Lock = field(
        default_factory=threading.Lock
    )  # `unclaimed`: the workers change it, the loop reads

    def busy(self) -> frozenset:
        self.reap()
        with self.counting:
            lost = frozenset(key for key, n in self.unclaimed.items() if n >= PREPARE_TRIES)
        return frozenset(self.running) | lost

    def reap(self) -> None:
        for key in [key for key, future in self.running.items() if future.done()]:
            future = self.running.pop(key)
            if future.exception() is not None:
                self.log.write("result", key=key, ok=False, said=type(future.exception()).__name__)

    def wait(self) -> None:
        for future in list(self.running.values()):
            future.exception()
        self.reap()

    def run(self, decision: Any) -> None:
        from expcore.campaign.elastic import LedgerError

        for action in decision.actions:
            kind = type(action).__name__
            try:
                if kind in ("Add", "Retire", "Prepare"):
                    self.start(action)
                else:
                    self.in_ledger(action)
            except (LedgerError, ValueError) as error:
                self.log.write("result", action=kind, label=getattr(action, "label", None), ok=False, said=str(error))

    def in_ledger(self, action: Any) -> None:
        kind = type(action).__name__
        if kind == "ProveEnded":
            self.ledger.prove_ended(action.label, action.proof)
        elif kind == "Drain":
            late = drain_refused(self.ledger, action.label, self.clock)
            if late is not None:
                self.log.write("result", action=kind, label=action.label, ok=False, said=late)
                return
            self.ledger.drain(action.label)
        elif kind == "Settle":
            try:
                found = self.spend(self.sites.get(action.site), action)
            except (OSError, ValueError, RuntimeError, LookupError):  # evidence that cannot be read is no spend
                found = None
            if found is None:
                if action.lease_id not in self.unread:
                    self.unread.add(action.lease_id)
                    self.log.write("result", action=kind, label=action.label, lease=action.lease_id, ok=False,
                                   said="its call log could not be read: it stays charged its grant")  # fmt: skip
                return
            spend, source, charged = found
            if charged is None:  # a settle closes a lost lease only with its charged attempts: else none at all
                self.log.write("result", action=kind, label=action.label, lease=action.lease_id, ok=False,
                               said="its read gave no charged attempts: not settled")  # fmt: skip
                return
            self.ledger.settle(action.lease_id, spend, source, attempts_charged=charged)
            self.log.write("result", action=kind, label=action.label, lease=action.lease_id, ok=True,
                           spend_micro=spend, source=source, attempts_charged=charged)  # fmt: skip
            return
        elif kind == "Join":
            from expcore.campaign.elastic.control import environment_digest

            found = self.identity(action.site, action.member)
            if found is None:
                self.log.write("result", action=kind, label=action.label, ok=False,
                               said="no identity read for it yet: join waits")  # fmt: skip
                return
            site = self.sites.get(action.site)
            if site is not None and not self.launch_worker(action, site):
                return
            self.ledger.join(action.label, site=action.site, job_ids=tuple(action.job_ids),
                             job_end_at=action.job_end_at, fingerprint=found[0],
                             environment=environment_digest(found[1]),
                             kill_wait_s=action.kill_wait_s)  # fmt: skip
            self.log.write("queue_wait", site=action.site, member=action.member, queue_wait_s=action.queue_wait_s)
        self.log.write("result", action=kind, label=action.label, ok=True)

    def launch_worker(self, action: Any, site: SiteConfig) -> bool:
        """The joining federation's worker, started on its site before the join, so no lease goes to a federation with
        none; False (the join waits, and is decided again next tick) when the launch fails. Nothing when the site has
        no channel or the controller no shard settings."""
        for argv in render(action, site, self.evidence, self.ledger_path, settings=self.settings).site_steps:
            try:
                code, out, err = self.transport.run(site, list(argv), LAUNCH_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError) as error:
                code, out, err = 1, "", f"it could not be run ({type(error).__name__})"
            self.log.write("result", action="Worker", label=action.label, argv=shown(site, argv), code=code,
                           ok=code == 0, said=said_of(site, err, out))  # fmt: skip
            if code != 0:
                return False
        return True

    def retire_refused(self, action: Any) -> Optional[str]:
        """Why a retirement must not start now, as the ledger shows it at this moment; None when it may."""
        from expcore.campaign.elastic.control import ledger_label
        from expcore.campaign.elastic.records import LIVE

        label = ledger_label(action.site, action.member)
        found = {f.label: f for f in self.ledger.federations()}.get(label)
        if action.label is None:
            return None if found is None else f"{label} joined the ledger since the decision"
        if found is None:
            return f"{label} is not in the ledger"
        if found.drained_at is None and found.ended_at is None:
            return f"{label} is neither drained nor proven ended"
        live = [lease.lease_id for lease in self.ledger.leases() if lease.federation == label and lease.state in LIVE]
        if self.sites[action.site].remote:  # an archive not pulled here yet is pulled before any down
            live += [lease.lease_id for lease in self.ledger.leases()
                     if lease.federation == label and lease.state == "reported"]  # fmt: skip
        return f"{label} holds lease(s) {live} in flight" if live else None

    def start(self, action: Any) -> None:
        from expcore.campaign.elastic.control import prepare_key

        kind = type(action).__name__
        site = self.sites[action.site]
        if kind == "Add":
            key = action.site
        elif kind == "Prepare":
            key = prepare_key(action.site, action.member)
        else:
            key = f"{action.site}/{action.member}"
        if key in self.running:
            return
        if kind == "Retire":
            refused = self.retire_refused(action)
            if refused is not None:
                self.log.write("result", action=kind, member=action.member, ok=False, said=f"not retired: {refused}")
                return
        self.running[key] = self.pool.submit(self.prepare_steps if kind == "Prepare" else self.site_steps, action, site)

    def prepare_steps(self, action: Any, site: SiteConfig) -> bool:
        """A member's prepare: one call at the site (`fleetprepare.py run`), which bounds the command, keeps its output
        in the site's own log and records its start and end there. What this logs is the call's status and the outcome,
        exit status, times and duration it printed, never the command, its output, what the call said on stderr (a
        path may be in it) or a path; True when it exited 0. A call that fails with no outcome is asked again on the
        next tick, up to `PREPARE_TRIES` calls: then the member is reported lost, and retired."""
        from expcore.campaign.elastic.control import prepare_lost_key

        (argv,) = render(action, site, self.evidence, self.ledger_path).steps
        try:
            code, out, _ = self.transport.run(site, list(argv), site.settings.prepare_timeout_s + PREPARE_GRACE_S)
        except (OSError, subprocess.SubprocessError):
            code, out = 1, ""
        found = prepare_outcome(out)
        lost = prepare_lost_key(action.site, action.member)
        with self.counting:
            if code == 0 or found:
                self.unclaimed.pop(lost, None)
            else:  # no outcome: the wrapper did not run, or could not claim; asked again, up to PREPARE_TRIES calls
                self.unclaimed[lost] = self.unclaimed.get(lost, 0) + 1
        self.log.write("result", action="Prepare", site=action.site, member=action.member, code=code, ok=code == 0,
                       **found)  # fmt: skip
        return code == 0

    def site_steps(self, action: Any, site: SiteConfig) -> bool:
        """An add's or a retirement's commands on the site, in order, each only after the one before succeeded."""
        kind = type(action).__name__
        digest = DIGEST
        rendered = render(action, site, self.evidence, self.ledger_path)
        places = rendered.places or (SITE,) * len(rendered.steps)
        for index, (step, place) in enumerate(zip(rendered.steps, places)):
            argv = [digest if word == DIGEST else word for word in step]
            try:
                if place == PULL:
                    code, out, err = self.transport.pull(site, argv[1], Path(argv[2]), argv[3])
                elif place == PULL_PLACES:
                    code, out, err = self.transport.pull_places(site, argv[1], Path(argv[2]))
                elif place == HERE_PLACE:
                    code, out, err = self.transport.run_here(argv, self.evidence, COMMAND_TIMEOUT_S)
                else:
                    code, out, err = self.transport.run(site, argv, COMMAND_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError) as error:
                code, out, err = 1, "", f"it could not be run ({type(error).__name__})"
            if kind == "Retire" and step[4:5] == ("collect",) and code == 0:
                digest = out.strip()
                if not HEX64.fullmatch(digest):
                    code, err = 1, "collect printed no manifest hash: nothing is verified or taken down"
            self.log.write("result", action=kind, site=action.site, member=getattr(action, "member", None),
                           step=index, argv=shown(site, argv), code=code, ok=code == 0,
                           said=said_of(site, err, out))  # fmt: skip
            if code != 0:
                return False
        return True
