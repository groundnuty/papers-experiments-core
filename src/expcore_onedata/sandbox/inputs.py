# Provenance: new
"""Read-only inputs: files a study supplies to the agent, such as scripts it may run but not change (O20).

A `ReadOnlyInput` names a directory on the host and the absolute path the agent sees it at. Before anything exists, the
start checks both (`validate`): the path is plain and absolute, and is not, does not hold and does not lie under another
mount of the agent or another input; the directory holds at least one regular file, and nothing but regular files and
directories, and is not, does not hold and does not lie under a storage a direct mount reads (O40). Once the run's
directory exists, each input is copied into it (`snapshot`): every file is read once, its bytes hashed and written to
the copy, which is the driver's, mode 0755 where the source is executable by anyone and 0644 otherwise, directories
0755. The copy, not the source, is mounted read-only in the agent's container (`readonly`), so the bytes hashed are the
bytes the agent sees, whatever happens to the source during the run.

The hash covers each entry's path relative to the input, whether it is a directory, and for a file whether it is
executable, its size and its SHA-256, in path order (`digest`). The plugin declares its inputs once (`declare`), which
checks them against every path the sandbox reserves and gives each the hash of its content, and puts them in the
campaign fingerprint (`fingerprint`), so a resume whose inputs changed is refused. The platform is built with the
declared inputs and `launch` refuses any other; every start hashes each input again and refuses one whose content is
no longer the declared one, so an input edited mid-campaign never runs under the old fingerprint. The run's metadata
records the hash under `read_only_inputs`, with the path in the agent and the input's files and bytes, beside the other
inputs of the run it records by hash (the images, the TLS mode, the platform file), and `InputsRecord`, an instrument
`launch` fills, puts the same in the run's record itself (`RunRecord.instruments["read_only_inputs"]`).

The copy is given to root and made read-only on the host too (0444 files, 0555 directories, or 0555 for an executable
file), and the collection hashes it again: a copy that no longer has its hash sets the run aside.

Once the agent runs, the start proves the mount read-only from inside it (`probe_source`, `judge`): the agent reads a
file and may execute an executable one, while writing it, renaming, chmod-ing and deleting it, and creating a file or a
directory beside it each fail with EROFS. A read-only mount refuses each of these before any permission is checked, and
no permission the agent could change gives EROFS, so an input that only its modes protect is refused. The agent's own
working directory must stay writable. Then the copy and the source are hashed again: an input that changed between its
hash and its mount refuses the start.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox.api import ReadOnlyInput, StartRefused
from expcore_onedata.sandbox.containers import CA_PATH
from expcore_onedata.sandbox.hostops import describe_error
from expcore_onedata.sandbox.settings import SandboxSettings

PART = re.compile(r"[A-Za-z0-9._-]{1,255}")  # a name of a path in the agent: nothing docker's mount syntax splits on
MAX_TARGET = 1024
# what the agent's other mounts, and docker itself, already take: no input may be, hold or lie under one of them
TAKEN = ("/work", "/tmp", "/dev", "/proc", "/sys", "/etc/hosts", "/etc/hostname", "/etc/resolv.conf")
FILE_MODE, RUNNABLE_MODE, DIR_MODE = 0o644, 0o755, 0o755
PROBE_MARK = "# read-only input probe (O20)"
CHANGES = ("write", "beside", "mkdir", "rename", "chmod", "delete")  # each must fail with EROFS
CHUNK = 1 << 20


class InputUnsafe(ValueError):
    """An input that holds something the sandbox will not copy (a link, a special file); the message names no path."""


@dataclass(frozen=True)
class _Entry:
    rel: str  # the path relative to the input, '/'-separated
    path: Path
    is_dir: bool
    runnable: bool  # a file executable by anyone


def _entries(root: Path) -> list[_Entry]:
    """Every entry under `root`, in path order, by lstat; `InputUnsafe` for anything but a regular file or directory."""
    found: list[_Entry] = []
    pending = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        with os.scandir(directory) as listing:
            for item in listing:
                rel = f"{prefix}/{item.name}" if prefix else item.name
                info = item.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    found.append(_Entry(rel, Path(item.path), True, False))
                    pending.append((Path(item.path), rel))
                elif stat.S_ISREG(info.st_mode):
                    found.append(_Entry(rel, Path(item.path), False, bool(info.st_mode & 0o111)))
                else:
                    raise InputUnsafe("it holds a link or a special file, which the sandbox does not copy")
    return sorted(found, key=lambda entry: os.fsencode(entry.rel))  # a directory comes before what it holds


def _overlap(one: str, other: str) -> bool:
    return one == other or one.startswith(other.rstrip("/") + "/") or other.startswith(one.rstrip("/") + "/")


def taken_paths(settings: SandboxSettings) -> list[str]:
    """Every path in the agent an input may not be, hold or lie under: the agent's own directories and the files docker
    writes, the run's CA, the trust paths of the image and the data mount, in every data path alike."""
    return [*TAKEN, CA_PATH, *settings.trust_paths, *([settings.mount.path] if settings.mount else [])]


def storage_paths(settings: SandboxSettings) -> list[str]:
    """The host directories of a direct mount's storages, resolved (O40): an input's source may not be, hold or lie
    under one, or the agent's container would get a copy of a provider's storage. None without a direct mount."""
    return [str(Path(path).resolve()) for _, path in (settings.mount.storages if settings.mount else ())]


def validate(
    items: Sequence[ReadOnlyInput], taken: Sequence[str], *, storages: Sequence[str] = ()
) -> tuple[ReadOnlyInput, ...]:
    """The inputs with their sources resolved, or `StartRefused` naming the first bad one by its position, never a path.
    `taken` are the paths in the agent that its other mounts take (`taken_paths`), and `storages` the host directories
    of a direct mount's storages (`storage_paths`), which no source may be, hold or lie under (O40). An input with a
    declared `sha256` whose content no longer has it is refused too."""
    checked: list[ReadOnlyInput] = []
    for n, item in enumerate(items):
        where = f"read-only input {n}"
        if any(_overlap(str(Path(item.source).resolve()), storage) for storage in storages):
            raise StartRefused(f"{where}: its source is, holds or lies under a storage the mount reads directly")
        target = item.target if isinstance(item.target, str) else ""
        parts = target.split("/")[1:] if target.startswith("/") else []
        if not parts or len(target) > MAX_TARGET or any(p in (".", "..") or not PART.fullmatch(p) for p in parts):
            raise StartRefused(
                f"{where}: its path in the agent must be absolute, below the root, and made of plain names (letters, "
                "digits, '.', '_' and '-')"
            )
        if any(_overlap(target, other) for other in taken):
            raise StartRefused(f"{where}: its path in the agent is, holds or lies under another mount of the agent")
        if any(_overlap(target, other.target) for other in checked):
            raise StartRefused(f"{where}: its path in the agent is, holds or lies under another input's")
        try:
            source = Path(item.source).resolve(strict=True)
            if not source.is_dir():
                raise StartRefused(f"{where}: its source is not a directory")
            entries = _entries(source)
        except OSError as error:
            raise StartRefused(f"{where}: its source cannot be read ({describe_error(error)})") from None
        except InputUnsafe as error:
            raise StartRefused(f"{where}: {error}") from None
        if all(entry.is_dir for entry in entries):
            raise StartRefused(f"{where}: its source holds no file")
        if item.sha256 is not None and _hash_of(source, where) != item.sha256:
            raise StartRefused(f"{where}: its content is no longer the content it was declared with")
        checked.append(ReadOnlyInput(source, target, item.sha256))
    return tuple(checked)


def _hash_of(source: Path, where: str) -> str:
    try:
        return digest(source)["sha256"]
    except (OSError, InputUnsafe) as error:
        raise StartRefused(f"{where}: its source cannot be read ({describe_error(error)})") from None


def _feed(digest: Any, entry: _Entry, size: int = 0, content: str = "") -> None:
    """Add one entry to the hash of an input: its path, its kind and, for a file, its executable bit, size and hash."""
    if entry.is_dir:
        digest.update(b"d\0" + os.fsencode(entry.rel) + b"\0")
    else:
        flag = b"x" if entry.runnable else b"-"
        digest.update(b"f\0" + os.fsencode(entry.rel) + b"\0" + flag + b"\0%d\0" % size + content.encode() + b"\0")


def _stream(path: Path, sink: Any = None) -> tuple[int, str]:
    """Read a regular file once, never through a link, passing its bytes to `sink`; its size and SHA-256."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)  # a FIFO swapped in does not block
    with os.fdopen(descriptor, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise InputUnsafe("it holds a link or a special file, which the sandbox does not copy")
        content, size = hashlib.sha256(), 0
        while block := handle.read(CHUNK):
            content.update(block)
            size += len(block)
            if sink is not None:
                sink.write(block)
    return size, content.hexdigest()


def digest(root: Path) -> dict[str, Any]:
    """The hash of an input as it is now, with its files and bytes: `{"sha256", "files", "bytes"}`."""
    total, files, size = hashlib.sha256(), 0, 0
    for entry in _entries(root):
        if entry.is_dir:
            _feed(total, entry)
            continue
        length, content = _stream(entry.path)
        _feed(total, entry, length, content)
        files, size = files + 1, size + length
    return {"sha256": total.hexdigest(), "files": files, "bytes": size}


def snapshot(source: Path, copy: Path) -> dict[str, Any]:
    """Copy an input to the new directory `copy`, reading each file once and hashing the bytes it writes; return what
    `digest` returns for them. The copy is the driver's, with the modes of the module's docstring."""
    total, files, size = hashlib.sha256(), 0, 0
    entries = _entries(source)
    os.mkdir(copy, DIR_MODE)
    os.chmod(copy, DIR_MODE)  # whatever the umask took away
    for entry in entries:
        target = copy / entry.rel
        if entry.is_dir:
            os.mkdir(target, DIR_MODE)
            os.chmod(target, DIR_MODE)
            _feed(total, entry)
            continue
        mode = RUNNABLE_MODE if entry.runnable else FILE_MODE
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(descriptor, "wb") as sink:
            length, content = _stream(entry.path, sink)
            os.fchmod(sink.fileno(), mode)
        _feed(total, entry, length, content)
        files, size = files + 1, size + length
    return {"sha256": total.hexdigest(), "files": files, "bytes": size}


def declare(
    settings: SandboxSettings, declared: Mapping[str, Sequence[ReadOnlyInput]]
) -> dict[str, tuple[ReadOnlyInput, ...]]:
    """The plugin's read-only inputs, by the name it declares them under (a condition, or one name for every run),
    each checked against every path the sandbox reserves (`taken_paths`, as a start checks it) and given the hash of
    its content (`sha256`). Build the platform with them (`SandboxPlatform(..., read_only_inputs=...)`), put them in the
    campaign fingerprint (`fingerprint`) and pass them to `launch` as declared. An input that cannot be delivered raises
    `StartRefused`, naming its position, never a path."""
    out: dict[str, tuple[ReadOnlyInput, ...]] = {}
    for name, items in declared.items():
        checked = validate(items, taken_paths(settings), storages=storage_paths(settings))
        out[str(name)] = tuple(
            ReadOnlyInput(item.source, item.target, _hash_of(item.source, f"read-only input {n}"))
            for n, item in enumerate(checked)
        )
    return out


def fingerprint(declared: Mapping[str, Sequence[ReadOnlyInput]]) -> dict[str, Any]:
    """The read-only inputs' part of the campaign fingerprint, for the plugin to add to its platform half:
    `StudyPlugin(platform={**config.fingerprint(), **fingerprint(declared)})`, from the inputs `declare` returned: for
    each name, each input's path in the agent and the hash it was declared with, in order, so a resume whose inputs
    changed is refused (C06)."""
    out: dict[str, list[dict[str, str]]] = {}
    for name, items in declared.items():
        if any(item.sha256 is None for item in items):
            raise ValueError("read-only inputs enter the fingerprint as inputs.declare returns them, with their hash")
        out[str(name)] = [{"path": item.target, "sha256": str(item.sha256)} for item in items]
    return {"read_only_inputs": out}


class InputsRecord:
    """An instrument (`expcore.plugin.Instrument`) that keeps a run's read-only inputs in its record:
    `RunRecord.instruments["read_only_inputs"]`. The executor makes one per run, starts it with its other instruments
    and passes it to `launch(..., inputs_record=...)`, which fills it once the sandbox has started; `finish` gives
    `{"ok": True, "inputs": [{"path", "sha256", "files", "bytes"}, ...]}`, or `{"ok": False, "why": ...}` when no
    sandbox with these inputs started, never a guess (O08)."""

    namespace = "read_only_inputs"

    def __init__(self) -> None:
        self._inputs: list[dict[str, Any]] | None = None

    def start(self, identity: Mapping[str, Any]) -> None:
        self._inputs = None

    def attach(self, recorded: Sequence[Mapping[str, Any]]) -> None:
        """Keep what the started sandbox recorded of its inputs."""
        self._inputs = [dict(item) for item in recorded]

    def finish(self) -> Mapping[str, Any]:
        if self._inputs is None:
            return {"ok": False, "why": "no sandbox with these read-only inputs was started"}
        return {"ok": True, "inputs": self._inputs}


def probe_targets(copy: Path) -> tuple[str, str | None]:
    """The file of an input the probe tries to change (the first in path order) and the first executable one, if any,
    each relative to the input."""
    files = [entry for entry in _entries(copy) if not entry.is_dir]
    return files[0].rel, next((entry.rel for entry in files if entry.runnable), None)


PROBE_BODY = """
said = {}


def attempt(what, act):
    try:
        act()
    except OSError as error:
        said[what] = error.errno
    else:
        said[what] = "done"


def read():
    with open(params["path"], "rb") as handle:
        handle.read(1)


def create(path):
    with open(path, "x"):
        pass


def work():
    create(params["work"])
    os.unlink(params["work"])


attempt("read", read)
said["run"] = os.access(params["runnable"], os.X_OK) if params["runnable"] else None
attempt("write", lambda: open(params["path"], "w").close())
attempt("beside", lambda: create(params["beside"]))
attempt("mkdir", lambda: os.mkdir(params["directory"]))
attempt("rename", lambda: os.rename(params["path"], params["path"] + ".moved"))
attempt("chmod", lambda: os.chmod(params["path"], 0o777))
attempt("delete", lambda: os.unlink(params["path"]))
attempt("work", work)
print(json.dumps(said))
"""


def probe_source(target: str, name: str, runnable: str | None, *, work: str = "/work") -> str:
    """The Python source the agent runs to show an input at `target` read-only: `name` is the file it reads and tries
    to change, `runnable` the executable file it checks it may run (both relative to the input), and `work` the
    directory that must stay writable. It prints one JSON line: an errno or "done" for each attempt."""
    fresh = f".read-only-probe-{uuid.uuid4().hex[:12]}"  # a name no input holds
    params = {
        "path": f"{target}/{name}",
        "runnable": f"{target}/{runnable}" if runnable else None,
        "beside": f"{target}/{fresh}",
        "directory": f"{target}/{fresh}-dir",
        "work": f"{work}/{fresh}",
    }
    return f"{PROBE_MARK}\nimport json\nimport os\n\nparams = json.loads({json.dumps(params)!r})\n{PROBE_BODY}"


def _result(value: Any) -> str:
    if value == "done":
        return "it succeeded"
    if isinstance(value, int) and not isinstance(value, bool):
        return errno.errorcode.get(value, str(value))
    return "no answer"


def judge(stdout: str, runnable: bool) -> str | None:
    """Why a probe's answer does not show the input read-only and usable, or None when it does."""
    try:
        said = json.loads(stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return "the probe gave no answer"
    if not isinstance(said, dict):
        return "the probe gave no answer"
    if said.get("read") != "done":
        return f"the agent cannot read it ({_result(said.get('read'))})"
    if runnable and said.get("run") is not True:
        return "the agent cannot execute its executable file"
    for change in CHANGES:
        if said.get(change) != errno.EROFS:
            return f"{change} was not refused as a read-only file system refuses it ({_result(said.get(change))})"
    if said.get("work") != "done":
        return f"the agent's working directory is not writable ({_result(said.get('work'))})"
    return None


__all__ = [
    "PROBE_MARK",
    "InputUnsafe",
    "InputsRecord",
    "declare",
    "digest",
    "fingerprint",
    "judge",
    "probe_source",
    "probe_targets",
    "snapshot",
    "storage_paths",
    "taken_paths",
    "validate",
]
