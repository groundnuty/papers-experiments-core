"""The login node's fault relay (M4's `expcore_onedata.fault_relay`), started at the end of `up` and stopped first by
`down` (DESIGN.md, section 7).

A campaign that runs on a compute node cannot launch a Slurm step, so its fault injector writes each service verb as a
request under `<root>/fault-relay/`, and the relay, on the login node, runs it through this driver's argv prefix and
writes the answer back. `up` starts it detached, in an environment of PATH, HOME, LC_ALL (and SLURM_CONF when set)
alone (it refuses a secret-like name), and waits for its heartbeat; `down` stops it before anything else, so no verb
launches a step while the federation is taken down. A core without the relay module starts and stops none, and says
so.

Every Python here runs with `-I` (O58): `python -m` otherwise puts the directory it was launched from first on
`sys.path` and reads every PYTHON* variable, and on the login node's /tmp another user's `inspect.py` was imported by
`stop` in place of the standard library's (F2, 16 Nov 2026). The relay serves the federation's root, its secrets
included, for the federation's whole life, so it also works in that root, a 0700 directory of ours, never in the
directory `up` was launched from: its verbs are its children and inherit it.
"""

import importlib.util
import os
import pwd
import stat
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


def linked(root: Path) -> bool:
    """Whether a symbolic link reaches the root: its own name or any directory above it."""
    return os.path.realpath(root) != os.path.abspath(root)


def private_root(root: Path) -> str:
    """The relay's working directory: the federation's root, an absolute path that no link reaches (neither its own
    name nor any directory above it) to a directory of this user that no one else may enter; RuntimeError otherwise."""
    given = os.path.abspath(root)
    if linked(root):
        raise RuntimeError(
            "the federation's root is reached through a symbolic link (name it by its resolved path): the relay does "
            "not start there"
        )
    seen = os.lstat(given)
    if not stat.S_ISDIR(seen.st_mode) or seen.st_uid != os.getuid() or seen.st_mode & 0o077:
        raise RuntimeError("the federation's root is not a private directory of this user: the relay does not start")
    return given


def relay_argv(python: str, verb: str, root: Path) -> list:
    """`<python> -I -B -m expcore_onedata.fault_relay <verb> --root <root>`: isolated (no launch directory, PYTHON*
    variable or user site on `sys.path`), and no bytecode written."""
    return [python, "-I", "-B", "-m", RELAY, verb, "--root", str(root)]


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
    """Start the relay detached (one per root: it takes a lock) in the root, `record` its client at once (so `down` can
    end it whatever happens next), and wait for its heartbeat; return its client."""
    where = private_root(root)
    serve = [*relay_argv(python, "serve", root), "--", *command]
    client = runner.spawn([ENV, "-i", *relay_environment(environ), *serve], str(log), cwd=where)
    record(client)
    deadline = clock() + ALIVE_TIMEOUT_S
    while True:
        if runner.run(relay_argv(python, "alive", root), ALIVE_TIMEOUT_S).returncode == 0:
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
    return runner.run(relay_argv(python, "stop", root), STOP_TIMEOUT_S).returncode == 0
