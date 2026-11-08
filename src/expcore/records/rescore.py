# Provenance: new
"""Re-scoring: scorers run on saved records, offline, and the record keeps the version of each scorer (R13).

A scorer is a pure function of a record: it reads the record and nothing else, so no model call and no platform access
is needed, and a score computed later equals one computed at the end of the run. The scores sit in the record's `scores`
section under the scorer's name, apart from the instrument outputs. Each entry names the scorer's version, so a later
change to a parser or scorer is visible in the records it re-scored.

A scorer declares `version`. A plain function has none, so its version is the SHA-256 of its source file, which stands
in for a commit when the code is not in git.

An attempt that is set aside (an infrastructure failure or a harness defect) is never scored; re-scoring leaves it as
it was, with no scores.
"""

import copy
import hashlib
import inspect
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from expcore.records.digest import plain
from expcore.records.record import RunRecord, decode
from expcore.records.schema import SCHEMA_PREFIX, RecordError


class Scorer(Protocol):
    """A scorer reads a record and returns its scores as JSON values: a verdict, named numeric outcomes."""

    name: str
    version: str

    def score(self, record: RunRecord) -> Mapping[str, Any]:
        """Score one record. Must not read anything but the record."""
        ...


def scorer_name(scorer: Any) -> str:
    """Return a scorer's name: its `name`, else the name of the function."""
    name = getattr(scorer, "name", None) or getattr(scorer, "__name__", None)
    if not isinstance(name, str) or not name:
        raise RecordError(f"scorer {scorer!r} has no name")
    return name


def scorer_version(scorer: Any) -> str:
    """Return the version a scorer declares, else `sha256:` and the hash of the file its code is in."""
    declared = getattr(scorer, "version", None)
    if isinstance(declared, str) and declared:
        return declared
    target = scorer if inspect.isfunction(scorer) else type(scorer)
    source = inspect.getsourcefile(target)
    if source is None:
        raise RecordError(f"scorer {scorer_name(scorer)!r} declares no version and its source cannot be found")
    return "sha256:" + hashlib.sha256(Path(source).read_bytes()).hexdigest()


def _run(scorer: Any, record: RunRecord) -> Mapping[str, Any]:
    """Run a scorer, given a copy of the record so that it cannot change what is stored."""
    call = scorer.score if hasattr(scorer, "score") else scorer
    return call(copy.deepcopy(record))


def rescore(record: RunRecord, scorers: Iterable[Any]) -> RunRecord:
    """Return a copy of the record with the scorers' results in its `scores`, each with the scorer's version.

    The entries of scorers that did not run are kept; a scorer that did run replaces its earlier entry. A record that is
    set aside is returned as it is.
    """
    if record.set_aside:
        return record
    fresh: dict[str, Any] = {}
    for scorer in scorers:
        name = scorer_name(scorer)
        if name in fresh:
            raise RecordError(f"two scorers are named {name!r}")
        fresh[name] = {"version": scorer_version(scorer), "result": plain(dict(_run(scorer, record)))}
    return replace(record, scores={**record.scores, **fresh})


_RECORD_NAME = re.compile(r"[0-9a-f]{64}\.\d+\.json")


def _load_if_record(path: Path) -> RunRecord | None:
    """Load a file as a run record, or return None when it is not one: a check, a manifest, any JSON without the
    record schema marker. A file named like a record that cannot be read as one is an error, not something to skip."""
    named_like_a_record = bool(_RECORD_NAME.fullmatch(path.name))
    try:
        data = decode(path.read_bytes())
    except RecordError as exc:
        if named_like_a_record:
            raise RecordError(f"{path}: {exc}") from None
        return None
    marker = data.get("schema_version") if isinstance(data, dict) else None
    if not (isinstance(marker, str) and marker.startswith(SCHEMA_PREFIX)):
        if named_like_a_record:
            raise RecordError(f"{path}: named like a record, but it holds no record schema marker")
        return None
    try:
        return RunRecord.from_json(data)
    except RecordError as exc:
        raise RecordError(f"{path}: {exc}") from None


def rescore_files(
    source: str | os.PathLike[str], scorers: Iterable[Any], *, out_dir: str | os.PathLike[str] | None = None
) -> list[Path]:
    """Re-score every run record directly under a directory; write each back, atomically, into `out_dir` or in place.

    Only run records are touched: a file without the record schema marker (the replay check, a manifest) is left alone.
    It happens in two phases. First every record is loaded, validated, scored, and rendered as it will be written, so a
    record that is not valid, a scorer that fails on one, or a result that cannot be written stops the run before any
    file is changed. Then the records are written, each atomically. Returns the paths written.
    """
    scorers = list(scorers)
    target = Path(out_dir) if out_dir is not None else Path(source)
    planned: list[RunRecord] = []
    for path in sorted(Path(source).glob("*.json")):
        record = _load_if_record(path)
        if record is None:
            continue
        scored = rescore(record, scorers)
        scored.dumps()  # validates and redacts exactly as the write will, so a failure comes before the first write
        planned.append(scored)
    names = [r.filename for r in planned]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise RecordError(f"two files hold the same run key and attempt: {repeated}")
    return [r.write(target) for r in planned]
