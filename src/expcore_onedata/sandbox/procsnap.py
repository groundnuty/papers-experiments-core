# Provenance: new
"""The root half of the Docker diagnosis of a cell's survivors: a standalone script, run as
`sudo PYTHON -I -B -c SOURCE`, that reads a few `/proc` entries of the agent's processes (O97).

`sudoproc.py` passes this file's source to the interpreter it runs under, so the script imports nothing but the standard
library and nothing of the package. The agent runs as another user, so what its survivors wait on (their threads'
state and system call, their fds and the mounts those are on) is read through the host's privilege. A survivor can
be a process that waits in the kernel on a FUSE request: nothing here reads the file it waits on, only the process's
own `/proc` entries, and those that can block (`maps`, `mem`) only when a read names them, last.

The request is JSON on stdin: `{"targets": [{"pid": 7, "start": "123"}, ...], "snapshot": true, "reads": [...]}`. Each
target's `/proc/<pid>` is opened as a directory and its `stat` read through it: the start tick must be the one listed
(`replaced` otherwise; `gone` when there is no such entry; `unreadable` for anything else, which is never taken for
gone), and every later read of it goes through that directory, so a pid that ends and is given to another process
meanwhile (on the agent's node that could be the data gate) is never read. With `snapshot` the answer holds, for each
target that is `ok`, `snapshot`:

    {"text": {"stat": ..., "task/<tid>/stat": ..., "task/<tid>/wchan": ..., "task/<tid>/syscall": ...,
              "task/<live>/mountinfo": ..., "task/<live>/fdinfo/<n>": ...},
     "links": {"task/<live>/root": ..., "task/<live>/cwd": ..., "task/<live>/fd/<n>": ...},
     "listings": {"task": [tids], "task/<live>/fd": [fds]},
     "errors": {name: "ENOENT" | "EACCES" | ...},   # a file the snapshot tried and could not read
     "cut": ["task", "task/<live>/fd"]}              # a listing longer than `TASK_CAP` or `FD_CAP`: the rest is unread

`<live>` is the first thread, by its number, that is not a zombie: the process's mount table, root, working directory
and descriptors are read through that thread's directory, as the diagnosis (`stuckread`) reads them, since a zombie
leader's own entries are empty.

`reads` are single reads past the snapshot, each `{"pid", "start", "kind": "read" | "readlink" | "listdir" |
"read_at", "rel": <name below the process's directory>, "offset", "n"}`, made after the snapshots, with the same
identity check; the answer is one entry each, in order: `{"ok": true, "text" | "link" | "list" | "b64": ...}` or
`{"ok": false, "err": ...}` (`refused` for a name not on the list, a size past `READ_CAP` or a request that is not well
formed; `replaced`, `gone` or `unreadable` for the process; an errno name for a read).

Only these names are read (`ALLOWED`): `stat`, the listing of `task`, and below `task/<tid>/` its `stat`, `wchan`,
`syscall`, `mountinfo`, `maps`, `mem` (at an offset), `root` and `cwd` (links), the listing of `fd`, `fd/<n>` (a link)
and `fdinfo/<n>`. Never a command line, an environment or a file below a link. The exit code is 0 whenever the request
was read, 2 when it was not.
"""

import base64
import errno
import json
import os
import re
import sys

PROC_ROOT = "/proc"  # a test points it at a made-up tree; the program is run with this value
TASK_CAP = 64  # threads of one process whose state is read; the rest of the listing is cut
FD_CAP = 256  # fds of one process whose link and info are read
TEXT_CAP = 262144  # bytes of any one file of the snapshot
READ_CAP = 4096  # bytes of one `read_at` (a path argument is at most 4 KiB)
MAPS_CAP = 1048576  # bytes of `maps`
_TASK = r"task/[0-9]+"
READ = re.compile(rf"stat|{_TASK}/(?:stat|wchan|syscall|mountinfo|maps)|{_TASK}/fdinfo/[0-9]+")
LINK = re.compile(rf"{_TASK}/(?:root|cwd)|{_TASK}/fd/[0-9]+")
LIST = re.compile(rf"task|{_TASK}/fd")
MEMORY = re.compile(rf"{_TASK}/mem")
ALLOWED = re.compile("|".join(p.pattern for p in (READ, LINK, LIST, MEMORY)))
DEAD_END = ("replaced", "gone", "unreadable")


def _text(fd, cap):
    chunks, size = [], 0
    while size < cap:
        chunk = os.read(fd, min(65536, cap - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def _name(error):
    return errno.errorcode.get(error.errno, "EIO") if isinstance(error, OSError) else "EIO"


def _well_formed(target):
    pid, start = target.get("pid"), target.get("start")
    return (
        isinstance(pid, int)
        and not isinstance(pid, bool)
        and pid > 1
        and isinstance(start, str)
        and start.isascii()
        and start.isdigit()
    )


def _open(target):
    """The process's directory, opened, and checked to be the one listed: `(fd, None)`, or `(None, why)`."""
    if not _well_formed(target):
        return None, "refused"
    try:
        handle = os.open(f"{PROC_ROOT}/{target['pid']}", os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return None, "gone"
    except OSError:
        return None, "unreadable"
    try:
        stream = os.open("stat", os.O_RDONLY, dir_fd=handle)
        try:
            stat = _text(stream, 65536)
        finally:
            os.close(stream)
    except FileNotFoundError:
        os.close(handle)
        return None, "gone"
    except OSError:
        os.close(handle)
        return None, "unreadable"
    fields = stat[stat.rfind(")") + 2 :].split()
    if stat.rfind(")") < 0 or len(fields) < 20:
        os.close(handle)
        return None, "unreadable"
    if fields[19] != target["start"]:
        os.close(handle)
        return None, "replaced"
    return handle, None


def _list(handle, rel):
    stream = os.open(rel, os.O_RDONLY | os.O_DIRECTORY, dir_fd=handle)
    try:
        return os.listdir(stream)
    finally:
        os.close(stream)


def _file(handle, rel, cap):
    stream = os.open(rel, os.O_RDONLY, dir_fd=handle)
    try:
        return _text(stream, cap)
    finally:
        os.close(stream)


def _state(stat):
    """The state field of a `stat` text; None when it cannot be read."""
    fields = stat[stat.rfind(")") + 2 :].split()
    return fields[0] if stat.rfind(")") >= 0 and fields else None


def _snapshot(handle):
    text, links, listings, errors, cut = {}, {}, {}, {}, []

    def take(rel):
        try:
            text[rel] = _file(handle, rel, TEXT_CAP)
        except OSError as error:
            errors[rel] = _name(error)

    def link(rel):
        try:
            links[rel] = os.readlink(rel, dir_fd=handle)
        except OSError as error:
            errors[rel] = _name(error)

    def listing(rel, cap):
        try:
            found = sorted(_list(handle, rel), key=int)
        except (OSError, ValueError) as error:
            errors[rel] = _name(error)
            return []
        listings[rel] = found
        if len(found) > cap:
            cut.append(rel)
        return found[:cap]

    take("stat")
    live = None
    for tid in listing("task", TASK_CAP):
        for leaf in ("stat", "wchan", "syscall"):
            take(f"task/{tid}/{leaf}")
        if live is None and _state(text.get(f"task/{tid}/stat", "")) not in (None, "Z", "X", "x"):
            live = f"task/{tid}"
    if live is not None:  # read as the diagnosis reads it: through the first thread that is not a zombie
        take(f"{live}/mountinfo")
        link(f"{live}/root")
        link(f"{live}/cwd")
        for number in listing(f"{live}/fd", FD_CAP):
            link(f"{live}/fd/{number}")
            take(f"{live}/fdinfo/{number}")
    return {"text": text, "links": links, "listings": listings, "errors": errors, "cut": cut}


def _read_one(handle, request):
    kind, rel = request.get("kind"), request.get("rel")
    if not isinstance(rel, str):
        return {"ok": False, "err": "refused"}
    try:
        if kind == "read" and READ.fullmatch(rel):
            return {"ok": True, "text": _file(handle, rel, MAPS_CAP if rel.endswith("/maps") else TEXT_CAP)}
        if kind == "readlink" and LINK.fullmatch(rel):
            return {"ok": True, "link": os.readlink(rel, dir_fd=handle)}
        if kind == "listdir" and LIST.fullmatch(rel):
            return {"ok": True, "list": _list(handle, rel)}
        if kind == "read_at" and MEMORY.fullmatch(rel):
            offset, size = request.get("offset"), request.get("n")
            ints = all(isinstance(v, int) and not isinstance(v, bool) for v in (offset, size))
            if not ints or offset < 0 or not 0 < size <= READ_CAP:
                return {"ok": False, "err": "refused"}
            stream = os.open(rel, os.O_RDONLY, dir_fd=handle)
            try:
                return {"ok": True, "b64": base64.b64encode(os.pread(stream, size, offset)).decode("ascii")}
            finally:
                os.close(stream)
    except OSError as error:
        return {"ok": False, "err": _name(error)}
    return {"ok": False, "err": "refused"}


def main(argv, stdin, stdout=None, stderr=None):
    """The request on `stdin`; the answer is written to `stdout`, the exit code returned."""
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr
    try:
        request = json.load(stdin)
        targets, reads = request["targets"], request.get("reads", [])
        listed = isinstance(targets, list) and all(isinstance(t, dict) for t in targets)
        if not listed or not isinstance(reads, list) or not all(isinstance(r, dict) for r in reads):
            raise ValueError("targets and reads are lists of objects")
        if not all(_well_formed(t) for t in targets):
            raise ValueError("a target is not a pid and a start tick")
    except (ValueError, KeyError, TypeError) as error:
        print(f"the request cannot be read: {error}", file=stderr)
        return 2
    results = []
    for target in targets:
        handle, why = _open(target)
        if handle is None:
            results.append({"pid": target["pid"], "status": why})
            continue
        try:
            entry = {"pid": target["pid"], "status": "ok"}
            if request.get("snapshot"):
                entry["snapshot"] = _snapshot(handle)
        finally:
            os.close(handle)
        results.append(entry)
    answers = []
    for request_one in reads:  # after every snapshot: `maps` and `mem` are the reads that can wait
        handle, why = _open(request_one)
        if handle is None:
            answers.append({"ok": False, "err": "refused" if why == "refused" else why})
            continue
        try:
            answers.append(_read_one(handle, request_one))
        finally:
            os.close(handle)
    print(json.dumps({"results": results, "reads": answers}), file=stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin))
