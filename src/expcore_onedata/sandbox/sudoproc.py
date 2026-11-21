# Provenance: new
"""A survivor's `/proc` read through the host's privilege, as the diagnosis of a cell's deadline reads it (O97).

The agent runs as another user, so on the Docker backend what its survivors wait on is read by the root half
(`procsnap.py`, run as `sudo PYTHON -I -B -c SOURCE`). The diagnosis has a few seconds, and a survivor holds up to
dozens of files, so a sudo call for each would not fit: `SudoProcReader.snapshot` makes one call that reads every listed
process whole, and the reader answers the diagnosis's four reads from it, `listdir`, `read`, `readlink` and `read_at`:
it is a `stuckread.ProcReader`, the same reader an Apptainer node offers over `os`, with names relative to `/proc`
(`1234/task/1234/stat`; `/proc/1234/...` is taken too). Only what the snapshot did not hold costs another call: the
memory at a path argument, `maps` (which can wait on a survivor's memory lock, so the snapshot does not wait on it), and
a file past a cut listing. Those are at most `MISSES` calls.

Everything it cannot show is None, and raises nothing, and a diagnosis reads that as unknown, which never becomes the
agent's: a process that was not listed (the reader is a window on the survivors, never on the host), one that is gone
or was replaced by another process of the same pid (nothing of it was read: the root half checked its start tick as it
opened it), a name off the root half's list, a call that failed or answered wrongly, and a budget that is spent.
"""

from __future__ import annotations

import base64
import binascii
import errno
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox import procsnap
from expcore_onedata.sandbox.hostops import HostOps

PROGRAM = Path(procsnap.__file__).read_text(encoding="utf-8")  # runs as root under this interpreter, `-I -B -c`
MISSES = 6  # calls past the snapshot a diagnosis may make; the next read answers None
COMMAND_S = 10.0  # the most one call may take, whatever the diagnosis has left
NO_TIME = "the diagnosis has no time left"


def _error(why: str, name: str | None = None) -> OSError:
    code = getattr(errno, name, errno.EIO) if name else errno.EIO
    return OSError(code, why)


class SudoProcReader:
    """The reads of a diagnosis over the processes of one listing, from one snapshot made as root."""

    def __init__(
        self,
        host: HostOps,
        targets: Mapping[int, str],
        timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._host, self._targets, self._clock = host, dict(targets), clock
        self._until = clock() + timeout_s
        self._status: dict[int, str] = {}
        self._shot: dict[int, Mapping[str, Any]] = {}
        self._found: dict[tuple[int, str, str], Any] = {}  # (pid, kind, rel) -> the answer of a read past the snapshot
        self._misses = 0
        self._broken: str | None = None

    @classmethod
    def snapshot(
        cls,
        host: HostOps,
        left: Sequence[Mapping[str, Any]],
        timeout_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> SudoProcReader:
        """Read every process of `left` (each with its `pid` and `start`, as the deadline listed it) in one call to the
        root half. A call that fails leaves a reader whose every read raises, never one that is partly filled."""
        reader = cls(host, {int(p["pid"]): str(p["start"]) for p in left}, timeout_s, clock)
        targets = [{"pid": pid, "start": start} for pid, start in reader._targets.items()]
        answer, why = reader._call({"targets": targets, "snapshot": True, "reads": []})
        results = answer.get("results") if answer is not None else None
        sound = (
            isinstance(results, list)
            and len(results) == len(targets)
            and all(
                isinstance(r, dict) and r.get("pid") == t["pid"] and isinstance(r.get("status"), str)
                for r, t in zip(results, targets, strict=True)
            )
        )
        if not sound:
            reader._broken = why or "the root half's answer did not answer for every process listed"
            return reader
        for result in results:
            reader._status[result["pid"]] = result["status"]
            if isinstance(result.get("snapshot"), dict):
                reader._shot[result["pid"]] = result["snapshot"]
        return reader

    # --- the root half ------------------------------------------------------------------------------------------------
    def _call(self, request: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """One call, given the time that is left and at most `COMMAND_S`: its answer, or why there is none."""
        left = self._until - self._clock()
        if left <= 0:
            return None, NO_TIME
        try:
            done = self._host.run(
                [sys.executable, "-I", "-B", "-c", PROGRAM], min(left, COMMAND_S), sudo=True, input=json.dumps(request)
            )
        except Exception as error:  # a host that raises, a timeout: nothing was read
            return None, f"the call raised {type(error).__name__}"
        if done.returncode != 0:
            return None, f"the call exited {done.returncode}"
        try:
            answer = json.loads(done.stdout)
        except ValueError:
            return None, "the call's answer was not JSON"
        return (answer, None) if isinstance(answer, dict) else (None, "the call's answer was not an object")

    # --- what a read needs: who it is for and what the snapshot holds ------------------------------------------
    def _where(self, name: str | Path) -> tuple[int, str]:
        """The pid and the name below its directory that `name` names; `PermissionError` for anything else."""
        text = str(name)
        if text.startswith("/proc/"):
            text = text[len("/proc/") :]
        pid, _, rel = text.partition("/")
        if not (pid.isascii() and pid.isdigit() and int(pid) in self._targets) or not procsnap.ALLOWED.fullmatch(rel):
            raise PermissionError(errno.EACCES, "this reader reads only the listed processes' own /proc entries")
        return int(pid), rel

    def _process(self, pid: int) -> Mapping[str, Any] | None:
        """The snapshot of one process, None when the snapshot holds none (a read is then made past it); raises when
        the reader is broken or the process cannot be read: gone and replaced are ENOENT, anything else EIO."""
        if self._broken is not None:
            raise _error(self._broken)
        status = self._status.get(pid)
        if status in ("gone", "replaced"):
            raise FileNotFoundError(errno.ENOENT, f"the process is {status}")
        if status != "ok":
            raise _error("the process could not be read")
        return self._shot.get(pid)

    def _past(self, pid: int, kind: str, rel: str, **extra: Any) -> dict[str, Any]:
        """One read past the snapshot, a call of its own; the answer's entry, or the error it says."""
        self._misses += 1
        if self._misses > MISSES:
            raise TimeoutError(errno.ETIMEDOUT, "too many reads past the snapshot")
        request = {"pid": pid, "start": self._targets[pid], "kind": kind, "rel": rel, **extra}
        answer, why = self._call({"targets": [], "snapshot": False, "reads": [request]})
        if answer is None:  # this read fails; what the snapshot holds stands
            if why == NO_TIME:
                raise TimeoutError(errno.ETIMEDOUT, NO_TIME)
            raise _error(why or "the call failed")
        reads = answer.get("reads")
        entry = reads[0] if isinstance(reads, list) and len(reads) == 1 and isinstance(reads[0], dict) else None
        if entry is None:
            raise _error("the root half's answer did not answer the read")
        if not entry.get("ok"):
            err = entry.get("err")
            if err in ("gone", "replaced"):
                raise FileNotFoundError(errno.ENOENT, f"the process is {err}")
            if err == "refused":
                raise PermissionError(errno.EACCES, "the read was refused")
            raise _error("the read failed", err if isinstance(err, str) else None)
        return entry

    def _from(self, shot: Mapping[str, Any] | None, section: str, rel: str) -> Any:
        """What the snapshot holds of `rel` in `section` (`text`, `links`, `listings`); raises what it says went wrong
        reading it; None when it holds nothing (it was not asked, or past a cut)."""
        if shot is None:
            return None
        errors = shot.get("errors", {})
        if rel in errors:
            raise _error("the snapshot could not read it", errors[rel] if isinstance(errors[rel], str) else None)
        return shot.get(section, {}).get(rel)

    # --- the four reads of `stuckread.ProcReader`: None where the file cannot be shown, never a raise -----------
    def listdir(self, name: str | Path) -> list[str] | None:
        try:
            pid, rel = self._where(name)
            found = self._from(self._process(pid), "listings", rel)
            return (
                [str(item) for item in found]
                if isinstance(found, list)
                else list(self._cached(pid, "listdir", rel, "list"))
            )
        except OSError:
            return None

    def read(self, name: str | Path, limit: int) -> bytes | None:
        """The file's text as bytes, at most `limit` of them."""
        try:
            pid, rel = self._where(name)
            found = self._from(self._process(pid), "text", rel)
            text = found if isinstance(found, str) else str(self._cached(pid, "read", rel, "text"))
            return text.encode("utf-8")[: max(limit, 0)]
        except OSError:
            return None

    def readlink(self, name: str | Path) -> str | None:
        try:
            pid, rel = self._where(name)
            found = self._from(self._process(pid), "links", rel)
            return found if isinstance(found, str) else str(self._cached(pid, "readlink", rel, "link"))
        except OSError:
            return None

    def read_at(self, name: str | Path, offset: int, size: int) -> bytes | None:
        """`size` bytes of a thread's memory from `offset`: a call of its own, not kept (a blocked call's pointer)."""
        try:
            pid, rel = self._where(name)
            self._process(pid)
            entry = self._past(pid, "read_at", rel, offset=offset, n=size)
            return base64.b64decode(str(entry["b64"]), validate=True)
        except (OSError, KeyError, binascii.Error):
            return None

    def _cached(self, pid: int, kind: str, rel: str, key: str) -> Any:
        """A read past the snapshot, made once and kept."""
        mark = (pid, kind, rel)
        if mark not in self._found:
            self._found[mark] = self._past(pid, kind, rel).get(key)
        value = self._found[mark]
        if value is None:
            raise _error("the root half's answer held no value")
        return value


__all__ = ["COMMAND_S", "MISSES", "PROGRAM", "SudoProcReader"]
