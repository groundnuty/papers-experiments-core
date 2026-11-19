"""fleethop.py: the hop from an HPC job to the group's LLM fleet (O93; README.md, "The fleet hop").

The fleet's front is an OpenAI-compatible service listening on a unix socket of a login node. A compute node reaches
it through one ssh tunnel that forwards a port of the job's own loopback to that socket, so a study's `models.toml`
keeps a base URL on 127.0.0.1 and the hop only makes it reachable. A manifest's optional `fleet_hop` (and the fleet's
shard settings, for the elastic worker) is the table

    fleet_hop = { login = "<host>", socket = "<abs path of the front's socket on that login node>",
                  key = "<abs path of the forward-only key>", known_hosts = "<abs path>", local_port = 30051,
                  backup_socket = "<abs path of another front's socket on that login node>" }  # the last is optional

`HopKeeper` runs `ssh -F /dev/null -i KEY ... -N -L 127.0.0.1:PORT:SOCKET LOGIN` as a child of its own (`hop_argv`:
no agent, no config, one key, a host key that must already be known, no prompt, a forward that must bind). `start`
refuses (`HopRefused`) at once when the port already accepts a connection (something else holds it), and otherwise
returns when 127.0.0.1:PORT accepts a connection and the child is still alive a moment later, or refuses after
`WAIT_S`; while it is kept, a thread starts the hop again when it exits, no faster than once per `min_interval_s`
(10 s, longer after a hop that keeps dying); `stop` kills it. A step and a worker keep it for as long as their
dispatcher or worker runs and stop it in a `finally` with the stop signals held, as they remove the operator's key
files (`shards.py`, `fleetworker.py`).

With a `backup_socket` the hop is also judged by what comes through it, since a dead tunnel leaves its socket file
behind and `ssh -L` then still comes up with its port accepting: a keyless HTTP GET /v1/models through the port, where
any HTTP status means the front is alive and no answer within `PROBE_TIMEOUT_S` means dead (the key is never sent).
`start` tries the primary socket and, if its forward does not come up or its probe is dead, the backup the same way;
while the dispatcher runs the probe is repeated every `PROBE_S`, and after `DEAD_PROBES` dead ones in a row the forward
is restarted on the other socket. A hop that exits is started again on the primary when it had been proven alive, and on
the other socket when it never was. Without a `backup_socket` nothing of this is done.

Nothing of the table is ever printed or recorded: the ssh's own output goes nowhere, a refusal names `fleet_hop` and
what failed in general terms, and the only trace is `fleet_hop: true`, the restart count and, with a backup, which
socket is up (`fleet_hop_backup: true` or `false`) and how many times the forward changed (`HopKeeper.status`). The
values ride on the step's and the worker's line (`--fleet-hop`, one JSON argument), as `--home-files` rides.

    <python> -I -B deploy/hpc-federation/fleethop.py probe --fleet-hop JSON [--path /v1/models]

`probe` runs the hop, makes one keyless GET through it and prints the HTTP status alone: a front that refuses a request
without its key answers 401, which shows the path end to end without the key.

This file imports nothing of the core and nothing outside the standard library: it runs alone on a node.
"""

import argparse
import http.client
import json
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any, Callable, List, Mapping, Optional

FLEET_HOP_FAILED = 76  # the step's exit status when the hop did not open before its dispatcher (or worker) started
WAIT_S = 60.0  # how long a start waits for the port to accept, for each socket
SETTLE_S = 1.5  # how long a hop must go on running after its port first accepts: an ssh that cannot bind exits sooner
MIN_INTERVAL_S = 10.0  # the least time between two starts of the hop
MAX_INTERVAL_S = 60.0  # the most the interval grows to for a hop that keeps dying
PROBE_S = 60.0  # with a backup: how often the dispatcher's hop is probed
PROBE_TIMEOUT_S = 5.0  # with a backup: how long a probe waits for an HTTP answer
DEAD_PROBES = 2  # with a backup: dead probes in a row that move the forward to the other socket
POLL_S = 0.25
STOP_S = 5.0  # how long a killed hop has to exit before it is killed outright
HOP_KEYS = ("login", "socket", "key", "known_hosts", "local_port")
OPTIONAL_KEYS = ("backup_socket",)
_LOGIN = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*")


class HopRefused(Exception):
    """The hop could not be used; the message names `fleet_hop` and never a host, a path or a port."""


def _path_problem(value: Any, colon_ok: bool, space_ok: bool) -> Optional[str]:
    """Why `value` is no absolute path of a plain string: no empty, `.` or `..` part, no control character, and, where
    ssh would split it, no colon (the forward's `port:socket` form) or white space (an option's list of files)."""
    if isinstance(value, bool) or not isinstance(value, str) or not value.startswith("/") or len(value) > 4096:
        return "is not an absolute path"
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or (not colon_ok and ":" in value):
        return "holds a character a path of the hop may not"
    if not space_ok and any(c.isspace() for c in value):
        return "holds a character a path of the hop may not"
    if any(part in ("", ".", "..") for part in value.split("/")[1:]):
        return "has an empty, `.` or `..` part"
    return None


def hop_problem(value: Any) -> Optional[str]:
    """Why `value` is not a `fleet_hop` (a table of `HOP_KEYS` and perhaps `backup_socket`, each of its type, none able
    to add an option to the ssh command), or None. The reason names the key and never a value."""
    keys = set(HOP_KEYS)
    if not isinstance(value, Mapping) or not keys <= set(value) <= keys | set(OPTIONAL_KEYS):
        return f"must be a table of {', '.join(HOP_KEYS)} and perhaps {', '.join(OPTIONAL_KEYS)}"
    login = value["login"]
    if not isinstance(login, str) or len(login) > 255 or not _LOGIN.fullmatch(login):
        return "login must be a host name (with perhaps a user@): letters, digits, dot, dash, underscore"
    for key, colon_ok, space_ok in (("socket", False, False), ("key", True, True), ("known_hosts", True, False),
                                    ("backup_socket", False, False)):  # fmt: skip
        if key in value:
            why = _path_problem(value[key], colon_ok, space_ok)
            if why:
                return f"{key} {why}"
    if "backup_socket" in value and value["backup_socket"] == value["socket"]:
        return "backup_socket must be another socket than socket"
    port = value["local_port"]
    if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
        return "local_port must be a whole number from 1024 to 65535"
    return None


def parse_hop(text: Any) -> dict:
    """The table a step's or a worker's line carries (`--fleet-hop`, JSON); `HopRefused` naming `fleet_hop` when it is
    not one."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        raise HopRefused("fleet_hop is not the table the line should carry") from None
    problem = hop_problem(value)
    if problem:
        raise HopRefused(f"fleet_hop {problem}")
    return dict(value)


def sockets_of(hop: Mapping[str, Any]) -> List[str]:
    """The front's sockets of a hop in the order they are tried: the primary, then the backup when there is one."""
    return [hop["socket"], *([hop["backup_socket"]] if "backup_socket" in hop else [])]


def hop_argv(hop: Mapping[str, Any], index: int = 0) -> List[str]:
    """The ssh command of the hop, exactly: no config file, one identity and no agent, a host key that must be known
    and no host-address check, no prompt, a forward that must bind, a keepalive, and no command. `index` 0 forwards to
    the primary socket, 1 to the backup."""
    return ["ssh", "-F", "/dev/null", "-i", hop["key"], "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", f"UserKnownHostsFile={hop['known_hosts']}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes", "-o", "CheckHostIP=no", "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-N",
            "-L", f"127.0.0.1:{hop['local_port']}:{sockets_of(hop)[index]}", hop["login"]]  # fmt: skip


def answers(port: int, timeout: float, path: str = "/v1/models") -> bool:
    """Tell whether a keyless GET through 127.0.0.1:`port` gets an HTTP answer of any status within `timeout`: the front
    behind the forward is alive. No answer (refused, reset, timed out) is dead. No key is ever sent."""
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            connection.request("GET", path)
            connection.getresponse()
        finally:
            connection.close()
    except (OSError, http.client.HTTPException):
        return False
    return True


def kill(child: Any) -> None:
    """End a child: ask it to exit, kill it if it will not, and wait for it."""
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=STOP_S)
        except subprocess.TimeoutExpired:
            child.kill()
    child.wait()


class HopKeeper:
    """The hop, started, kept while it is wanted, and stopped. Use it as a context manager, or call `start` and `stop`
    (which is idempotent). `restarts` counts the times the hop was started again, `switches` (with a backup) the times
    its forward was moved to the other socket after the start."""

    def __init__(
        self,
        hop: Mapping[str, Any],
        *,
        wait_s: float = WAIT_S,
        settle_s: float = SETTLE_S,
        min_interval_s: float = MIN_INTERVAL_S,
        max_interval_s: float = MAX_INTERVAL_S,
        probe_s: float = PROBE_S,
        probe_timeout_s: float = PROBE_TIMEOUT_S,
        dead_probes: int = DEAD_PROBES,
        poll_s: float = POLL_S,
        clock: Callable[[], float] = time.monotonic,
        popen: Callable[..., Any] = subprocess.Popen,
    ):
        self._hop, self._port = dict(hop), int(hop["local_port"])
        self._sockets = sockets_of(hop)
        self._probing = len(self._sockets) > 1  # a backup is what makes the front's answer worth asking for
        self._wait_s, self._settle = wait_s, settle_s
        self._min, self._max, self._poll = min_interval_s, max_interval_s, poll_s
        self._probe_s, self._probe_timeout, self._dead_probes = probe_s, probe_timeout_s, dead_probes
        self._clock, self._popen = clock, popen
        self.restarts = 0
        self.switches = 0
        self._interval = min_interval_s
        self._lock = threading.Lock()  # guards the child, its start time and the stop
        self._child: Any = None
        self._index = 0  # the socket the child forwards to
        self._started_at = 0.0
        self._proven = False  # the child's forward answered a probe
        self._dead = 0  # dead probes in a row
        self._next_probe = 0.0
        self._stopped = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def status(self) -> str:
        """What a step or a worker says of the hop: that there was one, how often it was started again and, with a
        backup, whether the backup is the socket in use and how often the forward changed."""
        if not self._probing:
            return f"fleet_hop: true (restarts: {self.restarts})"
        backup = str(self._index == 1).lower()
        return f"fleet_hop: true (restarts: {self.restarts}, fleet_hop_backup: {backup}, switches: {self.switches})"

    def __enter__(self) -> "HopKeeper":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def _spawn(self, index: int) -> None:
        """Start the hop on a socket (the lock held): its words go nowhere, as an ssh says the host and the paths when
        it fails."""
        self._child = self._popen(hop_argv(self._hop, index), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)  # fmt: skip
        self._index, self._proven, self._dead = index, False, 0
        self._started_at = self._clock()
        self._next_probe = self._started_at + self._settle

    def _listening(self) -> bool:
        """The port accepts and the hop this keeper started is still running: a listener of someone else's, with the
        hop already gone, is no hop."""
        with self._lock:
            if self._child is None or self._child.poll() is not None:
                return False
        try:
            socket.create_connection(("127.0.0.1", self._port), timeout=1.0).close()
        except OSError:
            return False
        with self._lock:
            return self._child is not None and self._child.poll() is None

    def _held(self) -> bool:
        """Tell whether something already accepts connections on the port: another listener holds it, so the hop this
        keeper would start could never bind it, and what a model call sent there would not reach the front."""
        try:
            socket.create_connection(("127.0.0.1", self._port), timeout=1.0).close()
        except OSError:
            return False
        return True

    def start(self) -> None:
        """Start the hop and wait for it: `HopRefused` (the hop stopped) if the port is held by another listener, or if
        no socket's forward comes up within `wait_s`."""
        if self._held():
            raise HopRefused("fleet_hop: its local port is already in use; nothing was started")
        if self._probing:
            self._start_with_backup()
        else:
            self._start_single()

    def _begin(self) -> None:
        self._thread = threading.Thread(target=self._keep, name="fleet-hop", daemon=True)
        self._thread.start()

    def _refuse(self) -> HopRefused:
        return HopRefused(f"fleet_hop: its local port did not open within {self._wait_s:g} s; nothing was started")

    def _start_single(self) -> None:
        """One socket: start it, keep it (the thread restarts it from the first moment) and wait for the port."""
        try:
            with self._lock:
                self._spawn(0)
        except OSError as error:
            raise HopRefused(f"fleet_hop: ssh could not be started ({type(error).__name__})") from None
        self._begin()
        deadline = self._clock() + self._wait_s
        while True:
            if self._listening():
                time.sleep(self._settle)  # an ssh that cannot bind its forward has exited by now
                if self._listening():
                    return
            if self._clock() >= deadline:
                self.stop()
                raise self._refuse()
            time.sleep(self._poll)

    def _start_with_backup(self) -> None:
        """Each socket in turn: start it and take it when its port accepts, its ssh is still running a moment later and
        a keyless GET through it gets an HTTP answer; one that never comes up, or comes up and does not answer, is ended
        and the next is tried. The thread keeps the one that stays."""
        for index in range(len(self._sockets)):
            try:
                with self._lock:
                    self._spawn(index)
            except OSError as error:
                raise HopRefused(f"fleet_hop: ssh could not be started ({type(error).__name__})") from None
            if self._comes_up():
                self._begin()
                return
            with self._lock:
                child, self._child = self._child, None
            kill(child)
        raise self._refuse()

    def _comes_up(self) -> bool:
        """The child just started: its port accepts while it runs, it still runs after the settling time, and the front
        answers through it. An ssh that exits, a port that never accepts within `wait_s` and a tunnel that accepts and
        answers nothing are all dead."""
        deadline = self._clock() + self._wait_s
        while True:
            with self._lock:
                if self._child is None or self._child.poll() is not None:
                    return False
            if self._listening():
                time.sleep(self._settle)
                if not self._listening():
                    return False
                if answers(self._port, self._probe_timeout):
                    with self._lock:
                        self._proven = True
                    return True
                return False
            if self._clock() >= deadline:
                return False
            time.sleep(self._poll)

    def _keep(self) -> None:
        """While the hop is wanted: start it again when it has exited, no sooner than the interval after its last start,
        which doubles (to the maximum) for a hop that dies again quickly and starts over after one that ran that long;
        and, with a backup, probe it and move it to the other socket after enough dead probes."""
        while not self._stopped.wait(self._poll):
            probed = None
            with self._lock:
                if self._stopped.is_set():
                    return
                child = self._child
                if child is None:
                    continue
                if child.poll() is not None:
                    self._restart()
                    continue
                if self._probing and self._clock() >= self._next_probe:
                    probed = child
            if probed is not None:
                alive = answers(self._port, self._probe_timeout)  # outside the lock: a stop must not wait for it
                with self._lock:
                    if self._stopped.is_set() or self._child is not probed:
                        continue
                    self._next_probe = self._clock() + self._probe_s
                    if alive:
                        self._dead, self._proven = 0, True
                        continue
                    self._dead += 1
                    if self._dead >= self._dead_probes:
                        self._move(probed)

    def _restart(self) -> None:
        """The child has exited (the lock held): start it again once the interval since its start has passed, on the
        primary socket if it had been proven alive, and on the other socket if it never was."""
        lived = self._clock() - self._started_at
        if lived >= self._max:
            self._interval = self._min
        if lived < self._interval:
            return
        before = self._index
        index = 0 if (self._proven or not self._probing) else (self._index + 1) % len(self._sockets)
        try:
            self._spawn(index)
        except OSError:
            return  # tried again after the next poll, with the same interval
        self.restarts += 1
        self.switches += index != before
        self._interval = min(self._interval * 2, self._max)

    def _move(self, child: Any) -> None:
        """The forward has not answered `dead_probes` times in a row (the lock held): end it and start it on the other
        socket."""
        before = self._index
        kill(child)
        try:
            self._spawn((before + 1) % len(self._sockets))
        except OSError:
            return
        self.switches += 1

    def stop(self) -> None:
        """Stop keeping the hop and kill it. The keeper cannot start it again once this has taken the lock. The child
        stays on the keeper until it has been waited for, so a stop that is cut short (a signal that lands in the
        middle) is finished by the next call, which finds it still there."""
        with self._lock:
            self._stopped.set()
            child = self._child
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=STOP_S)
        if child is None:
            return
        kill(child)
        with self._lock:
            if self._child is child:
                self._child = None


def probe(args: argparse.Namespace) -> int:
    """`probe`: the hop, one keyless GET through it, the status printed alone; the exit status 0 when a reply came."""
    try:
        hop = parse_hop(args.fleet_hop)
        with HopKeeper(hop, wait_s=WAIT_S):
            connection = http.client.HTTPConnection("127.0.0.1", hop["local_port"], timeout=30)
            connection.request("GET", args.path)
            status = connection.getresponse().status
    except HopRefused as error:
        print(error, file=sys.stderr)
        return FLEET_HOP_FAILED
    except (OSError, http.client.HTTPException) as error:
        print(f"fleet_hop: no reply through the hop ({type(error).__name__})", file=sys.stderr)
        return 1
    print(status)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    found = argparse.ArgumentParser(prog="fleethop.py", description=__doc__.split("\n\n")[0])
    sub = found.add_subparsers(dest="command", required=True)
    one = sub.add_parser("probe")
    one.add_argument("--fleet-hop", dest="fleet_hop", required=True)
    one.add_argument("--path", default="/v1/models")
    return probe(found.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
