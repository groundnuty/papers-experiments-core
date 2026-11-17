"""fleet.py: N HPC federations from one layout, one shard of a campaign on each, as many at once as the operator allows
(DESIGN.md, section 1, "A fleet"; README.md, "A fleet of federations").

    uv run python deploy/hpc-federation/fleet.py --fleet DIR --layout PATH up N
    uv run python deploy/hpc-federation/fleet.py --fleet DIR add K
    uv run python deploy/hpc-federation/fleet.py --fleet DIR plan [--manifest SETTINGS]
    uv run python deploy/hpc-federation/fleet.py --fleet DIR step [--shard L] [--resume]
    uv run python deploy/hpc-federation/fleet.py --fleet DIR status
    uv run python deploy/hpc-federation/fleet.py --fleet DIR down [--member L]

Run on the login node with the site's values exported (`set -a; . site.env; set +a`), as `hpcfed.py up` is.

- up: refused past `HPC_FLEET_MAX` (the operator's maximum, which the grant and the scratch inode quota set; unset
  refuses), by the home guard (`hpcquota.home_guard`, when the site sets its keys: the account's home past 95% of its
  files or bytes, read once for the whole fleet), and past the quota by the inode guard (`hpcquota`, mandatory on
  scratch: the account's usage plus N times one federation's estimate, read once, before anything is made); then the
  fleet's directory (new: never adopted), N federations `fed-1` ... `fed-N`, each its own root `<fleet>/fed-k/root` (an
  exclusive mkdir), state file and held job, each brought up by `hpcfed.py up` in a process of its own,
  `HPC_FLEET_PARALLEL` at a time (1 by default), and N shards. A federation whose up fails is reported and the
  others stay up; it may hold a job until `down --member` releases it. Each federation's output is in
  `<fleet>/logs/<label>.<up|down>.log`.
- add: K fresh federations into the fleet, by the same rules, the maximum counting every one that may hold a job.
- plan: each shard on a federation (`fleetplan`), written to `<fleet>/shards.toml`, and each one's srun line printed
  as `shards.py plan` prints it.
- step: each assigned shard's line launched detached on the login node, its client recorded.
- status: what the record and each root's state file say; nothing is run.
- down: every federation this fleet made, or one, taken down by `hpcfed.py down` and verified gone; a root or state
  that is not the one this fleet's up made is refused and left untouched, and nothing else is ever looked for.
"""

import argparse
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
for _path in (HERE, HERE.parents[1] / "deploy" / "compose-federation"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import fleetplan  # noqa: E402
import hpcfed  # noqa: E402
import hpcquota  # noqa: E402
import hpcstate  # noqa: E402
from fedlayout import Layout  # noqa: E402
from fedspec import LayoutError, load_spec  # noqa: E402
from fleetrecord import (  # noqa: E402
    HOLDS_NOTHING,
    FleetError,
    Record,
    identity,
    jobs_of,
    member_of,
    new_member,
    new_record,
    root_of,
    shard_of,
    slot_of,
)
from hpcdriver import CORE_MISMATCH, CoreMismatch  # noqa: E402
from hpcsettings import SettingsError, agent_limits, load_settings, site_from_environment  # noqa: E402
from hpcslurm import Client, Runner, Slurm, SlurmError  # noqa: E402

MAX_VAR, PARALLEL_VAR = "HPC_FLEET_MAX", "HPC_FLEET_PARALLEL"
LINKED = "linked"  # a federation's phase once its up has finished (hpcstate.PHASES)
CEILING = 64  # no setting of either may pass it
COMMANDS = ("up", "add", "plan", "step", "status", "down")
UNSAFE = set('"\\') | {chr(c) for c in range(32)} | {chr(127)}  # never in a path the fleet writes into TOML and JSON


def _say(line: str) -> None:
    print(f"fleet: {line}", file=sys.stderr, flush=True)


# One federation's `hpcfed.py --layout L --root R <up|down>`: (label, command, layout, root, log) -> (its exit status,
# what it printed). By default a process of its own (`hpcfed_process`): a federation's driver changes process-wide
# state (the umask its secrets are written under), so two never share a process.
Operate = Callable[[str, str, Path, Path, Path], Tuple[int, str]]


@dataclass
class Hooks:
    """What a federation's up and down run through and a step is launched through: `hpcfed.py` in a process of its own
    and the login node's Runner by default, the tests' fake Slurm in their place."""

    federation: Operate
    runner: Callable[[str], Any]  # label -> the Runner that spawns and watches its federation's shard step
    inode_guard: Callable[[int, Layout, Any, Mapping[str, str]], str]  # (count, layout, site, environ) -> its words
    home_guard: Callable[[Any], str]  # (site) -> its words, empty for none: the account's home, read once per up or add
    self_check: Callable[[], Any] = hpcfed.self_check
    say: Callable[[str], None] = field(default=_say)


def inode_guard(runner: Any, count: int, layout: Layout, site: Any, environ: Mapping[str, str]) -> str:
    """The inode guard for `count` federations at once, the core's own (`hpcquota.guard`, DESIGN.md section 11): N
    concurrent ups would each read the same usage, so the fleet reads it once against N times one federation's estimate.
    It is mandatory as it is for one `up`: a scratch root with `HPC_QUOTA_CHECK=off`, an unset command or pattern or a
    quota that cannot be read refuses the fleet (`hpcquota.QuotaRefused`). The fleet asks once for all N; each member's
    own `up` then checks its own estimate as it always does (the quota command runs once more for each). Without a
    scratch root nothing of the federations is on scratch, and the fleet says that no check was made."""
    if not site.storage_root:
        return "inode check: none: HPC_STORAGE_ROOT is not set, so nothing of the federations is on scratch"
    return hpcquota.guard(runner, site, layout, environ, federations=count)


def hpcfed_process(environ: Mapping[str, str], run: Callable[..., Any] = subprocess.run) -> Operate:
    """Each federation's up or down as an operator runs it by hand: `hpcfed.py` under this interpreter with `-I` (no
    module of the launch directory is ever imported), from the fleet's own 0700 directory (never a shared one), in a
    process of its own with this environment (site.env's values), its output appended to the federation's log (0600)."""

    def operate(label: str, command: str, layout: Path, root: Path, log: Path) -> Tuple[int, str]:
        argv = [sys.executable, "-I", "-B", str(HERE / "hpcfed.py"), "--layout", str(layout), "--root", str(root),
                command]  # fmt: skip
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            done = run(argv, env=dict(environ), cwd=str(Path(root).parents[1]), stdin=subprocess.DEVNULL,
                       stdout=descriptor, stderr=subprocess.STDOUT, check=False)  # fmt: skip
        finally:
            os.close(descriptor)
        return done.returncode, log.read_text(encoding="utf-8", errors="replace")

    return operate


def default_hooks(environ: Mapping[str, str]) -> Hooks:
    runner = Runner()
    return Hooks(
        federation=hpcfed_process(environ),
        runner=lambda label: runner,
        inode_guard=lambda count, layout, site, env: inode_guard(runner, count, layout, site, env),
        home_guard=lambda site: hpcquota.home_guard(runner, site),
    )


def last_error(printed: str) -> str:
    """The line a failed command ended on, for the record: its last `error:` line, or its last line."""
    lines = [line.strip() for line in printed.splitlines() if line.strip()]
    errors = [line for line in lines if line.startswith("error:")]
    said = (errors or lines or ["it printed nothing"])[-1]
    return "".join(c if c.isprintable() else "?" for c in said)[-300:]


def setting(environ: Mapping[str, str], name: str, default: Optional[int] = None) -> int:
    value = environ.get(name) or ""
    if not value and default is not None:
        return default
    if not value:
        raise FleetError(f"{name} is not set: the operator's maximum of federations at once (site.env); refused")
    if not value.isdigit() or not 1 <= int(value) <= CEILING:
        raise FleetError(f"{name} must be a whole number from 1 to {CEILING}")
    return int(value)


def separate_only(spec: Any) -> None:
    """A fleet's federations hold their agent's node as a job of its own (`[hpc] sandbox_placement = "separate"`): on
    the site's Slurm, steps on a heterogeneous job's component 0 failed with an interconnect error after the services
    had started (live, 16 Nov 2026), so a `het` layout is refused before anything is made."""
    settings = load_settings(spec.hpc, agent_limits(spec.platform_template))
    if settings.sandbox_node() is not None and settings.sandbox_placement != "separate":
        raise FleetError("a fleet holds each agent's node as a job of its own: give the layout [hpc] "
                         'sandbox_placement = "separate" (steps on a heterogeneous job\'s services failed live); '
                         "nothing made")  # fmt: skip


def fleet_directory(value: str) -> Path:
    if not os.path.isabs(value) or set(value) & UNSAFE:
        raise FleetError("--fleet must be an absolute path with no quote, backslash or control character")
    path = Path(os.path.normpath(value))
    if not path.parent.is_dir() or path.parent == path:
        raise FleetError("--fleet must lie in an existing directory")
    return path


def pooled(labels: Sequence[str], parallel: int, work: Callable[[str], bool]) -> Dict[str, bool]:
    """`work` for every label, `parallel` at a time; one that fails never stops another."""
    with ThreadPoolExecutor(max_workers=max(1, min(parallel, len(labels) or 1))) as pool:
        return dict(zip(labels, pool.map(work, labels)))


def held(jobs: Mapping[str, Optional[str]]) -> str:
    """A federation's job ids, for a line: "4100, 4150"; empty when it holds none on record."""
    return ", ".join(job for job in (jobs.get("allocation"), jobs.get("sandbox")) if job)


def state_facts(root: Path) -> Dict[str, Any]:
    """What a root's state file says of its federation's identity and progress; nothing when it has none."""
    try:
        state = hpcstate.load(root) if root.is_dir() else None
    except hpcstate.StateError:
        state = None
    if state is None:
        return {}
    return {"name": state["allocation"].get("name"), "jobs": jobs_of(state), "phase": state.get("phase")}


@dataclass
class Fleet:
    directory: Path
    environ: Mapping[str, str]
    hooks: Hooks
    record: Record = field(init=False)

    def __post_init__(self) -> None:
        self.record = Record(self.directory)

    def say(self, line: str) -> None:
        self.hooks.say(line)

    def operate(self, label: str, command: str, layout: Path) -> Tuple[int, str]:
        """One federation's `hpcfed.py up` or `down`, its output in `<fleet>/logs/<label>.<command>.log`."""
        logs = self.directory / "logs"
        logs.mkdir(mode=0o700, exist_ok=True)
        log = logs / f"{label}.{command}.log"
        try:
            return self.hooks.federation(label, command, layout, root_of(self.directory, label), log)
        except (OSError, subprocess.SubprocessError) as error:
            return 1, f"error: {command}: it could not be run ({type(error).__name__})"

    # -- up and add -----------------------------------------------------------------------------------------------

    def check_guards(self, count: int, spec: Any, site: Any) -> None:
        """What `up` and `add` read before anything is made, each once for all `count` federations: the account's home
        (it has no estimate per federation, so one read answers for all) and the scratch inode quota against `count`
        times one federation's estimate. Either refuses the whole call; each federation's own `up` then reads both
        again, as it always does."""
        said = self.hooks.home_guard(site)
        if said:
            self.say(said)
        self.say(self.hooks.inode_guard(count, self.scratch_layout(spec), site, self.environ))

    def up(self, layout: Path, count: int) -> int:
        maximum, parallel = setting(self.environ, MAX_VAR), setting(self.environ, PARALLEL_VAR, 1)
        if count < 2:
            raise FleetError("a fleet runs a campaign's shards, one per federation: at least two")
        if count > maximum:
            raise FleetError(f"{count} federations asked for, past the maximum of {maximum} ({MAX_VAR}); nothing made")
        layout = layout.resolve()
        spec, site = load_spec(layout), site_from_environment(self.environ)
        separate_only(spec)
        self.hooks.self_check()
        self.check_guards(count, spec, site)
        try:
            os.mkdir(self.directory, 0o700)
        except FileExistsError:
            raise FleetError("the fleet's directory exists: a fleet's up makes its own and never adopts one") from None
        self.record.save(new_record(layout, spec.digest, count, maximum, parallel))
        with self.record.lifecycle("up"):
            return self.bring_up([f"fed-{n}" for n in range(1, count + 1)], layout, parallel)

    def add(self, count: int) -> int:
        maximum, parallel = setting(self.environ, MAX_VAR), setting(self.environ, PARALLEL_VAR, 1)
        with self.record.lifecycle("add"):
            record = self.record.read()
            live = [m for m in record["members"] if m["status"] not in HOLDS_NOTHING]
            if count < 1 or len(live) + count > maximum:
                raise FleetError(f"{len(live)} federation(s) may hold a job and {count} more were asked for, past the "
                                 f"maximum of {maximum} ({MAX_VAR}); nothing made")  # fmt: skip
            layout = Path(record["layout"]["path"])
            spec, site = load_spec(layout), site_from_environment(self.environ)
            separate_only(spec)
            if spec.digest != record["layout"]["digest"]:
                raise FleetError("the fleet's layout file changed since its up: its federations would differ")
            self.hooks.self_check()
            self.check_guards(count, spec, site)
            first = len(record["members"]) + 1
            return self.bring_up([f"fed-{n}" for n in range(first, first + count)], layout, parallel)

    def scratch_layout(self, spec: Any) -> Layout:
        """One federation's layout, for the inode estimate alone (nothing is made)."""
        return Layout(root_of(self.directory, "fed-1"), slot_of(self.directory, "fed-1"), spec)

    def claim(self, label: str) -> bool:
        """The slot and root of one federation, each by an exclusive mkdir, recorded before it is made and, once made,
        by its device and inode; a slot that exists is never adopted."""
        self.record.update(lambda r: r["members"].append(new_member(self.directory, label)))
        root = root_of(self.directory, label)
        try:
            os.mkdir(slot_of(self.directory, label), 0o700)
            os.mkdir(root, 0o700)
        except FileExistsError:
            self.record.update(lambda r: member_of(r, label).update(status="taken", said="its slot existed"))
            self.say(f"{label}: its slot exists and is never adopted: not brought up")
            return False
        info = os.lstat(root)
        self.record.update(lambda r: member_of(r, label).update(status="made", made=[info.st_dev, info.st_ino]))
        return True

    def bring_up(self, labels: List[str], layout: Path, parallel: int) -> int:
        made = [label for label in labels if self.claim(label)]

        def one(label: str) -> bool:
            root = root_of(self.directory, label)
            code, printed = self.operate(label, "up", layout)
            facts = state_facts(root)
            ok = code == 0 and facts.get("phase") == LINKED
            said = None if ok else last_error(printed) if code != 0 else f"not linked (phase {facts.get('phase')})"

            def done(record: Dict[str, Any]) -> None:
                member = member_of(record, label)
                member.update(status="up" if ok else "failed", said=said, phase=facts.get("phase"))
                member.update(name=facts.get("name") or member["name"], jobs=facts.get("jobs") or {})

            self.record.update(done)
            if ok:
                self.say(f"{label}: up, job(s) {held(facts['jobs'])}")
            else:
                jobs = held(facts.get("jobs") or {})
                kept = f"; its job(s) {jobs} may still be held" if jobs else ""
                self.say(f"{label}: up FAILED ({said}){kept}: `fleet.py down --member {label}`")
            return ok

        results = pooled(made, parallel, one)
        self.say(f"{sum(results.values())} of {len(labels)} federation(s) up")
        return 0 if len(results) == len(labels) and all(results.values()) else 1

    # -- plan and step ----------------------------------------------------------------------------------------------

    def plan(self, settings_path: Optional[Path]) -> int:
        with self.record.lifecycle("plan"):
            record = self.record.read()
            given = fleetplan.read_settings(settings_path) if settings_path is not None else None
            settings = record["template"] or given
            if settings is None:
                raise FleetError("the first plan needs --manifest: the shard settings (shards.py's keys)")
            if given is not None and given != settings:
                raise FleetError("the shard settings differ from the ones the fleet's first plan read: refused")
            new, reference, said = fleetplan.assignments(record, self.directory)
            path = self.directory / fleetplan.MANIFEST
            draft = self.directory / f".{fleetplan.MANIFEST}.draft"
            fleetplan.write_whole(draft, fleetplan.manifest_text(settings, fleetplan.entries(record, self.directory,
                                                                                               new)))  # fmt: skip
            plan = fleetplan.load(draft)
            holding = {**{s["label"]: s["member"] for s in record["shards"] if s["member"]}, **new}
            lines = {label: fleetplan.line_of(plan, label, False) for label in sorted(holding)}

            def apply(current: Dict[str, Any]) -> None:
                current["template"] = settings
                if current["reference"] is None:
                    current["reference"] = {"sha256": fleetplan.sha256(reference), "fingerprint": reference}
                for shard_label, label in new.items():
                    shard, member = shard_of(current, shard_label), member_of(current, label)
                    shard.update(member=label, moved=shard["last"] is not None)
                    member["shard"] = shard_label
                    member["held"].append(shard_label)

            self.record.update(apply)
            os.replace(draft, path)
        for line in said:
            self.say(line)
        for shard_label in sorted(holding):
            print(json.dumps({"shard": shard_label, "federation": holding[shard_label], "argv": lines[shard_label]}))
        return 0 if len(holding) == len(record["shards"]) else 1

    def running(self, launch: Mapping[str, Any]) -> Optional[bool]:
        """Whether a launch's srun client still runs on the login node: None when it cannot be told."""
        if launch.get("failed"):
            return False
        if launch.get("pid") is None:
            return None  # spawned, perhaps, and never recorded
        return bool(self.hooks.runner(launch["member"]).client_alive(Client(launch["pid"], launch["start"])))

    def step(self, only: Optional[str], resume: bool) -> int:
        failed = 0
        with self.record.lifecycle("step"):
            record = self.record.read()
            if only is not None:
                shard_of(record, only)
            if not (self.directory / fleetplan.MANIFEST).is_file():
                raise FleetError("the fleet has no shards.toml: run plan first")
            found, said, refused = fleetplan.launches(record, self.directory, only, resume, self.running)
            logs = self.directory / "logs"
            logs.mkdir(mode=0o700, exist_ok=True)
            for launch in found:
                reason = self.job_ours(launch)
                if reason is not None:
                    refused.append(f"{launch.shard}: not launched: {reason}")
                    continue
                shard = shard_of(self.record.read(), launch.shard)
                log = logs / f"{launch.shard}.{launch.member}.{len(shard['launches']) + 1}.log"
                entry = {"member": launch.member, "log": str(log), "resume": launch.resume, "pid": None,
                         "start": None, "failed": None}  # fmt: skip
                self.record.update(lambda r, e=entry, s=launch.shard: shard_of(r, s)["launches"].append(dict(e)))
                try:
                    client = self.hooks.runner(launch.member).spawn(launch.argv, str(log))
                    change = {"pid": client.pid, "start": client.start}
                except OSError as error:
                    change = {"failed": type(error).__name__}
                    failed += 1
                self.record.update(lambda r, c=change, s=launch.shard: shard_of(r, s)["launches"][-1].update(c))
                done = "FAILED to launch" if change.get("failed") else f"launched (client pid {change['pid']})"
                self.say(f"{launch.shard} on {launch.member}: {done}{' with --resume' if launch.resume else ''}; "
                         f"its log {log}" + (f"; {launch.note}" if launch.note else ""))  # fmt: skip
        for line in said + refused:
            self.say(line)
        return 1 if failed or refused or (only is not None and not found) else 0

    def job_ours(self, launch: fleetplan.Launch) -> Optional[str]:
        """Why a launch's job is not the one its federation's up holds, as Slurm records it now; None when it is: the
        job its state names, still known to Slurm, by the name up gave it (Slurm reuses ids, and a job that ended long
        ago may have handed its id to another), and running."""
        try:
            found = Slurm(self.hooks.runner(launch.member)).job(launch.job)
        except (SlurmError, OSError, ValueError) as error:
            return f"job {launch.job} could not be looked up ({type(error).__name__})"
        if found is None:
            return f"{launch.member}'s job {launch.job} is gone"
        if found.get("JobName") != launch.name:
            return f"job {launch.job} now names another job than {launch.member}'s: never stepped into"
        if found.get("JobState") != "RUNNING":
            return f"{launch.member}'s job {launch.job} is not running ({found.get('JobState')})"
        return None

    # -- status and down --------------------------------------------------------------------------------------------

    def status(self) -> int:
        record = self.record.read()
        reference = (record.get("reference") or {}).get("sha256")
        print(f"fleet: {len(record['shards'])} shard(s), {len(record['members'])} federation(s), platform "
              f"fingerprint {reference[:12] if reference else 'not yet planned'}")  # fmt: skip
        for member in record["members"]:
            facts = state_facts(root_of(self.directory, member["label"]))
            jobs = held(facts.get("jobs") or member.get("jobs") or {}) or "none"
            line = f"{member['label']}: {member['status']}, phase {facts.get('phase')}, job(s) {jobs}"
            line += f", shard {member['shard']}" if member["shard"] else ""
            print(line + (f" ({member['said']})" if member["said"] else ""))
        for shard in record["shards"]:
            launches = [launch for launch in shard["launches"] if launch["member"] == shard["member"]]
            if shard["member"] is None:
                state = f"unassigned (last on {shard['last']})" if shard["last"] else "unassigned"
            elif not launches:
                state = f"on {shard['member']}, not launched"
            else:
                alive = self.running(launches[-1])
                state = f"on {shard['member']}, its step " + {True: "runs", False: "ended"}.get(alive, "cannot be told")
            print(f"{shard['label']}: {state}" + (" (moved: its first reset there is full)" if shard["moved"] else ""))
        return 0

    def down(self, only: Optional[str]) -> int:
        with self.record.lifecycle("down"):
            record = self.record.read()
            if only is not None:
                member_of(record, only)
            targets = [m["label"] for m in record["members"] if (only is None or m["label"] == only)
                       and m["status"] != "down"]  # fmt: skip
            parallel = setting(self.environ, PARALLEL_VAR, record.get("parallel") or 1)
            layout = Path(record["layout"]["path"])
            results = pooled(targets, parallel, lambda label: self.down_one(label, layout))
        self.say(f"{sum(results.values())} of {len(targets)} federation(s) down")
        return 0 if all(results.values()) else 1

    @staticmethod
    def never_marked(root: Path) -> bool:
        """A root this fleet made that its federation's up never marked: an up refused before it touched the root (its
        preflight) leaves the fleet's mkdir as it was, no state file, no mark, nothing in it; hpcfed's down refuses a
        root it did not mark, so the fleet removes this one itself. One holding anything is never this."""
        from fedlayout import ROOT_MARKER

        return root.is_dir() and not (root / ROOT_MARKER).exists() and not hpcstate.path(root).exists()

    @staticmethod
    def remove_empty(root: Path) -> Tuple[int, str]:
        """The fleet's own mkdir, removed only while empty (`rmdir` removes nothing else)."""
        try:
            os.rmdir(root)
        except OSError as error:
            return 1, f"error: down: its root holds files no up of this fleet's marked ({type(error).__name__}): kept"
        return 0, "down: its up never marked the root; the empty root removed"

    def down_one(self, label: str, layout: Path) -> bool:
        member = member_of(self.record.read(), label)
        if member["made"] is None:
            self.say(f"{label}: never made by this fleet ({member['status']}): nothing touched")
            return True
        reason, name = identity(self.directory, member)
        if reason is not None:
            self.record.update(lambda r: member_of(r, label).update(status="down-failed", said=reason))
            self.say(f"{label}: NOT TOUCHED: {reason}")
            return False
        if name is not None and member["name"] is None:
            self.record.update(lambda r: member_of(r, label).update(name=name))
        root = root_of(self.directory, label)
        if self.never_marked(root):  # its up stopped before it marked the root: hpcfed's down would refuse it
            code, printed = self.remove_empty(root)
        else:
            code, printed = self.operate(label, "down", layout)
        ok = code == 0 and not root.exists()
        said = None if ok else last_error(printed) if code != 0 else "its root is still there"

        def done(record: Dict[str, Any]) -> None:
            member_of(record, label).update(status="down" if ok else "down-failed", said=said, shard=None)
            for shard in record["shards"]:
                if ok and shard["member"] == label:
                    shard.update(member=None, last=label)

        self.record.update(done)
        self.say(f"{label}: down, its job gone and its root removed" if ok else f"{label}: down FAILED ({said}): run "
                 "down again")  # fmt: skip
        return ok


def main(argv: Optional[Sequence[str]] = None, environ: Optional[Mapping[str, str]] = None,
         hooks: Optional[Hooks] = None) -> int:  # fmt: skip
    parser = argparse.ArgumentParser(prog="fleet.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("--fleet", required=True, help="the fleet's directory (up makes it)")
    parser.add_argument("--layout", type=Path, help="the layout file every federation is brought up from (up)")
    parser.add_argument("command", choices=COMMANDS)
    parser.add_argument("count", nargs="?", type=int, help="how many federations (up, add)")
    parser.add_argument("--manifest", type=Path, help="the shard settings (plan): shards.py's keys, no [[shards]]")
    parser.add_argument("--shard", help="one shard (step)")
    parser.add_argument("--resume", action="store_true", help="launch an ended shard's step again, resuming (step)")
    parser.add_argument("--member", help="one federation (down)")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    hooks = hooks or default_hooks(environ)
    try:
        fleet = Fleet(fleet_directory(args.fleet), environ, hooks)
        if args.command in ("up", "add") and args.count is None:
            raise FleetError(f"{args.command} needs a count")
        if args.command == "up":
            if args.layout is None:
                raise FleetError("up needs --layout")
            return fleet.up(args.layout, args.count)
        if args.command == "add":
            return fleet.add(args.count)
        if args.command == "plan":
            return fleet.plan(args.manifest)
        if args.command == "step":
            return fleet.step(args.shard, args.resume)
        if args.command == "status":
            return fleet.status()
        return fleet.down(args.member)
    except CoreMismatch as error:
        print(f"error: {args.command}: {error}", file=sys.stderr)
        return CORE_MISMATCH
    except (FleetError, LayoutError, SettingsError, RuntimeError, OSError) as error:
        print(f"error: {args.command}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
