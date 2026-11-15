# Provenance: new
"""Placing a declared side file beside its record, safely, and settling what a kill leaves of it (#248, R15).

A study's executor takes a declared side file (`replay.side_files`) out of the sandbox's `/work` and puts it into the
attempt directory `request.run_dir`, where the record that names its SHA-256 is written. Two calls do it:

    staged = stage_side_file(sandbox, request, study.side_files, "extent_ranges.json.gz")
    # ... put staged.sha256 (None: the run made no side file) in the record's `hash_field`, write the record ...
    staged.commit()

`stage_side_file` collects the file through `sandbox.collect(dest, outputs=[...])`, so the copy keeps the guarantees of
`expcore_onedata.sandbox.copyout` (O19), into a scratch directory outside the batch; refuses a name no declaration
holds and a file over `MAX_SIDE_FILE_BYTES` before anything enters `run_dir`; and writes the file there under the
temporary name `.<pattern>.tmp-<attempt>` (`temporary_name`), fsynced, hashing it as it copies. `commit` renames it to
the declared name once the record that names it is on disk, and refuses to before that.

The order is what makes a kill recoverable: the temporary file, then the record with its hash, then the rename. Each
state a kill can leave is one `check` settles (`settle_side_files`, which it runs before it parses the batch), and
none is one it refuses for a reason the study did not cause:

- Killed after the temporary file, before the record: a dot-name is never read by the check, and no record names the
  file, so `check` removes it, once it holds the campaign's lock (no attempt can be staging a file of its own, whose
  record is not written yet and whose partial hash is not a record's). A check made while the campaign runs leaves it.
- Killed after the record, before the rename: the record names a hash and the file is still under its temporary name.
  `check` renames it, when the directory holds exactly one record, that record names the file's SHA-256 at the declared
  field, and nothing lies at the declared name; the pairing of the check then hashes and verifies it like any other.
  That needs no lock: the hash is the record's, and the record is written only once the file is complete.
- Killed after the rename: complete, nothing to settle.

A temporary file no record in its directory names is removed (when the lock is held). A file is never put in place on
a guess: a hash that is not the record's, a directory of several records that one of them names, or a file already at
the declared name (which the pairing refuses as changed, and which `commit` never overwrites either) leave it as it
is, and the check's pairing still verifies every file that is in place against the record. A check of a running
campaign is a snapshot of a batch that is changing: an attempt it finds between its record and its rename is
settled when the check lists it, and one that reaches that point after is the next check's to settle.
"""

import hashlib
import os
import re
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from expcore.records.record import RecordError, decode, record_filename
from expcore.replay import side_files as side_module
from expcore.replay.check import is_record_name
from expcore.replay.side_files import SideFile, SideFileTooLarge, named_hash, too_large

FINISHED = "finished"  # a settled rename: the temporary file was put in place
REMOVED = "removed"  # a settled temporary file that nothing needed
COPY_CHUNK_BYTES = 1024 * 1024
_TEMPORARY_ATTEMPT = re.compile(r"[0-9]+")


class UndeclaredSideFile(ValueError):
    """A name that no `[[study.side_files]]` declaration holds was asked for."""


class PlacementError(RuntimeError):
    """The record, or the file, is not as the order of a placement needs it: commit before the record, a lost file."""


class Collector(Protocol):
    """What the helper needs of a sandbox: `DockerSandbox.collect`, which copies the named files of /work."""

    def collect(self, dest_dir: Path, outputs: Sequence[str] = ()) -> Any: ...


class _Identity(Protocol):
    @property
    def run_key(self) -> str: ...

    @property
    def attempt(self) -> int: ...


class SideFileRequest(Protocol):
    """What the helper needs of the run's request (`expcore.campaign.executor.RunRequest`): where the record is
    written, and which attempt this is."""

    @property
    def run_dir(self) -> Path: ...

    @property
    def identity(self) -> _Identity: ...


def temporary_name(pattern: str, attempt: int) -> str:
    """The name a side file has until its record is written: a dot-name, which the check never reads."""
    return f".{pattern}.tmp-{attempt}"


@dataclass(frozen=True)
class StagedSideFile:
    """A side file written under its temporary name, waiting for its record: `sha256` goes into the record's field."""

    declaration: SideFile
    sha256: str
    size: int
    run_dir: Path
    record: Path  # the file of the record that must name `sha256` before the rename
    temporary: Path

    @property
    def path(self) -> Path:
        """Where the file lies once it is committed: the declared name, in the attempt directory."""
        return self.run_dir / self.declaration.pattern

    def commit(self) -> Path:
        """Rename the file to its declared name, once the record on disk names its SHA-256; return its path.

        Refuses, renaming nothing, when the record is not there yet or names another hash, and never overwrites a file
        that lies at the declared name with other bytes. A `check` made meanwhile may have finished the rename
        already; the file in place with the hash is then the result.
        """
        field = self.declaration.hash_field
        if not self.record.is_file():
            raise PlacementError(
                f"no record {self.record.name} lies in the attempt directory yet: write the record that names "
                f"{self.declaration.pattern} at {field} first, then commit"
            )
        named = _named_in(self.record, field)
        if named != self.sha256:
            raise PlacementError(
                f"the record names {named!r} at {field}, not the SHA-256 of {self.declaration.pattern} staged "
                f"({self.sha256}): put staged.sha256 in the record before the rename"
            )
        if os.path.lexists(self.path):  # nothing is overwritten: a file in place is for the check's pairing to judge
            if not _holds(self.path, self.sha256):
                raise PlacementError(
                    f"{self.declaration.pattern} already lies in the attempt directory, holding other bytes than "
                    f"the SHA-256 {self.sha256} the record names: it is not overwritten"
                )
            self.temporary.unlink(missing_ok=True)  # a check finished the rename already: this is the same file
            return self.path
        try:
            os.replace(self.temporary, self.path)
        except FileNotFoundError:  # a check finished the rename between the test above and this call
            if not _holds(self.path, self.sha256):
                raise PlacementError(
                    f"{self.declaration.pattern} is lost: neither its temporary file nor a file of the declared name "
                    f"with the SHA-256 {self.sha256} lies in the attempt directory"
                ) from None
        else:
            _fsync_directory(self.run_dir)
        return self.path

    def discard(self) -> None:
        """Remove the staged file, for an attempt that fails before it writes its record; a second call does nothing."""
        self.temporary.unlink(missing_ok=True)


def stage_side_file(
    sandbox: Collector,
    request: SideFileRequest,
    side_files: Sequence[SideFile],
    name: str,
    *,
    source: str | None = None,
) -> StagedSideFile | None:
    """Collect the declared side file `name` from the sandbox's /work and stage it beside the record, under a dot-name.

    `name` is a declared `pattern`; `source` is where the file is in /work when that is not `name` (a relative path
    inside /work). The sandbox must not have been stopped. Returns None when the sandbox copied nothing: the run made no
    side file (the file is absent, or the agent made it a link or the like, which the collection refuses and records
    under `refused_output` for the scorers), so the record then holds no hash for it. Raises `UndeclaredSideFile` for a
    name no declaration holds and `SideFileTooLarge` for a file over `MAX_SIDE_FILE_BYTES`, before anything is written
    into the attempt directory.
    """
    declaration = _declared(side_files, name)
    in_work = _work_path(name if source is None else source)
    run_dir, identity = Path(request.run_dir), request.identity
    with tempfile.TemporaryDirectory(prefix="side-file-") as scratch:
        sandbox.collect(Path(scratch), outputs=[in_work])
        copy = Path(scratch) / "work" / in_work
        if not os.path.lexists(copy):
            return None
        record = run_dir / record_filename(identity.run_key, identity.attempt)
        return _stage_copy(copy, run_dir, declaration, identity.attempt, record)


def _declared(side_files: Sequence[SideFile], name: str) -> SideFile:
    for declaration in side_files:
        if declaration.pattern == name:
            return declaration
    held = ", ".join(repr(d.pattern) for d in side_files) or "none"
    raise UndeclaredSideFile(f"study.toml declares no side file named {name!r} (it declares: {held})")


def _work_path(path: str) -> str:
    """A path inside /work, as `collect` takes it: relative, with no `..`, and naming a file."""
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or ".." in parts:
        raise ValueError(f"a side file is collected from a relative path inside the working directory, not {path!r}")
    return path


def _size_of(descriptor: int) -> int:
    return os.fstat(descriptor).st_size


def _stage_copy(copy: Path, run_dir: Path, declaration: SideFile, attempt: int, record: Path) -> StagedSideFile:
    """Copy the collected file into the attempt directory under its temporary name, hashing it as it goes."""
    try:
        descriptor = os.open(copy, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise PlacementError(
            f"the collected copy of {declaration.pattern} cannot be opened as a plain file ({error.strerror})"
        ) from None
    with os.fdopen(descriptor, "rb") as origin:
        if not stat.S_ISREG(os.fstat(origin.fileno()).st_mode):
            raise PlacementError(f"the collected copy of {declaration.pattern} is not a regular file")
        limit = side_module.MAX_SIDE_FILE_BYTES  # read where used, as the check and the analysis read it
        if _size_of(origin.fileno()) > limit:
            raise SideFileTooLarge(too_large())
        run_dir.mkdir(parents=True, exist_ok=True)
        temporary = run_dir / temporary_name(declaration.pattern, attempt)
        digest, size = hashlib.sha256(), 0
        try:
            with open(temporary, "wb") as target:
                while chunk := origin.read(COPY_CHUNK_BYTES):
                    size += len(chunk)
                    if size > limit:
                        raise SideFileTooLarge(too_large())
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            _fsync_directory(run_dir)  # the name too, so that a record written after it never outlives it
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return StagedSideFile(declaration, digest.hexdigest(), size, run_dir, record, temporary)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(COPY_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _is_regular(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _holds(path: Path, sha256: str) -> bool:
    """Whether a regular file (never a link) lies at the path with this SHA-256."""
    return _is_regular(path) and _file_sha256(path) == sha256


def _named_in(record: Path, field: str) -> Any:
    """What a record file holds at a dotted field; None for a record that does not load or has no such field."""
    try:
        return named_hash(decode(record.read_bytes()), field)
    except (RecordError, OSError):
        return None


@dataclass(frozen=True)
class Settled:
    """What `settle_side_files` did to one temporary file: its path in the batch, `action` and why."""

    path: str
    action: str
    why: str

    def __str__(self) -> str:
        return f"{self.action} {self.path}: {self.why}"


def settle_side_files(
    batch: str | os.PathLike[str], side_files: Sequence[SideFile], *, remove_unnamed: bool = False
) -> list[Settled]:
    """Finish or remove the temporary files of declared side files that a kill left in a batch; return what was done.

    A temporary file (`temporary_name`, any attempt number) whose directory holds exactly one record, which names its
    SHA-256 at the declaration's field, is renamed to the declared name unless something lies there already: a file
    with the same hash makes the temporary copy redundant and it is removed, any other is left for the check's pairing
    to refuse. That is safe whenever it is done: the hash is the record's, which is written only after the file is
    complete. A temporary file no record in its directory names is removed only with `remove_unnamed`, which the caller
    sets when no attempt can be writing in the batch (it holds the campaign's lock): an attempt that is staging has a
    file that no record names yet, and its partial hash is not a record's. A file in a directory of several records
    that one of them names, and anything that is not a regular file, is left alone. Nothing outside the declared
    temporary names is touched, and a second call finds nothing to do.
    """
    settled: list[Settled] = []
    if not side_files:
        return settled
    root = Path(batch)
    for directory, subdirectories, names in os.walk(root):
        subdirectories.sort()
        for name in sorted(names):
            declaration = _declaration_of(name, side_files)
            if declaration is not None:
                done = _settle_one(Path(directory), name, declaration, remove_unnamed)
                if done is not None:
                    settled.append(Settled(Path(directory, name).relative_to(root).as_posix(), *done))
    return settled


def _declaration_of(name: str, side_files: Sequence[SideFile]) -> SideFile | None:
    """The declaration a temporary file name belongs to: `.<pattern>.tmp-<digits>`, or None."""
    for declaration in side_files:
        prefix = f".{declaration.pattern}.tmp-"
        if name.startswith(prefix) and _TEMPORARY_ATTEMPT.fullmatch(name[len(prefix) :]):
            return declaration
    return None


def _settle_one(directory: Path, name: str, declaration: SideFile, remove_unnamed: bool) -> tuple[str, str] | None:
    """Settle one temporary file; return (action, why), or None when it is left as it is."""
    temporary = directory / name
    try:
        if not _is_regular(temporary):
            return None
        sha256 = _file_sha256(temporary)
        records = [p for p in directory.iterdir() if is_record_name(p.name) and _is_regular(p)]
        names_it = [p for p in records if _named_in(p, declaration.hash_field) == sha256]
        if names_it:
            return _finish(temporary, directory / declaration.pattern, sha256) if len(records) == 1 else None
        if not remove_unnamed:
            return None
        temporary.unlink()
        return REMOVED, "no record beside it names its SHA-256" if records else "no record lies beside it to name it"
    except FileNotFoundError:
        return None  # the attempt's own commit, or another check, settled it meanwhile


def _finish(temporary: Path, final: Path, sha256: str) -> tuple[str, str] | None:
    """Put the file a record names into place, or drop it when the same file is there already; else leave it."""
    if not os.path.lexists(final):
        os.replace(temporary, final)
        _fsync_directory(final.parent)
        return FINISHED, f"the record beside it names its SHA-256, so it is renamed to {final.name}"
    if _holds(final, sha256):
        temporary.unlink()
        return REMOVED, f"{final.name} lies in place with the same SHA-256"
    return None  # another file is in place: the check's pairing refuses it, and nothing here overwrites it
