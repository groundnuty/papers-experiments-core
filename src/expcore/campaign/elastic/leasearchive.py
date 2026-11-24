# Provenance: new
"""A lease's archive read as evidence: tied to the SHA-256 its completion names, then the attempt rows of its store.

The worker archives a lease store's `out/` as a plain tar (`worker._archive`, `archives/lease-<id>.out.tar`) and the
completion names that tar's SHA-256, which the ledger verified before it counted the lease (`Ledger.verify_archive`). An
operator may have kept the archive compressed (`.tar.zst`); the ledger still names the hash of the plain tar, so a
compressed archive is hashed as the tar it holds, after decompression. Either form is read in one pass over one open
file, and nothing of it is extracted but the store:

- the file is opened once, never through a symbolic link, and must be a regular file (`fstat` of the open descriptor, so
  the file that is hashed is the file that is read). It is hashed to its end, whatever the tar says of its own length;
- a zstd stream is decompressed by the `zstd` program fed that same descriptor (the standard library has no zstd
  before Python 3.14, and the core imports nothing else); with no such program, or if it fails, the archive is refused;
- of the tar only `out/state.sqlite` and, when the live store left one, `out/state.sqlite-wal` are copied, into a
  private temporary directory, and only as plain files that appear once. No member is ever extracted by its name;
- the store is opened read-only on that copy. The `-wal` goes with it, because a store in WAL mode can hold its newest
  rows there and a copy of the main file alone would not show them (`campaign.storecopy`). The copy is used only when
  the hash matches the completion's, and is removed on the way out.

`read_evidence` returns what the store says of one run's attempts, after judging them (`check_attempts`).
"""

import hashlib
import io
import os
import posixpath
import re
import shutil
import sqlite3
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from expcore.campaign.elastic.records import ReopenRefused

STORE = "out/state.sqlite"
WAL = "out/state.sqlite-wal"
_COPIES = {STORE: "state.sqlite", WAL: "state.sqlite-wal"}  # what of the archive is copied, and under what name
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
MAX_STORE_BYTES = 2 << 30  # a lease store holds one run's attempts: a member past this is not one
CHUNK = 1 << 20
ZSTD_WAIT_S = 60.0
FORMS = ("tar", "tar.zst")
# The exception a harness error's detail starts with is the class name and no more: `Store.finish_attempt` writes
# `<class> <12 hex of the stack>: <message>`. A class that only begins with it (`LedgerErrorX`) is another defect.
_LEDGER_ERROR = re.compile(r"LedgerError(?!\w)")


def refused(number: int, name: str, why: str) -> ReopenRefused:
    """The refusal of condition `number` (named `name`), for the caller to raise."""
    return ReopenRefused(f"condition {number} ({name}): {why}")


@dataclass(frozen=True)
class AttemptRow:
    """One row of the lease store's `attempts`: its id, the attempt number, state, infrastructure kind, whether it was
    charged (0 or 1) and the detail it ended with. The detail is judged and never shown."""

    row_id: int
    attempt: int
    state: str
    infra_kind: str | None
    charged: int
    detail: str | None

    def summary(self) -> dict[str, Any]:
        """The row as the reopening's event keeps it: no detail."""
        return {
            "id": self.row_id,
            "attempt": self.attempt,
            "state": self.state,
            "infra_kind": self.infra_kind,
            "charged": self.charged,
        }


@dataclass(frozen=True)
class ArchiveEvidence:
    """What an archive shows: its hash (as the plain tar's, equal to the completion's), its form (`FORMS`) and the
    attempt rows the store holds for the run, in row order."""

    sha256: str
    form: str
    attempts: tuple[AttemptRow, ...]


class _Hashed(io.RawIOBase):
    """A raw reader that hashes every byte read through it."""

    def __init__(self, source: BinaryIO) -> None:
        super().__init__()
        self._source = source
        self.digest = hashlib.sha256()

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        count = self._source.readinto(buffer) or 0
        if count:
            self.digest.update(memoryview(buffer)[:count])
        return count


def _open_regular(path: Path) -> BinaryIO:
    """The file at `path`, opened once and never through a symbolic link, if it is a regular file."""
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise refused(3, "archive", f"{path} is a symbolic link: an archive is a regular file, never followed")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as error:
        raise refused(3, "archive", f"{path} cannot be opened ({type(error).__name__}: {error.strerror})") from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise refused(3, "archive", f"{path} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _scan(source: BinaryIO, work: Path) -> tuple[str, set[str], str | None]:
    """Read the tar in `source` to its end, copying the store's files into `work`; return the SHA-256 of every byte
    read, the names copied, and what is wrong with the store's members, if anything (a name twice, a member that is
    not a regular file, one too large). That is said only after the hash is judged: a file that is not the lease's
    archive is refused for that, whatever else it holds."""
    hashed = _Hashed(source)
    seen: set[str] = set()
    problem: str | None = None
    try:
        with tarfile.open(fileobj=hashed, mode="r|") as tar:
            for member in tar:
                name = posixpath.normpath(member.name)
                if name not in _COPIES or problem is not None:
                    continue
                if name in seen:
                    problem = f"the archive holds {name} more than once"
                elif not member.isreg():
                    problem = f"{name} in the archive is not a regular file"
                elif member.size > MAX_STORE_BYTES:
                    problem = f"{name} in the archive is {member.size} bytes: not a lease store"
                elif (source_file := tar.extractfile(member)) is None:
                    problem = f"{name} in the archive cannot be read"
                else:
                    with source_file, open(work / _COPIES[name], "xb") as sink:
                        shutil.copyfileobj(source_file, sink, CHUNK)
                    if (work / _COPIES[name]).stat().st_size != member.size:
                        raise refused(3, "archive", f"{name} is cut short: the archive is not whole")
                    seen.add(name)
        while hashed.read(CHUNK):  # the end of the file, past the tar's own end marker: the hash covers all of it
            pass
    except (tarfile.TarError, EOFError) as error:
        raise refused(3, "archive", f"it is not a readable tar ({type(error).__name__}: {error})") from None
    return hashed.digest.hexdigest(), seen, problem


def _scan_zstd(source: BinaryIO, work: Path) -> tuple[str, set[str], str | None]:
    """`_scan` of the plain tar inside a zstd stream: decompressed by the `zstd` program reading `source` itself."""
    program = shutil.which("zstd")
    if program is None:
        raise refused(3, "archive", "it is zstd-compressed and no zstd program is on PATH to decompress it")
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            [program, "-d", "-c", "-q"], stdin=source, stdout=subprocess.PIPE, stderr=errors
        )  # a fixed argument list: no shell, no name of the operator's in it
        try:
            assert process.stdout is not None
            scanned = _scan(process.stdout, work)
            status = process.wait(timeout=ZSTD_WAIT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise refused(3, "archive", f"zstd did not end within {ZSTD_WAIT_S:g} s") from None
        except BaseException:
            process.kill()
            process.wait()
            raise
        finally:
            if process.stdout is not None:
                process.stdout.close()
        if status != 0:
            errors.seek(0)
            said = errors.read(300).decode("utf-8", "replace").strip()
            raise refused(3, "archive", f"zstd could not decompress it (exit {status}): {said}")
    return scanned


def _attempt_rows(store: Path, run_key: str) -> list[AttemptRow]:
    """The attempts the store copy at `store` holds for `run_key`, in row order, read-only."""
    try:
        db = sqlite3.connect(store.as_uri() + "?mode=ro", uri=True)
        try:
            found = db.execute(
                "SELECT id, attempt, state, infra_kind, charged, detail FROM attempts WHERE run_key=? ORDER BY id",
                (run_key,),
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error as error:
        raise refused(4, "attempt rows", f"its {STORE} cannot be read ({type(error).__name__}: {error})") from None
    return [AttemptRow(*row) for row in found]


def check_attempts(rows: list[AttemptRow], run_key: str) -> None:
    """Judge a run's attempt rows in its lease store (condition 4); raise `ReopenRefused` naming what fails. Every
    row must be uncharged, one must be an infrastructure failure of kind `key_rejected`, and the last must be a harness
    error whose detail starts with `LedgerError`."""
    if not rows:
        raise refused(4, "attempt rows", f"the store holds no attempt of run {run_key}")
    charged = [row for row in rows if row.charged != 0]
    if charged:
        shown = ", ".join(
            f"row {row.row_id} (attempt {row.attempt}, {row.state}) charged {row.charged}" for row in charged
        )
        raise refused(4, "attempt rows", f"every attempt must be uncharged, and {shown}")
    if not any(row.state == "infra_fail" and row.infra_kind == "key_rejected" for row in rows):
        raise refused(4, "attempt rows", "no attempt ended infra_fail of kind key_rejected")
    last = rows[-1]
    if last.state != "harness_error":
        raise refused(
            4,
            "attempt rows",
            f"the last attempt (row {last.row_id}, attempt {last.attempt}) is {last.state}, not harness_error",
        )
    if not _LEDGER_ERROR.match(last.detail or ""):
        raise refused(
            4, "attempt rows", f"the last attempt's (row {last.row_id}) detail does not start with LedgerError"
        )


def read_evidence(path: Path, run_key: str, recorded_sha256: str) -> ArchiveEvidence:
    """Read the archive at `path` for the attempts of `run_key`: refused (`ReopenRefused`, conditions 3 and 4) unless
    the path is a regular file, never a link, whose plain tar hashes to `recorded_sha256` (what the lease's completion
    names), holds `out/state.sqlite`, and the store's rows for the run pass `check_attempts`. Nothing is written
    outside a private temporary directory that is gone on return."""
    with _open_regular(Path(path)) as handle:
        compressed = os.pread(handle.fileno(), len(ZSTD_MAGIC), 0) == ZSTD_MAGIC
        with tempfile.TemporaryDirectory(prefix="expcore-reopen-") as scratch:
            work = Path(scratch)
            digest, copied, problem = (_scan_zstd if compressed else _scan)(handle, work)
            if digest != recorded_sha256:
                raise refused(
                    3,
                    "archive",
                    f"its SHA-256 is {digest}, and the lease's completion names {recorded_sha256}: it is not the "
                    "archive the ledger verified"
                    + (" (a zstd archive is hashed as the tar it holds)" if compressed else ""),
                )
            if problem is not None:
                raise refused(4, "attempt rows", problem)
            if STORE not in copied:
                raise refused(4, "attempt rows", f"the archive holds no {STORE}")
            rows = _attempt_rows(work / _COPIES[STORE], run_key)
    check_attempts(rows, run_key)
    return ArchiveEvidence(digest, FORMS[1] if compressed else FORMS[0], tuple(rows))
