"""A fake Slurm and Apptainer for the HPC federation driver's tests (deploy/hpc-federation/hpcfed.py): every command the
driver runs on the login node is answered here, and nothing reaches a cluster, a node or a container.

`FakeSlurm` is a `hpcslurm.Runner`: `sbatch` gives a job id, `scontrol` reports the job as asked (or as a test
changes it), `squeue -s` lists the steps still running, `scancel` ends a job or one step. A control step's command
(behind `srun ... /usr/bin/env -i <allowlist>`) is answered by what it runs: the node facts, the zone's exports, a
teardown, the node check, the core's step. A service step writes its log as the bundle's batch scripts do: the work
directory, then the zone's verdict, or the provider's wait for its token and, once its token is delivered, its verdict.
"""

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
LAYOUT = HPC / "layouts" / "pilot-shape.toml"
SITE = {
    "HPC_ACCOUNT": "test-account",
    "HPC_PARTITION": "test-partition",
    "HPC_STEP_PATH": "/usr/bin:/bin",
    "HPC_NODE_TMP": "/tmp",
}
PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"


def import_hpc():
    """The driver's modules, imported by their plain names as hpcfed.py imports them."""
    for path in (HPC, HPC.parent / "compose-federation"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import hpcboot
    import hpcdriver
    import hpcfed
    import hpclaunch
    import hpcsettings
    import hpcslurm
    import hpcstate

    return type(
        "Hpc",
        (),
        {
            "boot": hpcboot,
            "driver": hpcdriver,
            "cli": hpcfed,
            "launch": hpclaunch,
            "settings": hpcsettings,
            "slurm": hpcslurm,
            "state": hpcstate,
        },
    )


class FakeProcess:
    """A command that has finished: its result is known at once."""

    def __init__(self, completed, *, hangs: bool = False) -> None:
        self.completed = completed
        self.hangs = hangs
        self.killed = False

    def wait(self, timeout: float) -> bool:
        return not self.hangs or self.killed

    def kill(self) -> None:
        self.killed = True

    def result(self):
        return self.completed


@dataclass
class Srun:
    """One srun command line, taken apart."""

    argv: list
    job: str
    node: str
    name: str
    cpus: int
    mem: str
    env: list
    command: list
    output: str | None
    exact: bool
    overlap: bool


def parse_srun(argv: list) -> Srun:
    assert argv[0] == "srun"
    words = iter(argv[1:])
    found = {"exact": False, "overlap": False, "output": None}
    for word in words:
        if word == "/usr/bin/env":
            break
        if word.startswith("--jobid="):
            found["job"] = word.split("=", 1)[1]
        elif word == "--exact":
            found["exact"] = True
        elif word == "--overlap":
            found["overlap"] = True
        elif word == "-w":
            found["node"] = next(words)
        elif word == "-c":
            found["cpus"] = int(next(words))
        elif word.startswith("--mem="):
            found["mem"] = word.split("=", 1)[1]
        elif word == "-J":
            found["name"] = next(words)
        elif word.startswith("--output="):
            found["output"] = word.split("=", 1)[1]
    rest = list(words)
    assert rest[0] == "-i", "every step's command runs behind env -i"
    env = [w for w in rest[1:] if re.fullmatch(r"[A-Z][A-Z0-9_]*=.*", w)]
    command = rest[1 + len(env) :]
    return Srun(argv, env=env, command=command, **found)


@dataclass
class FakeSlurm:
    """The login node's view of a cluster with one user's held job."""

    nodes: list = field(default_factory=lambda: [f"node{i:02d}" for i in range(1, 9)])
    job_id: str = "4242"
    state: str = "RUNNING"
    recorded: dict = field(default_factory=dict)  # fields scontrol reports otherwise than asked
    calls: list = field(default_factory=list)
    steps: dict = field(default_factory=dict)  # name -> step id, running now
    sruns: list = field(default_factory=list)
    spawned: list = field(default_factory=list)
    cancelled: list = field(default_factory=list)
    ended_clients: list = field(default_factory=list)
    asked: dict = field(default_factory=dict)
    logs: dict = field(default_factory=dict)  # service name -> log path
    delivered: list = field(default_factory=list)
    on_control: Callable | None = None  # a test's own answer to a control step, or None for the default
    teardown_rc: int = 0
    next_step: int = 0
    queued: bool = True
    relay_calls: list = field(default_factory=list)  # alive, stop, ... in order
    relay_rc: dict = field(default_factory=dict)  # a relay verb's exit status, when not 0
    relays: list = field(default_factory=list)  # each relay started: its argv

    # -- the Runner interface ------------------------------------------------------------------------------------

    def run(self, argv, timeout):
        from hpcslurm import Completed

        argv = list(argv)
        self.calls.append(argv)
        tool = argv[0]
        if tool == "sbatch":
            self.asked = {w.split("=", 1)[0]: w.split("=", 1)[1] for w in argv[1:] if "=" in w}
            return Completed(0, f"{self.job_id}\n", "")
        if argv[:3] == ["scontrol", "show", "job"]:
            if self.state == "GONE" or self.job_id in self.cancelled:
                return Completed(1, "", "slurm_load_jobs error: Invalid job id specified\n")
            return Completed(0, self.scontrol_line(), "")
        if argv[:3] == ["scontrol", "show", "hostnames"]:
            count = int(self.asked.get("--nodes", "0"))
            return Completed(0, "\n".join(self.nodes[:count]) + "\n", "")
        if tool == "squeue" and "-s" in argv:
            if self.job_id in self.cancelled:
                return Completed(0, "", "")
            lines = [f"{self.job_id}.batch batch", f"{self.job_id}.extern extern"]
            lines += [f"{step} {name}" for name, step in self.steps.items()]
            return Completed(0, "\n".join(lines) + "\n", "")
        if tool == "squeue" and "-n" in argv:
            name = argv[argv.index("-n") + 1]
            listed = self.queued and self.asked.get("--job-name") == name and self.job_id not in self.cancelled
            return Completed(0, f"{self.job_id} {name}\n" if listed else "", "")
        if tool == "squeue":
            gone = self.job_id in self.cancelled or self.state == "GONE"
            return Completed(0, "" if gone else f"{self.job_id}\n", "")
        if tool == "scancel":
            target = argv[1]
            self.cancelled.append(target)
            if "." in target:
                self.steps = {n: s for n, s in self.steps.items() if s != target}
            else:
                self.steps = {}
            return Completed(0, "", "")
        if tool == "sacct":
            return Completed(0, "", "")
        if argv[1:3] == ["-m", "expcore_onedata.fault_relay"]:
            self.relay_calls.append(argv[3])
            return Completed(self.relay_rc.get(argv[3], 0), "", "")
        raise AssertionError(f"an unexpected command: {argv}")

    def scontrol_line(self) -> str:
        count = int(self.asked.get("--nodes", "0"))
        cpus = int(self.asked.get("--cpus-per-task", "0"))
        fields = {
            "JobId": self.job_id,
            "JobName": self.asked.get("--job-name"),
            "JobState": self.state,
            "TimeLimit": self.asked.get("--time"),
            "Requeue": "0",
            "Account": self.asked.get("--account"),
            "Partition": self.asked.get("--partition"),
            "NumNodes": str(count),
            "NumCPUs": str(count * cpus),
            "MinMemoryNode": self.asked.get("--mem"),
            "NodeList": f"node[01-{count:02d}]",
            "StartTime": "2026-11-16T10:00:00",
            "EndTime": "2026-11-16T12:00:00",
            **self.recorded,
        }
        return " ".join(f"{key}={value}" for key, value in fields.items()) + "\n"

    def _step(self, argv) -> Srun:
        srun = parse_srun(list(argv))
        self.calls.append(list(argv))
        self.sruns.append(srun)
        return srun

    def start(self, argv, data=None):
        from hpcslurm import Completed

        srun = self._step(argv)
        if self.on_control is not None:
            answer = self.on_control(srun)
            if answer is not None:
                return answer
        return FakeProcess(Completed(*self.answer(srun)))

    def answer(self, srun: Srun) -> tuple:
        command = srun.command
        script = Path(command[1]).name if len(command) > 1 else command[0]
        if script == "nodefacts.py" or (len(command) > 2 and Path(command[2]).name == "nodefacts.py"):
            index = self.nodes.index(srun.node)
            facts = {
                "fqdn": f"{srun.node}.cluster.example.org",
                "address": f"10.0.0.{index + 1}",
                "uid": 5001,
                "gid": 5001,
            }
            return 0, json.dumps(facts) + "\n", ""
        if script == "zone-export.sh":
            return (0, "A1b2C3d4E5f6G7h8I9j0K1l2\n", "") if command[2] == "passphrase" else (0, PEM, "")
        if script == "teardown.sh":
            workdir = command[command.index("--workdir") + 1]
            self.steps = {n: s for n, s in self.steps.items() if not n.endswith(self._service_of(workdir))}
            return self.teardown_rc, "", "ok: service stopped\n"
        if command[0] == "test":
            return 0, "", ""
        if "hpcfed.py" in " ".join(command):
            return 0, "core: the link check passed\n", ""
        raise AssertionError(f"an unexpected control step: {command}")

    def _service_of(self, workdir: str) -> str:
        for name, log in self.logs.items():
            if workdir in Path(log).read_text(encoding="utf-8"):
                return f"-{name}-run-1"
        return "-no-such-service"

    def pipe(self, first, second):
        from hpcslurm import Completed

        mint, deliver = self._step(first), self._step(second)
        assert Path(mint.command[1]).name == "mint-registration-token.sh"
        assert Path(deliver.command[1]).name == "deliver-token.sh"
        provider = mint.command[2]
        self.delivered.append(provider)
        log = Path(self.logs[provider])
        log.write_text(log.read_text(encoding="utf-8") + "hpcop: OK provider pid-x at a.example, storage s\n")
        return FakeProcess(Completed(0, "", "")), FakeProcess(Completed(0, "", ""))

    def spawn(self, argv, client_log):
        from hpcslurm import Client

        if argv[0] == "/usr/bin/env":  # the login node's fault relay, not a step
            self.relays.append(list(argv))
            self.relay_calls.append("serve")
            return Client(9999, 1)
        srun = self._step(argv)
        self.spawned.append(srun)
        self.next_step += 1
        self.steps[srun.name] = f"{self.job_id}.{self.next_step}"
        script = Path(srun.command[1]).name
        kind = "oz" if script == "zone.sbatch" else "op"
        service = "zone" if kind == "oz" else srun.command[4]
        self.logs[service] = srun.output
        workdir = f"/tmp/hpcop-{kind}.{'abcdef' if kind == 'oz' else service[:3] + 'xyz'}"
        lines = [f"hpcop: work directory {workdir}"]
        lines.append("hpcop: OK zone" if kind == "oz" else "hpcop: waiting for the registration token (mint | deliver)")
        Path(srun.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return Client(10000 + self.next_step, 77)

    def client_alive(self, client) -> bool:
        return client not in self.ended_clients

    def end_client(self, client) -> None:
        self.ended_clients.append(client)

    # -- what the tests read ---------------------------------------------------------------------------------------

    def control_names(self) -> list:
        return [s.name for s in self.sruns if s not in self.spawned]
