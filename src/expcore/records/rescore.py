# Provenance: new
"""Re-scoring: scorers run on saved records, offline, and the record keeps the version of each scorer (R13).

A scorer is a pure function of a record and of the record's side files: it reads them and nothing else, so no model call
and no platform access is needed, and a score computed later equals one computed at the end of the run. The scores sit
in the record's `scores` section under the scorer's name, apart from the instrument outputs. Each entry names the
scorer's version, so a later change to a parser or scorer is visible in the records it re-scored.

A scorer declares `version`. A plain function has none, so its version is the SHA-256 of its source file, which stands
in for a commit when the code is not in git.

A scorer that declares a parameter named `side_files` (#144) is handed the record's side files (`replay.side_files`) by
declared pattern, as bytes the loader read once and verified against the hash the record names: the analysis gets them
from `study.verified`, and `rescore_files` reads them itself. It never opens a path. A record with no side file gives
an empty mapping. A scorer without the parameter is called with the record alone, exactly as before.

An attempt that is set aside (an infrastructure failure, unless its condition declares that kind as treatment, or a
harness defect) is never scored; re-scoring leaves it as it was, with no scores.
"""

import copy
import hashlib
import inspect
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from expcore.records.digest import plain
from expcore.records.record import RunRecord, decode
from expcore.records.schema import SCHEMA_PREFIX, RecordError
from expcore.replay.side_files import SideFile, SideFileTooLarge, beside, pair, read_side_file

SIDE_FILES = "side_files"  # the parameter by which a scorer takes the record's verified side files
_TAKES = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


class Scorer(Protocol):
    """A scorer reads a record and returns its scores as JSON values: a verdict, named numeric outcomes."""

    name: str
    version: str

    def score(self, record: RunRecord) -> Mapping[str, Any]:
        """Score one record. Must not read anything but the record."""
        ...


class SideFileScorer(Protocol):
    """A scorer that also reads the record's side files, handed over verified by declared pattern."""

    name: str
    version: str

    def score(self, record: RunRecord, side_files: Mapping[str, bytes]) -> Mapping[str, Any]:
        """Score one record. Must not read anything but the record and the bytes in `side_files`."""
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


def _call(scorer: Any) -> Any:
    """What runs a scorer: its `score` method, or the scorer itself when it is a plain function."""
    return scorer.score if hasattr(scorer, "score") else scorer


def takes_side_files(scorer: Any) -> bool:
    """Tell whether a scorer declares the `side_files` parameter, by name, positional or keyword-only."""
    try:
        parameter = inspect.signature(_call(scorer)).parameters.get(SIDE_FILES)
    except (TypeError, ValueError):  # a callable whose signature cannot be read declares nothing
        return False
    return parameter is not None and parameter.kind in _TAKES


def _run(scorer: Any, record: RunRecord, side_files: Mapping[str, bytes] | None) -> Mapping[str, Any]:
    """Run a scorer, given a copy of the record so that it cannot change what is stored, and, when it takes them, a
    read-only mapping of its own of the record's side files."""
    if not takes_side_files(scorer):
        return _call(scorer)(copy.deepcopy(record))
    if side_files is None:
        raise RecordError(
            f"scorer {scorer_name(scorer)!r} takes the side files of the record ({SIDE_FILES}), and none were handed "
            "over: score through `analyze`, or give `rescore_files` the study's side_files"
        )
    return _call(scorer)(copy.deepcopy(record), side_files=MappingProxyType(dict(side_files)))


def rescore(record: RunRecord, scorers: Iterable[Any], *, side_files: Mapping[str, bytes] | None = None) -> RunRecord:
    """Return a copy of the record with the scorers' results in its `scores`, each with the scorer's version.

    The entries of scorers that did not run are kept; a scorer that did run replaces its earlier entry. A record that is
    set aside is returned as it is. `side_files` holds the record's verified side files by declared pattern, empty for
    a record with none; a scorer that takes them (`takes_side_files`) is handed them, and refused when the caller hands
    over none (None), so it never takes a record that has one for a record without.
    """
    if record.set_aside:
        return record
    fresh: dict[str, Any] = {}
    for scorer in scorers:
        name = scorer_name(scorer)
        if name in fresh:
            raise RecordError(f"two scorers are named {name!r}")
        fresh[name] = {"version": scorer_version(scorer), "result": plain(dict(_run(scorer, record, side_files)))}
    return replace(record, scores={**record.scores, **fresh})


_RECORD_NAME = re.compile(r"[0-9a-f]{64}\.\d+\.json")


def _load_if_record(path: Path) -> tuple[RunRecord, Any] | None:
    """Load a file as a run record, with its stored JSON, or return None when it is not one: a check, a manifest, any
    JSON without the record schema marker. A file named like a record that cannot be read as one is an error, not
    something to skip."""
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
        return RunRecord.from_json(data), data
    except RecordError as exc:
        raise RecordError(f"{path}: {exc}") from None


def _side_files_beside(
    source: Path, stored: Mapping[str, Any], side_files: Sequence[SideFile]
) -> dict[str, dict[str, bytes]]:
    """Read each file of a declared name directly under `source` once, within the cap, pair it with the record beside
    it by the hash the record names, and return each record's side files by file name and declared pattern; raise
    RecordError, naming the file, for what does not pair."""
    contents: dict[str, bytes] = {}
    for declaration in side_files:
        path = source / declaration.pattern
        try:
            contents[declaration.pattern] = read_side_file(path)
        except FileNotFoundError:
            continue  # none beside the record: `pair` refuses it if the record names one
        except (OSError, SideFileTooLarge) as error:
            raise RecordError(f"{path}: {error}") from None
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    problems = pair(stored, hashes, side_files)
    if problems:
        name, problem = problems[0]
        raise RecordError(f"{source / name}: {problem}")
    return beside(stored, contents, side_files)


def rescore_files(
    source: str | os.PathLike[str],
    scorers: Iterable[Any],
    *,
    out_dir: str | os.PathLike[str] | None = None,
    side_files: Sequence[SideFile] | None = None,
) -> list[Path]:
    """Re-score every run record directly under a directory; write each back, atomically, into `out_dir` or in place.

    Only run records are touched: a file without the record schema marker (the replay check, a manifest) is left alone.
    It happens in two phases. First every record is loaded, validated, scored, and rendered as it will be written, so a
    record that is not valid, a scorer that fails on one, or a result that cannot be written stops the run before any
    file is changed. Then the records are written, each atomically. Returns the paths written.

    `side_files` is the study's declaration (`StudyConfig.side_files`). With it, each file of a declared name directly
    under the directory is read once, within the cap, and paired with the record beside it by the hash that record
    names, as the replay check pairs them: the directory holds one attempt's record, as an attempt directory does. A
    side file that does not pair is refused, naming it, before any scorer runs; a scorer that takes `side_files` is
    handed the bytes so read. Without it nothing beside the records is read, and such a scorer is refused.
    """
    scorers = list(scorers)
    target = Path(out_dir) if out_dir is not None else Path(source)
    loaded = {path.name: found for path in sorted(Path(source).glob("*.json")) if (found := _load_if_record(path))}
    held = None
    if side_files is not None:
        held = _side_files_beside(Path(source), {name: data for name, (_, data) in loaded.items()}, side_files)
    planned: list[RunRecord] = []
    for name, (record, _) in loaded.items():
        scored = rescore(record, scorers, side_files=None if held is None else held[name])
        scored.dumps()  # validates and redacts exactly as the write will, so a failure comes before the first write
        planned.append(scored)
    names = [r.filename for r in planned]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise RecordError(f"two files hold the same run key and attempt: {repeated}")
    return [r.write(target) for r in planned]
