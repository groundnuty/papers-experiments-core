"""Slurm, as the HPC federation driver uses it from the login node (DESIGN.md, sections 1, 3 and 5).

- **The held job**: one `sbatch` holding the federation's nodes with `exec sleep`, `--no-requeue` and a `--time`; what
  Slurm recorded is read back (`scontrol show job -o`) and the job is cancelled, by its own id only, unless it is what
  was asked for. A heterogeneous job (`sbatch <component 0> : <component 1>`, the agent's node as component 1) is read
  back component by component: one record each, by `HetJobOffset`; squeue lists it as `<leader>+<k>`, and a cancel of
  its leader's id ends every component.
- **Every step** is `srun --jobid --exact` with a name, its cores and memory, and `/usr/bin/env -i` with an explicit
  allowlist in front of its command, so neither the login environment nor a site prolog decides what it sees. No step
  uses `--overlap`.
- **A control step's srun client** that outlives its bound is ended only once its step has been gone from `squeue -s`
  for three polls in a row (a finished step's client can hang on the login node); a step still listed at the bound is
  cancelled by its own id.
- **A service step** is launched detached (its own session), so it outlives the driver; its client is recorded by pid
  and start tick and ended by `down` only once its step is gone.

Every call goes through a `Runner`, so the tests run all of it against a fake Slurm.
"""

import contextlib
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Mapping, NamedTuple, Optional, Sequence, Tuple

ENV = "/usr/bin/env"
GONE_POLLS = 3  # a step absent from squeue -s this many polls in a row has ended
READBACK = ("JobName", "TimeLimit", "Requeue", "Account", "Partition", "NumNodes", "NumCPUs", "MinMemoryNode")
WAITING_STATES = ("PENDING", "CONFIGURING", "REQUEUED", "RESIZING")
JOB_LINE = re.compile(r"([0-9]{1,12})(?:;[A-Za-z0-9_.-]+)?")
JOB_LISTED = re.compile(r"([0-9]{1,12})(?:\+[0-9]{1,6})?")  # squeue's %i: a job, or a component <leader>+<k>
# A step as squeue -s lists it: of a job, or of a heterogeneous job's component (<leader>+<k>.<n>, or the
# component's own id); the batch and extern steps included.
STEP_LINE = re.compile(r"([0-9]{1,12}(?:\+[0-9]{1,6})?\.(?:[0-9]{1,9}|batch|extern))\s+(\S+)")
STEP_ID = re.compile(r"[0-9]{1,12}(?:\+[0-9]{1,6})?\.[0-9]{1,9}")
OFFSET = re.compile(r"[0-9]{1,6}")


class SlurmError(RuntimeError):
    """A Slurm command that failed, or a job or step that is not what was asked for."""


class StepTimeout(SlurmError, TimeoutError):
    """A step that was still listed at its bound; it was cancelled by its own id."""


class Completed(NamedTuple):
    returncode: Optional[int]  # None when the step's own status could not be read
    stdout: str
    stderr: str


class Client(NamedTuple):
    """A detached srun client on the login node: its pid and its start tick (stat field 22), so a reused pid is never
    signalled."""

    pid: int
    start: int


class Process:
    """A running command whose output is kept: wait for it with a bound, read its result, or kill it."""

    def __init__(self, popen: "subprocess.Popen[bytes]", data: Optional[bytes]) -> None:
        self.popen = popen
        self.data = data
        self.out = b""
        self.err = b""
        self.done = False

    def wait(self, timeout: float) -> bool:
        if self.done:
            return True
        try:
            out, err = self.popen.communicate(self.data, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.data = None  # communicate wrote it already; a retry must not write it again
            return False
        self.out, self.err, self.done = out or b"", err or b"", True
        return True

    def kill(self) -> None:
        if not self.done:
            self.popen.kill()
            out, err = self.popen.communicate()
            self.out, self.err, self.done = out or b"", err or b"", True

    def result(self) -> Completed:
        return Completed(
            self.popen.returncode, self.out.decode("utf-8", "replace"), self.err.decode("utf-8", "replace")
        )


def proc_start(pid: int, proc: str = "/proc") -> Optional[int]:
    """Field 22 of /proc/<pid>/stat (the start tick), or None when the process is gone or a zombie."""
    try:
        with open(f"{proc}/{pid}/stat", encoding="ascii") as handle:
            text = handle.read()
    except OSError:
        return None
    fields = text[text.rindex(")") + 2 :].split()
    return None if fields[0] in ("Z", "X") else int(fields[19])


def clients_of(job: str, proc: str = "/proc") -> List[Client]:
    """This user's srun clients on this host for the job: every `srun` process of our uid whose argv names
    `--jobid=<job>`, recorded or not (a client spawned just before a crash, say). Only this job's are ever named."""
    found = []
    for entry in os.listdir(proc):
        if not entry.isdigit():
            continue
        try:
            if os.stat(f"{proc}/{entry}").st_uid != os.getuid():
                continue
            with open(f"{proc}/{entry}/cmdline", "rb") as handle:
                argv = handle.read().split(b"\0")
        except OSError:
            continue
        if argv and os.path.basename(argv[0]) == b"srun" and f"--jobid={job}".encode() in argv:
            start = proc_start(int(entry), proc)
            if start is not None:
                found.append(Client(int(entry), start))
    return found


class Runner:
    """Runs the commands on the login node. The tests replace it with a fake Slurm."""

    def run(self, argv: Sequence[str], timeout: float) -> Completed:
        try:
            done = subprocess.run(
                list(argv), capture_output=True, timeout=timeout, check=False, stdin=subprocess.DEVNULL
            )
        except subprocess.TimeoutExpired:
            raise SlurmError(f"{argv[0]} did not answer in {timeout:.0f} s") from None
        return Completed(
            done.returncode, done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace")
        )

    def start(self, argv: Sequence[str], data: Optional[bytes] = None) -> Process:
        stdin = subprocess.PIPE if data is not None else subprocess.DEVNULL
        popen = subprocess.Popen(list(argv), stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return Process(popen, data)

    def pipe(self, first: Sequence[str], second: Sequence[str]) -> Tuple[Process, Process]:
        """`first | second`: the payload goes from one step to the other through a pipe of this process, never a file,
        an argument or the environment."""
        left = subprocess.Popen(list(first), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        right = subprocess.Popen(list(second), stdin=left.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert left.stdout is not None
        left.stdout.close()  # the right-hand side holds the only reading end
        return Process(left, None), Process(right, None)

    def spawn(self, argv: Sequence[str], client_log: str) -> Client:
        """Start a command in its own session, detached from this one, its output into a 0600 file."""
        descriptor = os.open(client_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            popen = subprocess.Popen(
                list(argv), stdin=subprocess.DEVNULL, stdout=descriptor, stderr=descriptor, start_new_session=True
            )
        finally:
            os.close(descriptor)
        start = proc_start(popen.pid)
        return Client(popen.pid, start if start is not None else -1)

    def client_alive(self, client: Client) -> bool:
        return client.start >= 0 and proc_start(client.pid) == client.start

    def clients_of(self, job: str) -> List[Client]:
        return clients_of(job)

    def end_client(self, client: Client, grace_s: float = 10.0) -> bool:
        """End a client of ours, verified: SIGTERM, then SIGKILL after `grace_s`, each only while the process is still
        the one recorded (pid and start tick). True once it is gone."""
        for sig, wait_s in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 5.0)):
            if not self.client_alive(client):
                return True
            with contextlib.suppress(ProcessLookupError):
                os.kill(client.pid, sig)
            deadline = time.monotonic() + wait_s
            while self.client_alive(client) and time.monotonic() < deadline:
                time.sleep(0.2)
        return not self.client_alive(client)


@dataclass(frozen=True)
class Shape:
    """One component of a held job: its nodes, and each node's cores and memory."""

    nodes: int
    cpus_per_node: int
    memory_mib: int


@dataclass(frozen=True)
class Allocation:
    """The held job, as asked for: one component, or more for a heterogeneous job (`het`, components 1 and on)."""

    name: str
    account: str
    partition: str
    nodes: int
    cpus_per_node: int
    memory_mib: int
    time: str
    time_s: int
    het: Tuple[Shape, ...] = ()

    def shapes(self) -> Tuple[Shape, ...]:
        """Every component's shape, component 0 first."""
        return (Shape(self.nodes, self.cpus_per_node, self.memory_mib), *self.het)

    def argv(self) -> List[str]:
        """sbatch's argv. A further component follows a `:` and names its account, partition and time again (Slurm
        propagates no partition, nodes or cores to it, and the read-back checks what it recorded) with its own nodes,
        cores and memory; `--wrap` stands once, in component 0's options, as sbatch builds the script once."""
        argv = [
            "sbatch",
            "--parsable",
            f"--job-name={self.name}",
            f"--account={self.account}",
            f"--partition={self.partition}",
            "--no-requeue",
            f"--time={self.time}",
            f"--nodes={self.nodes}",
            "--ntasks-per-node=1",
            f"--cpus-per-task={self.cpus_per_node}",
            f"--mem={self.memory_mib}M",
            "--chdir=/",
            "--output=/dev/null",
            "--export=NONE",
            f"--wrap=exec sleep {self.time_s}",
        ]
        for shape in self.het:
            argv += [
                ":",
                f"--account={self.account}",
                f"--partition={self.partition}",
                f"--time={self.time}",
                f"--nodes={shape.nodes}",
                "--ntasks-per-node=1",
                f"--cpus-per-task={shape.cpus_per_node}",
                f"--mem={shape.memory_mib}M",
            ]
        return argv


@dataclass(frozen=True)
class Step:
    """One job step: where it runs, its size, its name, and its command behind the environment allowlist. A step in a
    heterogeneous job's further component names it (`het_group`, srun's `--het-group`; a step without one runs in
    component 0) and that component's own job id (`component`), by which squeue -s and sacct list its steps."""

    job: str
    node: str
    name: str
    cpus: int
    memory_mib: int
    env: Tuple[str, ...]
    command: Tuple[str, ...]
    output: Optional[str] = None
    het_group: Optional[int] = None
    component: Optional[str] = None

    @property
    def listed_as(self) -> str:
        """The job id squeue -s and sacct list this step under."""
        return self.component or self.job

    def argv(self) -> List[str]:
        out = [f"--output={self.output}"] if self.output else []
        group = [] if self.het_group is None else [f"--het-group={self.het_group}"]
        return [
            "srun",
            f"--jobid={self.job}",
            *group,
            "--exact",
            "-N1",
            "-n1",
            "-w",
            self.node,
            "-c",
            str(self.cpus),
            f"--mem={self.memory_mib}M",
            "-J",
            self.name,
            "--export=PATH",
            *out,
            ENV,
            "-i",
            *self.env,
            *self.command,
        ]


def allowlist(step_path: str, node_tmp: str, extra: Mapping[str, str] = {}) -> Tuple[str, ...]:
    """The whole environment of a step: PATH, the C locale, TMPDIR on the node's tmpfs (never Slurm's per-job
    TMPDIR, a link into a shared filesystem on the site), and any extra the caller names. Never a secret."""
    pairs = {"PATH": step_path, "LC_ALL": "C", "TMPDIR": node_tmp, **extra}
    for key, value in pairs.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key) or not re.fullmatch(r"[A-Za-z0-9_./:@=+-]*", value):
            raise SlurmError(f"the step environment's {key} holds a character a step's argv must not carry")
    return tuple(f"{key}={value}" for key, value in pairs.items())


def _fields(text: str) -> Dict[str, str]:
    """`scontrol show job -o`'s one line as key=value pairs (a value with a space keeps only its first word)."""
    found = {}
    for word in text.split():
        key, sep, value = word.partition("=")
        if sep and key not in found:
            found[key] = value
    return found


def limit_seconds(value: str) -> Optional[int]:
    """A Slurm time limit (`[D-]HH:MM:SS`, `MM:SS`) in seconds; None for anything else (UNLIMITED)."""
    match = re.fullmatch(r"(?:([0-9]+)-)?(?:([0-9]+):)?([0-9]+):([0-9]+)", value)
    if match is None:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def memory_mib(value: str) -> Optional[int]:
    """A Slurm memory size (`11264M`, `11G`, `11T`) in MiB; None for anything else."""
    match = re.fullmatch(r"([0-9]+)([KMGT]?)", value)
    if match is None:
        return None
    number, unit = int(match.group(1)), match.group(2) or "M"
    return {"K": number // 1024, "M": number, "G": number * 1024, "T": number * 1024 * 1024}[unit]


def _count(value: str) -> Optional[int]:
    """`6` or `6-6` (a pending job's range) as 6; None for anything else."""
    match = re.fullmatch(r"([0-9]+)(?:-([0-9]+))?", value)
    if match is None or (match.group(2) is not None and match.group(2) != match.group(1)):
        return None
    return int(match.group(1))


def mismatches(recorded: Mapping[str, str], asked: Allocation, shape: Optional[Shape] = None) -> List[str]:
    """The fields of the held job (or of one of its components, `shape`) Slurm recorded otherwise than asked, by name;
    empty when it is what was asked."""
    shape = shape or asked.shapes()[0]
    wanted = {
        "JobName": (recorded.get("JobName"), asked.name),
        "TimeLimit": (limit_seconds(recorded.get("TimeLimit", "")), asked.time_s),
        "Requeue": (recorded.get("Requeue"), "0"),
        "Account": ((recorded.get("Account") or "").lower(), asked.account.lower()),
        "Partition": (recorded.get("Partition"), asked.partition),
        "NumNodes": (_count(recorded.get("NumNodes", "")), shape.nodes),
        "NumCPUs": (_count(recorded.get("NumCPUs", "")), shape.nodes * shape.cpus_per_node),
        "MinMemoryNode": (memory_mib(recorded.get("MinMemoryNode", "")), shape.memory_mib),
    }
    return [field for field in READBACK if wanted[field][0] != wanted[field][1]]


def component_mismatches(records: Sequence[Mapping[str, str]], asked: Allocation, job: str) -> List[str]:
    """What a job's records (one per component, `Slurm.components`) hold otherwise than asked: a missing or extra
    component, or a field of one, named with its component in a heterogeneous job."""
    shapes = asked.shapes()
    if len(records) != len(shapes):
        return [f"its components ({len(records)} recorded, {len(shapes)} asked)"]
    wrong: List[str] = []
    for index, (record, shape) in enumerate(zip(records, shapes)):
        named = mismatches(record, asked, shape)
        if asked.het:
            own = record.get("JobId", "")
            if record.get("HetJobId") != job or record.get("HetJobOffset") != str(index):
                named.append("HetJobOffset")
            if not JOB_LINE.fullmatch(own) or ";" in own or (index == 0) != (own == job):
                named.append("JobId")
        wrong += [f"component {index}'s {field}" if asked.het else field for field in named]
    return wrong


class Slurm:
    """The driver's Slurm: the held job, its steps, and nothing of anyone else's."""

    def __init__(
        self,
        runner: Runner,
        *,
        poll_s: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        command_timeout_s: float = 60.0,
    ) -> None:
        self.runner = runner
        self.poll_s = poll_s
        self.sleep = sleep
        self.clock = clock
        self.command_timeout_s = command_timeout_s

    def _call(self, argv: Sequence[str]) -> Completed:
        done = self.runner.run(argv, self.command_timeout_s)
        if done.returncode != 0:
            raise SlurmError(f"{argv[0]} failed with status {done.returncode}: {done.stderr.strip()[:300]}")
        return done

    # -- the held job ------------------------------------------------------------------------------------------------

    def submit(self, asked: Allocation) -> str:
        """Submit the held job; return its id. sbatch's stderr is kept apart from the id it prints."""
        done = self._call(asked.argv())
        line = done.stdout.strip()
        match = JOB_LINE.fullmatch(line)
        if match is None:
            raise SlurmError("sbatch printed no job id")
        return match.group(1)

    def records(self, job: str) -> Optional[List[Dict[str, str]]]:
        """Every record `scontrol show job -o` answers for a job id, one per line: the job's, or each component's of a
        heterogeneous job asked by its leader's id; None when Slurm no longer knows the id."""
        done = self.runner.run(["scontrol", "show", "job", "-o", job], self.command_timeout_s)
        if done.returncode != 0:
            if "Invalid job id" in done.stderr:
                return None
            raise SlurmError(f"scontrol show job failed with status {done.returncode}: {done.stderr.strip()[:300]}")
        found = [_fields(line) for line in done.stdout.splitlines() if line.strip()]
        if not any(record.get("JobId") == job for record in found):
            raise SlurmError("scontrol show job answered for another job")
        return found

    def job(self, job: str) -> Optional[Dict[str, str]]:
        """What Slurm records of a job (of a heterogeneous job, its component 0), or None when it no longer knows it."""
        found = self.records(job)
        return None if found is None else next(r for r in found if r.get("JobId") == job)

    def components(self, job: str) -> Optional[List[Dict[str, str]]]:
        """The job's records in component order: its own alone, or a heterogeneous job's (its leader's id) by
        `HetJobOffset`; None when Slurm no longer knows the id."""
        found = self.records(job)
        if found is None:
            return None
        het = [record for record in found if record.get("HetJobId") == job]
        if not het:
            return [record for record in found if record.get("JobId") == job]
        if not all(OFFSET.fullmatch(record.get("HetJobOffset", "")) for record in het):
            raise SlurmError("a component of the heterogeneous job has no offset")
        return sorted(het, key=lambda record: int(record["HetJobOffset"]))

    def checked(self, job: str, asked: Allocation) -> List[Dict[str, str]]:
        """Every component's record of the job just submitted, read back; unless each is what was asked, cancel that
        one job (a heterogeneous job by its leader's id, which ends every component) and fail, naming the fields."""
        records = self.components(job)
        self._ours(job, records, asked.name)
        wrong = ["the job itself"] if records is None else component_mismatches(records, asked, job)
        if wrong:
            self.cancel(job)
            raise SlurmError(f"the held job was recorded otherwise than asked ({', '.join(wrong)}); it was cancelled")
        assert records is not None
        return records

    def readback(self, job: str, asked: Allocation) -> Dict[str, str]:
        """`checked`, reduced to the read-back's fields of component 0."""
        return {field: self.checked(job, asked)[0][field] for field in READBACK}

    @staticmethod
    def _ours(job: str, records: Optional[Sequence[Mapping[str, str]]], name: Optional[str]) -> None:
        """Slurm reuses ids: a job id whose job (component 0) carries another name than the one submitted is not ours,
        and is never cancelled; fail instead, naming it."""
        if records and name is not None and records[0].get("JobName") != name:
            raise SlurmError(f"job {job} carries another job's name: not the job submitted, and not cancelled")

    def wait_running(self, job: str, timeout_s: float, name: Optional[str] = None) -> Dict[str, str]:
        """Wait for the held job (every component of it) to run; at the bound, or if it ends first, cancel it by its
        id and fail. With `name`, a job that carries another is never cancelled (`_ours`)."""
        deadline = self.clock() + timeout_s
        while True:
            records = self.components(job) or []
            self._ours(job, records, name)
            states = [record.get("JobState") for record in records]
            if states and all(state == "RUNNING" for state in states):
                return records[0]
            ended = [state for state in states if state not in (*WAITING_STATES, "RUNNING")]
            if not states or ended:
                self.cancel(job)
                raise SlurmError(f"the held job is {(ended or ['gone'])[0]} before it ran")
            if self.clock() >= deadline:
                self.cancel(job)
                raise SlurmError(f"the held job did not start within {timeout_s:.0f} s; it was cancelled")
            self.sleep(self.poll_s)

    def hostnames(self, nodelist: str) -> List[str]:
        done = self._call(["scontrol", "show", "hostnames", nodelist])
        nodes = done.stdout.split()
        if not nodes or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}", n) for n in nodes):
            raise SlurmError("scontrol show hostnames gave no usable node name")
        return nodes

    def jobs_named(self, user: str, name: str) -> List[str]:
        """The ids of this user's jobs with exactly this name (the allocation's, which holds up's random part); a
        heterogeneous job, listed once per component (`<leader>+<k>`), counts once, by its leader's id."""
        done = self._call(["squeue", "-u", user, "-h", "-n", name, "-o", "%i %j"])
        found: List[str] = []
        for line in done.stdout.splitlines():
            words = line.split()
            match = JOB_LISTED.fullmatch(words[0]) if len(words) == 2 and words[1] == name else None
            if match and match.group(1) not in found:
                found.append(match.group(1))
        return found

    def gone(self, job: str) -> bool:
        """Whether squeue lists the job no more: neither its id nor any component of it (`<job>+<k>`)."""
        done = self._call(["squeue", "-h", "-j", job, "-o", "%i"])
        listed = (JOB_LISTED.fullmatch(word) for word in done.stdout.split())
        return not any(match and match.group(1) == job for match in listed)

    def cancel(self, job: str) -> None:
        """Cancel one job by its id: never by user, never by name."""
        if not JOB_LINE.fullmatch(job) or ";" in job:
            raise SlurmError("refusing to cancel something that is not one job id")
        self._call(["scancel", job])

    # -- steps --------------------------------------------------------------------------------------------------------

    def steps(self, job: str) -> Dict[str, str]:
        """The job's steps listed now, as name to step id (the batch and extern steps left out)."""
        done = self.runner.run(["squeue", "-s", "-j", job, "-h", "-o", "%i %j"], self.command_timeout_s)
        if done.returncode != 0:
            if "Invalid job id" in done.stderr:
                return {}
            raise SlurmError(f"squeue -s failed with status {done.returncode}: {done.stderr.strip()[:300]}")
        found = {}
        for line in done.stdout.splitlines():
            match = STEP_LINE.fullmatch(line.strip())
            if match and not match.group(1).endswith((".batch", ".extern")):
                found[match.group(2)] = match.group(1)
        return found

    def cancel_step(self, step_id: str) -> None:
        """Cancel one step by the id squeue -s lists it under: `<job>.<n>`, or a component's `<leader>+<k>.<n>`."""
        if not STEP_ID.fullmatch(step_id):
            raise SlurmError("refusing to cancel something that is not one step id")
        self._call(["scancel", step_id])

    def step_status(self, job: str, name: str) -> Optional[int]:
        """A finished step's exit status, by its name, from the accounting; None when it is not recorded."""
        done = self.runner.run(["sacct", "-j", job, "-n", "-P", "-o", "JobID,JobName,ExitCode"], self.command_timeout_s)
        if done.returncode != 0:
            return None
        for line in done.stdout.splitlines():
            parts = line.split("|")
            if len(parts) == 3 and parts[1] == name and re.fullmatch(r"[0-9]+:[0-9]+", parts[2]):
                status, killed = (int(part) for part in parts[2].split(":"))
                return 128 + killed if killed else status  # a step a signal ended never reads as a success
        return None

    def _finish(self, process: Process, step: Step, timeout_s: float) -> Completed:
        """The result of a control step within its bound; past it, end a client whose step is gone (three polls in a
        row), and cancel a step still listed, by its own id."""
        if process.wait(timeout_s):
            return process.result()
        absent = 0
        deadline = self.clock() + max(self.poll_s * (GONE_POLLS + 2), 30.0)
        while self.clock() < deadline:
            listed = self.steps(step.listed_as)
            absent = 0 if step.name in listed else absent + 1
            if absent >= GONE_POLLS:
                process.kill()
                done = process.result()
                return Completed(self.step_status(step.listed_as, step.name), done.stdout, done.stderr)
            if process.wait(self.poll_s):
                return process.result()
        listed = self.steps(step.listed_as)
        if step.name in listed:
            self.cancel_step(listed[step.name])
        process.kill()
        raise StepTimeout(f"the step {step.name} did not end within its bound; it was cancelled")

    def control(self, step: Step, timeout_s: float, data: Optional[bytes] = None) -> Completed:
        """Run a control step to its end, within a bound; stdin is the payload when given, else /dev/null."""
        return self._finish(self.runner.start(step.argv(), data), step, timeout_s)

    def control_pipe(self, first: Step, second: Step, timeout_s: float) -> Tuple[Completed, Completed]:
        """`first | second`, two control steps joined by a pipe of this process: a payload that never touches a file."""
        left, right = self.runner.pipe(first.argv(), second.argv())
        results, failure = [], None
        for process, step in ((left, first), (right, second)):  # both, whatever the first does
            try:
                results.append(self._finish(process, step, timeout_s))
            except (SlurmError, OSError) as error:
                process.kill()
                failure = failure or error
        if failure is not None:
            raise failure
        return results[0], results[1]

    def service(self, step: Step, client_log: str) -> Client:
        """Launch a service step detached; it holds its service for the step's life."""
        return self.runner.spawn(step.argv(), client_log)
