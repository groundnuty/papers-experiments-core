# Provenance: new
"""Signalling the agent's processes without signalling a process that is not the one listed (O72).

The harness lists the agent container's processes from its cgroup and signals them later through the host's privilege
(the agent runs as another user), so a pid that ends and is given to another process between the two could be the data
gate or the dispatcher on the agent's node. A listing therefore carries, with each process's pid and start tick, the
digest of its command line (`cmd_digest`, key `cmd_sha256`), and `signal_processes` hands both to the root half
(`killhelper.py`, run as `sudo PYTHON -I -B -c SOURCE TERM|KILL`, with a pidfd where the kernel has one), which signals
a pid only while it is still that process. It answers for each target (`Signalled`): a process it signalled, found gone
or found replaced by another has been reached; one it did not signal, because its command line changed (an exec), its
identity could not be read or the signal could not be sent, has not, and the caller lists it again and never reads it
as gone or as quiet.

A process whose command line the listing could not read has `cmd_sha256` None, which the root half refuses: no
identity, no signal. A command that fails as a whole (sudo refusing it, the interpreter missing) has answered for
nobody (`failed`)."""

from __future__ import annotations

import dataclasses
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox import killhelper
from expcore_onedata.sandbox.hostops import HostOps

PROGRAM = Path(killhelper.__file__).read_text(encoding="utf-8")  # runs as root under this interpreter, `-I -B -c`
KEY = "cmd_sha256"  # the digest of a listed process's command line, in the listing's dicts
digest_of = killhelper.digest_of
REACHED = (killhelper.SIGNALLED, killhelper.GONE, killhelper.REPLACED)  # the listed process needs no further signal
CHANGED, UNREADABLE = killhelper.CHANGED, killhelper.UNREADABLE


def cmd_digest(host: HostOps, pid: int) -> str | None:
    """The digest of a process's command line as the host reads it (/proc/PID/cmdline); None when it cannot be read,
    which is unknown and never an empty command line."""
    try:
        return digest_of(host.read_text(f"/proc/{pid}/cmdline"))
    except OSError:
        return None


@dataclasses.dataclass(frozen=True)
class Signalled:
    """What a signal did to each target, by pid (`outcomes`, the root half's words). `failed` says why the command
    gave no answer at all, when it did not: nothing is then known to have been signalled."""

    outcomes: Mapping[int, str]
    failed: str | None = None

    def reached(self, proc: Mapping[str, Any]) -> bool:
        """Whether the process needs no further signal: signalled, gone, or its pid taken by another process."""
        return self.outcomes.get(proc["pid"]) in REACHED

    def declined(self, procs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """The processes among `procs` that were not reached, and so may still be running."""
        return [proc for proc in procs if not self.reached(proc)]


def _answer(text: str, asked: Sequence[int]) -> dict[int, str] | None:
    """The outcomes in the root half's answer, by pid, when it is the answer to this request: one entry for each pid
    asked, in the order asked, each with that integer pid (never one coerced from another type) and a text outcome.
    Anything else is not an answer, and reaches nobody: a pid read as another one's could mark a process done."""
    try:
        results = json.loads(text)["results"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(results, list) or len(results) != len(asked):
        return None
    outcomes: dict[int, str] = {}
    for entry, pid in zip(results, asked, strict=True):
        if not isinstance(entry, dict):
            return None
        got, outcome = entry.get("pid"), entry.get("outcome")
        if isinstance(got, bool) or not isinstance(got, int) or got != pid or not isinstance(outcome, str):
            return None
        outcomes[pid] = outcome
    return outcomes


def signal_processes(host: HostOps, procs: Sequence[Mapping[str, Any]], name: str, timeout_s: float) -> Signalled:
    """Send SIGTERM or SIGKILL (`name`, "TERM" or "KILL") to each listed process, as the root half does: only while it
    is the process the listing saw. A host that raises raises here; a command that fails, or whose answer is not one,
    has reached none of them."""
    if not procs:
        return Signalled({})
    targets = [{"pid": p["pid"], "start": p.get("start"), "cmd_sha256": p.get(KEY)} for p in procs]
    done = host.run(
        [sys.executable, "-I", "-B", "-c", PROGRAM, name], timeout_s, sudo=True, input=json.dumps({"targets": targets})
    )
    if done.returncode != 0:
        return Signalled({}, failed=f"exit {done.returncode}: {done.stderr.strip()[:200]}")
    outcomes = _answer(done.stdout, [target["pid"] for target in targets])
    if outcomes is None:
        return Signalled({}, failed="the answer was not read")
    return Signalled(outcomes)
