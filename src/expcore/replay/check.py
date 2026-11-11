# Provenance:
#   source: www-2027-experiments @ 46a5828ddb3276ba9582e4b4a6c2ae0f217e9da7, harness/agent/replay_check.py, blob c1f933ca96b49b028d88085daaa334529f4b7400
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The replay check: the gate a batch of run records passes before any analysis (R04, R05, R08).

Every record of a batch (a directory of record files) must load, write back to exactly its stored bytes, and replay
offline through the real loop. The result is written beside the records as `replay-check.json`:

    {"schema": "replay-check/1", "record_schema", "checked_at", "code": {the code identity},
     "hashed_only_patterns", "side_files": [{"pattern", "hash_field"}, when the caller declares any], "n_records",
     "n_ok", "failures": [{"file", "stage", "error"}], "hashed_only": [paths],
     "recompute": [{"name", "version", "records", "decisions", "failures"}], "inputs": {path: sha256}, "inputs_sha256",
     "environment": {the environment, when the caller gives one}}

`replayed` lists the records that loaded, wrote back and replayed. A run record is never hashed only: a pattern
that names one is refused when the check is made (`HashedOnlyRecord`), and `require_current_check` refuses a stored
check that has one, so a check that replayed nothing cannot vouch for the records beside it. A hashed-only file is a
record when it is named as one or holds the record schema marker; one larger than `RECORD_PROBE_BYTES` is looked at by
its size and a prefix only, and refused when that prefix opens a JSON object, which could be a record.

`stage` is `load`, `roundtrip`, `secret` (the record holds a value of this environment's secrets), `replay`,
`decision` (a failure at `decision` also names the `hook`) or `side_file`. Paths are relative to the batch, so a batch
extracted from an archive elsewhere has the same digest. An input is a record (a `.json` file anywhere under the batch,
the check file excepted) or a file named by `hashed_only` patterns, which is hashed but not replayed, such as a manifest
the analysis reads. A `.json` file that is not a record must therefore be named in `hashed_only`, or it fails the check
at `load`.

Side files (`replay.side_files`): a study may declare files its runs write beside each record, each named by its SHA-256
in that record. Every file of a declared name in the batch is a hashed-only input, found by the patterns the
declaration adds to `hashed_only_patterns`, and is paired with the one record in its directory; what does not pair
fails the check at stage `side_file`, against the record, or against the side file when no record lies beside it. A
side file larger than the cap the analysis reads (`side_files.MAX_SIDE_FILE_BYTES`) fails there too, against itself. The
check records the declaration (`side_files`), only when there is one, so a batch of a study that declares none is
checked exactly as before. `require_current_check` refuses a check made under another declaration than the caller's.
The check scores nothing: scores are derived by the analysis (`expcore.records.rescore`).

What is held in memory: every hashed-only input is hashed in chunks. A declared side file is never read whole, by the
check or by `require_current_check`: by its declaration it is no record, so neither looks into it for one, and the cap
is checked by its size. Any other hashed-only file is read whole only when it holds at most `RECORD_PROBE_BYTES`, and a
larger one only up to `RECORD_PREFIX_BYTES`. Records are read whole, to be replayed.

The code identity is the one `expcore.provenance.codestamp.version` returns for the code the check covers. The analysis
accepts a batch only while `require_current_check` finds that its check passed, that no input was added, removed or
changed, that the check was made under the code in use (the same commit and tree hash, neither unknown nor stale nor,
unless allowed, dirty), that it was made under the schemas in use and, when the caller names the environment in use
(`expcore.provenance.environment`), under that environment. Otherwise the check runs again.

Decisions served during replay (R05): the core records none yet. A study that records a decision and serves it back
from the record passes `recompute`, hooks that each have a `name` and a `version` and take the loaded record, return
how many decisions they recomputed in it, and raise when one is contradicted by recomputing it offline from what the
record keeps; the record then fails the check at stage `decision`. The check records which hooks ran, on how many
records, and how many decisions each recomputed. A study that serves decisions names its hooks as required
(`required_recompute`, `--require-recompute`), and `require_current_check` and `archive_batch` then refuse a check
that lacks one, that ran it on fewer records than the batch holds, or that it disagreed with: a check made without the
recompute does not vouch for the decisions, so a decision is never accepted unverified.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from expcore.config.loading import ConfigError
from expcore.errors import infra_kinds
from expcore.provenance.codestamp import UNKNOWN, CodeScope, CodeVersionUnknown, require_known
from expcore.provenance.codestamp import version as code_version
from expcore.provenance.environment import differences
from expcore.records.record import RunRecord, decode
from expcore.records.redact import Redactor, redact_text
from expcore.records.schema import SCHEMA_PREFIX, SCHEMAS, RecordError, SchemaVersion
from expcore.replay import side_files as side_module
from expcore.replay.divergence import ReplayDivergence
from expcore.replay.native import has_native_parser
from expcore.replay.replay import replay_record
from expcore.replay.side_files import STAGE as SIDE_FILE_STAGE
from expcore.replay.side_files import SideFile, declared, is_side_file, pair, too_large
from expcore.replay.side_files import patterns as side_file_patterns

CHECK_SCHEMA = "replay-check/1"
CHECK_FILE = "replay-check.json"
ERROR_LIMIT = 500
EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2
# The most of a hashed-only file read whole to tell whether it is a run record, and the prefix a larger one is looked
# at by instead. Both are read where they are used, never bound as defaults.
RECORD_PROBE_BYTES = 64 * 1024 * 1024
RECORD_PREFIX_BYTES = 4096


class BatchNotVerified(Exception):
    """A batch has no passing, current replay check; the message says why and what to run."""


class HashedOnlyRecord(BatchNotVerified):
    """A run record was named by a hashed-only pattern: it would be hashed but never replayed."""


_RECORD_FILE = re.compile(r"[0-9a-f]{64}\.\d+\.json")


def is_record_name(name: str) -> bool:
    """Tell whether a file name is the name a run record is written under (`records.record.record_filename`)."""
    return _RECORD_FILE.fullmatch(name) is not None


def is_run_record(rel: str, raw: bytes) -> bool:
    """Tell whether a file is a run record: it is named as one, or it holds the record schema marker.

    A file that is neither (an outcome marker, a snapshot of the store, a manifest) may be hashed without being replayed.
    """
    if is_record_name(Path(rel).name):
        return True
    try:
        data = decode(raw)
    except RecordError:
        return False
    marker = data.get("schema_version") if isinstance(data, dict) else None
    return isinstance(marker, str) and marker.startswith(SCHEMA_PREFIX)


IS_A_RECORD = "is a run record, so it must be replayed"


def _record_problem(path: Path, rel: str) -> str | None:
    """Why a hashed-only file not named as a record may not be hashed only, or None: it holds the record schema
    marker, or it is too large to tell.

    The file is read whole only when it holds at most RECORD_PROBE_BYTES; a larger one is looked at by its size and a
    prefix of RECORD_PREFIX_BYTES. A record is a JSON object in UTF-8 (`records.record.decode`), so a large file whose
    prefix does not open one is no record; a JSON object that large cannot be told from a record without reading it
    whole, so it is refused.
    """
    limit = RECORD_PROBE_BYTES
    with open(path, "rb") as handle:
        if os.fstat(handle.fileno()).st_size > limit:
            if not handle.read(RECORD_PREFIX_BYTES).lstrip(b" \t\r\n").startswith(b"{"):
                return None
            return f"is a JSON object of more than {limit} bytes, too large to tell whether it is a run record"
        raw = handle.read(limit + 1)
    return IS_A_RECORD if is_run_record(rel, raw) else None


def _refuse_hashed_records(
    root: Path, hashed: Sequence[str], side_files: Sequence[SideFile], rerun: str | None = None
) -> None:
    """Raise HashedOnlyRecord for the first hashed-only file that is a run record, or too large to tell. A file named
    as a record is one whatever it holds; a declared side file is never one, so it is not read here at all."""
    for rel in hashed:
        if is_record_name(Path(rel).name):
            problem: str | None = IS_A_RECORD
        elif is_side_file(rel, side_files):
            continue
        else:
            problem = _record_problem(root / rel, rel)
        if problem is not None:
            raise HashedOnlyRecord(
                f"{root}: {rel} {problem}; a hashed-only pattern may not name one" + (f"; {rerun}" if rerun else "")
            )


class Recompute(Protocol):
    """A hook that recomputes, offline, the decisions a record holds (R05).

    It has a `name` and a `version`, takes a loaded record, returns how many decisions it recomputed in it, and raises
    if one is contradicted by what the record keeps.
    """

    name: str
    version: str

    def __call__(self, record: RunRecord) -> int: ...


@dataclass(frozen=True)
class RecomputeHook:
    """A named, versioned hook made from a function that returns how many decisions it recomputed (or None for none)."""

    name: str
    version: str
    check: Callable[[RunRecord], int | None]

    def __call__(self, record: RunRecord) -> int:
        return int(self.check(record) or 0)


def _hooks(recompute: Sequence[Recompute]) -> list[Recompute]:
    """Check that every hook has a name and a version, and that no name is used twice."""
    hooks = list(recompute)
    for hook in hooks:
        for field in ("name", "version"):
            if not isinstance(getattr(hook, field, None), str) or not getattr(hook, field):
                raise TypeError(f"a recompute hook has a {field}, so the check can say which one ran")
    names = [hook.name for hook in hooks]
    if len(set(names)) != len(names):
        raise ValueError(f"two recompute hooks are named alike: {sorted(n for n in names if names.count(n) > 1)}")
    return hooks


def input_files(batch: Path, hashed_only: Sequence[str] = ()) -> tuple[list[str], list[str]]:
    """Return the records and the hashed-only files of a batch, as sorted paths relative to it.

    The check file and the temporary files an interrupted write leaves (names starting with a dot) are not inputs.
    """
    records, hashed = [], []
    for path in batch.rglob("*"):
        rel = path.relative_to(batch).as_posix()
        if not path.is_file() or rel == CHECK_FILE or path.name.startswith("."):
            continue
        if any(fnmatch.fnmatchcase(rel, pattern) for pattern in hashed_only):
            hashed.append(rel)
        elif path.suffix == ".json":
            records.append(rel)
    return sorted(records), sorted(hashed)


class RecordNeeds(NamedTuple):
    """What the records of a batch name that this process cannot read yet, each list sorted."""

    backends: list[str]  # backends with no native parser registered
    kinds: list[str]  # infrastructure kinds outside the vocabulary


def _kinds_named(data: Mapping[str, Any]) -> set[str]:
    """The infrastructure kinds a record names: what its condition declared, and what its outcome and request ended in."""
    declared = data.get("treatment_kinds")
    named = list(declared) if isinstance(declared, list) else []
    for section in ("outcome", "failed_request"):
        part = data.get(section)
        if isinstance(part, Mapping):
            named.append(part.get("infra_kind"))
    return {kind for kind in named if isinstance(kind, str)}


def what_records_need(batch: Path, hashed_only: Sequence[str] = ()) -> RecordNeeds:
    """Return the backends and the infrastructure kinds the records of a batch name that this process cannot read.

    Nothing here runs a record or a study's code, so a caller can decide what to import before anything is replayed. A
    file that does not parse, or a field that is not what it should be, is left for the check to report.
    """
    backends: set[str] = set()
    kinds: set[str] = set()
    for rel in input_files(batch, hashed_only)[0]:
        try:
            data = json.loads((batch / rel).read_bytes())
            route = data.get("route")
        except (AttributeError, RecursionError, ValueError):  # not JSON, or not an object: the check says so
            continue
        backend = route.get("backend") if isinstance(route, Mapping) else None
        if isinstance(backend, str):
            backends.add(backend)
        kinds |= _kinds_named(data)
    return RecordNeeds(
        sorted(backend for backend in backends if not has_native_parser(backend)),
        sorted(kinds - set(infra_kinds())),
    )


def inputs_sha256(hashes: Mapping[str, str]) -> str:
    """Return the SHA-256 of the sorted list of [path, file hash] pairs, which names every input at once."""
    pairs = sorted([path, digest] for path, digest in hashes.items())
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """The SHA-256 of a file, read in chunks, so a large hashed-only file is never held in memory whole."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def input_hashes(batch: Path, hashed_only: Sequence[str] = ()) -> dict[str, str]:
    """Return the SHA-256 of every input of a batch, by relative path."""
    records, hashed = input_files(batch, hashed_only)
    return {rel: file_sha256(batch / rel) for rel in [*records, *hashed]}


def _redacted(text: str) -> str:
    """Return a failure's text for the check file, with any secret redacted; if the environment's secrets cannot be
    read (one is too short to redact), return no text at all rather than text that may hold one."""
    try:
        return redact_text(text)[0][:ERROR_LIMIT]
    except ConfigError:
        return "(text withheld: the environment holds a secret that cannot be redacted)"


def _error(exc: BaseException) -> str:
    """Return a short description of a failure for the check file, with any secret redacted."""
    return _redacted(f"{type(exc).__name__}: {exc}")


def check_record(raw: bytes, *, schemas: Sequence[SchemaVersion] = SCHEMAS) -> tuple[str, str] | None:
    """Return None when the stored bytes load, write back to themselves, hold no secret of this environment and
    replay; else (stage, error).

    The write-back redacts nothing: it checks the record's canonical form only, so a record written under an earlier
    version of the redaction rules writes back to itself whatever the rules are now. Whether the record holds a value
    of this environment's secrets, in any form the rules now recognise, is a stage of its own (`secret`): a leak to
    report as one, never a format or replay divergence. The replay redacts nothing either (`Redactor.none`)."""
    try:
        record = RunRecord.from_json(decode(raw), tuple(schemas))
    except RecordError as exc:
        return "load", _redacted(str(exc))
    try:
        written = record.dumps(tuple(schemas), redactor=Redactor.none()).encode("utf-8")
    except RecordError as exc:
        return "roundtrip", _error(exc)
    if written != raw:
        return "roundtrip", "writing the loaded record does not give back its stored bytes"
    _, leaked = Redactor.from_environment().structure(decode(raw))
    if leaked:
        return "secret", (
            f"a value of this environment's secrets appears in the record at {', '.join(leaked[:5])}: the record "
            "holds a secret (written under another environment, or before the core recognised that form of it), so "
            "it must not be published; rerun the attempt or remove the record"
        )
    try:
        replay_record(record, schemas=schemas)
    except ReplayDivergence as exc:
        return "replay", _redacted(str(exc))
    except Exception as exc:
        return "replay", _error(exc)
    return None


def with_side_files(hashed_only: Sequence[str], side_files: Sequence[SideFile]) -> list[str]:
    """The hashed-only patterns as given, followed by each pattern that finds a declared side file and is not given."""
    return [*hashed_only, *(pattern for pattern in side_file_patterns(side_files) if pattern not in hashed_only)]


def recorded_side_files(check: Mapping[str, Any], where: object = "the replay check") -> tuple[SideFile, ...]:
    """The side files a stored check was made under: none for a check that records no declaration."""
    entries = check.get("side_files", [])
    shape = isinstance(entries, list) and all(
        isinstance(e, dict) and set(e) == {"pattern", "hash_field"} and all(isinstance(v, str) for v in e.values())
        for e in entries
    )
    if not shape:
        raise BatchNotVerified(f"{where}: the declaration of side files in {CHECK_FILE} was altered")
    return tuple(SideFile(entry["pattern"], entry["hash_field"]) for entry in entries)


def verify_batch(
    batch: str | os.PathLike[str],
    *,
    code: Mapping[str, Any],
    hashed_only: Sequence[str] = (),
    recompute: Sequence[Recompute] = (),
    schemas: Sequence[SchemaVersion] = SCHEMAS,
    environment: Mapping[str, Any] | None = None,
    side_files: Sequence[SideFile] = (),
) -> dict[str, Any]:
    """Check every record of a batch and return the check, which `write_check` stores.

    `code` is the identity of the code the check covers, and `environment`, if given, the environment it runs under.
    The bytes hashed are the bytes checked. Each of the `recompute` hooks runs on every record that loads, writes back
    and replays, and the check says which ran, on how many records, and how many decisions each recomputed. Each of
    the declared `side_files` found in the batch is hashed and paired with the record beside it (`side_files.pair`);
    what does not pair fails at stage `side_file`.
    """
    root = Path(batch)
    hooks = _hooks(recompute)
    ran = {h.name: {"name": h.name, "version": h.version, "records": 0, "decisions": 0, "failures": 0} for h in hooks}
    patterns = with_side_files(hashed_only, side_files)
    records, hashed = input_files(root, patterns)
    hashes: dict[str, str] = {}
    failures: list[dict[str, Any]] = []
    stored: dict[str, Any] = {}  # each record's stored JSON for the pairing; None for a record that failed on its own
    for rel in records:
        raw = (root / rel).read_bytes()
        hashes[rel] = hashlib.sha256(raw).hexdigest()
        failed = check_record(raw, schemas=schemas)
        stored[rel] = decode(raw) if failed is None and side_files else None
        if failed is not None:
            failures.append({"file": rel, "stage": failed[0], "error": failed[1]})
            continue
        record = RunRecord.from_json(decode(raw), tuple(schemas)) if hooks else None
        for hook in hooks:
            stat = ran[hook.name]
            stat["records"] += 1
            try:
                stat["decisions"] += int(hook(record))
            except Exception as exc:
                stat["failures"] += 1
                failures.append({"file": rel, "stage": "decision", "hook": hook.name, "error": _error(exc)})
    _refuse_hashed_records(root, hashed, side_files)
    for rel in hashed:
        hashes[rel] = file_sha256(root / rel)
    if side_files:
        side = {rel: hashes[rel] for rel in hashed if is_side_file(rel, side_files)}
        failures += [
            {"file": file, "stage": SIDE_FILE_STAGE, "error": error} for file, error in pair(stored, side, side_files)
        ]
        failures += [  # the analysis would refuse it, so the check does not pass it
            {"file": rel, "stage": SIDE_FILE_STAGE, "error": too_large()}
            for rel in side
            if (root / rel).stat().st_size > side_module.MAX_SIDE_FILE_BYTES
        ]
    failed_files = {f["file"] for f in failures}  # an orphan side file fails against its own path, not a record's
    check = {
        "schema": CHECK_SCHEMA,
        "record_schema": schemas[-1].name,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "code": dict(code),
        "hashed_only_patterns": patterns,
        **({"side_files": declared(side_files)} if side_files else {}),
        "n_records": len(records),
        "n_ok": sum(1 for rel in records if rel not in failed_files),
        "failures": failures,
        "hashed_only": hashed,
        "replayed": [rel for rel in records if rel not in failed_files],
        "recompute": list(ran.values()),
        "inputs": dict(sorted(hashes.items())),
        "inputs_sha256": inputs_sha256(hashes),
    }
    if environment is not None:
        check["environment"] = json.loads(json.dumps(environment, sort_keys=True))
    return check


def write_check(batch: str | os.PathLike[str], check: Mapping[str, Any]) -> Path:
    """Write the check beside the records atomically and return its path."""
    target = Path(batch) / CHECK_FILE
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(check, handle, ensure_ascii=False, indent=1)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return target


def _code_problem(code: Mapping[str, Any], what: str, allow_dirty: bool) -> str | None:
    """Say why a code identity cannot back a check, or None. Unknown and stale code never can; dirty code only if allowed."""
    commit = code.get("commit")
    if not commit or commit == UNKNOWN or not code.get("tree_sha256"):
        return f"{what} has no known commit or tree hash"
    if code.get("stale"):
        return f"{what} ({str(commit)[:7]}) differs from the stamp that names it"
    if code.get("dirty") and not allow_dirty:
        return f"{what} ({str(commit)[:7]}) has uncommitted changes ({code.get('why')})"
    return None


def _read_check(batch: Path, rerun: str) -> dict[str, Any]:
    """Read the stored check of a batch, or raise BatchNotVerified saying there is none."""
    path = batch / CHECK_FILE
    if not path.is_file():
        raise BatchNotVerified(f"{batch}: no {CHECK_FILE}, so its records were never shown to replay; {rerun}")
    try:
        check = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BatchNotVerified(f"{path}: not readable ({type(exc).__name__}); {rerun}") from None
    if not isinstance(check, dict):
        raise BatchNotVerified(f"{path}: not a check; {rerun}")
    return check


def _changed_inputs(stored: Mapping[str, str], current: Mapping[str, str]) -> str:
    """Name what changed between the hashes the check holds and the hashes now."""
    added, removed = sorted(set(current) - set(stored)), sorted(set(stored) - set(current))
    changed = sorted(p for p in set(stored) & set(current) if stored[p] != current[p])
    parts = [
        f"{label} {', '.join(names[:3])}{' and more' if len(names) > 3 else ''}"
        for label, names in (("added", added), ("removed", removed), ("changed", changed))
        if names
    ]
    return "; ".join(parts)


def _require_recompute(root: Path, check: Mapping[str, Any], required: Sequence[str], rerun: str) -> None:
    """Refuse a check that lacks a required recompute hook, ran it on too few records, or whose hook disagreed."""
    ran = {entry.get("name"): entry for entry in check.get("recompute") or [] if isinstance(entry, dict)}
    for name in required:
        entry = ran.get(name)
        if entry is None:
            raise BatchNotVerified(
                f"{root}: the check did not run the required recompute hook {name!r} (it ran: {sorted(ran) or 'none'}); "
                f"run the check with that hook, then {rerun}"
            )
        if entry.get("failures"):
            raise BatchNotVerified(
                f"{root}: the recompute hook {name!r} disagreed with {entry['failures']} record(s) (see {CHECK_FILE})"
            )
        if entry.get("records") != check.get("n_records"):
            raise BatchNotVerified(
                f"{root}: the recompute hook {name!r} ran on {entry.get('records')} of {check.get('n_records')} records"
            )


def require_current_check(
    batch: str | os.PathLike[str],
    *,
    code: Mapping[str, Any],
    allow_dirty: bool = False,
    required_recompute: Sequence[str] = (),
    schemas: Sequence[SchemaVersion] = SCHEMAS,
    rerun_command: str | None = None,
    environment: Mapping[str, Any] | None = None,
    compare_sources: bool = True,
    side_files: Sequence[SideFile] | None = None,
) -> dict[str, Any]:
    """Return the batch's stored check when it passed and is current; else raise BatchNotVerified saying why (R04).

    Every refusal ends by naming the command that makes the check again: this module's own by default, or
    `rerun_command` when the caller is a command line of its own (for example `python -m expcore check BATCH`).

    Refused: no check; a check of another check schema or of other record schemas than the ones in use; a check with
    failures; inputs added, removed or changed since; a check made under another commit or tree hash than `code`, the
    identity of the code in use; code that is unknown, stale or, unless `allow_dirty`, dirty; and, for each name in
    `required_recompute`, a check that did not run that recompute hook, ran it on fewer records than the batch holds,
    or whose hook disagreed with a record (R05). With `environment`, the environment in use, a check made under
    another one, or that records none, is refused too; `compare_sources` false compares only the Python version and
    the distributions' versions, as a replication package installed from its own wheels does. With `side_files`, the
    declaration in use (empty for a study that declares none), a check made under another declaration is refused.
    The side files the check's declaration names are inputs like any other, so one changed, removed or added since is
    refused whether the caller declares them or not.
    """
    root = Path(batch)
    rerun = f"run `{rerun_command or f'python -m expcore.replay.check {root}'}` first"
    check = _read_check(root, rerun)
    if check.get("schema") != CHECK_SCHEMA:
        raise BatchNotVerified(f"{root}: check schema {check.get('schema')!r}, not {CHECK_SCHEMA!r}; {rerun}")
    if check.get("record_schema") != schemas[-1].name:
        raise BatchNotVerified(
            f"{root}: the check was made under record schema {check.get('record_schema')!r}, "
            f"the schema in use is {schemas[-1].name!r}; {rerun}"
        )
    _require_recompute(root, check, required_recompute, rerun)
    recorded = recorded_side_files(check, root)
    if side_files is not None and declared(recorded) != declared(side_files):
        raise BatchNotVerified(
            f"{root}: the check was made under another declaration of side files ([[study.side_files]] in "
            f"study.toml) than the one in use; {rerun}"
        )
    if check.get("n_ok") != check.get("n_records") or check.get("failures"):
        raise BatchNotVerified(
            f"{root}: the replay check failed for {len(check.get('failures') or [])} of {check.get('n_records')} "
            f"records (see {CHECK_FILE}); fix them, then {rerun}"
        )
    stored = check.get("inputs")
    if not isinstance(stored, dict) or check.get("inputs_sha256") != inputs_sha256(stored):
        raise BatchNotVerified(f"{root}: the list of inputs in {CHECK_FILE} was altered; {rerun}")
    current = input_hashes(root, with_side_files(check.get("hashed_only_patterns") or (), recorded))
    if current != stored:
        raise BatchNotVerified(
            f"{root}: inputs changed since the replay check ({_changed_inputs(stored, current)}); {rerun}"
        )
    hashed = check.get("hashed_only") or []
    replayed = check.get("replayed")
    if replayed is not None and (set(replayed) & set(hashed) or set(replayed) | set(hashed) != set(stored)):
        raise BatchNotVerified(
            f"{root}: the lists of replayed and hashed-only inputs in {CHECK_FILE} were altered; {rerun}"
        )
    _refuse_hashed_records(root, hashed, recorded, rerun)
    made = check.get("code") or {}
    problem = _code_problem(code, "the code in use", allow_dirty) or _code_problem(
        made, "the code the check was made under", allow_dirty
    )
    if problem is None and made["commit"] != code["commit"]:
        problem = (
            f"the check was made under code {str(made['commit'])[:7]}, the code in use is {str(code['commit'])[:7]}"
        )
    if problem is None and made["tree_sha256"] != code["tree_sha256"]:
        problem = f"the check was made under code tree {made['tree_sha256'][:12]}, the code in use has {code['tree_sha256'][:12]}"
    if problem:
        raise BatchNotVerified(f"{root}: {problem}; {rerun}")
    if environment is not None:
        changed = differences(check.get("environment"), environment, sources=compare_sources)
        if changed:
            raise BatchNotVerified(
                f"{root}: the check was made under another environment than the one in use ({'; '.join(changed[:5])}"
                f"{' and more' if len(changed) > 5 else ''}); {rerun}"
            )
    return check


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(prog="python -m expcore.replay.check", description=__doc__.split("\n\n")[0])
    parser.add_argument("batch", type=Path, help="directory of record files")
    parser.add_argument("--code-root", type=Path, default=Path("."), help="root of the code the check covers")
    parser.add_argument(
        "--root",
        action="append",
        required=True,
        help="file or directory under the code root that the code identity covers (repeat)",
    )
    parser.add_argument(
        "--hashed-only",
        action="append",
        default=[],
        metavar="GLOB",
        help="inputs that are hashed, not replayed (repeat)",
    )
    parser.add_argument(
        "--allow-dirty", action="store_true", help="accept code with uncommitted changes (the check says so)"
    )
    parser.add_argument(
        "--require", action="store_true", help="do not run the check; refuse unless the stored one is current"
    )
    parser.add_argument(
        "--require-recompute",
        action="append",
        default=[],
        metavar="NAME",
        help="refuse unless the check ran this recompute hook on every record and it agreed (repeat)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check on a batch, or with --require only test the stored one; exit 0 when it passes, 1 when it does not."""
    args = build_parser().parse_args(argv)
    scope = CodeScope.of(args.root)
    try:
        if args.require:
            require_current_check(
                args.batch,
                code=code_version(args.code_root, scope),
                allow_dirty=args.allow_dirty,
                required_recompute=args.require_recompute,
            )
            print(f"ok {args.batch}: the replay check is current")
            return EXIT_OK
        code = require_known("replay check", args.code_root, scope, allow_dirty=args.allow_dirty)
    except (BatchNotVerified, CodeVersionUnknown) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    try:
        check = verify_batch(args.batch, code=code, hashed_only=args.hashed_only)
    except HashedOnlyRecord as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    path = write_check(args.batch, check)
    for failure in check["failures"]:
        print(f"FAIL {failure['file']} [{failure['stage']}] {failure['error']}")
    print(f"{check['n_ok']} of {check['n_records']} records passed; check written to {path}")
    if args.require_recompute and not check["failures"]:
        try:  # this command runs no recompute hook, so a check it made cannot satisfy a requirement for one
            _require_recompute(args.batch, check, args.require_recompute, "run the check from the study's own code")
        except BatchNotVerified as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
    return EXIT_OK if not check["failures"] else EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
