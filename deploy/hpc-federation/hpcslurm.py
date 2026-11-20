"""Slurm, as the HPC federation driver uses it from the login node (DESIGN.md, sections 1, 3 and 5).

- **The held job**: one `sbatch` holding the federation's nodes with `exec sleep`, `--no-requeue` and a `--time`; what
  Slurm recorded is read back (`scontrol show job -o`) and the job is cancelled, by its own id only, unless it is what
  was asked for. A heterogeneous job (`sbatch <component 0> : <component 1>`, the agent's node as component 1) is read
  back component by component: one record each, by `HetJobOffset`; squeue lists it as `<leader>+<k>`, and a cancel of
  its leader's id ends every component.
- **Every step** is `srun --jobid --exact` with a name, its cores and memory, `--chdir=/`, and `/usr/bin/env -i` with
  an explicit allowlist in front of its command, so neither the login environment nor a site prolog decides what it
  sees, and it never starts in the login node's working directory (or, where that does not exist on the node, in the
  node's shared /tmp, where slurmstepd falls back). No step uses `--overlap` but a kept stop's holder (`Step.overlap`),
  a sleep that shares the node's resources with every other step, so it never holds back a control or service step.
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
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, NamedTuple, NoReturn, Optional, Sequence, Tuple, TypeVar

T = TypeVar("T")

ENV = "/usr/bin/env"
GONE_POLLS = 3  # a step absent from squeue -s this many polls in a row has ended
READBACK = ("JobName", "TimeLimit", "Requeue", "Account", "Partition", "NumNodes", "NumCPUs", "MinMemoryNode")
WAITING_STATES = ("PENDING", "CONFIGURING", "REQUEUED", "RESIZING")
# The wait for a held job takes a read of it that failed (the command timed out, or exited nonzero: what a slow or
# restarting controller gives) for a missed poll. It gives up once every read has failed for this many seconds since
# the first one of the streak, sleeping the poll interval after the first, twice it after the second and so on, but
# never longer than the backoff cap, so that several reads fit in the time.
MISSED_POLLS_GIVE_UP_S = 600.0
MISSED_POLL_BACKOFF_MAX_S = 60.0
JOB_LINE = re.compile(r"([0-9]{1,12})(?:;[A-Za-z0-9_.-]+)?")
JOB_LISTED = re.compile(r"([0-9]{1,12})(?:\+[0-9]{1,6})?")  # squeue's %i: a job, or a component <leader>+<k>
# A step as squeue -s lists it: of a job, or of a heterogeneous job's component (<leader>+<k>.<n>, or the
# component's own id); the batch and extern steps included.
STEP_LINE = re.compile(r"([0-9]{1,12}(?:\+[0-9]{1,6})?\.(?:[0-9]{1,9}|batch|extern))\s+(\S+)")
STEP_ID = re.compile(r"[0-9]{1,12}(?:\+[0-9]{1,6})?\.[0-9]{1,9}")
OFFSET = re.compile(r"[0-9]{1,6}")
# A value of srun's --network (HPC_STEP_NETWORK): letters, digits and `_ . , = : + -`, led by a letter or digit, so no
# shell character, space or option of its own reaches the argv. It names no plugin's words: other sites take others.
NETWORK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.,=:+-]{0,127}")
NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")  # a node name, as `Slurm.hostnames` accepts one


SLURM_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}")  # sacct's own, zone-less
# What sacct calls a step that has ended; its first word, so `CANCELLED by 1234` is CANCELLED.
ENDED_STATES = (
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "NODE_FAIL", "OUT_OF_MEMORY", "DEADLINE", "PREEMPTED", "BOOT_FAIL"
)  # fmt: skip


# What `Slurm.step_observed` says when the two reads do not settle a step. These are what Slurm's accounting being
# behind explains (a step just stopped: squeue no longer lists it and sacct has not caught up, or the reverse);
# `accounting_behind` names them, so a caller that knows the step was stopped can wait for them and for nothing else.
UNSEEN = "the step is not listed by squeue and accounting holds no record of it"
UNLISTED_RUNNING = "accounting says the step runs and squeue does not list it"
UNLISTED_NOT_ENDED = "the accounting state of the step is not known, or has no end time"
LISTED_ENDED = "squeue lists the step and accounting records it as an ended one"
LISTED_NOT_ENDED = "the accounting state of the step is not known"
BEHIND = (UNSEEN, UNLISTED_RUNNING, UNLISTED_NOT_ENDED, LISTED_ENDED, LISTED_NOT_ENDED)


ACCOUNT_ID = re.compile(
    r"[0-9]{1,12}(?:\+[0-9]{1,6})?(?:\.[A-Za-z0-9_-]{1,32})?"
)  # sacct's JobID: job, step or its word
ACCOUNT_TIME = re.compile(SLURM_TIME.pattern + "|Unknown|None")  # a time sacct prints, or says there is none
ACCOUNT_STATE = re.compile(r"[A-Z_]+")  # the first word of sacct's State


def _accounting_row(parts: List[str]) -> bool:
    """Whether six fields of `sacct -P -o JobID,JobName,State,Start,End,ExitCode` are a row of accounting: a JobID, a
    state word, two times (or none) and an exit code. Six empty fields are no row, and count as no evidence."""
    return (
        len(parts) == 6
        and ACCOUNT_ID.fullmatch(parts[0]) is not None
        and ACCOUNT_STATE.fullmatch(parts[2].split()[0] if parts[2].split() else "") is not None
        and ACCOUNT_TIME.fullmatch(parts[3]) is not None
        and ACCOUNT_TIME.fullmatch(parts[4]) is not None
        and re.fullmatch(r"[0-9]+:[0-9]+", parts[5]) is not None
    )


def _slurm_time(value: str) -> Optional[str]:
    """A time as sacct prints it, or None for `Unknown`, `None`, nothing, or anything that is not one."""
    return value if SLURM_TIME.fullmatch(value) else None


def _step_answer(
    running: Optional[bool], state: Optional[str], started: Optional[str], ended: Optional[str], code: Optional[int]
) -> Dict[str, Any]:
    return {
        "running": running,
        "state": state,
        "started_at": started,
        "ended_at": ended,
        "exit_code": code,
        "unknown": None,
    }


def _unknown_step(reason: str) -> Dict[str, Any]:
    """`Slurm.step_observed`'s answer for a step that could not be told, and why."""
    return {**_step_answer(None, None, None, None, None), "unknown": reason}


def accounting_behind(answer: Mapping[str, Any]) -> bool:
    """Whether a `Slurm.step_observed` answer is one that Slurm's accounting being behind explains, for a caller that
    knows the step was stopped: the step reads as running (squeue lists it, or no row of accounting says otherwise), or
    is unknown for a reason of `BEHIND` (not listed and not recorded, recorded as running or as not ended, listed and
    recorded as ended). An ended step is no lag, and nor is a command that failed or answered something unreadable, a
    step recorded twice or listed under another name, or an id that is not a step id: each keeps its own reason."""
    return answer.get("running") is True or (answer.get("running") is None and answer.get("unknown") in BEHIND)


def valid_network(value: object) -> bool:
    """Whether a step's network option can be used: the empty string (no option), or a string `NETWORK` matches. Any
    other type is no option: a state record is data, and a false or a null must not read as none."""
    return isinstance(value, str) and (value == "" or NETWORK.fullmatch(value) is not None)


class SlurmError(RuntimeError):
    """A Slurm command that failed, or a job or step that is not what was asked for."""


def scrubbed(text: str, names: Sequence[str]) -> str:
    """`text` with each of `names` (nodes) replaced by `<node>`, the longest first, so that what Slurm says of a request
    that excluded nodes never carries one into a message."""
    for name in sorted(names, key=len, reverse=True):
        text = text.replace(name, "<node>")
    return text


class ReadFailed(SlurmError):
    """A read of a job's record that failed: `scontrol show job` timed out or exited nonzero (a controller that is slow
    or restarting says so). Not Slurm's "Invalid job id" (a job it no longer knows, which `records` answers with
    None), and not an answer for another job."""


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


ALIVE, GONE, ANOTHER = "alive", "gone", "another process"  # what a recorded client is now (`Runner.client_state`)


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


# squeue takes its defaults (the states, names, accounts and partitions it lists) from SQUEUE_* variables, and the
# form of what Slurm's commands print from a few SLURM_* ones: a login shell that exported one (`SQUEUE_NAMES=x` hides
# every federation job) would change what a read of the driver answers, and the guards that read the account's jobs
# would fail open without a word. The commands `Runner.run` runs get an environment without them; every other variable,
# the site's own SLURM_CONF and SBATCH_* among them, reaches them as it was.
OUTPUT_FORMAT_VARIABLES = ("SLURM_TIME_FORMAT", "SLURM_BITSTR_LEN")
WIDENING_VARIABLES = (
    "SQUEUE_ALL",
)  # `--all`: jobs of hidden partitions too. It only widens, and a site that hides its
# partition may rely on it for every squeue read of the driver, so it is the one SQUEUE_* variable that is kept


def read_environment(environ: Mapping[str, str]) -> Dict[str, str]:
    """`environ` without every SQUEUE_* variable that narrows or reshapes what squeue lists (all but `SQUEUE_ALL`) and
    without Slurm's output-format variables."""
    return {
        k: v
        for k, v in environ.items()
        if (not k.startswith("SQUEUE_") or k in WIDENING_VARIABLES) and k not in OUTPUT_FORMAT_VARIABLES
    }


class Runner:
    """Runs the commands on the login node. The tests replace it with a fake Slurm."""

    def run(self, argv: Sequence[str], timeout: float) -> Completed:
        try:
            done = subprocess.run(
                list(argv),
                capture_output=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
                env=read_environment(os.environ),
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

    def spawn(self, argv: Sequence[str], client_log: str, cwd: Optional[str] = None) -> Client:
        """Start a command in its own session, detached from this one, its output into a 0600 file; in `cwd` when
        given (the fault relay's: the federation's root), else in this process's working directory."""
        descriptor = os.open(client_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            popen = subprocess.Popen(
                list(argv), stdin=subprocess.DEVNULL, stdout=descriptor, stderr=descriptor, start_new_session=True,
                cwd=cwd,
            )  # fmt: skip
        finally:
            os.close(descriptor)
        start = proc_start(popen.pid)
        return Client(popen.pid, start if start is not None else -1)

    def client_state(self, client: Client) -> str:
        """What the process a client was recorded as is now: `ALIVE` while its pid holds a process of the recorded start
        tick; `GONE` when no process (or a zombie) holds the pid; `ANOTHER` when one does with another start tick, or
        when the client's own tick was never read (-1). A pid is never signalled on less than its start tick."""
        now = proc_start(client.pid)
        if now is None:
            return GONE
        return ALIVE if client.start >= 0 and now == client.start else ANOTHER

    def client_alive(self, client: Client) -> bool:
        return self.client_state(client) == ALIVE

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
    exclude: Tuple[
        str, ...
    ] = ()  # the nodes of other federations' live jobs (hpcexclude): each component's `--exclude`
    after: Optional[str] = None  # the id of a job this one waits to START (`--dependency=after:`, component 0 only)
    exclusive: bool = False  # `--exclusive` on every component (HPC_EXCLUSIVE_NODES): no other job shares its nodes
    own: int = 0  # how many of `exclude` are the nodes of this federation's own services job (the agent's, `separate`)

    def shapes(self) -> Tuple[Shape, ...]:
        """Every component's shape, component 0 first."""
        return (Shape(self.nodes, self.cpus_per_node, self.memory_mib), *self.het)

    def argv(self) -> List[str]:
        """sbatch's argv. A further component follows a `:` and names its account, partition and time again (Slurm
        propagates no partition, nodes or cores to it, and the read-back checks what it recorded) with its own nodes,
        cores and memory; `--wrap` stands once, in component 0's options, as sbatch builds the script once. `--exclude`
        names the same nodes in every component, so that nothing depends on Slurm passing it on, and only when there
        are nodes to name: without them the argv is what it was before the option existed. `--exclusive` follows the
        memory of every component, likewise only when asked (Slurm propagates it to no component).
        `--dependency=after:<job>` (`after`, set only on the agent's own job, never on a job that has components) holds
        the job until `<job>` has started or been cancelled: Slurm's "begin after the job starts" type, never
        `afterok`, which would wait for the job to end. One argv may carry both."""
        if not all(NODE.fullmatch(node) for node in self.exclude):
            raise SlurmError("an excluded node's name is not one an sbatch argv may carry")
        if self.after is not None and not re.fullmatch(r"[0-9]{1,12}", self.after):
            raise SlurmError("the job this one waits to start is not one job id")
        exclude = [f"--exclude={','.join(self.exclude)}"] if self.exclude else []
        dependency = [f"--dependency=after:{self.after}"] if self.after is not None else []
        whole = ["--exclusive"] if self.exclusive else []
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
            *whole,
            *exclude,
            *dependency,
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
                *whole,
                *exclude,
            ]
        return argv


@dataclass(frozen=True)
class Step:
    """One job step: where it runs, its size, its name, and its command behind the environment allowlist. A step of a
    heterogeneous job names its component (`het_group`, srun's `--het-group`), 0 included: on the site's Slurm a step
    of one with no `--het-group` is refused. A step of a further component also carries that component's own job id
    (`component`), by which squeue -s and sacct list its steps. A job of one component takes no `het_group`. A step
    names `--network=<value>` (`network`, the site's `HPC_STEP_NETWORK`) only when it has a value: with none, its argv
    is what it was before the setting existed. `overlap` (a kept stop's holder alone, O96) names `--overlap` in place
    of `--exact`: the step shares the node's cores and memory with every other step, as the holder proven live did."""

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
    network: str = ""
    overlap: bool = False

    @property
    def listed_as(self) -> str:
        """The job id squeue -s and sacct list this step under."""
        return self.component or self.job

    def argv(self) -> List[str]:
        out = [f"--output={self.output}"] if self.output else []
        group = [] if self.het_group is None else [f"--het-group={self.het_group}"]
        if not valid_network(self.network):
            raise SlurmError("HPC_STEP_NETWORK: the step's network option is not one an srun argv may carry")
        network = [f"--network={self.network}"] if self.network else []
        return [
            "srun",
            f"--jobid={self.job}",
            *group,
            "--overlap" if self.overlap else "--exact",
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
            "--chdir=/",  # never the login node's directory, or the node's shared /tmp where that does not exist
            *network,
            *out,
            ENV,
            "-i",
            *self.env,
            *self.command,
        ]


def home_env(home: str) -> Dict[str, str]:
    """A step's own home, so that no tool of a step falls back to the account's home, which every job and agent of the
    account shares (with HOME unset, many tools take the passwd entry's): HOME, the XDG base directories, Apptainer's
    configuration and cache, uv's and pip's caches, all under `home`; Python's user site off; git's global and system
    configuration none. The launcher names a path nobody can make (`hpclaunch.NO_HOME`); a step that makes a private
    directory of its own on the node's tmpfs points them into it (`lib.sh`'s home_in, `shards.py step`)."""
    return {
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
        "APPTAINER_CONFIGDIR": f"{home}/.apptainer",
        "APPTAINER_CACHEDIR": f"{home}/.apptainer/cache",
        "UV_CACHE_DIR": f"{home}/.cache/uv",
        "PIP_CACHE_DIR": f"{home}/.cache/pip",
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
    }


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
    cores = _count(recorded.get("NumCPUs", ""))
    if asked.exclusive and cores is not None and cores >= shape.nodes * shape.cpus_per_node:
        cores = shape.nodes * shape.cpus_per_node  # an exclusive job holds every core of its nodes, and Slurm says so
    wanted = {
        "JobName": (recorded.get("JobName"), asked.name),
        "TimeLimit": (limit_seconds(recorded.get("TimeLimit", "")), asked.time_s),
        "Requeue": (recorded.get("Requeue"), "0"),
        "Account": ((recorded.get("Account") or "").lower(), asked.account.lower()),
        "Partition": (recorded.get("Partition"), asked.partition),
        "NumNodes": (_count(recorded.get("NumNodes", "")), shape.nodes),
        "NumCPUs": (cores, shape.nodes * shape.cpus_per_node),
        "MinMemoryNode": (memory_mib(recorded.get("MinMemoryNode", "")), shape.memory_mib),
    }
    wrong = [field for field in READBACK if wanted[field][0] != wanted[field][1]]
    if asked.exclusive and recorded.get("OverSubscribe") != "NO":  # read only when asked: not a READBACK field
        wrong.append("OverSubscribe")
    return wrong


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
        """Submit the held job; return its id. sbatch's stderr is kept apart from the id it prints. A request that
        excluded nodes which sbatch refuses says how many it excluded, and no node name of sbatch's own words stays."""
        try:
            done = self._call(asked.argv())
        except SlurmError as error:
            if not asked.exclude:
                raise
            raise SlurmError(
                f"{scrubbed(str(error), asked.exclude)}; the request excluded {len(asked.exclude)} node(s) of other "
                "live federations of this account, which may leave none that fit"
            ) from None
        line = done.stdout.strip()
        match = JOB_LINE.fullmatch(line)
        if match is None:
            raise SlurmError("sbatch printed no job id")
        return match.group(1)

    def records(self, job: str) -> Optional[List[Dict[str, str]]]:
        """Every record `scontrol show job -o` answers for a job id, one per line: the job's, or each component's of a
        heterogeneous job asked by its leader's id; None when Slurm no longer knows the id."""
        try:
            done = self.runner.run(["scontrol", "show", "job", "-o", job], self.command_timeout_s)
        except SlurmError as error:  # the command did not answer within its bound
            raise ReadFailed(str(error)) from None
        if done.returncode != 0:
            if "Invalid job id" in done.stderr:
                return None
            raise ReadFailed(f"scontrol show job failed with status {done.returncode}: {done.stderr.strip()[:300]}")
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

    def ride_out(
        self,
        read: Callable[[], T],
        what: str,
        say: Optional[Callable[[str], None]] = None,
        deadline: Optional[float] = None,
        past: Optional[Callable[[ReadFailed], SlurmError]] = None,
    ) -> T:
        """`read()`, where a `ReadFailed` (a Slurm command that timed out or exited nonzero, as a slow or restarting
        controller makes it) is a missed poll, not an answer: `say` hears of the first of a streak, the read is made
        again after a sleep that doubles from the poll interval and never passes `MISSED_POLL_BACKOFF_MAX_S`, and
        whatever else `read` raises (a definite answer: a job that is gone, a mismatch, an unparsable line) ends it as
        it always did, and so does an answer. It fails, with a plain `SlurmError`, only once every read has failed for
        `MISSED_POLLS_GIVE_UP_S` since the streak's first (never on one failure). With `deadline` (a `clock()` time) it
        fails with `past(error)` at the failed read that finds it passed. It cancels nothing: the caller's release does,
        after reading what it releases (`Driver.release_held`). `what` names the read in what `say` says."""
        first_missed: Optional[float] = None  # when the streak of failed reads began
        missed = 0
        while True:
            try:
                return read()
            except ReadFailed as error:
                now = self.clock()
                first_missed = now if first_missed is None else first_missed
                missed += 1
                if now - first_missed >= MISSED_POLLS_GIVE_UP_S:
                    raise SlurmError(
                        f"{error}; {missed} reads in a row failed over {now - first_missed:.0f} s"
                    ) from None
                if deadline is not None and past is not None and now >= deadline:
                    raise past(error) from None
                if missed == 1 and say is not None:
                    say(f"{error}: a missed poll of {what}; going on, and giving up only if every read fails for "
                        f"{MISSED_POLLS_GIVE_UP_S:.0f} s")  # fmt: skip
                left = MISSED_POLL_BACKOFF_MAX_S if deadline is None else deadline - now
                self.sleep(min(self.poll_s * 2 ** min(missed - 1, 16), MISSED_POLL_BACKOFF_MAX_S, left))

    def checked(self, job: str, asked: Allocation, say: Optional[Callable[[str], None]] = None) -> List[Dict[str, str]]:
        """Every component's record of the job just submitted, read back (a read that fails is a missed poll,
        `ride_out`); unless each is what was asked, cancel that one job (a heterogeneous job by its leader's id, which
        ends every component) and fail, naming the fields."""
        records = self.ride_out(lambda: self.components(job), f"the read-back of held job {job}", say)
        self._ours(job, records, asked.name)
        wrong = ["the job itself"] if records is None else component_mismatches(records, asked, job)
        if wrong:
            self.cancel(job)
            raise SlurmError(f"the held job was recorded otherwise than asked ({', '.join(wrong)}); it was cancelled")
        assert records is not None
        return records

    def check_dependency(
        self,
        job: str,
        asked: Allocation,
        records: Sequence[Mapping[str, str]],
        say: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Read back the dependency of a job just submitted (`asked.after`), at submit time only: the job's record must
        name `after:<that job>` (Slurm adds `(unfulfilled)` while it waits), or none while that job is no longer
        waiting, since Slurm drops a dependency once it holds. A job recorded with no dependency while the job it was to
        wait for still waits would start with no services, so it is cancelled by its own id and the up fails. The read
        of the job it waits for, which that asks, is a missed poll when it fails (`ride_out`: said once, read again),
        and only a read that fails for ten minutes, or a definite answer that is no answer, leaves it unchecked and
        cancels the job: nothing shows the dependency holds. The check is never repeated on the running job (its
        dependency is gone by then): `checked` does not look at it."""
        if asked.after is None:
            return
        recorded = records[0].get("Dependency", "")
        if re.fullmatch(rf"after:{asked.after}(?:\([a-z]+\))?", recorded):
            return
        if recorded in ("", "(null)"):
            try:
                waited = self.ride_out(lambda: self.job(asked.after), f"the job that held job {job} waits for", say)
            except SlurmError:  # nothing shows the dependency holds: fail closed, never leave the job free to start
                self._fail_cancelling(
                    job,
                    asked,
                    "the held job's dependency could not be checked (the job it waits for could not be read)",
                )
            if waited is None or waited.get("JobState") not in WAITING_STATES:
                return  # it has started (or ended), so the dependency holds: Slurm has dropped it
        self._fail_cancelling(job, asked, "the held job was recorded otherwise than asked (its dependency)")

    def _fail_cancelling(self, job: str, asked: Allocation, why: str) -> NoReturn:
        """Fail the up for `why`, cancelling the job just submitted by its id, but only while it still carries the name
        it was given: the check may have waited out a slow controller for ten minutes (`ride_out`), and Slurm reuses
        ids, so a cancel by the saved id could end another up's job. A job that cannot be read is not cancelled on a
        guess (the failure says so: `down` releases it); one Slurm no longer knows needs none."""
        try:
            records = self.components(job)
        except SlurmError:
            raise SlurmError(f"{why}; it could not be read to cancel it: run down to release it") from None
        if records is None:
            raise SlurmError(f"{why}; Slurm no longer knows it")
        if records[0].get("JobName") != asked.name:
            raise SlurmError(f"{why}; job {job} carries another job's name now, and was not cancelled")
        self.cancel(job)
        raise SlurmError(f"{why}; it was cancelled")

    def readback(self, job: str, asked: Allocation) -> Dict[str, str]:
        """`checked`, reduced to the read-back's fields of component 0."""
        return {field: self.checked(job, asked)[0][field] for field in READBACK}

    @staticmethod
    def _ours(job: str, records: Optional[Sequence[Mapping[str, str]]], name: Optional[str]) -> None:
        """Slurm reuses ids: a job id whose job (component 0) carries another name than the one submitted is not ours,
        and is never cancelled; fail instead, naming it."""
        if records and name is not None and records[0].get("JobName") != name:
            raise SlurmError(f"job {job} carries another job's name: not the job submitted, and not cancelled")

    def wait_running(
        self,
        job: str,
        timeout_s: float,
        name: Optional[str] = None,
        note: str = "",
        say: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, str]:
        """Wait for the held job (every component of it) to run; at the bound, or if it ends first, cancel it by its
        id and fail. With `name`, a job that carries another is never cancelled (`_ours`). `note` ends the failure at
        the bound: what the caller knows that may keep the job waiting.

        A read of the job that fails (`ReadFailed`) is a missed poll, not the job's end (`ride_out`): the wait goes on,
        and fails only once every read has failed for `MISSED_POLLS_GIVE_UP_S` since the streak's first (never on one
        failure), or at the bound with a read that failed; it cancels nothing then, since nothing is known of the
        job: the caller's release reads it first (`Driver.release_held`). A job Slurm no longer knows, one that
        ended, and an answer for another job or with no offset end the wait as they always did."""
        deadline = self.clock() + timeout_s

        def past(error: ReadFailed) -> SlurmError:
            return SlurmError(
                f"the held job did not start within {timeout_s:.0f} s, and the last read of it failed ({error}){note}"
            )

        while True:
            records = self.ride_out(lambda: self.components(job) or [], f"held job {job}", say, deadline, past)
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
                raise SlurmError(f"the held job did not start within {timeout_s:.0f} s; it was cancelled{note}")
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

    def step_listing(self, job: str) -> List[Tuple[str, str]]:
        """The job's steps listed now, as (step id, name) in listing order, one for each step (the batch and extern
        steps left out): Slurm lists as many steps of one name as were started."""
        done = self.runner.run(["squeue", "-s", "-j", job, "-h", "-o", "%i %j"], self.command_timeout_s)
        if done.returncode != 0:
            if "Invalid job id" in done.stderr:
                return []
            raise SlurmError(f"squeue -s failed with status {done.returncode}: {done.stderr.strip()[:300]}")
        found = []
        for line in done.stdout.splitlines():
            match = STEP_LINE.fullmatch(line.strip())
            if match and not match.group(1).endswith((".batch", ".extern")):
                found.append((match.group(1), match.group(2)))
        return found

    def steps(self, job: str) -> Dict[str, str]:
        """The job's steps listed now, as name to step id (the batch and extern steps left out); of two steps of one
        name the later is kept (`steps_named` lists them all)."""
        return {name: step_id for step_id, name in self.step_listing(job)}

    def steps_named(self, job: str, name: str) -> List[str]:
        """The ids of every step of the job listed under `name` now, in listing order."""
        return [step_id for step_id, listed in self.step_listing(job) if listed == name]

    def cancel_step(self, step_id: str) -> None:
        """Cancel one step by the id squeue -s lists it under: `<job>.<n>`, or a component's `<leader>+<k>.<n>`."""
        if not STEP_ID.fullmatch(step_id):
            raise SlurmError("refusing to cancel something that is not one step id")
        self._call(["scancel", step_id])

    def term_step(self, step_id: str) -> None:
        """Send TERM to one step's processes by its id, without cancelling it: on the site a step's cancel was an
        outright kill, no trap run (K1), and a step's own TERM cleanup (the worker's removal of its directory) runs
        only on a signal it is sent."""
        if not STEP_ID.fullmatch(step_id):
            raise SlurmError("refusing to signal something that is not one step id")
        self._call(["scancel", "--signal=TERM", step_id])

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

    def step_rows(self, job: str) -> Optional[str]:
        """sacct's rows of a job's steps, `JobID|JobName|State|ExitCode|End` one per line, as sacct prints them: the
        state (`CANCELLED by <uid>` is a step a user cancelled), the exit code (`exit:signal`) and the end (local
        time of the node that prints it) raw, where `step_status` folds the code. None when sacct fails, does not
        answer or cannot run: no row is no proof, and the caller keeps its own failure (`up`'s launch retry,
        `hpclaunchproof`)."""
        try:
            done = self.runner.run(
                ["sacct", "-j", job, "-n", "-P", "-o", "JobID,JobName,State,ExitCode,End"], self.command_timeout_s
            )
        except (SlurmError, OSError):
            return None
        return done.stdout if done.returncode == 0 else None

    def step_observed(self, step_id: str, name: Optional[str] = None) -> Dict[str, Any]:
        """One step as Slurm itself shows it now, by its EXACT id: read-only (`squeue -s` and `sacct`, nothing that
        changes a job or a step), and never a guess. The answer has `running` (True, False or None), `state` (the
        accounting's word, or `RUNNING` for a step squeue lists and accounting has no row for yet), `started_at` and
        `ended_at` (Slurm's own text, a zone-less local time, compared as text and never as a clock), `exit_code` (an
        ended step's, 128 plus the signal when one ended it) and `unknown` (None, or why the step could not be told).

        The step is running when squeue lists exactly this id and accounting does not say it ended; ended when squeue
        lists no such id and accounting holds a terminal row with an end time for exactly this id. Everything else is
        unknown, with the reason: a command that failed or did not answer, an id neither listed nor recorded, the two
        disagreeing (the driver itself waits for `GONE_POLLS` absent polls before it takes a step as ended), the id
        listed under another name than `name`, a row twice, a state this read does not know. `step_status` reads a
        finished step by its name and answers None for a failed command and for no record alike; this one tells them
        apart and never takes another step's row for the one asked about (`4242.1` is not `4242.10`)."""
        if not STEP_ID.fullmatch(step_id):
            return _unknown_step("the step id is not a Slurm step id")
        job = step_id.rsplit(".", 1)[0]
        try:
            listed = self._listed_steps(job)
            rows = self._accounted_steps(job, step_id)
        except SlurmError as error:
            return _unknown_step(str(error).split(":")[0])
        if len(rows) > 1:
            return _unknown_step("accounting holds the step id more than once")
        if step_id in listed and name is not None and listed[step_id] != name:
            return _unknown_step("the step id is listed under another name than the service's step")
        if not rows:
            if step_id in listed:  # squeue lists it and accounting has not recorded it yet
                return _step_answer(True, "RUNNING", None, None, None)
            return _unknown_step(UNSEEN)
        state, started, ended, code = rows[0]
        if step_id in listed:
            if state == "RUNNING":
                return _step_answer(True, state, started, None, None)
            return _unknown_step(LISTED_ENDED if state in ENDED_STATES else LISTED_NOT_ENDED)
        if state in ENDED_STATES and ended is not None:
            return _step_answer(False, state, started, ended, code)
        if state == "RUNNING":
            return _unknown_step(UNLISTED_RUNNING)
        return _unknown_step(UNLISTED_NOT_ENDED)

    def _listed_steps(self, job: str) -> Dict[str, str]:
        """The job's steps squeue -s lists now, as id to name (the batch and extern steps left out); a job Slurm no
        longer knows lists none."""
        done = self.runner.run(["squeue", "-s", "-j", job, "-h", "-o", "%i %j"], self.command_timeout_s)
        if done.returncode != 0:
            if "Invalid job id" in done.stderr:
                return {}
            raise SlurmError("squeue -s failed with status %s" % done.returncode)
        found = {}
        for line in done.stdout.splitlines():
            if not line.strip():
                continue
            match = STEP_LINE.fullmatch(line.strip())
            if match is None:  # a row that is not one is an answer that cannot be read, never an absent step
                raise SlurmError("squeue -s answered something unreadable")
            if not match.group(1).endswith((".batch", ".extern")):
                found[match.group(1)] = match.group(2)
        return found

    def _accounted_steps(self, job: str, step_id: str) -> List[Tuple[str, Optional[str], Optional[str], Optional[int]]]:
        """The accounting's rows whose JobID is exactly `step_id`, each as (state, start, end, exit code): the state's
        first word, the times as text or None when Slurm gives none, the exit code None for a step not ended."""
        done = self.runner.run(
            ["sacct", "-j", job, "-n", "-P", "-o", "JobID,JobName,State,Start,End,ExitCode"], self.command_timeout_s
        )
        if done.returncode != 0:
            raise SlurmError("sacct failed with status %s" % done.returncode)
        rows = []
        seen = 0
        for line in done.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("|")
            if not _accounting_row(parts):  # a row that is not one is an answer that cannot be read, never the lag
                raise SlurmError("sacct answered something unreadable")
            seen += 1
            if parts[0] != step_id:
                continue
            words = parts[2].split()
            state = words[0] if words else ""
            code = None
            if state in ENDED_STATES and re.fullmatch(r"[0-9]+:[0-9]+", parts[5]):
                status, killed = (int(part) for part in parts[5].split(":"))
                code = 128 + killed if killed else status
            rows.append((state, _slurm_time(parts[3]), _slurm_time(parts[4]), code))
        if not seen:  # the job itself has rows (its batch step at least): none at all is no record of it
            raise SlurmError("accounting holds no record of the job")
        return rows

    def accounted(self, job: str) -> Dict[str, Dict[str, object]]:
        """The job's steps as the accounting records them, by name: each one's id, its exit status (None when it is not
        a status), its state and its Start (epoch seconds, from sacct's local time, None when it has none); the batch
        and extern steps left out. SlurmError when the accounting cannot be read, so a caller never takes an unread
        record for an empty one."""
        done = self.runner.run(
            ["sacct", "-j", job, "-n", "-P", "-o", "JobID,JobName,ExitCode,State,Start"], self.command_timeout_s
        )
        if done.returncode != 0:
            raise SlurmError(f"sacct failed with status {done.returncode}: {done.stderr.strip()[:300]}")
        found: Dict[str, Dict[str, object]] = {}
        for line in done.stdout.splitlines():
            parts = line.split("|")
            if len(parts) != 5 or not STEP_ID.fullmatch(parts[0]):
                continue
            status: Optional[int] = None
            if re.fullmatch(r"[0-9]+:[0-9]+", parts[2]):
                code, killed = (int(part) for part in parts[2].split(":"))
                status = 128 + killed if killed else code
            began: Optional[float] = None
            with contextlib.suppress(ValueError, OverflowError, OSError):  # "Unknown" and the like: no Start
                began = datetime.strptime(parts[4], "%Y-%m-%dT%H:%M:%S").timestamp()
            found.setdefault(
                parts[1],
                {"id": parts[0], "exit": status, "state": parts[3].split()[0] if parts[3] else "", "start": began},
            )
        return found

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
