"""A fake Slurm and Apptainer for the HPC federation driver's tests (deploy/hpc-federation/hpcfed.py): every command the
driver runs on the login node is answered here, and nothing reaches a cluster, a node or a container.

`FakeSlurm` is a `hpcslurm.Runner`: `sbatch` gives a job id, `scontrol` reports the job as asked (or as a test
changes it), `squeue -s` lists the steps still running, `scancel` ends a job or one step. A control step's command
(behind `srun ... /usr/bin/env -i <allowlist>`) is answered by what it runs: the node facts, the zone's exports (its CA
the fake's `ca_pem`: a real one where the core's real phase loads it), a teardown, the node check, the core's step,
the agent's node's sweep (run on this host with `sweep_here`, whose tmpfs then stands for the node's). A service
step writes its log as the bundle's batch scripts do: the work directory, then the zone's verdict, or the provider's
wait for its token and, once its token is delivered, its verdict.

A heterogeneous job (`sbatch <component 0> : <component 1>`) is kept as Slurm 24.11 documents one
(heterogeneous_jobs.html): its leader's id is component 0's, component k's own id is the leader's plus k, `scontrol show
job` answers one record per component (`HetJobId`, `HetJobOffset`), `squeue` lists `<leader>+<k>`, a scancel of the
leader ends every component, and an option of Slurm's propagated list (`PROPAGATED`) passes from component 0 to the
next while `--partition`, `--nodes`, `--ntasks-per-node` and `--cpus-per-task` fall back to Slurm's defaults. A second
`sbatch` gets `second_id` (`[hpc] sandbox_placement = "separate"`). Every step must land on a node of the job and the
component (`--het-group`) it names, or the fake fails the test.

As the site's Slurm does (its first live slot, 16 Nov 2026): a step of a heterogeneous job that names no `--het-group`
is refused, its srun exiting 1 with `HET_REFUSAL` on stderr and no step made, component 0 included; and a step of
component k > 0 is listed by `squeue -s -j <component k's own id>` alone, as `<component id>.<n>`, never under the
leader's id. How a step of component 0 is listed was not seen live; here it is `<leader>.<n>`, under the leader's id.

On a site whose switch plugin gives each job a virtual network identifier, a heterogeneous job's steps on component 0
failed with `INTERCONNECT_REFUSAL` from a point on (the live slot of 16 Nov 2026), while the same steps with
`--network=no_vni` ran, and component 1's ran without it. `interconnect_after=N` stands for that: the Nth step of
component 0 that names no `--network` is the last to start, and every later one is refused as that site's was. The cause
(a count of steps per component, or exhausted identifiers) was not established, so the fake counts and nothing more.
"""

import hashlib
import json
import re
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from fake_elf import bundle_members, write_tar

HPC = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation"
LAYOUT = HPC / "layouts" / "pilot-shape.toml"
PILOT_TEMPLATE = LAYOUT.parent / "pilot-shape.platform.toml.template"


def without_sandbox(template: str) -> str:
    """A platform template without its `[sandbox]` (the pilot's last table and everything under it): a federation
    with no agent's node, or one to give another `[sandbox]`."""
    assert "\n[sandbox]" in template, "the template declares no [sandbox]"
    return template.split("\n[sandbox]", 1)[0] + "\n"


def services_only(folder: Path) -> Path:
    """The pilot's layout and its template without `[sandbox]`, written into `folder`: the services alone."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / LAYOUT.name).write_text(LAYOUT.read_text(encoding="utf-8"), encoding="utf-8")
    template = PILOT_TEMPLATE.read_text(encoding="utf-8")
    (folder / PILOT_TEMPLATE.name).write_text(without_sandbox(template), encoding="utf-8")
    return folder / LAYOUT.name


SITE = {
    "HPC_ACCOUNT": "test-account",
    "HPC_PARTITION": "test-partition",
    "HPC_STEP_PATH": "/usr/bin:/bin",
    "HPC_NODE_TMP": "/tmp",
}
PEM = "-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n"  # no certificate: for a core's phase stood in


def agent_site(directory: Path, sif: bytes = b"an invented SIF image\n", shim: bytes = b"an invented egress shim\n",
               work_root: Path | None = None) -> dict:  # fmt: skip
    """The site's variables of the agent's Apptainer sandbox (the pilot template's `[sandbox]`), its files written
    into `directory` and pinned by their SHA-256 (oneclient's bundle an invented tar a host mount can unpack:
    fake_elf); the work root `work_root`, or one under `directory` (made by the shard step on the agent's node, never
    here)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "agent.sif").write_bytes(sif)
    (directory / "egresslog.so").write_bytes(shim)
    bundle = directory / "oneclient-bundle.tar"
    return {
        "HPC_AGENT_SIF": str(directory / "agent.sif"),
        "HPC_AGENT_SIF_SHA256": hashlib.sha256(sif).hexdigest(),
        "HPC_EGRESS_SHIM": str(directory / "egresslog.so"),
        "HPC_EGRESS_SHIM_SHA256": hashlib.sha256(shim).hexdigest(),
        "HPC_ONECLIENT_BUNDLE": str(bundle),
        "HPC_ONECLIENT_BUNDLE_SHA256": write_tar(bundle, bundle_members()),
        "HPC_AGENT_WORK_ROOT": str(work_root or directory / "agent-work"),
    }


def data_stand_in(differ: str | None = None) -> Any:
    """`hpcboot.core_phase`'s `data`: each space's data read back as a live federation holds it once up wrote its
    baseline (`fedcontent.Content.reset`, which reads every file back itself): the baseline plan's files. With `differ`,
    one file of that space has one byte other, as a federation whose data is not its declared manifest's."""
    from fedlayout import baseline_plan

    def reader(boot: Any) -> Any:
        plan = baseline_plan(boot.layout.spec)

        def read(space: str) -> Any:
            files = {f.path: (f.size, hashlib.sha256(f.content()).hexdigest()) for f in plan[space]}
            if space == differ and files:
                first = sorted(files)[0]
                files[first] = (files[first][0], hashlib.sha256(b"one byte other").hexdigest())
            dirs = {str(d) for f in plan[space] for d in PurePosixPath(f.path).parents if str(d) != "."}
            return files, dirs

        return read

    return reader


def manifest_values(root: Path, layout: Path = LAYOUT) -> dict:
    """The `@data_manifest.<space>@` placeholders of a layout's template, each a manifest of the space's baseline
    written into `root` as `up` writes it (an imported space's too: a stand-in for the layout's own manifest)."""
    import fedspec
    import hpcdata
    from fedlayout import baseline_plan

    from expcore_onedata.importmanifest import write_manifest

    spec = fedspec.load_spec(layout)
    plan, directory = baseline_plan(spec), root / "data-manifests"
    directory.mkdir(parents=True, exist_ok=True)
    found = {}
    for space in spec.spaces:
        found[space.name] = directory / f"{space.name}.json"
        write_manifest(hpcdata.baseline_manifest(plan.get(space.name, ())), found[space.name])
    return hpcdata.values(found)


def pin_values(directory: Path) -> dict:
    """The agent's sandbox's placeholders as `up` fills them (`hpcpins.values`), over `agent_site(directory)`."""
    import hpcpins
    import hpcsettings

    site = {**SITE, **agent_site(directory), "SH": "/invented/images", "L": "/invented/logs"}
    return hpcpins.values(hpcsettings.site_from_environment(site))


# Options Slurm passes from one component of a heterogeneous job to the next (heterogeneous_jobs.html, those sbatch
# takes that the driver uses); every other option of a later component falls back to Slurm's default.
PROPAGATED = ("--job-name", "--account", "--time", "--mem", "--no-requeue", "--export", "--output", "--chdir")
# What the site's srun says, exiting 1, of a step of a heterogeneous job with no --het-group (live slot 1).
HET_REFUSAL = "srun: error: No directives to start application on any available hetjob components\n"
INTERCONNECT_REFUSAL = "srun: error: task 0 launch failed: Error configuring interconnect\n"


@contextmanager
def hpc_on_path():
    """The driver's modules, imported by their plain names as hpcfed.py imports them, with both deploy directories on
    `sys.path` only for as long as the context lasts (the driver imports some modules lazily, by plain name).

    `sys.path` is restored when the context ends: `deploy/hpc-federation` holds a `facts/` directory, and left on the
    path it would be found as a namespace package `facts` by every later test that imports a module of that name (the
    plugin-module tests do)."""
    with pytest.MonkeyPatch.context() as patch:
        for path in (HPC.parent / "compose-federation", HPC):
            patch.syspath_prepend(str(path))
        yield _import_hpc()


def _import_hpc():
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
    het_group: int | None = None
    refused: bool = False  # srun refuses the step, as the site's does: it names no component, or its interconnect fails
    refusal: str = HET_REFUSAL  # what srun prints to stderr when it refuses the step
    network: str | None = None  # srun's --network=<value>, when the step names one (the site's HPC_STEP_NETWORK)


def parse_srun(argv: list) -> Srun:
    assert argv[0] == "srun"
    words = iter(argv[1:])
    found = {"exact": False, "overlap": False, "output": None}
    for word in words:
        if word == "/usr/bin/env":
            break
        if word.startswith("--jobid="):
            found["job"] = word.split("=", 1)[1]
        elif word.startswith("--het-group="):
            found["het_group"] = int(word.split("=", 1)[1])
        elif word.startswith("--network="):
            found["network"] = word.split("=", 1)[1]
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
    stray_clients: list = field(default_factory=list)  # srun clients of the job found on the host, never recorded
    unkillable: list = field(default_factory=list)  # clients that survive their end
    dead_clients: list = field(default_factory=list)  # clients that exited by themselves (a refused step's)
    running_recorded: dict = field(default_factory=dict)  # fields scontrol reports once the job has been read twice
    reads: int = 0
    second_id: str = "4343"  # the id a second sbatch prints (the agent's own held job)
    submitted: dict = field(default_factory=dict)  # job id -> each component's options, as Slurm took them
    component_recorded: dict = field(default_factory=dict)  # (job id, component) -> fields reported otherwise
    job_states: dict = field(default_factory=dict)  # a job id other than job_id -> its state (else RUNNING)
    node_lists: dict = field(default_factory=dict)  # (job id, component) -> the nodes it got, when a test says
    ca_pem: str = PEM  # what the zone's CA export prints: a real CA's PEM where the core's real phase loads it
    node_tmp: str = "/tmp"  # the nodes' tmpfs, where a service's work directory is (the site's HPC_NODE_TMP)
    sweep_here: bool = False  # run down's sweep of the agent's node on this host (its tmpfs is here), else answer it
    sweeps: list = field(default_factory=list)  # each sweep step down launched
    leftovers: dict = field(default_factory=dict)  # node -> the work directories of ours leftovers.py counts there
    interconnect_after: int | None = None  # the steps of component 0 with no --network that start before the rest fail
    component_zero_steps: int = 0  # the steps of component 0 that named no --network, so far

    # -- the Runner interface ------------------------------------------------------------------------------------

    def run(self, argv, timeout):
        from hpcslurm import Completed

        argv = list(argv)
        self.calls.append(argv)
        tool = argv[0]
        if tool == "sbatch":
            ident = self.second_id if self.job_id in self.submitted else self.job_id
            self.submitted[ident] = self._components(argv[1:])
            if ident == self.job_id:
                self.asked = self.submitted[ident][0]
            return Completed(0, f"{ident}\n", "")
        if argv[:3] == ["scontrol", "show", "job"]:
            ident = argv[4]
            if self.gone_job(ident):
                return Completed(1, "", "slurm_load_jobs error: Invalid job id specified\n")
            if ident == self.job_id:
                self.reads += 1
            return Completed(0, self.scontrol_lines(ident), "")
        if argv[:3] == ["scontrol", "show", "hostnames"]:
            return Completed(0, "\n".join(n for n in argv[3].split(",") if n) + "\n", "")
        if tool == "squeue" and "-s" in argv:
            lines = []
            for ident in argv[argv.index("-j") + 1].split(","):  # a job's or a component's own id
                if self.leader_of(ident) is None or self.gone_job(self.leader_of(ident)):
                    continue
                lines += [f"{ident}.batch batch"] if self.leader_of(ident) == ident else []
                lines += [f"{ident}.extern extern"]
                lines += [f"{step} {name}" for name, step in self.steps.items() if step.split(".")[0] == ident]
            return Completed(0, "".join(f"{line}\n" for line in lines), "")
        if tool == "squeue" and "-n" in argv:
            name = argv[argv.index("-n") + 1]
            named = [i for i in self.listed_ids() if self._options(i.split("+")[0])[0].get("--job-name") == name]
            lines = [f"{ident} {name}" for ident in named]
            return Completed(0, "".join(f"{line}\n" for line in lines) if self.queued else "", "")
        if tool == "squeue":
            ident = argv[argv.index("-j") + 1]
            return Completed(0, "".join(f"{i}\n" for i in self.listed_ids() if i.split("+")[0] == ident), "")
        if tool == "scancel":
            target = argv[1]
            self.cancelled.append(target)
            if "." in target:
                self.steps = {n: s for n, s in self.steps.items() if s != target}
            else:  # a job's cancel ends its steps, a leader's those of every component
                self.steps = {n: s for n, s in self.steps.items() if self.leader_of(s.split(".")[0]) != target}
            return Completed(0, "", "")
        if tool == "sacct":
            return Completed(0, "", "")
        if argv[1:4] == ["-B", "-m", "expcore_onedata.fault_relay"]:
            self.relay_calls.append(argv[4])
            return Completed(self.relay_rc.get(argv[4], 0), "", "")
        raise AssertionError(f"an unexpected command: {argv}")

    @staticmethod
    def _components(words) -> list:
        """sbatch's words, one options dict per component, each later one with what Slurm propagates to it."""
        groups, current = [], {}
        for word in words:
            if word == ":":
                groups.append(current)
                current = {}
            else:
                key, _, value = word.partition("=")
                current[key] = value
        groups.append(current)
        first = groups[0]
        return [first] + [{**{k: first[k] for k in PROPAGATED if k in first}, **group} for group in groups[1:]]

    def _options(self, ident) -> list:
        return self.submitted.get(ident) or [self.asked]

    def leader_of(self, ident) -> str | None:
        """The job an id belongs to: a job's own id, a component's own id (the leader's plus k) or `<leader>+<k>`, as
        its leader's id; None for an id no sbatch printed."""
        head = ident.split("+")[0]
        for job in self.submitted or {self.job_id: None}:
            own = {str(int(job) + k) for k in range(1, len(self._options(job)))}
            if head == job or ("+" not in ident and head in own):
                return job
        return None

    def listing_of(self, srun: Srun) -> str:
        """The id squeue -s lists a step under: its job's, or its component's own id for a component above 0."""
        return str(int(srun.job) + srun.het_group) if srun.het_group else srun.job

    def gone_job(self, ident) -> bool:
        if ident in self.cancelled:
            return True
        return (self.state if ident == self.job_id else self.job_states.get(ident, "RUNNING")) == "GONE"

    def listed_ids(self) -> list:
        """What squeue's %i lists of every job still known: a job's id, or `<leader>+<k>` per component."""
        found = []
        for ident in self.submitted or {self.job_id: None}:
            if not self.gone_job(ident):
                count = len(self._options(ident))
                found += [ident] if count == 1 else [f"{ident}+{k}" for k in range(count)]
        return found

    def placed(self, ident, component) -> list:
        """The nodes a component got: the first free ones, in submit order, unless a test names them."""
        if (ident, component) in self.node_lists:
            return list(self.node_lists[(ident, component)])
        start = 0
        for job in self.submitted or {self.job_id: None}:
            for index, options in enumerate(self._options(job)):
                count = int(options.get("--nodes", "1"))
                if (job, index) == (ident, component):
                    return self.nodes[start : start + count]
                start += count
        return []

    def scontrol_lines(self, ident) -> str:
        components = self._options(ident)
        lines = []
        for index, options in enumerate(components):
            count = int(options.get("--nodes", "1"))
            cpus = int(options.get("--cpus-per-task", "1"))
            het = {"HetJobId": ident, "HetJobOffset": str(index)} if len(components) > 1 else {}
            fields = {
                "JobId": str(int(ident) + index),
                **het,
                "JobName": options.get("--job-name"),
                "JobState": self.state if ident == self.job_id else self.job_states.get(ident, "RUNNING"),
                "TimeLimit": options.get("--time"),
                "Requeue": "0" if "--no-requeue" in options else "1",
                "Account": options.get("--account"),
                "Partition": options.get("--partition", "default-partition"),
                "NumNodes": str(count),
                "NumCPUs": str(count * cpus),
                "MinMemoryNode": options.get("--mem"),
                "NodeList": ",".join(self.placed(ident, index)),
                "StartTime": "2026-11-16T10:00:00",
                "EndTime": "2026-11-16T12:00:00",
            }
            if (ident, index) == (self.job_id, 0):
                fields.update({**self.recorded, **(self.running_recorded if self.reads > 2 else {})})
            fields.update(self.component_recorded.get((ident, index), {}))
            lines.append(" ".join(f"{key}={value}" for key, value in fields.items()))
        return "\n".join(lines) + "\n"

    def _step(self, argv) -> Srun:
        srun = parse_srun(list(argv))
        self.calls.append(list(argv))
        self.sruns.append(srun)
        if self.submitted:  # Slurm places a step only on a node of the job and the component it names
            assert srun.job in self.submitted, f"a step of a job never submitted: {srun.job}"
            assert srun.node in self.placed(srun.job, srun.het_group or 0), (
                f"a step on {srun.node}, outside component {srun.het_group or 0} of job {srun.job}"
            )
            srun.refused = srun.het_group is None and len(self.submitted[srun.job]) > 1
            if srun.het_group == 0 and srun.network is None:
                self.component_zero_steps += 1
                if self.interconnect_after is not None and self.component_zero_steps > self.interconnect_after:
                    srun.refused, srun.refusal = True, INTERCONNECT_REFUSAL
        return srun

    def start(self, argv, data=None):
        from hpcslurm import Completed

        srun = self._step(argv)
        if srun.refused:
            return FakeProcess(Completed(1, "", srun.refusal))
        if self.on_control is not None:
            answer = self.on_control(srun)
            if answer is not None:
                return answer
        return FakeProcess(Completed(*self.answer(srun)))

    def answer(self, srun: Srun) -> tuple:
        command = srun.command
        script = Path(command[1]).name if len(command) > 1 else command[0]
        if any(Path(word).name == "nodefacts.py" for word in command):
            index = self.nodes.index(srun.node)
            facts = {
                "fqdn": f"{srun.node}.cluster.example.org",
                "address": f"10.0.0.{index + 1}",
                "uid": 5001,
                "gid": 5001,
            }
            return 0, json.dumps(facts) + "\n", ""
        if script == "zone-export.sh":
            return (0, "A1b2C3d4E5f6G7h8I9j0K1l2\n", "") if command[2] == "passphrase" else (0, self.ca_pem, "")
        if script == "teardown.sh":
            workdir = command[command.index("--workdir") + 1]
            self.steps = {n: s for n, s in self.steps.items() if not n.endswith(self._service_of(workdir))}
            return self.teardown_rc, "", "ok: service stopped\n"
        if command[0] == "test":
            return 0, "", ""
        if any(Path(word).name == "leftovers.py" for word in command):  # down's count of a node's work directories
            return 0, json.dumps({"kind": command[-2], "count": self.leftovers.get(srun.node, 0)}) + "\n", ""
        if any(Path(word).name == "shards.py" for word in command) and "sweep" in command:
            self.sweeps.append(srun)
            if not self.sweep_here:
                return 0, "shards: sweep: 0 step directory(ies) removed; kept: none\n", ""
            env = dict(word.split("=", 1) for word in srun.env)  # this host stands for the agent's node: run it here
            done = subprocess.run([sys.executable, *command[1:]], capture_output=True, text=True, env=env, check=False)
            return done.returncode, done.stdout, done.stderr
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
        if mint.refused or deliver.refused:  # no token is minted, or none delivered
            ends = [Completed(1, "", s.refusal) if s.refused else Completed(0, "", "") for s in (mint, deliver)]
            return FakeProcess(ends[0]), FakeProcess(ends[1])
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
        if srun.refused:  # srun exits at once with its error in the client's log; no step is made, no log written
            Path(client_log).write_text(srun.refusal, encoding="utf-8")
            self.dead_clients.append(Client(20000 + len(self.spawned), 77))
            return self.dead_clients[-1]
        self.next_step += 1
        self.steps[srun.name] = f"{self.listing_of(srun)}.{self.next_step}"
        script = Path(srun.command[1]).name
        kind = "oz" if script == "zone.sbatch" else "op"
        service = "zone" if kind == "oz" else srun.command[4]
        self.logs[service] = srun.output
        workdir = f"{self.node_tmp}/hpcop-{kind}.{'abcdef' if kind == 'oz' else service[:3] + 'xyz'}"
        lines = [f"hpcop: work directory {workdir}"]
        lines.append("hpcop: OK zone" if kind == "oz" else "hpcop: waiting for the registration token (mint | deliver)")
        Path(srun.output).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return Client(10000 + self.next_step, 77)

    def client_alive(self, client) -> bool:
        return client not in self.ended_clients and client not in self.dead_clients

    def clients_of(self, job):
        return list(self.stray_clients)

    def end_client(self, client) -> bool:
        self.ended_clients.append(client)
        return client not in self.unkillable

    # -- what the tests read ---------------------------------------------------------------------------------------

    def control_names(self) -> list:
        return [s.name for s in self.sruns if s not in self.spawned]
