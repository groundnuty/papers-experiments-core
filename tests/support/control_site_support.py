"""A simulated site for the elastic controller's deploy tests: a fleet's directory (its record and its members' state
files, as `fleet.py up` writes them), a fake Slurm that answers the controller's read-only queries (`sinfo`,
`scontrol show job -o <id>`) and the site's invented quota command, and a transport that runs `observe` in process and
records every other command the controller gives the site, answering each as the test says."""

import contextlib
import io
import json
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hpc_fed_support import SITE
from hpc_fleet_support import SEPARATE_LAYOUT

QUOTA_PATTERN = r"files: (?P<used>[0-9]+) of (?P<quota>[0-9]+)"


def slurm_time(epoch: float) -> str:
    """An epoch as Slurm prints it: zone-less, in this host's local time (the login node's, the cluster's)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


@dataclass
class Job:
    state: str = "RUNNING"
    submitted: float = 0.0
    started: float | None = None
    ends: float | None = None
    name: str | None = None  # the name Slurm holds for the id now (another job's after a reuse)


@dataclass
class FakeSite:
    """One site: its fleet, its jobs by id, its partition's nodes by state, its quota, and what its commands answer."""

    base: Path
    deploy: Any  # the deploy modules (fleetcontrol, fleetobserve, fleetrecord, hpcstate, hpcslurm)
    now: float = 1_800_000_000.0
    jobs: dict = field(default_factory=dict)
    nodes: dict = field(default_factory=lambda: {"idle": 70, "mixed": 300, "allocated": 40, "idle~": 12})
    quota: tuple | None = (826_221, 1_000_000)
    sinfo_fails: bool = False
    scontrol_fails: bool = False
    calls: list = field(default_factory=list)  # every argv the fake Slurm or the quota command was given
    commands: list = field(default_factory=list)  # every other command the controller ran on the site
    answers: dict = field(default_factory=dict)  # a command's word ("add", "collect", "verify-copy", "down") -> answer
    on_command: Callable | None = None  # (argv) -> None: what the command does to the fleet (a down marks it down)
    processes: set = field(default_factory=set)  # (pid, start tick) of the processes alive on this login node

    def __post_init__(self) -> None:
        self.fleet = self.base / "fleet"
        self.fleet.mkdir(mode=0o700, parents=True)
        for folder in ("images", "logs", "scratch"):
            (self.base / folder).mkdir(exist_ok=True)
        record = self.deploy.fleetrecord.new_record(SEPARATE_LAYOUT, "a-digest", 2, 7, 1)
        self.deploy.fleetrecord.Record(self.fleet).save(record)
        self.environ = {**SITE, "SH": str(self.base / "images"), "L": str(self.base / "logs"),
                        "HPC_STORAGE_ROOT": str(self.base / "scratch"), "HPC_QUOTA_CMD": "invented-quota --files",
                        "HPC_QUOTA_PATTERN": QUOTA_PATTERN}  # fmt: skip

    # -- the fleet ---------------------------------------------------------------------------------------------------

    def add_member(self, label: str, status: str = "up", phase: str | None = "linked", verified: bool = True,
                   jobs: dict | None = None, relay: str | None = None) -> None:  # fmt: skip
        """A member of the fleet with its state file: its held jobs (`{"allocation": id, "sandbox": id}`, each a `Job`
        under the name up gave it), its phase and, for its one ingested space, whether it is verified; with `relay`
        (`here`, `elsewhere` or `gone`) its up recorded a fault relay serving, beating now on this login node or
        another, or last 300 s ago."""
        fleetrecord = self.deploy.fleetrecord
        name = f"hpcop-{label}-abc123"
        jobs = jobs or {}
        ids = {key: job_id for key, (job_id, _) in jobs.items()}
        for key, (job_id, job) in jobs.items():
            job.name = job.name or (name if key == "allocation" else f"{name}-sandbox")
            self.jobs[job_id] = job

        root = fleetrecord.root_of(self.fleet, label)
        made = [1, 2]
        if phase is not None:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            made = [os.lstat(root).st_dev, os.lstat(root).st_ino]

        def change(record: dict) -> None:
            member = fleetrecord.new_member(self.fleet, label)
            member.update(status=status, name=name, jobs=ids, made=made if status != "claimed" else None)
            record["members"].append(member)

        fleetrecord.Record(self.fleet).update(change)
        if phase is None:
            return
        marker = {"manifest": "b" * 64, "files": 3, "bytes": 30, "space_id": "space-one"}
        command = ["python", "hpcfed.py", "--layout", str(SEPARATE_LAYOUT), "--root", str(root), "up"]
        state = {"format": "expcore-hpc-federation/1", "project": "p", "phase": phase, "site": {}, "services": {},
                 "command": command,
                 "allocation": {"job": ids.get("allocation"), "name": name},
                 "data": {"ingested": {"delta": {"declared": True, "manifest": "b" * 64, "space_id": "space-one",
                                                 "verified": marker if verified else None}}}}  # fmt: skip
        if relay is not None:
            state["fault_relay"] = "serving"
            pid = 7000 + len(self.processes) + int(label.split("-")[1])
            if relay == "here":
                self.processes.add((pid, 99))
            wall = self.now - (300.0 if relay == "gone" else 3.0)
            beat = {"format": "relay", "nonce": "n" * 32, "pid": pid, "start": 99, "beat": 7, "wall": wall}
            (root / "fault-relay").mkdir(mode=0o700)
            (root / "fault-relay" / "heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")
        if "sandbox" in ids:
            state["sandbox"] = {"placement": "separate", "job": ids["sandbox"], "het_group": None,
                                "name": f"{name}-sandbox"}  # fmt: skip
        self.deploy.hpcstate.save(root, state)

    def set_status(self, label: str, status: str) -> None:
        fleetrecord = self.deploy.fleetrecord
        fleetrecord.Record(self.fleet).update(lambda record: fleetrecord.member_of(record, label).update(status=status))

    # -- the fake Slurm and quota command (the login node's Runner) ---------------------------------------------------

    def client_alive(self, client: Any) -> bool:
        """Whether the process (pid and start tick) is alive on this login node."""
        return (client.pid, client.start) in self.processes

    def run(self, argv: list, timeout: float) -> Any:
        from hpcslurm import Completed

        self.calls.append(list(argv))
        if argv[0] == "sinfo":
            assert argv == ["sinfo", "-h", "-p", SITE["HPC_PARTITION"], "-o", "%T %D"], argv
            if self.sinfo_fails:
                return Completed(1, "", "sinfo: error: the controller is down\n")
            return Completed(0, "".join(f"{state} {count}\n" for state, count in self.nodes.items()), "")
        if argv[:4] == ["scontrol", "show", "job", "-o"]:
            if self.scontrol_fails:
                return Completed(1, "", "slurm_load_jobs error: Socket timed out\n")
            job = self.jobs.get(argv[4])
            if job is None:
                return Completed(1, "", "slurm_load_jobs error: Invalid job id specified\n")
            fields = {"JobId": argv[4], "JobName": job.name, "JobState": job.state,
                      "SubmitTime": slurm_time(job.submitted),
                      "StartTime": slurm_time(job.started) if job.started else "Unknown",
                      "EndTime": slurm_time(job.ends) if job.ends else "Unknown"}  # fmt: skip
            return Completed(0, " ".join(f"{k}={v}" for k, v in fields.items()) + "\n", "")
        if argv[0] == "invented-quota":
            if self.quota is None:
                return Completed(3, "", "quota: cannot reach the quota server\n")
            return Completed(0, f"files: {self.quota[0]} of {self.quota[1]}\n", "")
        raise AssertionError(f"the controller asked the site something it never may: {argv}")

    # -- the transport --------------------------------------------------------------------------------------------

    def transport(self, environ: Any = None) -> "FakeTransport":
        return FakeTransport(self)


@dataclass
class FakeTransport:
    """`observe` run in process on the fake site; every other command recorded and answered as the test says."""

    site: FakeSite

    def run(self, config: Any, argv: list, timeout: float) -> tuple:
        words = list(argv)
        assert words[1:3] == ["-I", "-B"], f"a site command not run isolated: {words}"
        script = os.path.basename(words[3])
        if script == "fleetcontrol.py" and words[4] == "observe":
            printed = io.StringIO()
            hooks = self.site.deploy.fleetcontrol.Hooks(runner=lambda: self.site, clock=lambda: self.site.now,
                                                        say=printed.write)  # fmt: skip
            with contextlib.redirect_stderr(io.StringIO()) as err:
                code = self.site.deploy.fleetcontrol.main(words[4:], self.site.environ, hooks)
            return code, printed.getvalue(), err.getvalue()
        self.site.commands.append(words)
        if script == "fleetprepare.py":  # the site's own wrapper: really run, its child too (real, short commands)
            done = subprocess.run(words, env=dict(os.environ), cwd=str(self.site.fleet), stdin=subprocess.DEVNULL,
                                  capture_output=True, text=True, timeout=timeout, check=False)  # fmt: skip
            return done.returncode, done.stdout, done.stderr
        verb = words[4] if script == "shards.py" else words[words.index("--fleet") + 2]
        if self.site.on_command is not None:
            self.site.on_command(words)
        return self.site.answers.get(verb, (0, "", ""))


def observed(site: FakeSite) -> dict:
    """What `observe` prints for the site, parsed."""
    code, out, err = site.transport().run(None, ["python", "-I", "-B", "fleetcontrol.py", "observe", "--fleet",
                                                 str(site.fleet)], 60)  # fmt: skip
    assert code == 0, err
    return json.loads(out)
