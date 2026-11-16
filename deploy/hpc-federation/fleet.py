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
  refuses), and past the quota by the inode guard (`hpcquota`, when this core has it: the account's usage plus N times
  one federation's estimate, read once, before anything is made); then the fleet's directory (new: never adopted),
  N federations `fed-1` ... `fed-N`, each its own root `<fleet>/fed-k/root` (an exclusive mkdir), state file and held
  job, brought up by `hpcfed`'s own driver, `HPC_FLEET_PARALLEL` at a time (1 by default), and N shards. A federation
  whose up fails is reported and the others stay up; it may hold a job until `down --member` releases it.
- add: K fresh federations into the fleet, by the same rules, the maximum counting every one that may hold a job.
- plan: each shard on a federation (`fleetplan`), written to `<fleet>/shards.toml`, and each one's srun line printed
  as `shards.py plan` prints it.
- step: each assigned shard's line launched detached on the login node, its client recorded.
- status: what the record and each root's state file say; nothing is run.
- down: every federation this fleet made, or one, taken down by its driver and verified gone; a root or state that is
  not the one this fleet's up made is refused and left untouched, and nothing else is ever looked for.
"""

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
for _path in (HERE, HERE.parents[1] / "deploy" / "compose-federation"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import fleetplan  # noqa: E402
import hpcfed  # noqa: E402
import hpcstate  # noqa: E402
from fedlayout import Layout  # noqa: E402
from fedspec import LayoutError, load_spec  # noqa: E402
from fleetrecord import (  # noqa: E402
    HOLDS_NOTHING,
    FleetError,
    Record,
    identity,
    member_of,
    new_member,
    new_record,
    root_of,
    shard_of,
    slot_of,
)
from hpcdriver import CORE_MISMATCH, CoreMismatch  # noqa: E402
from hpcsettings import SettingsError, site_from_environment  # noqa: E402
from hpcslurm import Client, Runner  # noqa: E402

MAX_VAR, PARALLEL_VAR = "HPC_FLEET_MAX", "HPC_FLEET_PARALLEL"
LINKED = "linked"  # a federation's phase once its up has finished (hpcstate.PHASES)
CEILING = 64  # no setting of either may pass it
COMMANDS = ("up", "add", "plan", "step", "status", "down")
UNSAFE = set('"\\') | {chr(c) for c in range(32)} | {chr(127)}  # never in a path the fleet writes into TOML and JSON


def _say(line: str) -> None:
    print(f"fleet: {line}", file=sys.stderr, flush=True)


@dataclass
class Hooks:
    """What a federation's up and down run on and a step is launched through: `hpcfed.make_driver` over the login
    node's Runner by default, the tests' fake Slurm in their place."""

    driver: Callable[[str, Path, Path, bool], Any]  # (label, layout, root, agent) -> hpcdriver.Driver
    runner: Callable[[str], Any]  # label -> the Runner that spawns and watches its federation's shard step
    inode_guard: Callable[[int, Layout, Any, Mapping[str, str]], str]  # (count, layout, site, environ) -> its words
    self_check: Callable[[], Any] = hpcfed.self_check
    say: Callable[[str], None] = field(default=_say)


def inode_guard(runner: Any, count: int, layout: Layout, site: Any, environ: Mapping[str, str]) -> str:
    """The inode guard for `count` federations at once (`hpcquota`, when this core has it): N concurrent ups would each
    read the same usage, so the fleet reads it once against N times one federation's estimate. QuotaRefused refuses."""
    try:
        import hpcquota
    except ImportError:
        return "inode check: none: this core has no inode guard (hpcquota)"
    if not getattr(site, "storage_root", None):
        return "inode check: none: HPC_STORAGE_ROOT is not set, so nothing of the federations is on scratch"
    each = sum(hpcquota.estimate_inodes(layout, environ).values())
    return hpcquota.check_inodes(runner, site, count * each)


def default_hooks(environ: Mapping[str, str]) -> Hooks:
    runner = Runner()
    return Hooks(
        driver=lambda label, layout, root, agent: hpcfed.make_driver(layout, str(root), runner, environ, agent=agent),
        runner=lambda label: runner,
        inode_guard=lambda count, layout, site, env: inode_guard(runner, count, layout, site, env),
    )


def setting(environ: Mapping[str, str], name: str, default: Optional[int] = None) -> int:
    value = environ.get(name) or ""
    if not value and default is not None:
        return default
    if not value:
        raise FleetError(f"{name} is not set: the operator's maximum of federations at once (site.env); refused")
    if not value.isdigit() or not 1 <= int(value) <= CEILING:
        raise FleetError(f"{name} must be a whole number from 1 to {CEILING}")
    return int(value)


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


def state_facts(root: Path) -> Dict[str, Any]:
    """What a root's state file says of its federation's identity and progress; nothing when it has none."""
    try:
        state = hpcstate.load(root) if root.is_dir() else None
    except hpcstate.StateError:
        state = None
    if state is None:
        return {}
    sandbox = state.get("sandbox") or {}
    jobs = [state["allocation"].get("job"), sandbox.get("job") if sandbox.get("placement") == "separate" else None]
    return {"name": state["allocation"].get("name"), "jobs": [j for j in jobs if j], "phase": state.get("phase")}


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

    def speaker(self, label: str) -> Callable[[str], None]:
        return lambda line: print(f"hpcfed: {label}: {line}", file=sys.stderr, flush=True)

    # -- up and add -----------------------------------------------------------------------------------------------

    def up(self, layout: Path, count: int) -> int:
        maximum, parallel = setting(self.environ, MAX_VAR), setting(self.environ, PARALLEL_VAR, 1)
        if count < 2:
            raise FleetError("a fleet runs a campaign's shards, one per federation: at least two")
        if count > maximum:
            raise FleetError(f"{count} federations asked for, past the maximum of {maximum} ({MAX_VAR}); nothing made")
        layout = layout.resolve()
        spec, site = load_spec(layout), site_from_environment(self.environ)
        self.hooks.self_check()
        self.say(self.hooks.inode_guard(count, self.scratch_layout(spec), site, self.environ))
        try:
            os.mkdir(self.directory, 0o700)
        except FileExistsError:
            raise FleetError("the fleet's directory exists: a fleet's up makes its own and never adopts one") from None
        self.record.save(new_record(layout, spec.digest, count, maximum, parallel))
        with self.record.lifecycle("up"):
            return self.bring_up([f"fed-{n}" for n in range(1, count + 1)], layout, site, parallel)

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
            if spec.digest != record["layout"]["digest"]:
                raise FleetError("the fleet's layout file changed since its up: its federations would differ")
            self.hooks.self_check()
            self.say(self.hooks.inode_guard(count, self.scratch_layout(spec), site, self.environ))
            first = len(record["members"]) + 1
            return self.bring_up([f"fed-{n}" for n in range(first, first + count)], layout, site, parallel)

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

    def bring_up(self, labels: List[str], layout: Path, site: Any, parallel: int) -> int:
        made = [label for label in labels if self.claim(label)]

        def one(label: str) -> bool:
            root, said, out = root_of(self.directory, label), None, ""
            try:
                driver = self.hooks.driver(label, layout, root, True)
                driver.say = self.speaker(label)
                out = driver.up(site)
            except Exception as error:  # one federation's failure, named, never another's
                said = " ".join(f"{type(error).__name__}: {error}".split())
            facts = state_facts(root)
            ok = said is None and facts.get("phase") == LINKED

            def done(record: Dict[str, Any]) -> None:
                member = member_of(record, label)
                member.update(status="up" if ok else "failed", said=said, phase=facts.get("phase"))
                member.update(name=facts.get("name") or member["name"], job=facts.get("jobs") or None)

            self.record.update(done)
            for line in out.splitlines():
                self.say(f"{label}: {line}")
            if not ok:
                held = f"; its job(s) {', '.join(facts['jobs'])} may still be held" if facts.get("jobs") else ""
                self.say(f"{label}: up FAILED ({said or 'not linked'}){held}: `fleet.py down --member {label}`")
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
            found, said = fleetplan.launches(record, self.directory, only, resume, self.running)
            logs = self.directory / "logs"
            logs.mkdir(mode=0o700, exist_ok=True)
            for launch in found:
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
        for line in said:
            self.say(line)
        return 1 if failed or (only is not None and not found) else 0

    # -- status and down --------------------------------------------------------------------------------------------

    def status(self) -> int:
        record = self.record.read()
        reference = (record.get("reference") or {}).get("sha256")
        print(f"fleet: {len(record['shards'])} shard(s), {len(record['members'])} federation(s), platform "
              f"fingerprint {reference[:12] if reference else 'not yet planned'}")  # fmt: skip
        for member in record["members"]:
            facts = state_facts(root_of(self.directory, member["label"]))
            jobs = ", ".join(facts.get("jobs") or member.get("job") or []) or "none"
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
        root, said = root_of(self.directory, label), None
        try:
            driver = self.hooks.driver(label, layout, root, False)
            driver.say = self.speaker(label)
            self.say(f"{label}: {driver.down()}")
        except Exception as error:  # one federation's failure, named, never another's
            said = " ".join(f"{type(error).__name__}: {error}".split())
        ok = said is None and not root.exists()

        def done(record: Dict[str, Any]) -> None:
            member_of(record, label).update(status="down" if ok else "down-failed", said=said, shard=None)
            for shard in record["shards"]:
                if ok and shard["member"] == label:
                    shard.update(member=None, last=label)

        self.record.update(done)
        if not ok:
            self.say(f"{label}: down FAILED ({said or 'its root is still there'}): run down again")
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
