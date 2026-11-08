# Provenance: new
"""Archives of record sets: none is made without its replay check, and every one is found by the tests (R08).

Two failures of an earlier study meant a batch went unchecked: sharded archives (`records-1.tar.gz`, `records-2.tar.gz`)
that a test glob did not match, and a run killed before it wrote its replay check. `archive_batch` refuses a batch
whose check is missing or not current, and `archived_records` finds every `records*.tar.gz` under a directory so a test
can put each record in each of them through the same load, write-back and replay as a live batch.
"""

import hashlib
import io
import json
import os
import tarfile
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from expcore.records.schema import SCHEMAS, SchemaVersion
from expcore.replay.check import CHECK_FILE, BatchNotVerified, input_files, require_current_check

ARCHIVE_GLOB = "records*.tar.gz"


def archives_under(root: str | os.PathLike[str]) -> list[Path]:
    """Return every record archive under a directory, shards included, sorted."""
    return sorted(Path(root).rglob(ARCHIVE_GLOB))


def archived_records(root: str | os.PathLike[str]) -> Iterator[tuple[str, bytes]]:
    """Yield (`<archive name>:<member>`, bytes) for every JSON record in every record archive under a directory.

    The check file inside an archive is not a record and is not yielded.
    """
    for archive in archives_under(root):
        with tarfile.open(archive, "r:gz") as tar:
            for member in tar:
                name = Path(member.name)
                if (
                    not member.isfile()
                    or name.suffix != ".json"
                    or member.name == CHECK_FILE
                    or name.name.startswith(".")
                ):
                    continue
                handle = tar.extractfile(member)
                if handle is not None:
                    yield f"{archive.name}:{member.name}", handle.read()


def archive_batch(
    batch: str | os.PathLike[str],
    archive: str | os.PathLike[str],
    *,
    code: Mapping[str, Any],
    allow_dirty: bool = False,
    required_recompute: Sequence[str] = (),
    schemas: Sequence[SchemaVersion] = SCHEMAS,
) -> Path:
    """Archive a batch, its inputs and its check, into a `.tar.gz` file; refuse unless the check is current.

    Raises BatchNotVerified (see `require_current_check`) when the batch has no passing check made under the code in
    use, or lacks a recompute hook in `required_recompute`. Each input is read once, its SHA-256 is compared with the
    one the check holds, and the archive is written from those same bytes, so a record replaced after the check was
    verified is refused, and what is archived is what was checked. The archive is written to a temporary file and
    renamed, so a killed process leaves no archive that looks whole.
    """
    root = Path(batch)
    check = require_current_check(
        root, code=code, allow_dirty=allow_dirty, required_recompute=required_recompute, schemas=schemas
    )
    records, hashed = input_files(root, check.get("hashed_only_patterns") or ())
    names = [*records, *hashed]
    held = check["inputs"]
    if sorted(names) != sorted(held):
        raise BatchNotVerified(f"{root}: the inputs changed between the check and the archive; check the batch again")
    contents: dict[str, bytes] = {}
    for rel in names:
        data = (root / rel).read_bytes()  # read once: these are the bytes that are verified and archived
        if hashlib.sha256(data).hexdigest() != held[rel]:
            raise BatchNotVerified(f"{root}: {rel} changed after the check was verified; check the batch again")
        contents[rel] = data
    check_bytes = (root / CHECK_FILE).read_bytes()
    if json.loads(check_bytes) != check:
        raise BatchNotVerified(f"{root}: {CHECK_FILE} changed while the batch was archived; check the batch again")
    contents[CHECK_FILE] = check_bytes
    target = Path(archive)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    os.close(fd)
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for rel, data in contents.items():
                info = tarfile.TarInfo(rel)
                info.size, info.mode, info.mtime = len(data), 0o644, 0
                tar.addfile(info, io.BytesIO(data))
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target
