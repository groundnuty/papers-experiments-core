"""fleethop.py: the hop from an HPC job to the group's LLM fleet (O93; README.md, "The fleet hop").

The fleet's front is an OpenAI-compatible service listening on a unix socket of a login node. A compute node reaches
it through one ssh tunnel that forwards a port of the job's own loopback to that socket, so a study's `models.toml`
keeps a base URL on 127.0.0.1 and the hop only makes it reachable. A manifest's optional `fleet_hop` (and the fleet's
shard settings, for the elastic worker) is the table

    fleet_hop = { login = "<host>", socket = "<abs path of the front's socket on that login node>",
                  key = "<abs path of the forward-only key>", known_hosts = "<abs path>", local_port = 30051 }

`HopKeeper` runs `ssh -F /dev/null -i KEY ... -N -L 127.0.0.1:PORT:SOCKET LOGIN` as a child of its own (`hop_argv`:
no agent, no config, one key, a host key that must already be known, no prompt, a forward that must bind). `start`
returns when 127.0.0.1:PORT accepts a connection and the child is still alive a moment later, or refuses
(`HopRefused`) after `WAIT_S`; while it is kept, a thread starts the hop again when it exits, no faster than once
per `min_interval_s` (10 s, longer after a hop that keeps dying); `stop` kills it. A step and a worker keep it for
as long as their dispatcher or worker runs and stop it in a `finally` with the stop signals held, as they remove the
operator's key files (`shards.py`, `fleetworker.py`).

Nothing of the table is ever printed or recorded: the ssh's own output goes nowhere, a refusal names `fleet_hop` and
what failed in general terms, and the only trace is `fleet_hop: true` and the restart count (`HopKeeper.status`). The
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
WAIT_S = 60.0  # how long a start waits for the port to accept
SETTLE_S = 1.5  # how long a hop must go on running after its port first accepts: an ssh that cannot bind exits sooner
MIN_INTERVAL_S = 10.0  # the least time between two starts of the hop
MAX_INTERVAL_S = 60.0  # the most the interval grows to for a hop that keeps dying
POLL_S = 0.25
STOP_S = 5.0  # how long a killed hop has to exit before it is killed outright
HOP_KEYS = ("login", "socket", "key", "known_hosts", "local_port")
_LOGIN = re.compile(r"(?:[A-Za-z0-9_][A-Za-z0-9._-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*")


class HopRefused(Exception):
    """The hop could not be used; the message names `fleet_hop` and never a host, a path or a port."""


def _path_problem(value: Any, colon_ok: bool) -> Optional[str]:
    """Why `value` is no absolute path of a plain string: no empty, `.` or `..` part, no control character, and (for
    the socket, which the forward's `port:socket` form would split) no colon."""
    if isinstance(value, bool) or not isinstance(value, str) or not value.startswith("/") or len(value) > 4096:
        return "is not an absolute path"
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or (not colon_ok and ":" in value):
        return "holds a character a path of the hop may not"
    if any(part in ("", ".", "..") for part in value.split("/")[1:]):
        return "has an empty, `.` or `..` part"
    return None


def hop_problem(value: Any) -> Optional[str]:
    """Why `value` is not a `fleet_hop` (a table of exactly `HOP_KEYS`, each of its type, none able to add an option to
    the ssh command), or None. The reason names the key and never a value."""
    if not isinstance(value, Mapping) or set(value) != set(HOP_KEYS):
        return f"must be a table of exactly {', '.join(HOP_KEYS)}"
    login = value["login"]
    if not isinstance(login, str) or len(login) > 255 or not _LOGIN.fullmatch(login):
        return "login must be a host name (with perhaps a user@): letters, digits, dot, dash, underscore"
    for key, colon_ok in (("socket", False), ("key", True), ("known_hosts", True)):
        why = _path_problem(value[key], colon_ok)
        if why:
            return f"{key} {why}"
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


def hop_argv(hop: Mapping[str, Any]) -> List[str]:
    """The ssh command of the hop, exactly: no config file, one identity and no agent, a host key that must be known
    and no host-address check, no prompt, a forward that must bind, a keepalive, and no command."""
    return ["ssh", "-F", "/dev/null", "-i", hop["key"], "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", f"UserKnownHostsFile={hop['known_hosts']}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes", "-o", "CheckHostIP=no", "-o", "BatchMode=yes",
            "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-N",
            "-L", f"127.0.0.1:{hop['local_port']}:{hop['socket']}", hop["login"]]  # fmt: skip


class HopKeeper:
    """The hop, started, kept while it is wanted, and stopped. Use it as a context manager, or call `start` and `stop`
    (which is idempotent). `restarts` counts the times the hop was started again."""

    def __init__(
        self,
        hop: Mapping[str, Any],
        *,
        wait_s: float = WAIT_S,
        settle_s: float = SETTLE_S,
        min_interval_s: float = MIN_INTERVAL_S,
        max_interval_s: float = MAX_INTERVAL_S,
        poll_s: float = POLL_S,
        clock: Callable[[], float] = time.monotonic,
        popen: Callable[..., Any] = subprocess.Popen,
    ):
        self._argv, self._port = hop_argv(hop), int(hop["local_port"])
        self._wait_s, self._settle = wait_s, settle_s
        self._min, self._max, self._poll = min_interval_s, max_interval_s, poll_s
        self._clock, self._popen = clock, popen
        self.restarts = 0
        self._interval = min_interval_s
        self._lock = threading.Lock()  # guards the child, its start time and the stop
        self._child: Any = None
        self._started_at = 0.0
        self._stopped = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def status(self) -> str:
        """What a step or a worker says of the hop: that there was one and how often it was started again."""
        return f"fleet_hop: true (restarts: {self.restarts})"

    def __enter__(self) -> "HopKeeper":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    def _spawn(self) -> None:
        """Start the hop (the lock held): its words go nowhere, as an ssh says the host and the paths when it fails."""
        self._child = self._popen(self._argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)  # fmt: skip
        self._started_at = self._clock()

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

    def start(self) -> None:
        """Start the hop and wait for its port: `HopRefused` (the hop stopped) if it does not accept within `wait_s`."""
        try:
            with self._lock:
                self._spawn()
        except OSError as error:
            raise HopRefused(f"fleet_hop: ssh could not be started ({type(error).__name__})") from None
        self._thread = threading.Thread(target=self._keep, name="fleet-hop", daemon=True)
        self._thread.start()
        deadline = self._clock() + self._wait_s
        while True:
            if self._listening():
                time.sleep(self._settle)  # an ssh that cannot bind its forward (a port held) has exited by now
                if self._listening():
                    return
            if self._clock() >= deadline:
                self.stop()
                raise HopRefused(
                    f"fleet_hop: its local port did not open within {self._wait_s:g} s; nothing was started"
                )
            time.sleep(self._poll)

    def _keep(self) -> None:
        """Start the hop again when it has exited, no sooner than the interval after its last start, which doubles (to
        the maximum) for a hop that dies again quickly and starts over after one that ran that long."""
        while not self._stopped.wait(self._poll):
            with self._lock:
                if self._stopped.is_set():
                    return
                if self._child is None or self._child.poll() is None:
                    continue
                lived = self._clock() - self._started_at
                if lived >= self._max:
                    self._interval = self._min
                if lived < self._interval:
                    continue
                try:
                    self._spawn()
                except OSError:
                    continue  # tried again after the next poll, with the same interval
                self.restarts += 1
                self._interval = min(self._interval * 2, self._max)

    def stop(self) -> None:
        """Stop keeping the hop and kill it. The keeper cannot start it again once this has taken the lock. The child
        stays on the keeper until it has been waited for, so a stop that is cut short (a signal that lands in the
        middle) is finished by the next call, which finds it still there."""
        with self._lock:
            self._stopped.set()
            child = self._child
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=STOP_S)
        if child is None:
            return
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=STOP_S)
            except subprocess.TimeoutExpired:
                child.kill()
        child.wait()
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
