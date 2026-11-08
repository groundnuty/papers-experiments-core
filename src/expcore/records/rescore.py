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
from collections.abc import Iterable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

from expcore.records.digest import plain
from expcore.records.record import RunRecord, load_record
from expcore.records.schema import RecordError


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


def rescore_files(
    source: str | os.PathLike[str], scorers: Iterable[Any], *, out_dir: str | os.PathLike[str] | None = None
) -> list[Path]:
    """Re-score every record file directly under a directory; write each back, atomically, into `out_dir` or in place.

    Each record is validated on load. Returns the paths written.
    """
    scorers = list(scorers)
    target = Path(out_dir) if out_dir is not None else Path(source)
    return [rescore(load_record(path), scorers).write(target) for path in sorted(Path(source).glob("*.json"))]
