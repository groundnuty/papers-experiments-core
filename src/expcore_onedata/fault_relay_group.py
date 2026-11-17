# Provenance: new
"""A verb of the fault relay as a process group of its own, ended whole at its bound (O58).

The relay runs each verb (`hpcfed.py stop|start|status|exec`) as a process that launches an `srun` step and waits for
it. Ended alone at the relay's bound, that process leaves its `srun` child behind, orphaned: the child goes on waiting
on the Slurm controller, which can stall silently for its message timeout (about 200 s on the site), and when the
controller catches up the step is made and launched, so the verb runs after the relay has answered `timed_out`, and the
verb's own cancel path never ran. A late stop could land inside a restore, a late link restart inside a heal's settle.

So each verb runs in a session and process group of its own (`start_new_session`), and at the bound the whole group is
ended: SIGTERM, a short grace, then SIGKILL, so no `srun` client of the verb survives it. The group is signalled while
its leader is still unreaped (a zombie holds the group id), so the id cannot belong to another process by then.

The same goes for a verb that exits before its bound with a child still in its group (hpcfed failing while it ends
its own step, say): the child is ended and the relay is told (`on_stragglers`), so that it opens the watch for the
steps that child may already have asked for.

What leaves the group on purpose stays: a start's service client is launched detached, in a session of its own, and is
recorded in the federation's state file by the verb (`hpcslurm.Runner.spawn`); it is that start, not a straggler.
"""

import contextlib
import os
import signal
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from expcore_onedata.compose import CommandResult

GRACE_S = 5.0  # between the group's SIGTERM and its SIGKILL
_POLL_S = 0.02


def _exited(pid: int) -> bool:
    """Whether the child has ended, with no reaping: a zombie leader still holds its group id."""
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:  # reaped already
        return True


def _signal_group(group: int, signum: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(group, signum)


def end_group(process: "subprocess.Popen[str]", grace_s: float = GRACE_S) -> None:
    """End the process and everything in its group: SIGTERM, wait up to `grace_s` for the leader to go, SIGKILL to the
    whole group, then reap the leader. The leader is reaped only after the last signal, so the group id is its own."""
    group = process.pid  # the group a `start_new_session` child leads
    _signal_group(group, signal.SIGTERM)
    end = time.monotonic() + grace_s
    while time.monotonic() < end and not _exited(group):
        time.sleep(_POLL_S)
    _signal_group(group, signal.SIGKILL)
    process.wait()


def members(group: int) -> list[int]:
    """The live processes of a process group, from /proc: those whose group is `group` and that are no zombie (a zombie
    runs nothing). A group has members only while a process of it runs, and its id is not given to another process
    meanwhile."""
    found: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2 :].split()
        if len(fields) > 2 and fields[0] not in ("Z", "X") and fields[2] == str(group):
            found.append(int(entry))
    return found


def _wait_empty(group: int, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end and members(group):
        time.sleep(_POLL_S)


def end_stragglers(group: int, grace_s: float = GRACE_S) -> bool:
    """End whatever a verb that has returned left in its group: SIGTERM, up to `grace_s` for it to go, SIGKILL; whether
    anything was left. The group is signalled only while a live member holds it, so its id is never another's."""
    if not members(group):
        return False
    _signal_group(group, signal.SIGTERM)
    _wait_empty(group, grace_s)
    if members(group):
        _signal_group(group, signal.SIGKILL)
        _wait_empty(group, 2.0)
    return True


def _release(process: "subprocess.Popen[str]") -> None:
    """Close our ends of the verb's pipes: a descendant that left the group still holds the other ends, and must never
    make the relay wait for their end."""
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError, ValueError):
                pipe.close()


def run_in_group(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    timeout_s: float,
    grace_s: float = GRACE_S,
    on_stragglers: Callable[[], None] | None = None,
) -> CommandResult:
    """Run `argv` in a session of its own with `env` and no stdin; its end as text (output that is not UTF-8 is still an
    answer). At `timeout_s`, or on any other way out, end the whole group (`end_group`) and raise what ended the wait:
    `subprocess.TimeoutExpired` at the bound. A verb that returns with a child left in its group has the child ended
    (`end_stragglers`) and `on_stragglers` called, its answer unchanged."""
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        env=dict(env),
        start_new_session=True,
    )
    try:
        out, err = process.communicate(timeout=timeout_s)
    except BaseException:  # the bound, or anything that interrupts the wait: nothing of the verb is left running
        end_group(process, grace_s)
        _release(process)
        raise
    if end_stragglers(process.pid, grace_s) and on_stragglers is not None:
        on_stragglers()
    return CommandResult(process.returncode, out, err)
