"""fleetexecute.py: what the elastic fleet's controller does with a decision (#462 step 5, C43; `fleetcontrol.py`).

Every action is the call an operator makes by hand, rendered once (`render`): what the dry run prints is exactly what
the executor runs. On a site's login node: `fleet.py --fleet F add K`; a retirement's `shards.py collect --roots-only
--root R --into DIR`, `shards.py verify-copy DIR/archives.sha256.json DIR --manifest-sha256 <the hash collect
printed>` and `fleet.py --fleet F down --member fed-k`, in that order, each only after the one before succeeded. On the
orchestrating host, in the ledger: `fleetcontrol.py drain`, `ended`, `join` and `settle`, which the executor calls in
process.

- **The ledger first.** Proven ends, settlements, drains and joins run in the ledger at once, in the decision's order.
  A lost lease is settled to the spend its dead federation's call log shows, read through the `spend` reader (step 4's
  reader of a dead store's spend, hooked up in step 6; until then none is read, and the lease stays charged its
  grant, logged once). A retirement
  is checked against the ledger again just before it starts: a federation in the ledger must be drained or proven
  ended and hold no lease that is issued or sent; one that is not in the ledger must still not be. A proven end
  `cancelled` comes only on a later tick, once the fleet's record shows the down verified.
- **Never `--discard-evidence`, never `scancel`.** A down goes through `fleet.py`, whose `hpcfed.py down` refuses a
  root with evidence not collected (its gate), and cancels only the jobs that member's up recorded, by id.
- **One operation per site and per member at a time.** An add (one per site) and a retirement (one per member) run in
  the background; until each ends its key is busy, and the decision gives that site no add and that member nothing.
- **The copy back.** Step 5 runs every site command on this host (`transport = "local"`, the login node itself), so
  the collected copy is already here and `verify-copy` checks it; a remote site's pull is step 6's.
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

HERE = Path(__file__).resolve().parent
DIGEST = "<the hash collect printed>"  # the dry run's stand-in for collect's output, which verify-copy takes
FINGERPRINT, ENVIRONMENT = "<its campaign fingerprint>", "<its environment identity>"
SPEND = "<the spend its call log shows, micro-dollars>"
ARCHIVES_MANIFEST = "archives.sha256.json"  # `shards.ARCHIVES_MANIFEST`
HEX64 = re.compile(r"[0-9a-f]{64}")
COMMAND_TIMEOUT_S = 6 * 3600.0  # the fleet's commands bound their own waits (an up's queue wait, a down's steps)


@dataclass(frozen=True)
class SiteConfig:
    """A site as the controller reaches it: its settings, its fleet's directory, how its commands run, and the
    interpreter and deploy directory on its login node."""

    settings: Any  # expcore.campaign.elastic.control.SiteSettings
    fleet: Path
    transport: str = "local"
    python: str = sys.executable
    deploy: Path = HERE

    @property
    def label(self) -> str:
        return self.settings.label

    def script(self, name: str, *words: str) -> List[str]:
        return [self.python, "-I", "-B", str(self.deploy / name), *words]


@dataclass(frozen=True)
class Rendered:
    """An action as commands: `where` is `site` (the site's login node) or `ledger` (this host); `steps` in order."""

    where: str
    steps: Tuple[Tuple[str, ...], ...]


def evidence_dir(evidence: Path, site: str, member: str) -> Path:
    """Where a member's root evidence is collected: one directory per member, so a retry finds its own copy."""
    return evidence / f"{site}-{member}"


def render(action: Any, site: SiteConfig, evidence: Path, ledger: Path, digest: str = DIGEST) -> Rendered:
    """The commands of one action, exactly as the dry run prints them and the executor runs them."""
    kind = type(action).__name__
    control = site.script("fleetcontrol.py")
    if kind == "Add":
        return Rendered("site", (tuple(site.script("fleet.py", "--fleet", str(site.fleet), "add", str(action.count))),))
    if kind == "Retire":
        into = evidence_dir(evidence, site.label, action.member)
        steps = []
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
                 "--source", "root")  # fmt: skip
        return Rendered("ledger", ((*control, *words),))
    if kind == "Join":
        words = ("join", "--ledger", str(ledger), "--label", action.label, "--site", action.site, "--jobs",
                 ",".join(action.job_ids), "--job-end", repr(action.job_end_at), "--kill-wait",
                 repr(action.kill_wait_s), "--fingerprint", FINGERPRINT, "--environment", ENVIRONMENT)  # fmt: skip
        return Rendered("ledger", ((*control, *words),))
    raise ValueError(f"no rendering for an action of kind {kind}")


def described(action: Any, rendered: Rendered) -> Dict[str, Any]:
    """An action for the log and the dry run: its kind, site, member, reason and commands."""
    return {"action": type(action).__name__, "site": action.site, "member": getattr(action, "member", None),
            "label": getattr(action, "label", None), "reason": action.reason, "where": rendered.where,
            "steps": [list(step) for step in rendered.steps]}  # fmt: skip


def printed(entry: Mapping[str, Any]) -> str:
    """One action as the dry run prints it: where it runs, then each command, one per line."""
    where = f"on {entry['site']}'s login node" if entry["where"] == "site" else "in the ledger, on this host"
    lines = [f"# {entry['action']} {entry['label'] or entry['member'] or entry['site']} ({where}): {entry['reason']}"]
    return "\n".join(lines + [shlex.join(step) for step in entry["steps"]])


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


def last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    said = (lines or ["nothing"])[-1]
    return "".join(c if c.isprintable() else "?" for c in said)[-300:]


def drain_refused(ledger: Any, label: str, now: float) -> Optional[str]:
    """Why a federation must not be drained now: its recorded job end has passed (the drain comes before it, never
    after); None when it may."""
    found = {f.label: f for f in ledger.federations()}.get(label)
    if found is not None and now >= found.job_end_at:
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
    identity: Callable[[str, str], Optional[Tuple[str, str]]] = lambda site, member: None
    # (site, the Settle) -> (spend in micro-dollars, its source "root" or "pulled", charged attempts or None), or None
    spend: Callable[[Any, Any], Optional[Tuple[int, str, Optional[int]]]] = lambda site, action: None
    unread: set = field(default_factory=set)  # lost leases whose log could not be read, said once each
    clock: Callable[[], float] = time.time
    pool: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=8))
    running: Dict[str, Future] = field(default_factory=dict)

    def busy(self) -> frozenset:
        self.reap()
        return frozenset(self.running)

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
                if kind in ("Add", "Retire"):
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
            late = drain_refused(self.ledger, action.label, self.clock())
            if late is not None:
                self.log.write("result", action=kind, label=action.label, ok=False, said=late)
                return
            self.ledger.drain(action.label)
        elif kind == "Settle":
            found = self.spend(self.sites.get(action.site), action)
            if found is None:
                if action.lease_id not in self.unread:
                    self.unread.add(action.lease_id)
                    self.log.write("result", action=kind, label=action.label, lease=action.lease_id, ok=False,
                                   said="its call log could not be read: it stays charged its grant")  # fmt: skip
                return
            spend, source, charged = found
            self.ledger.settle(action.lease_id, spend, source, attempts_charged=charged)
            self.log.write("result", action=kind, label=action.label, lease=action.lease_id, ok=True,
                           spend_micro=spend, source=source)  # fmt: skip
            return
        elif kind == "Join":
            found = self.identity(action.site, action.member)
            if found is None:
                self.log.write("result", action=kind, label=action.label, ok=False,
                               said="no identity read for it yet: join waits")  # fmt: skip
                return
            self.ledger.join(action.label, site=action.site, job_ids=tuple(action.job_ids),
                             job_end_at=action.job_end_at, fingerprint=found[0], environment=found[1],
                             kill_wait_s=action.kill_wait_s)  # fmt: skip
            self.log.write("queue_wait", site=action.site, member=action.member, queue_wait_s=action.queue_wait_s)
        self.log.write("result", action=kind, label=action.label, ok=True)

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
        return f"{label} holds lease(s) {live} in flight" if live else None

    def start(self, action: Any) -> None:
        kind = type(action).__name__
        site = self.sites[action.site]
        key = action.site if kind == "Add" else f"{action.site}/{action.member}"
        if key in self.running:
            return
        if kind == "Retire":
            refused = self.retire_refused(action)
            if refused is not None:
                self.log.write("result", action=kind, member=action.member, ok=False, said=f"not retired: {refused}")
                return
        self.running[key] = self.pool.submit(self.site_steps, action, site)

    def site_steps(self, action: Any, site: SiteConfig) -> bool:
        """An add's or a retirement's commands on the site, in order, each only after the one before succeeded."""
        kind = type(action).__name__
        digest = DIGEST
        steps = render(action, site, self.evidence, self.ledger_path).steps
        for index, step in enumerate(steps):
            argv = [digest if word == DIGEST else word for word in step]
            try:
                code, out, err = self.transport.run(site, argv, COMMAND_TIMEOUT_S)
            except (OSError, subprocess.SubprocessError) as error:
                code, out, err = 1, "", f"it could not be run ({type(error).__name__})"
            if kind == "Retire" and step[4:5] == ("collect",) and code == 0:
                digest = out.strip()
                if not HEX64.fullmatch(digest):
                    code, err = 1, "collect printed no manifest hash: nothing is verified or taken down"
            self.log.write("result", action=kind, site=action.site, member=getattr(action, "member", None),
                           step=index, argv=argv, code=code, ok=code == 0,
                           said=last_line(err or out))  # fmt: skip
            if code != 0:
                return False
        return True
