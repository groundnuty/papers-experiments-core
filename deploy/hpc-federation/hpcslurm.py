"""Slurm, as the HPC federation driver uses it from the login node (DESIGN.md, sections 1, 3 and 5).

- **The held job**: one `sbatch` holding the federation's nodes with `exec sleep`, `--no-requeue` and a `--time`; what
  Slurm recorded is read back (`scontrol show job -o`) and the job is cancelled, by its own id only, unless it is what
  was asked for.
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
STEP_LINE = re.compile(r"([0-9]{1,12}\.(?:[0-9]{1,9}|batch|extern))\s+(\S+)")


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


def proc_start(pid: int) -> Optional[int]:
    """Field 22 of /proc/<pid>/stat (the start tick), or None when the process is gone."""
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            text = handle.read()
    except OSError:
        return None
    return int(text[text.rindex(")") + 2 :].split()[19])


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

    def end_client(self, client: Client) -> None:
        if self.client_alive(client):
            with contextlib.suppress(ProcessLookupError):
                os.kill(client.pid, signal.SIGTERM)


@dataclass(frozen=True)
class Allocation:
    """The held job, as asked for."""

    name: str
    account: str
    partition: str
    nodes: int
    cpus_per_node: int
    memory_mib: int
    time: str
    time_s: int

    def argv(self) -> List[str]:
        return [
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


@dataclass(frozen=True)
class Step:
    """One job step: where it runs, its size, its name, and its command behind the environment allowlist."""

    job: str
    node: str
    name: str
    cpus: int
    memory_mib: int
    env: Tuple[str, ...]
    command: Tuple[str, ...]
    output: Optional[str] = None

    def argv(self) -> List[str]:
        out = [f"--output={self.output}"] if self.output else []
        return [
            "srun",
            f"--jobid={self.job}",
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


def mismatches(recorded: Mapping[str, str], asked: Allocation) -> List[str]:
    """The fields of the held job Slurm recorded otherwise than asked, by name; empty when it is what was asked."""
    wanted = {
        "JobName": (recorded.get("JobName"), asked.name),
        "TimeLimit": (limit_seconds(recorded.get("TimeLimit", "")), asked.time_s),
        "Requeue": (recorded.get("Requeue"), "0"),
        "Account": ((recorded.get("Account") or "").lower(), asked.account.lower()),
        "Partition": (recorded.get("Partition"), asked.partition),
        "NumNodes": (_count(recorded.get("NumNodes", "")), asked.nodes),
        "NumCPUs": (_count(recorded.get("NumCPUs", "")), asked.nodes * asked.cpus_per_node),
        "MinMemoryNode": (memory_mib(recorded.get("MinMemoryNode", "")), asked.memory_mib),
    }
    return [field for field in READBACK if wanted[field][0] != wanted[field][1]]


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

    def job(self, job: str) -> Optional[Dict[str, str]]:
        """What Slurm records of a job, or None when it no longer knows it."""
        done = self.runner.run(["scontrol", "show", "job", "-o", job], self.command_timeout_s)
        if done.returncode != 0:
            if "Invalid job id" in done.stderr:
                return None
            raise SlurmError(f"scontrol show job failed with status {done.returncode}: {done.stderr.strip()[:300]}")
        fields = _fields(done.stdout)
        if fields.get("JobId") != job:
            raise SlurmError("scontrol show job answered for another job")
        return fields

    def readback(self, job: str, asked: Allocation) -> Dict[str, str]:
        """What Slurm recorded of the job just submitted; unless it is what was asked, cancel that one job and fail,
        naming the fields."""
        recorded = self.job(job)
        wrong = ["the job itself"] if recorded is None else mismatches(recorded, asked)
        if wrong:
            self.cancel(job)
            raise SlurmError(f"the held job was recorded otherwise than asked ({', '.join(wrong)}); it was cancelled")
        assert recorded is not None
        return {field: recorded[field] for field in READBACK}

    def wait_running(self, job: str, timeout_s: float) -> Dict[str, str]:
        """Wait for the held job to run; at the bound, or if it ends first, cancel it by its id and fail."""
        deadline = self.clock() + timeout_s
        while True:
            fields = self.job(job)
            state = None if fields is None else fields.get("JobState")
            if state == "RUNNING":
                assert fields is not None
                return fields
            if state not in WAITING_STATES:
                self.cancel(job)
                raise SlurmError(f"the held job is {state or 'gone'} before it ran")
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
        """The ids of this user's jobs with exactly this name (the allocation's, which holds up's random part)."""
        done = self._call(["squeue", "-u", user, "-h", "-n", name, "-o", "%i %j"])
        found = []
        for line in done.stdout.splitlines():
            words = line.split()
            if len(words) == 2 and words[1] == name and JOB_LINE.fullmatch(words[0]):
                found.append(words[0])
        return found

    def gone(self, job: str) -> bool:
        done = self._call(["squeue", "-h", "-j", job, "-o", "%i"])
        return job not in done.stdout.split()

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
        if not re.fullmatch(r"[0-9]{1,12}\.[0-9]{1,9}", step_id):
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
                return int(parts[2].split(":")[0])
        return None

    def _finish(self, process: Process, step: Step, timeout_s: float) -> Completed:
        """The result of a control step within its bound; past it, end a client whose step is gone (three polls in a
        row), and cancel a step still listed, by its own id."""
        if process.wait(timeout_s):
            return process.result()
        absent = 0
        deadline = self.clock() + max(self.poll_s * (GONE_POLLS + 2), 30.0)
        while self.clock() < deadline:
            listed = self.steps(step.job)
            absent = 0 if step.name in listed else absent + 1
            if absent >= GONE_POLLS:
                process.kill()
                done = process.result()
                return Completed(self.step_status(step.job, step.name), done.stdout, done.stderr)
            if process.wait(self.poll_s):
                return process.result()
        listed = self.steps(step.job)
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
        return self._finish(left, first, timeout_s), self._finish(right, second, timeout_s)

    def service(self, step: Step, client_log: str) -> Client:
        """Launch a service step detached; it holds its service for the step's life."""
        return self.runner.spawn(step.argv(), client_log)
