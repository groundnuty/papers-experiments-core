# Provenance: new
"""The bytes of a batch that the replay check verified, and nothing else: what an analysis may use.

`require_current_check` compares the files on disk with the check's hashes at one moment. An analysis that opens the
files afterwards could read others, so this module reads each file once, compares the SHA-256 of the bytes in hand with
the check's, and parses those bytes. It also cross-validates the records against the snapshot of the campaign store
that the check holds:

- the check lists the records it replayed (`replayed`), and every record the analysis uses is in that list;
- every record maps to an attempt of the snapshot, by run key, attempt number and the directory of the try;
- every attempt the analysis would score (state `done`, which includes a scored treatment failure) has such a record,
  and agrees with it on the run key, the attempt, the outcome and the treatment flag;
- every file of a name the study declares (`replay.side_files`) is found in the batch again, and one the check did not
  list is refused, so a side file added after `require_current_check` compared the batch is never passed over; each
  the check listed is read once, within the cap (`side_files.read_side_file`), compared with the check's hash, and
  paired again with the hash the record beside it names, so a check file edited to vouch for a side file its record
  does not name is refused all the same.

A merged batch (`campaign.merge`) has the snapshot of each of its shards instead of one: the check records the SHA-256
of its merge.json (`check["merge"]`), merge.json records each snapshot's, and each is read once and compared with the
hash that names it. The records are cross-validated against all the snapshots together.

Any mismatch is refused, naming it. With `partial`, an attempt that no replayed record covers is left out of the
analysis instead, and the report says so, and attempts still running at the check are tolerated.

The side files' bytes so read are kept with the records (`VerifiedBatch.side_files_for`): they are what a scorer that
takes `side_files` is handed, so it reads exactly the bytes verified here, and never opens a path. Every side file of
the batch is held in memory for the analysis, as every record is.
"""

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore.campaign.factors import FactorSchema
from expcore.campaign.merge import CHECK_KEY, SHARDS_DIR, MergeRefused, read_merge
from expcore.campaign.shard import MERGE_FILE
from expcore.campaign.snapshot import SNAPSHOT_FILE, SnapshotError, StoreSnapshot
from expcore.records.record import RunRecord, decode, record_filename
from expcore.records.schema import RecordError
from expcore.replay.check import input_files
from expcore.replay.side_files import SideFile, SideFileTooLarge, beside, is_side_file, pair, read_side_file
from expcore.replay.side_files import patterns as side_file_patterns

UNCOVERED = "no replayed record covers its final attempt"  # why a partial analysis leaves a run out


class Unverified(RuntimeError):
    """Something the analysis would use is not what the replay check verified; the message names it."""


@dataclass(frozen=True)
class VerifiedBatch:
    """What the analysis may use: the snapshot of the store, the records by path, the side files of each record, and
    what is left out and why."""

    snapshots: Mapping[int | None, StoreSnapshot]  # the store's, under None; a merged batch's, by shard index
    records: Mapping[str, RunRecord]  # by path relative to the batch, parsed from the bytes the check hashed
    running_at_check: int = 0  # attempts the snapshots hold as still running
    excluded: tuple[str, ...] = field(default_factory=tuple)  # run keys whose final attempt no replayed record covers
    # by record path, each side file beside it by declared pattern: the bytes read once and verified
    side_files: Mapping[str, Mapping[str, bytes]] = field(default_factory=dict)
    shards: tuple[Mapping[str, Any], ...] = ()  # merge.json's list of shards for a merged batch, else empty

    def close(self) -> None:
        """Close every snapshot."""
        for snapshot in self.snapshots.values():
            snapshot.close()

    def record_for(self, digest: str, try_name: str, run_key: str, attempt: int) -> RunRecord | None:
        """The verified record of an attempt, or None when no replayed record is there."""
        return self.records.get(record_path(digest, try_name, run_key, attempt))

    def side_files_for(self, digest: str, try_name: str, run_key: str, attempt: int) -> Mapping[str, bytes]:
        """The verified side files of an attempt's record by declared pattern; empty when it has none, or the study
        declares none."""
        return self.side_files.get(record_path(digest, try_name, run_key, attempt), {})


def record_path(digest: str, try_name: str, run_key: str, attempt: int) -> str:
    """Where an attempt's record lies in the batch: <run digest>/<try directory>/<record file>."""
    return f"{digest}/{try_name}/{record_filename(run_key, attempt)}"


def _read_verified(batch: Path, rel: str, expected: str, read: Callable[[Path], bytes] = Path.read_bytes) -> bytes:
    """Read a file once, with `read`, and return its bytes if they are the ones the check hashed."""
    try:
        raw = read(batch / rel)
    except SideFileTooLarge as error:
        raise Unverified(f"{rel}: {error}") from None
    except OSError as error:
        raise Unverified(f"{rel}: cannot be read ({type(error).__name__}) although the replay check lists it") from None
    if hashlib.sha256(raw).hexdigest() != expected:
        raise Unverified(f"{rel}: changed after the replay check read it; the analysis uses only the bytes it verified")
    return raw


def _replayed_set(check: Mapping[str, Any]) -> set[str]:
    replayed, hashed, inputs = check.get("replayed"), check.get("hashed_only"), check.get("inputs")
    if not (isinstance(replayed, list) and isinstance(hashed, list) and isinstance(inputs, dict)):
        raise Unverified(
            "the replay check does not list the records it replayed; make it again with `python -m expcore check`"
        )
    if set(replayed) & set(hashed) or set(replayed) | set(hashed) != set(inputs):
        raise Unverified("the replay check's lists of replayed and hashed-only inputs do not add up to its inputs")
    return set(replayed)


def _load_snapshot(batch: Path, check: Mapping[str, Any], schema: FactorSchema) -> StoreSnapshot:
    if SNAPSHOT_FILE not in (check.get("hashed_only") or []):
        raise Unverified(
            f"the replay check holds no snapshot of the campaign ({SNAPSHOT_FILE}); "
            "make it with `python -m expcore check`"
        )
    raw = _read_verified(batch, SNAPSHOT_FILE, check["inputs"][SNAPSHOT_FILE])
    try:
        return StoreSnapshot(raw, schema)
    except SnapshotError as error:
        raise Unverified(f"{SNAPSHOT_FILE}: {error}") from None


def _load_shards(batch: Path, merge: Any, schema: FactorSchema) -> tuple[dict[int | None, StoreSnapshot], tuple]:
    """The snapshots of a merged batch's shards, each verified against the hash merge.json names, and the shard list."""
    out = batch.parent
    expected = merge.get("sha256") if isinstance(merge, dict) else None
    if not isinstance(merge, dict) or merge.get("file") != MERGE_FILE or not isinstance(expected, str):
        raise Unverified(
            "the replay check's record of the merge was altered; make it again with `python -m expcore check`"
        )
    try:
        document = read_merge(_read_verified(out, MERGE_FILE, expected), MERGE_FILE)
    except MergeRefused as error:
        raise Unverified(str(error)) from None
    snapshots: dict[int | None, StoreSnapshot] = {}
    try:
        for shard in document["shards"]:
            rel = f"{SHARDS_DIR}/{shard['index']}/{SNAPSHOT_FILE}"
            try:
                snapshots[shard["index"]] = StoreSnapshot(_read_verified(out, rel, shard["snapshot_sha256"]), schema)
            except SnapshotError as error:
                raise Unverified(f"{rel}: {error}") from None
    except Unverified:
        for opened in snapshots.values():
            opened.close()
        raise
    return snapshots, tuple(document["shards"])


def _parse(rel: str, raw: bytes) -> RunRecord:
    try:
        record = RunRecord.from_json(decode(raw))
    except RecordError as error:
        raise Unverified(f"{rel}: not a record although the replay check replayed it ({error})") from None
    parts = rel.split("/")
    if len(parts) != 3 or parts[2] != record.filename or parts[0] != record.filename.split(".")[0]:
        raise Unverified(f"{rel}: lies where no record of {record.run_key!r} attempt {record.attempt} belongs")
    return record


def _attempts(snapshot: StoreSnapshot) -> list[Any]:
    return snapshot.q(
        "SELECT a.id, a.run_key, a.attempt, a.state, a.termination, a.treatment, a.run_dir, r.digest "
        "FROM attempts a JOIN runs r ON r.run_key = a.run_key ORDER BY a.id"
    )


def _agree(row: Any, record: RunRecord, rel: str) -> None:
    """A scored attempt and its record must say the same of the run, the attempt, the outcome and the treatment flag."""
    said = {
        "run key": (row["run_key"], record.run_key),
        "attempt": (row["attempt"], record.attempt),
        "outcome": (row["termination"], record.outcome["name"]),
        "treatment flag": (bool(row["treatment"]), bool(record.outcome["treatment"])),
    }
    for what, (store, held) in said.items():
        if store != held:
            raise Unverified(f"{rel}: the campaign snapshot has {what} {store!r}, the record has {held!r}")


def _pair_side_files(
    batch: Path, check: Mapping[str, Any], raws: Mapping[str, bytes], side_files: Sequence[SideFile]
) -> dict[str, dict[str, bytes]]:
    """Find every file of a declared name in the batch again, refuse one the check did not list, then read each the
    check hashed once, within the cap, against the check's hash, and pair it with its record; return the bytes so
    read of each record's side files, by record path and declared pattern."""
    listed = [rel for rel in check["hashed_only"] if is_side_file(rel, side_files)]
    present = input_files(batch, side_file_patterns(side_files))[1]  # the same walk the check made
    unlisted = sorted(set(present) - set(listed))
    if unlisted:
        raise Unverified(
            f"{unlisted[0]}: a side file the replay check did not list, added after it; remove it, or make the check "
            "again with `python -m expcore check`"
        )
    contents = {rel: _read_verified(batch, rel, check["inputs"][rel], read_side_file) for rel in listed}
    side = {rel: check["inputs"][rel] for rel in listed}  # each the SHA-256 of the bytes in `contents`
    problems = pair({rel: decode(raw) for rel, raw in raws.items()}, side, side_files)
    if problems:
        rel, problem = problems[0]
        raise Unverified(f"{rel}: {problem}")
    return beside(raws, contents, side_files)


def load_verified(
    batch: Path,
    check: Mapping[str, Any],
    schema: FactorSchema,
    *,
    partial: bool = False,
    side_files: Sequence[SideFile] = (),
) -> VerifiedBatch:
    """Read the snapshot and the replayed records once each, verify them against `check`, and cross-validate them.

    `check` is the stored check that `require_current_check` accepted. Raises `Unverified` naming the first thing that
    does not agree. With `partial` an attempt that is still running, or whose final attempt has no replayed record, does
    not refuse; the second is left out and listed in `excluded`. Each of the declared `side_files` is read and paired
    with its record again, and its bytes are kept for the scorers (`VerifiedBatch.side_files_for`).
    """
    replayed = _replayed_set(check)
    merge = check.get(CHECK_KEY)
    shards: tuple = ()
    if merge is None:
        snapshots: dict[int | None, StoreSnapshot] = {None: _load_snapshot(batch, check, schema)}
    else:
        snapshots, shards = _load_shards(batch, merge, schema)
    try:
        return _cross_validated(batch, check, snapshots, shards, replayed, partial, side_files)
    except Unverified:
        for snapshot in snapshots.values():
            snapshot.close()
        raise


def _cross_validated(
    batch: Path,
    check: Mapping[str, Any],
    snapshots: Mapping[int | None, StoreSnapshot],
    shards: tuple,
    replayed: set[str],
    partial: bool,
    side_files: Sequence[SideFile],
) -> VerifiedBatch:
    """Read the replayed records once each and cross-validate them with the attempts of every snapshot."""
    raws = {rel: _read_verified(batch, rel, check["inputs"][rel]) for rel in sorted(replayed)}
    records = {rel: _parse(rel, raw) for rel, raw in raws.items()}
    held = _pair_side_files(batch, check, raws, side_files) if side_files else {}
    attempts = [a for snapshot in snapshots.values() for a in _attempts(snapshot)]
    expected: dict[str, Any] = {}
    for a in attempts:
        where = record_path(a["digest"], Path(a["run_dir"]).name, a["run_key"], a["attempt"])
        if where in expected:
            raise Unverified(f"{where}: more than one attempt of the campaign snapshots lies here")
        expected[where] = a
    running = sum(1 for a in attempts if a["state"] == "running")
    if running and not partial:
        raise Unverified(
            f"the replay check was made while {running} attempt(s) were still running; analysing it needs --partial"
        )
    for rel, record in records.items():
        row = expected.get(rel)
        if row is None:
            raise Unverified(f"{rel}: no attempt of the campaign snapshot has this record")
        if row["state"] == "done":
            _agree(row, record, rel)
    excluded = []
    for rel, row in expected.items():
        if row["state"] == "done" and rel not in records:
            if not partial:
                raise Unverified(
                    f"{rel}: attempt {row['attempt']} of run {row['run_key']!r} is final in the campaign snapshot but "
                    "no replayed record covers it; pass --partial to leave it out"
                )
            excluded.append(row["run_key"])
    return VerifiedBatch(dict(snapshots), records, running, tuple(sorted(set(excluded))), held, shards)
