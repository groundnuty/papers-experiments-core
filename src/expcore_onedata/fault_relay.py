# Provenance: new
"""The fault relay: the HPC federation's fault verbs, run on the login node for a campaign that runs on a node (O58).

A Slurm step can only be launched from the cluster's login node, and the campaign's dispatcher runs as a step on the
sandbox node, so it cannot run the federation's verbs (`hpcfed.py stop|start|status|exec`) itself. The relay is a
long-lived process on the login node, started by the federation's `up` and stopped by its `down`, that runs them for
it. The dispatcher's driver (`fault_hpc.HpcDriver`) hands each request to it through files on the federation's
shared root (`RelayClient`), and the relay answers through files.

**The files.** `<root>/fault-relay/` (0700) holds `requests/`, `taken/` and `answers/` (each 0700), every file 0600 and
written atomically (a draft beside it, fsynced, renamed), one request or answer per file, named by the request's id. A
request names the relay it is for (the `nonce` of its heartbeat), the federation (its allocation's name, from the state
file `hpc-federation.json`), one verb of `fault_hpc.VERBS`, one service, for an exec one command of
`fault_hpc.EXEC_COMMANDS` (a name, never an argv), its command's bound and its deadline.

**How a request and its answer cross hosts.** The client runs on a compute node and the relay on the login node, over
one shared file system (NFS). A node's NFS client keeps a negative entry for a name it looked up while the file was
missing, and keeps failing that lookup until the directory's attributes expire (30 s with the default `acdirmin`),
however soon the file was created. A client that polled `answers/<id>.json` by path made that first lookup before the
relay had answered, so every verb cost about 30 s whatever its own time. So a poll that waits for a file another host
creates lists the directory (`os.scandir`: an `opendir` revalidates the directory and drops its stale negative
entries) and opens the file only once the listing shows its exact name, never a stat or an open of the path first. The
client does this for its answer, in a fresh listing on every poll; the relay lists `requests/` for its requests the same
way. The heartbeat is read once per admission and once per try, and `up`'s wait for it and `stop`'s wait for it to go
run on the host that writes it: none of them is a poll for a file another host creates.

**What the relay runs.** Only the four verbs of its own federation: it claims a request by renaming it into `taken/`
(so no two relays, or one relay twice, run it), and refuses, with a word and nothing run, anything else: a request for
another relay (an earlier one: a restart never replays an old request), an id it has taken before (`replayed`: a
request file written again under a taken id never runs twice), another federation, a service its state file does not
list, a verb or exec command outside the sets, a key it does not know, a field of the wrong type, a deadline or bound
that is no finite number, or a deadline already past. No request, however malformed, stops the relay. It runs
the verb as `[*prefix, <verb>, <service>]` (an exec: `[*prefix, "exec", <service>, "--", <the allowed command's
argv>]`, `fault_hpc.VerbCommand`), where `prefix` is how `up` started it (`hpcfed.py` with the root and the layout),
never anything a request says, within the request's bound and deadline, one request at a time, in the order they were
issued, with an environment of its own (`ENVIRONMENT`'s names only). It refuses to start when its own environment
holds a name that looks like a secret (`SECRET_NAME`): no token is ever in its arguments or its environment.

**Fail-closed.** A request with no answer by its deadline is unknown, never taken as done: the client withdraws it when
it can (the request still in `requests/`: never run, an OSError, read as not sent) and otherwise raises
`subprocess.TimeoutExpired` (it may have run, or may still run), which the driver reads as a command with no answer,
so the injector journals a stop of unknown outcome (`stop_unknown`, `faults.py`) and the restore waits for the service
to show down, a start is verified only by a changed identity read afterwards, and a link restart is `command_timeout`,
never retried. A relay that is not answering (no heartbeat, or one older than `RELAY_STALE_S`) gets no request at all.

**A timed-out verb's late steps.** A verb runs as a process that launches an `srun` step. It runs in a process group of
its own, and at its bound the whole group is ended (`fault_relay_group`), so no `srun` client survives it to create
the step after the relay has answered. The relay also stamps each verb's steps with a tag of its own, and after a
timed-out verb starts a watch that cancels, by exact id and in the federation's own job alone, any step of that verb's
exact names that is made late, and journals it (`fault_relay_late`). The answer stays `timed_out`.

**Liveness.** The relay writes `heartbeat.json` every `BEAT_S` (its nonce, the federation, a count and the wall clock),
removes it when it stops, and holds `relay.lock` while it runs, so a second relay for the same root refuses to start.
The fault gate's pre-run check reads it and notes what it saw in the try's note (`RELAY_NOTE`), and the run admission
refuses an outage run, as the hold `federation_busy` that uses no attempt, when that check saw no live relay or none is
live at the admission (`relay_refusal`). A heartbeat is live while its wall clock is within `RELAY_STALE_S` of the
reader's: the login node and the nodes keep the cluster's time.
"""

import contextlib
import ctypes
import errno
import fcntl
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from expcore_onedata.compose import CommandResult, DriverError
from expcore_onedata.fault_hpc import EXEC_COMMANDS, VERBS, Request, VerbCommand
from expcore_onedata.fault_relay_group import GRACE_S, run_in_group
from expcore_onedata.fault_relay_late import (
    LATE_POLL_S,
    LATE_WINDOW_S,
    journal_path,
    record,
    spawn_watch,
    step_tag,
    watch_argv,
)

FORMAT = "expcore-fault-relay/1"
RELAY_DIR = "fault-relay"
STATE_FILE = "hpc-federation.json"
BEAT_S = 5.0
RELAY_STALE_S = 30.0  # a heartbeat older than this, by the reader's wall clock, is a relay not answering
RELAY_MODULE = "expcore_onedata.fault_relay"  # the relay runs as `-m RELAY_MODULE serve --root <root>`: how it is told
RELAY_NOTE = "fault_relay"  # the try's note key: what the fault gate's pre-run check saw of the relay
POLL_S = 0.5  # between two listings that look for an answer
OUTPUT_CAP = 65536  # the most of a command's output an answer carries, per stream
MIN_RUN_S = 1.0  # a request with less than this left before its deadline is refused, never started
ENVIRONMENT = ("PATH", "HOME", "LANG", "LC_ALL", "USER", "LOGNAME", "SLURM_CONF")
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|PASSPHRASE|CREDENTIAL|COOKIE|(^|_)KEY($|_)", re.IGNORECASE)
_ID = re.compile(r"[0-9a-f]{32}")
_FEDERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_REQUEST_KEYS = {"format", "id", "relay", "federation", "verb", "service", "command", "bound_s", "deadline"}


class RelayDown(OSError):
    """No request was sent, or one was withdrawn before the relay took it: nothing ran."""


def relay_dir(root: Path) -> Path:
    return Path(root) / RELAY_DIR


def _write(path: Path, record: Mapping[str, Any]) -> None:
    """Write one JSON file atomically, 0600: a draft beside it, fsynced, renamed over it, the directory fsynced."""
    draft = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    fd = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(record), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, path)
    except BaseException:
        with contextlib.suppress(OSError):
            draft.unlink()
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _read(path: Path) -> dict[str, Any] | None:
    """One JSON object from a file of ours that is no link, or None when it is missing, not ours or not one."""
    try:
        if path.is_symlink() or path.stat().st_uid != os.getuid():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _listed(directory: Path, name: str) -> bool:
    """Whether a fresh listing of `directory` shows a file called exactly `name`; False when it cannot be listed (the
    next poll lists it again). `os.scandir` opens the directory on every call, which revalidates it on an NFS client;
    nothing here looks `name` up by path, so no negative entry is left for it."""
    try:
        with os.scandir(directory) as entries:
            return any(entry.name == name for entry in entries)
    except OSError:
        return False


def _private_dir(path: Path) -> None:
    """Make `path` a 0700 directory of ours, or refuse one that is a link, another's, or open to others."""
    path.mkdir(mode=0o700, exist_ok=True)
    seen = path.lstat()
    if path.is_symlink() or seen.st_uid != os.getuid() or seen.st_mode & 0o077:
        raise PermissionError(f"the relay's directory {path.name} is not a private directory of this user")


def federation_of(root: Path) -> str | None:
    """The federation's name, the allocation's in the state file; None when it cannot be read."""
    allocation = (_read(Path(root) / STATE_FILE) or {}).get("allocation")
    name = allocation.get("name") if isinstance(allocation, dict) else None
    return name if isinstance(name, str) and _FEDERATION.fullmatch(name) else None


def proc_start(pid: int) -> str | None:
    """Field 22 of /proc/PID/stat (the process's start, in clock ticks after boot), or None when it cannot be read."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    fields = stat[stat.rfind(")") + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def services_of(root: Path) -> set[str]:
    """The providers' services the state file lists, by their layout names (its `kind` "op"): what an outage or a heal
    may act on, never the zone; empty when it cannot be read."""
    services = (_read(Path(root) / STATE_FILE) or {}).get("services")
    if not isinstance(services, dict):
        return set()
    return {
        name
        for name, entry in services.items()
        if isinstance(name, str) and isinstance(entry, dict) and entry.get("kind") == "op"
    }


def beat(root: Path, wall: Callable[[], float] = time.time, stale_s: float = RELAY_STALE_S) -> dict[str, Any] | None:
    """The relay's heartbeat when it is live (its nonce, federation, count and wall time), None otherwise."""
    record = _read(relay_dir(root) / "heartbeat.json")
    if record is None or record.get("format") != FORMAT or not _ID.fullmatch(str(record.get("nonce", ""))):
        return None
    at = record.get("wall")
    if isinstance(at, bool) or not isinstance(at, int | float) or abs(wall() - at) > stale_s:
        return None
    return record


def relay_refusal(root: Path, note: Mapping[str, Any] | None, wall: Callable[[], float] = time.time) -> str | None:
    """Why an outage run may not be admitted for the relay's sake, or None when it may: its try's pre-run check saw no
    live relay (or a try that has a note never had it read), or none is live now. A caller that is no campaign (no
    note) is held to the second alone. The refusal is a hold that uses no attempt."""
    again = "the run is checked again before it is admitted, and no attempt is used"
    if note is not None:
        seen = note.get(RELAY_NOTE)
        if not isinstance(seen, Mapping) or seen.get("alive") is not True:
            return (
                f"the run's pre-run check saw no live fault relay, which an outage run's stop and restore need: {again}"
            )
    if beat(root, wall) is None:
        return f"the fault relay is not answering, and an outage run's stop and restore go through it: {again}"
    return None


class RelayClient:
    """The dispatcher's transport (`fault_hpc.Transport`): each request a file for the relay, each answer awaited, for
    the command's bound and `wait_s` more (see the module docstring)."""

    def __init__(
        self,
        root: Path,
        *,
        wait_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._root, self._wait_s = Path(root), wait_s
        self._clock, self._sleep, self._wall = clock, sleep, wall

    def __call__(self, request: Request, *, timeout_s: float) -> CommandResult:
        live, federation = beat(self._root, self._wall), federation_of(self._root)
        if live is None or federation is None:
            raise RelayDown("the fault relay is not answering, or the federation's state cannot be read: nothing sent")
        directory, key = relay_dir(self._root), uuid.uuid4().hex
        waited = timeout_s + self._wait_s
        record = {
            "format": FORMAT,
            "id": key,
            "relay": live["nonce"],
            "federation": federation,
            **request.to_json(),
            "bound_s": timeout_s,
            "deadline": self._wall() + waited,
        }
        _write(directory / "requests" / f"{key}.json", record)
        answers, name = directory / "answers", f"{key}.json"
        began = self._clock()
        while True:
            # the answer's path is looked up only once a listing has shown its name (see the module docstring)
            answer = _read(answers / name) if _listed(answers, name) else None
            if answer is not None:
                with contextlib.suppress(OSError):
                    (answers / name).unlink()
                return _answered(answer, key, live["nonce"], timeout_s)
            if self._clock() - began >= waited:
                break
            self._sleep(min(POLL_S, max(0.0, waited - (self._clock() - began))))
        try:
            (directory / "requests" / f"{key}.json").unlink()  # never taken: withdrawn, and never run
        except FileNotFoundError:
            raise subprocess.TimeoutExpired(["fault-relay", request.verb], waited) from None  # taken: unknown
        raise RelayDown("the fault relay took no request in time: withdrawn, nothing ran")


def _answered(answer: Mapping[str, Any], key: str, nonce: str, timeout_s: float) -> CommandResult:
    """An answer as a command's end; raise DriverError for a refusal (nothing ran) and `subprocess.TimeoutExpired` for
    a command the relay ran past its bound (its outcome unknown). An answer not for this request is no answer."""
    if answer.get("id") != key or answer.get("relay") != nonce or answer.get("format") != FORMAT:
        raise subprocess.TimeoutExpired(["fault-relay"], timeout_s)
    refused = answer.get("refused")
    if refused is not None:
        word = refused if isinstance(refused, str) and re.fullmatch(r"[a-z_]{1,40}", refused) else "refused"
        raise DriverError(f"the fault relay refused the request ({word}): nothing ran")
    if answer.get("timed_out") is True:
        raise subprocess.TimeoutExpired(["fault-relay"], timeout_s)
    code, out, err = answer.get("returncode"), answer.get("stdout"), answer.get("stderr")
    if isinstance(code, bool) or not isinstance(code, int) or not isinstance(out, str) or not isinstance(err, str):
        raise subprocess.TimeoutExpired(["fault-relay"], timeout_s)
    return CommandResult(code, out, err)


def _issued(paths: Iterator[Path]) -> list[Path]:
    """Request files in the order they were written (each written once, atomically), drafts left out."""
    found = []
    for path in paths:
        if path.name.startswith("."):
            continue
        try:
            found.append((path.stat().st_mtime_ns, path.name, path))
        except OSError:  # taken meanwhile
            continue
    return [path for _, _, path in sorted(found)]


def clean_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment the relay runs a verb with: `ENVIRONMENT`'s names only, never one that looks like a secret."""
    return {name: environ[name] for name in ENVIRONMENT if name in environ and not SECRET_NAME.search(name)}


def run_clean(
    environ: Mapping[str, str], *, grace_s: float = GRACE_S, on_stragglers: Callable[[], None] | None = None
) -> Callable[..., CommandResult]:
    """A command runner whose commands get `clean_environment(environ)` and no stdin, each in a process group of its own
    that is ended whole at the bound (SIGTERM, `grace_s`, SIGKILL: `fault_relay_group`), so nothing a verb launched, its
    `srun` client above all, outlives it; what a verb that returned left in its group is ended too, and `on_stragglers`
    called."""
    env = clean_environment(environ)

    def run(argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        return run_in_group(argv, env=env, timeout_s=timeout_s, grace_s=grace_s, on_stragglers=on_stragglers)

    return run


class Relay:
    """The relay of one federation's root (see the module docstring). `prefix` is how the verbs are run (`hpcfed.py`
    with this root and its layout); `runner` runs them (`run_clean` of the relay's environment, by default)."""

    def __init__(
        self,
        root: Path,
        prefix: Sequence[str],
        *,
        runner: Callable[..., CommandResult] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        watcher: Callable[[Sequence[str], Mapping[str, str]], Any] | None = None,
        late_window_s: float = LATE_WINDOW_S,
        late_poll_s: float = LATE_POLL_S,
    ) -> None:
        environ = dict(os.environ if environ is None else environ)
        named = sorted(name for name in environ if SECRET_NAME.search(name))
        if named:  # names only: never a value
            raise PermissionError(f"the relay refuses to run with a secret-like name in its environment: {named}")
        self.root, self.nonce = Path(root), uuid.uuid4().hex
        self._prefix = list(prefix)
        self._left_behind = False  # the verb now running left a child in its group, which the runner ended
        self._verbs = VerbCommand(prefix, runner or run_clean(environ, on_stragglers=self._note_stragglers))
        self._clock, self._wall = clock, wall
        self._env = clean_environment(environ)  # what the verbs, and the watch, are run with
        self._watcher = watcher or spawn_watch  # (argv, env) -> a process handle with `poll()`
        self._late_window_s, self._late_poll_s = late_window_s, late_poll_s
        self._watches: list[tuple[str, Any]] = []  # each watch started: its request id and handle, until it ends
        self._beats = 0
        self._taken: set[str] = set()  # every request id this relay has claimed: one never runs twice
        self._stop = threading.Event()

    @contextlib.contextmanager
    def holding(self) -> Iterator[None]:
        """Make the relay's directories, take `relay.lock` (a second relay of this root refuses), beat while held,
        and remove the heartbeat when it ends."""
        directory = relay_dir(self.root)
        _private_dir(directory)
        for name in ("requests", "taken", "answers"):
            _private_dir(directory / name)
        lock = os.open(directory / "relay.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PermissionError("another fault relay holds this federation's root") from None
            self.beat()
            try:
                yield
            finally:
                with contextlib.suppress(OSError):
                    (directory / "heartbeat.json").unlink()
        finally:
            os.close(lock)

    def beat(self) -> None:
        self._beats += 1
        record = {"format": FORMAT, "nonce": self.nonce, "federation": federation_of(self.root), "pid": os.getpid()}
        record["start"] = proc_start(os.getpid())
        _write(relay_dir(self.root) / "heartbeat.json", {**record, "beat": self._beats, "wall": self._wall()})

    def serve_once(self) -> int:
        """Answer every request waiting now, in the order they were issued; return how many were taken."""
        directory, taken = relay_dir(self.root), 0
        for path in _issued((directory / "requests").glob("*.json")):
            claimed = directory / "taken" / path.name
            try:
                os.rename(path, claimed)  # atomic: a request is taken once, by one relay
            except OSError:
                continue
            taken += 1
            record = _read(claimed)
            key = path.stem if _ID.fullmatch(path.stem) else None
            replayed = key is not None and key in self._taken
            if key is not None:
                self._taken.add(key)
            try:
                answer = {"refused": "replayed"} if replayed else self._answer(record, key)
            except Exception:  # a request that breaks its own checks never stops the relay: refused before it ran
                answer = {"refused": "malformed"}
            if key is not None and not replayed:  # a replay's id is answered for its first run, never overwritten
                with contextlib.suppress(OSError):  # no answer: the client reads the taken request as unknown
                    _write(
                        directory / "answers" / f"{key}.json",
                        {"format": FORMAT, "id": key, "relay": self.nonce, **answer},
                    )
            with contextlib.suppress(OSError):
                claimed.unlink()
        self._reap_watches()
        return taken

    def _refusal(self, record: dict[str, Any] | None, key: str | None) -> str | None:
        """The word a request is refused with, or None when it may run."""
        if record is None or key is None or record.get("id") != key or record.get("format") != FORMAT:
            return "malformed"
        if set(record) - _REQUEST_KEYS:
            return "unknown_key"
        if record.get("relay") != self.nonce:
            return "other_relay"  # an earlier relay's: a restart never replays it
        if record.get("federation") is None or record.get("federation") != federation_of(self.root):
            return "other_federation"
        verb, service, command = record.get("verb"), record.get("service"), record.get("command")
        if not all(isinstance(value, str) for value in (verb, service)) or not isinstance(command, str | None):
            return "malformed"
        if verb not in VERBS or service not in services_of(self.root):
            return "not_allowed"
        if verb == "exec" and command not in EXEC_COMMANDS:
            return "not_allowed"
        deadline, bound = record.get("deadline"), record.get("bound_s")
        for value in (deadline, bound):
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
                return "malformed"
        if bound <= 0:
            return "malformed"
        if deadline <= self._wall():
            return "expired"
        return None

    def _answer(self, record: dict[str, Any] | None, key: str | None) -> dict[str, Any]:
        refused = self._refusal(record, key)
        if refused is not None:
            return {"refused": refused}
        assert record is not None
        try:
            request = Request(record["verb"], record["service"], record.get("command"))
        except DriverError:
            return {"refused": "not_allowed"}
        left = record["deadline"] - self._wall() - POLL_S  # answered before the client's last look
        if left < MIN_RUN_S:  # too late to run and answer: refused, so nothing is started and killed at once
            return {"refused": "expired"}
        bound = min(float(record["bound_s"]), left)
        assert key is not None  # `_refusal` holds it
        tag, self._left_behind = step_tag(self.nonce, key), False
        try:
            done = self._verbs(request, timeout_s=bound, step_tag=tag)
        except subprocess.TimeoutExpired:
            self._watch(request, key, tag, "timed_out")  # before the answer: the guard is in place when it is read
            return {"timed_out": True}
        except OSError:  # the verb's process could not be started
            return {"refused": "not_run"}
        except Exception:  # anything else once the verb may have run: its outcome is unknown, never "not run"
            self._watch(request, key, tag, "unknown")
            return {"timed_out": True}
        if self._left_behind:  # it ended with an srun still in its group, now ended: its steps may yet be made
            self._watch(request, key, tag, "left_behind")
        return {"returncode": done.returncode, "stdout": done.stdout[:OUTPUT_CAP], "stderr": done.stderr[:OUTPUT_CAP]}

    def _note_stragglers(self) -> None:
        self._left_behind = True

    def _watch(self, request: Request, key: str, tag: str, why: str) -> None:
        """Journal that a watch is owed for the verb's steps (`why`: it timed out, ended unknown, or left an srun in
        its group), then start it. It never raises: a journal that cannot be written never keeps the watch from starting
        (the watch is the guard), and a watch that cannot be started is journalled."""
        journal = journal_path(self.root)
        event = {"request": key, "verb": request.verb, "service": request.service, "tag": tag, "why": why}
        with contextlib.suppress(OSError):
            record(journal, {"event": "watch_opened", **event, "window_s": self._late_window_s, "wall": self._wall()})
        argv = watch_argv(self._prefix, tag, request.verb, request.service, key, self._late_window_s, self._late_poll_s)
        try:
            handle = self._watcher(argv, self._env)
        except Exception as error:  # no process to start it with, say: said in the journal, the answer unchanged
            with contextlib.suppress(OSError):
                record(journal, {"event": "watch_failed", **event, "error": type(error).__name__, "wall": self._wall()})
            return
        self._watches.append((key, handle))

    def _reap_watches(self) -> None:
        """Collect the watches that ended; one that failed (a nonzero status) is journalled, since it may have ended
        with no closing line of its own. A running watch is never waited for."""
        running = []
        for key, handle in self._watches:
            status = handle.poll()
            if status is None:
                running.append((key, handle))
            elif status != 0:
                with contextlib.suppress(OSError):
                    record(
                        journal_path(self.root),
                        {"event": "watch_ended", "request": key, "returncode": status, "wall": self._wall()},
                    )
        self._watches = running

    def stop(self) -> None:
        self._stop.set()

    def serve(self, poll_s: float = 0.2) -> None:
        """Beat every `BEAT_S` from a thread of its own and answer requests until `stop` (SIGTERM, SIGINT)."""

        def beating() -> None:
            while not self._stop.wait(BEAT_S):
                with contextlib.suppress(OSError):
                    self.beat()

        with self.holding():
            beater = threading.Thread(target=beating, name="fault-relay-beat", daemon=True)
            beater.start()
            while not self._stop.is_set():
                try:
                    served = self.serve_once()
                except OSError:  # the relay's directory could not be read this time: looked at again, never fatal
                    served = 0
                if not served:
                    self._stop.wait(poll_s)
            beater.join(timeout=BEAT_S + 1)


def main(argv: Sequence[str]) -> int:
    """`serve --root R -- <prefix...>`: run the relay; `alive --root R`: exit 0 when a live relay beats (its record on
    stdout), 1 otherwise; `stop --root R`: SIGTERM to the relay that beats, then wait for its heartbeat to go."""
    args = list(argv)
    if len(args) >= 3 and args[1] == "--root":
        verb, root = args[0], Path(args[2])
        if verb == "serve" and len(args) > 4 and args[3] == "--":
            relay = Relay(root, args[4:])
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: relay.stop())
            relay.serve()
            return 0
        if verb == "alive" and len(args) == 3:
            live = beat(root)
            print(json.dumps({"alive": live is not None, "nonce": None if live is None else live["nonce"]}))
            return 0 if live is not None else 1
        if verb == "stop" and len(args) == 3:
            return _stop(root)
    print("usage: fault_relay serve --root R -- <prefix...> | alive --root R | stop --root R", file=sys.stderr)
    return 2


PIDFD_OPEN, PIDFD_SEND_SIGNAL = 434, 424  # the generic syscall numbers (x86_64 and aarch64 alike)


def _syscall(number: int, *args: int) -> int:
    """A raw system call through the C library, for a Python built without the pidfd calls (uv's standalone builds,
    made against an old glibc); raise OSError with its errno when it fails."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    done = libc.syscall(ctypes.c_long(number), *(ctypes.c_long(arg) for arg in args))
    if done < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(done)


def _open_pidfd(pid: int) -> int:
    """pidfd_open(2): `os.pidfd_open` where this Python has it, else the raw call. Raise OSError with ENOSYS when the
    kernel has no such call (Linux before 5.3), ProcessLookupError when the process is gone."""
    opener = getattr(os, "pidfd_open", None)
    return opener(pid) if opener is not None else _syscall(PIDFD_OPEN, pid, 0)


def proc_runs(pid: int, argv: Sequence[str]) -> bool:
    """Whether the process's command line (/proc/PID/cmdline) holds `argv` as consecutive arguments, in order; True for
    an empty `argv` of a process whose command line can be read, False when it cannot be read."""
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    want = [word.encode() for word in argv]
    return any(args[i : i + len(want)] == want for i in range(len(args) - len(want) + 1))


def _kill_if_started(pid: int, start: str, signum: int, argv: Sequence[str]) -> bool:
    """`signal_if_started` where the kernel has no pidfd: read `pid`'s command line, then its start tick and, only while
    it is `start`, send `signum` with `os.kill` at once; the pattern `hpcslurm.Runner.end_client` follows, whose caller
    rechecks that the process went (`_stop` waits for its heartbeat to go). A PID taken over since the process ended
    shows another tick and is never signalled. Weaker than the pidfd, which holds the process across the checks: a PID
    reused in the instant between the tick's read and the kill is not caught, and a PID is reused only once the whole
    PID space has cycled."""
    named = proc_runs(pid, argv)
    seen = proc_start(pid)  # the last read: the kill follows it at once, and a PID reused since `named` shows another
    if seen is None:  # no /proc entry: the process is gone, or its start cannot be shown to be ours
        raise ProcessLookupError(errno.ESRCH, f"no process {pid} with a readable start tick")
    if seen != start or not named:  # another process holds the PID now, or this one is not what was recorded
        return False
    os.kill(pid, signum)
    return True


def signal_if_started(pid: int, start: str, signum: int, *, argv: Sequence[str] = ()) -> bool:
    """Send `signum` to `pid` only while it is the process that started at clock tick `start` and, when `argv` is given,
    whose command line holds it as consecutive arguments (a heartbeat is a file: its start tick alone does not make a
    process the relay). A pidfd is opened first and held across the checks, so a PID reused before it shows another
    start and is never signalled (pidfd_open(2), pidfd_send_signal(2), through `os` and `signal` where this Python has
    them, else the raw calls). A kernel that answers ENOSYS to `pidfd_open` (a login node older than Linux 5.3) has no
    pidfd: the checks are then made and the signal sent with `os.kill` (`_kill_if_started`); any other failure is
    raised. No process group (PID 0 or below), no init (PID 1) and no process without a recorded start tick is ever
    signalled. Return whether it was sent; raise ProcessLookupError when the process is gone."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1 or not isinstance(start, str) or not start:
        return False
    try:
        handle = _open_pidfd(pid)
    except OSError as error:
        if error.errno != errno.ENOSYS:  # a gone process (ESRCH), too many open files, ...: never worked around
            raise
        return _kill_if_started(pid, start, signum, argv)
    try:
        if proc_start(pid) != start or not proc_runs(pid, argv):  # another process holds the PID now, or not ours
            return False
        sender = getattr(signal, "pidfd_send_signal", None)
        if sender is not None:
            sender(handle, signum)
        else:
            _syscall(PIDFD_SEND_SIGNAL, handle, int(signum), 0, 0)
        return True
    finally:
        os.close(handle)


def _stop(root: Path, timeout_s: float = 3 * BEAT_S) -> int:
    live = beat(root)
    if live is None:
        return 0  # nothing beats: nothing to stop
    pid, start = live.get("pid"), live.get("start")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 and isinstance(start, str):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            signal_if_started(pid, start, signal.SIGTERM, argv=("-m", RELAY_MODULE, "serve", "--root", str(root)))
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if not (relay_dir(root) / "heartbeat.json").exists():
            return 0
        time.sleep(0.2)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
