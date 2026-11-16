"""The login node's fault relay (M4's `expcore_onedata.fault_relay`), started at the end of `up` and stopped first by
`down` (DESIGN.md, section 7).

A campaign that runs on a compute node cannot launch a Slurm step, so its fault injector writes each service verb as a
request under `<root>/fault-relay/`, and the relay, on the login node, runs it through this driver's argv prefix and
writes the answer back. `up` starts it detached, in an environment of PATH, HOME, LC_ALL (and SLURM_CONF when set)
alone (it refuses a secret-like name), and waits for its heartbeat; `down` stops it before anything else, so no verb
launches a step while the federation is taken down. A core without the relay module starts and stops none, and says
so.
"""

import importlib.util
import os
import pwd
from pathlib import Path
from typing import Callable, Mapping, Sequence

from hpcslurm import Runner

RELAY = "expcore_onedata.fault_relay"
ALIVE_TIMEOUT_S = 30.0  # it beats every 5 s; about 15 s at most to its first heartbeat
STOP_TIMEOUT_S = 120.0
ENV = "/usr/bin/env"


def available() -> bool:
    """Whether this core has the relay (M4's module)."""
    try:
        return importlib.util.find_spec(RELAY) is not None
    except ModuleNotFoundError:
        return False


def relay_environment(environ: Mapping[str, str]) -> list:
    """The relay's whole environment: PATH, HOME, LC_ALL, and SLURM_CONF when the login shell sets it."""
    home = environ.get("HOME") or pwd.getpwuid(os.getuid()).pw_dir
    pairs = {"PATH": environ.get("PATH") or "/usr/bin:/bin", "HOME": home, "LC_ALL": "C"}
    if environ.get("SLURM_CONF"):
        pairs["SLURM_CONF"] = environ["SLURM_CONF"]
    return [f"{key}={value}" for key, value in pairs.items()]


def start(
    runner: Runner,
    python: str,
    root: Path,
    command: Sequence[str],
    log: Path,
    environ: Mapping[str, str],
    *,
    record: Callable[[object], None],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
) -> object:
    """Start the relay detached (one per root: it takes a lock), `record` its client at once (so `down` can end it
    whatever happens next), and wait for its heartbeat; return its client."""
    serve = [python, "-B", "-m", RELAY, "serve", "--root", str(root), "--", *command]
    client = runner.spawn([ENV, "-i", *relay_environment(environ), *serve], str(log))
    record(client)
    deadline = clock() + ALIVE_TIMEOUT_S
    while True:
        if runner.run([python, "-B", "-m", RELAY, "alive", "--root", str(root)], ALIVE_TIMEOUT_S).returncode == 0:
            return client
        if clock() >= deadline:
            ended = runner.end_client(client)
            state = "it was ended" if ended else "it could not be ended: down ends it"
            raise RuntimeError(
                f"the fault relay did not beat within {ALIVE_TIMEOUT_S:.0f} s ({state}; its log: {log.name})"
            )
        sleep(2.0)


def stop(runner: Runner, python: str, root: Path) -> bool:
    """Stop the relay of this root; True once no heartbeat beats (or none ever did)."""
    return runner.run([python, "-B", "-m", RELAY, "stop", "--root", str(root)], STOP_TIMEOUT_S).returncode == 0
