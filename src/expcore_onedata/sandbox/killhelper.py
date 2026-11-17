# Provenance: new
"""The root half of a signal to the agent's processes: a standalone script, run as `sudo PYTHON -I -c SOURCE TERM|KILL`
(O72).

`procsignal.py` passes this file's source to the interpreter it runs under, so the script imports nothing but the
standard library and nothing of the package. The agent runs as another user, so the harness signals its processes
through the host's privilege. It lists them first (from the container's cgroup, with each one's start tick and the
digest of its command line) and signals later, and in between a process can end and its pid be given to another one:
on the agent's node that could be the data gate or the dispatcher. So the request names each target by pid, start tick
and command line digest, and a target is signalled only while it is still that process.

The request is JSON on stdin, `{"targets": [{"pid": 7, "start": "123", "cmd_sha256": "<hex>" | null}, ...]}`; the answer
is JSON on stdout, `{"results": [{"pid": 7, "outcome": "<word>"}, ...]}`, one outcome for each target, in order. The
exit code is 0 whenever the request was read, 2 when it was not (nothing is signalled then).

    signalled   the signal was sent to the process that was listed
    gone        the process is gone (or only a zombie): there is nothing to signal
    replaced    the pid now holds a process with another start tick: the listed one ended and its pid was reused
    changed     the same start tick, another command line: the listed process is alive and changed (an exec);
                it was not signalled, and the caller lists it again with its new command line
    unreadable  the process's identity cannot be read, now or in the listing: unknown is never a match, so not signalled
    refused     a pid that is no process's own (1 or below, which a process group or init would take) or a request
                that is not well formed: never signalled
    failed      anything else the system said (too many open files, a permission): not signalled

With a pidfd (`pidfd_open(2)`, through `os` where this Python has it, else the raw call) the descriptor is opened first
and held across the checks, and the signal goes through it (`pidfd_send_signal(2)`): a pid reused after the descriptor
was opened shows another start tick or never gets the signal, since the descriptor names the process it was opened for.
A kernel that answers ENOSYS (Linux before 5.3) has none: the checks are then made and `os.kill` sent at once, which
leaves a window of the instant between the last read and the kill that only a pid reused within it can use, and a pid is
reused only after the whole pid space has cycled. Any other failure of `pidfd_open` is `failed`, never worked around.

The harness reads a command line as text (`HostOps.read_text`: universal newlines, undecodable bytes replaced) and this
script as bytes, so `digest_of` puts both in one form first, and the two agree on what an argument the agent chose is.
"""

import ctypes
import errno
import hashlib
import json
import os
import signal
import sys

PIDFD_OPEN, PIDFD_SEND_SIGNAL = 434, 424  # the generic syscall numbers (x86_64 and aarch64 alike)
SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL}

SIGNALLED, GONE, REPLACED, CHANGED = "signalled", "gone", "replaced", "changed"
UNREADABLE, REFUSED, FAILED = "unreadable", "refused", "failed"
DEAD = ("Z", "X", "x")  # a zombie, or dead: signalling it does nothing


def canonical(text):
    """A command line as `HostOps.read_text` returns it: a carriage return ends a line, as text mode reads it."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def digest_of(text):
    """The SHA-256 of a command line, as text, in its canonical form."""
    return hashlib.sha256(canonical(text).encode("utf-8")).hexdigest()


def _syscall(number, *args):
    """A raw system call through the C library, for a Python built without the pidfd calls (uv's standalone builds, made
    against an old glibc); raise OSError with its errno when it fails."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    done = libc.syscall(ctypes.c_long(number), *(ctypes.c_long(arg) for arg in args))
    if done < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return int(done)


def _open_pidfd(pid):
    """pidfd_open(2): `os.pidfd_open` where this Python has it, else the raw call. ProcessLookupError when the process
    is gone, OSError with ENOSYS when the kernel has no such call."""
    opener = getattr(os, "pidfd_open", None)
    return opener(pid) if opener is not None else _syscall(PIDFD_OPEN, pid, 0)


def _send_through(handle, signum):
    """pidfd_send_signal(2): `signal.pidfd_send_signal` where this Python has it, else the raw call."""
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is not None:
        sender(handle, signum)
    else:
        _syscall(PIDFD_SEND_SIGNAL, handle, int(signum), 0, 0)


def _read(pid, name):
    with open(f"/proc/{pid}/{name}", "rb") as stream:
        return stream.read()


def _mismatch(pid, start, digest):
    """None while `pid` is the process the listing saw; else why it is not, as an outcome."""
    try:
        stat = _read(pid, "stat").decode("utf-8", "replace")
        command = _read(pid, "cmdline").decode("utf-8", "replace")
    except (FileNotFoundError, ProcessLookupError):  # ENOENT, or ESRCH in a race: the /proc entry is gone
        return GONE
    except OSError:
        return UNREADABLE
    close = stat.rfind(")")
    fields = stat[close + 2 :].split()
    if close < 0 or len(fields) < 20:
        return UNREADABLE
    if fields[19] != start:
        return REPLACED
    if fields[0] in DEAD:
        return GONE
    if digest_of(command) != digest:
        return CHANGED
    return None


def _well_formed(pid, start):
    return (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 1
        and isinstance(start, str)
        and start.isascii()
        and start.isdigit()
    )


def signal_target(pid, start, digest, signum):
    """Send `signum` to `pid` only while it is the process that started at tick `start` with a command line whose digest
    is `digest`; the outcome (the module's text). Never raises."""
    if not _well_formed(pid, start) or not (digest is None or isinstance(digest, str)):
        return REFUSED
    if digest is None:  # the listing could not read the command line: nothing to check the process against
        return UNREADABLE
    try:
        handle = _open_pidfd(pid)
    except ProcessLookupError:
        return GONE
    except OSError as error:
        if error.errno != errno.ENOSYS:  # too many open files, a permission, ...: never worked around
            return FAILED
        return _kill_if_started(pid, start, digest, signum)
    try:
        mismatch = _mismatch(pid, start, digest)
        if mismatch is not None:
            return mismatch
        try:
            _send_through(handle, signum)
        except ProcessLookupError:
            return GONE
        except OSError:
            return FAILED
        return SIGNALLED
    finally:
        os.close(handle)


def _kill_if_started(pid, start, digest, signum):
    """`signal_target` where the kernel has no pidfd: the identity is read, and only while it matches `os.kill` follows
    at once."""
    mismatch = _mismatch(pid, start, digest)
    if mismatch is not None:
        return mismatch
    try:
        os.kill(pid, signum)
    except ProcessLookupError:
        return GONE
    except OSError:
        return FAILED
    return SIGNALLED


def main(argv, stdin):
    """`SIGNAL` (TERM or KILL) in `argv`, the request on `stdin`; the answer is printed, the exit code returned."""
    if len(argv) != 1 or argv[0] not in SIGNALS:
        print("usage: PYTHON -I -c SOURCE TERM|KILL, the request on stdin", file=sys.stderr)
        return 2
    try:
        targets = json.load(stdin)["targets"]
        if not isinstance(targets, list) or not all(isinstance(target, dict) for target in targets):
            raise ValueError("targets are not a list of objects")
    except (ValueError, KeyError, TypeError) as error:
        print(f"the request cannot be read: {error}", file=sys.stderr)
        return 2
    signum = SIGNALS[argv[0]]
    results = [
        {
            "pid": target.get("pid"),
            "outcome": signal_target(target.get("pid"), target.get("start"), target.get("cmd_sha256"), signum),
        }
        for target in targets
    ]
    print(json.dumps({"results": results}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin))
