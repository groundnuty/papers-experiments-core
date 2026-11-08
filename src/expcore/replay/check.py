# Provenance:
#   source: www-2027-experiments @ 46a5828ddb3276ba9582e4b4a6c2ae0f217e9da7, harness/agent/replay_check.py, blob c1f933ca96b49b028d88085daaa334529f4b7400
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The replay check: the gate a batch of run records passes before any analysis (R04, R05, R08).

Every record of a batch (a directory of record files) must load, write back to exactly its stored bytes, and replay
offline through the real loop. The result is written beside the records as `replay-check.json`:

    {"schema": "replay-check/1", "record_schema", "checked_at", "code": {the code identity},
     "hashed_only_patterns", "n_records", "n_ok", "failures": [{"file", "stage", "error"}], "hashed_only": [paths],
     "decisions_recomputed", "inputs": {path: sha256}, "inputs_sha256"}

`stage` is `load`, `roundtrip`, `replay` or `decision`. Paths are relative to the batch, so a batch extracted from an
archive elsewhere has the same digest. An input is a record (a `.json` file anywhere under the batch, the check file
excepted) or a file named by `hashed_only` patterns, which is hashed but not replayed, such as a manifest the analysis
reads. A `.json` file that is not a record must therefore be named in `hashed_only`, or it fails the check at `load`.

The code identity is the one `expcore.provenance.codestamp.version` returns for the code the check covers. The analysis
accepts a batch only while `require_current_check` finds that its check passed, that no input was added, removed or
changed, that the check was made under the code in use (the same commit and tree hash, neither unknown nor stale nor,
unless allowed, dirty), and that it was made under the schemas in use. Otherwise the check runs again.

Decisions served during replay (R05): the core records none yet. A study that records a decision and serves it back
from the record passes `recompute`, a function that takes the loaded record and raises when the decision the record
holds is contradicted by recomputing it offline from what the record keeps; the record then fails the check at stage
`decision`, so a decision is never accepted unverified.
"""

import argparse
import fnmatch
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expcore.provenance.codestamp import UNKNOWN, CodeScope, CodeVersionUnknown, require_known
from expcore.provenance.codestamp import version as code_version
from expcore.records.record import RunRecord, decode
from expcore.records.redact import redact_text
from expcore.records.schema import SCHEMAS, RecordError, SchemaVersion
from expcore.replay.divergence import ReplayDivergence
from expcore.replay.replay import replay_record

CHECK_SCHEMA = "replay-check/1"
CHECK_FILE = "replay-check.json"
ERROR_LIMIT = 500
EXIT_OK, EXIT_REFUSED, EXIT_USAGE = 0, 1, 2


class BatchNotVerified(Exception):
    """A batch has no passing, current replay check; the message says why and what to run."""


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


def inputs_sha256(hashes: Mapping[str, str]) -> str:
    """Return the SHA-256 of the sorted list of [path, file hash] pairs, which names every input at once."""
    pairs = sorted([path, digest] for path, digest in hashes.items())
    return hashlib.sha256(json.dumps(pairs, separators=(",", ":")).encode("utf-8")).hexdigest()


def input_hashes(batch: Path, hashed_only: Sequence[str] = ()) -> dict[str, str]:
    """Return the SHA-256 of every input of a batch, by relative path."""
    records, hashed = input_files(batch, hashed_only)
    return {rel: hashlib.sha256((batch / rel).read_bytes()).hexdigest() for rel in [*records, *hashed]}


def _error(exc: BaseException) -> str:
    """Return a short description of a failure for the check file, with any secret redacted."""
    return redact_text(f"{type(exc).__name__}: {exc}")[0][:ERROR_LIMIT]


def check_record(
    raw: bytes,
    *,
    schemas: Sequence[SchemaVersion] = SCHEMAS,
    recompute: Callable[[RunRecord], None] | None = None,
) -> tuple[str, str] | None:
    """Return None when the stored bytes load, write back to themselves and replay; else (stage, error)."""
    try:
        record = RunRecord.from_json(decode(raw), tuple(schemas))
    except RecordError as exc:
        return "load", redact_text(str(exc))[0][:ERROR_LIMIT]
    try:
        written = record.dumps(tuple(schemas)).encode("utf-8")
    except RecordError as exc:
        return "roundtrip", _error(exc)
    if written != raw:
        return "roundtrip", "writing the loaded record does not give back its stored bytes"
    try:
        replay_record(record, schemas=schemas)
    except ReplayDivergence as exc:
        return "replay", redact_text(str(exc))[0][:ERROR_LIMIT]
    except Exception as exc:
        return "replay", _error(exc)
    if recompute is not None:
        try:
            recompute(record)
        except Exception as exc:
            return "decision", _error(exc)
    return None


def verify_batch(
    batch: str | os.PathLike[str],
    *,
    code: Mapping[str, Any],
    hashed_only: Sequence[str] = (),
    recompute: Callable[[RunRecord], None] | None = None,
    schemas: Sequence[SchemaVersion] = SCHEMAS,
) -> dict[str, Any]:
    """Check every record of a batch and return the check, which `write_check` stores.

    `code` is the identity of the code the check covers. The bytes hashed are the bytes checked.
    """
    root = Path(batch)
    records, hashed = input_files(root, hashed_only)
    hashes: dict[str, str] = {}
    failures = []
    for rel in records:
        raw = (root / rel).read_bytes()
        hashes[rel] = hashlib.sha256(raw).hexdigest()
        failed = check_record(raw, schemas=schemas, recompute=recompute)
        if failed is not None:
            failures.append({"file": rel, "stage": failed[0], "error": failed[1]})
    for rel in hashed:
        hashes[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
    return {
        "schema": CHECK_SCHEMA,
        "record_schema": schemas[-1].name,
        "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "code": dict(code),
        "hashed_only_patterns": list(hashed_only),
        "n_records": len(records),
        "n_ok": len(records) - len(failures),
        "failures": failures,
        "hashed_only": hashed,
        "decisions_recomputed": 0 if recompute is None else len(records) - len(failures),
        "inputs": dict(sorted(hashes.items())),
        "inputs_sha256": inputs_sha256(hashes),
    }


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


def require_current_check(
    batch: str | os.PathLike[str],
    *,
    code: Mapping[str, Any],
    allow_dirty: bool = False,
    schemas: Sequence[SchemaVersion] = SCHEMAS,
) -> dict[str, Any]:
    """Return the batch's stored check when it passed and is current; else raise BatchNotVerified saying why (R04).

    Refused: no check; a check of another check schema or of other record schemas than the ones in use; a check with
    failures; inputs added, removed or changed since; a check made under another commit or tree hash than `code`, the
    identity of the code in use; code that is unknown, stale or, unless `allow_dirty`, dirty.
    """
    root = Path(batch)
    rerun = f"run `python -m expcore.replay.check {root}` first"
    check = _read_check(root, rerun)
    if check.get("schema") != CHECK_SCHEMA:
        raise BatchNotVerified(f"{root}: check schema {check.get('schema')!r}, not {CHECK_SCHEMA!r}; {rerun}")
    if check.get("record_schema") != schemas[-1].name:
        raise BatchNotVerified(
            f"{root}: the check was made under record schema {check.get('record_schema')!r}, "
            f"the schema in use is {schemas[-1].name!r}; {rerun}"
        )
    if check.get("n_ok") != check.get("n_records") or check.get("failures"):
        raise BatchNotVerified(
            f"{root}: the replay check failed for {len(check.get('failures') or [])} of {check.get('n_records')} "
            f"records (see {CHECK_FILE}); fix them, then {rerun}"
        )
    stored = check.get("inputs")
    if not isinstance(stored, dict) or check.get("inputs_sha256") != inputs_sha256(stored):
        raise BatchNotVerified(f"{root}: the list of inputs in {CHECK_FILE} was altered; {rerun}")
    current = input_hashes(root, check.get("hashed_only_patterns") or ())
    if current != stored:
        raise BatchNotVerified(
            f"{root}: inputs changed since the replay check ({_changed_inputs(stored, current)}); {rerun}"
        )
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the check on a batch, or with --require only test the stored one; exit 0 when it passes, 1 when it does not."""
    args = build_parser().parse_args(argv)
    scope = CodeScope.of(args.root)
    try:
        if args.require:
            require_current_check(args.batch, code=code_version(args.code_root, scope), allow_dirty=args.allow_dirty)
            print(f"ok {args.batch}: the replay check is current")
            return EXIT_OK
        code = require_known("replay check", args.code_root, scope, allow_dirty=args.allow_dirty)
    except (BatchNotVerified, CodeVersionUnknown) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    check = verify_batch(args.batch, code=code, hashed_only=args.hashed_only)
    path = write_check(args.batch, check)
    for failure in check["failures"]:
        print(f"FAIL {failure['file']} [{failure['stage']}] {failure['error']}")
    print(f"{check['n_ok']} of {check['n_records']} records passed; check written to {path}")
    return EXIT_OK if not check["failures"] else EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
