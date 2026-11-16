# Provenance: new
"""The fault relay: the HPC federation's fault verbs, run on the login node for a campaign that runs on a node (O56).

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

**What the relay runs.** Only the four verbs of its own federation: it claims a request by renaming it into `taken/`
(so no two relays, or one relay twice, run it), and refuses, with a word and nothing run, anything else: a request for
another relay (an earlier one: a restart never replays an old request), another federation, a service its state file
does not list, a verb or exec command outside the sets, a key it does not know, or a deadline already past. It runs
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

**Liveness.** The relay writes `heartbeat.json` every `BEAT_S` (its nonce, the federation, a count and the wall clock),
removes it when it stops, and holds `relay.lock` while it runs, so a second relay for the same root refuses to start.
The fault gate's pre-run check reads it and notes what it saw in the try's note (`RELAY_NOTE`), and the run admission
refuses an outage run, as the hold `federation_busy` that uses no attempt, when that check saw no live relay or none is
live at the admission (`relay_refusal`). A heartbeat is live while its wall clock is within `RELAY_STALE_S` of the
reader's: the login node and the nodes keep the cluster's time.
"""

import contextlib
import fcntl
import json
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

FORMAT = "expcore-fault-relay/1"
RELAY_DIR = "fault-relay"
STATE_FILE = "hpc-federation.json"
BEAT_S = 5.0
RELAY_STALE_S = 30.0  # a heartbeat older than this, by the reader's wall clock, is a relay not answering
RELAY_NOTE = "fault_relay"  # the try's note key: what the fault gate's pre-run check saw of the relay
POLL_S = 0.5  # between two looks for an answer, or for a request
OUTPUT_CAP = 65536  # the most of a command's output an answer carries, per stream
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
    """The services the state file lists, by their layout names; empty when it cannot be read."""
    services = (_read(Path(root) / STATE_FILE) or {}).get("services")
    return {name for name in services if isinstance(name, str)} if isinstance(services, dict) else set()


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
        began = self._clock()
        while True:
            answer = _read(directory / "answers" / f"{key}.json")
            if answer is not None:
                with contextlib.suppress(OSError):
                    (directory / "answers" / f"{key}.json").unlink()
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


def clean_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment the relay runs a verb with: `ENVIRONMENT`'s names only, never one that looks like a secret."""
    return {name: environ[name] for name in ENVIRONMENT if name in environ and not SECRET_NAME.search(name)}


def run_clean(environ: Mapping[str, str]) -> Callable[..., CommandResult]:
    """A command runner whose commands get `clean_environment(environ)` and no stdin."""
    env = clean_environment(environ)

    def run(argv: Sequence[str], *, timeout_s: float) -> CommandResult:
        done = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=env,
            stdin=subprocess.DEVNULL,
        )
        return CommandResult(done.returncode, done.stdout, done.stderr)

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
    ) -> None:
        environ = dict(os.environ if environ is None else environ)
        named = sorted(name for name in environ if SECRET_NAME.search(name))
        if named:  # names only: never a value
            raise PermissionError(f"the relay refuses to run with a secret-like name in its environment: {named}")
        self.root, self.nonce = Path(root), uuid.uuid4().hex
        self._verbs = VerbCommand(prefix, runner or run_clean(environ))
        self._clock, self._wall = clock, wall
        self._beats = 0
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
        waiting = sorted(p for p in (directory / "requests").glob("*.json") if not p.name.startswith("."))
        for path in waiting:
            claimed = directory / "taken" / path.name
            try:
                os.rename(path, claimed)  # atomic: a request is taken once, by one relay
            except OSError:
                continue
            taken += 1
            record = _read(claimed)
            key = path.stem if _ID.fullmatch(path.stem) else None
            answer = self._answer(record, key)
            if key is not None:
                _write(
                    directory / "answers" / f"{key}.json", {"format": FORMAT, "id": key, "relay": self.nonce, **answer}
                )
            with contextlib.suppress(OSError):
                claimed.unlink()
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
        if record.get("verb") not in VERBS or record.get("service") not in services_of(self.root):
            return "not_allowed"
        if record.get("verb") == "exec" and record.get("command") not in EXEC_COMMANDS:
            return "not_allowed"
        deadline, bound = record.get("deadline"), record.get("bound_s")
        for value in (deadline, bound):
            if isinstance(value, bool) or not isinstance(value, int | float):
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
        bound = max(0.0, min(float(record["bound_s"]), left))
        try:
            done = self._verbs(request, timeout_s=bound)
        except subprocess.TimeoutExpired:
            return {"timed_out": True}
        except OSError:
            return {"refused": "not_run"}
        return {"returncode": done.returncode, "stdout": done.stdout[:OUTPUT_CAP], "stderr": done.stderr[:OUTPUT_CAP]}

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
                if not self.serve_once():
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


def _stop(root: Path, timeout_s: float = 3 * BEAT_S) -> int:
    live = beat(root)
    if live is None:
        return 0  # nothing beats: nothing to stop
    pid, start = live.get("pid"), live.get("start")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1 and isinstance(start, str):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            handle = os.pidfd_open(pid)  # held across the check: a PID reused before it shows another start
            try:
                if proc_start(pid) == start:  # the relay that wrote the heartbeat, and no other process
                    signal.pidfd_send_signal(handle, signal.SIGTERM)
            finally:
                os.close(handle)
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if not (relay_dir(root) / "heartbeat.json").exists():
            return 0
        time.sleep(0.2)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
