# Provenance: new
"""The link-check record, `link-check.json`: the report of one all-pairs link check, written whole or not at all (O35).

`deploy/compose-federation`'s `up` writes it beside `facts.json` once its link check has run (#221), and so do a
restore's link check and every read of the pairs a heal makes (`fault_serves.ServingProbe.matrix`), so the record on
disk is the verdict of the latest check made, passing or failing, and an operator's pre-check that wants a check under
an hour old finds the heal's own re-check instead of re-running `up`. They all write it through `write_record`, so
there is one format: `record_text`, the report's JSON (`LinkReport.to_json`) with sorted keys and an indent of 2,
and a final newline, the bytes `up` always wrote.

**Atomic.** The text goes to a draft of the writer's own in the record's directory (its name holds the process and a
random part, so two writers never share one), which is flushed and fsynced and then renamed over the record, and the
directory is fsynced so that the rename survives a crash (best effort: the rename is done by then). A reader sees the
old record or the new one, whole. A write that fails leaves the old record as it was and no draft behind, and raises
`OSError`. The record keeps the mode a plain
write gives it (the process's umask), as `up`'s always had: it holds labels, seconds, bytes and transfer ids, no secret.

**The record is advisory.** The federation's link epoch (`fault_epoch`) and the gate's heal journal are what a run's
admission compares (`admission_recheck`); the record is read by no admission, takes no lock, and lives beside
`platform.toml`, not in the fault journal's directory. Writing it never raises or lowers the epoch.
"""

import contextlib
import json
import os
import uuid
from pathlib import Path
from typing import Any

from expcore_onedata.fault_journal import _fsync_directory

__all__ = ["RECORD", "discard_record", "record_text", "write_record"]

RECORD = "link-check.json"  # beside facts.json (under `up`) and platform.toml, wherever that file lies


def record_text(report: Any) -> str:
    """The text of a record: a `linkcheck.LinkReport` as JSON, sorted keys, indent 2, ending with a newline."""
    return json.dumps(report.to_json(), indent=2, sort_keys=True) + "\n"


def write_record(report: Any, path: Path) -> None:
    """Write `report`'s record at `path`, atomically: a draft in the same directory, fsynced, renamed over `path`, the
    directory fsynced (best effort: the rename is done by then). Raises OSError when the record cannot be written; the
    old one is then as it was, with no draft."""
    path = Path(path)
    draft = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)  # the umask's mode, as `open` gives
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(record_text(report))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, path)
    except BaseException:
        with contextlib.suppress(OSError):
            draft.unlink(missing_ok=True)
        raise
    with contextlib.suppress(OSError):  # the record is in place, whole: only its survival of a crash is not assured
        _fsync_directory(path.parent)


def discard_record(path: Path) -> None:
    """Remove the record, so that no older verdict stands as the latest one; a record that is not there is no error.
    Raises OSError when the removal is refused."""
    Path(path).unlink(missing_ok=True)
    with contextlib.suppress(OSError):  # the removal is done; its durability is the directory's
        _fsync_directory(Path(path).parent)
