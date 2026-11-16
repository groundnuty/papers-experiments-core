# Provenance: new
"""Merge the outputs of a campaign's shards into one batch (#154): what a shard says, the checks, and the layout.

`python -m expcore merge OUT SHARD_OUT...` takes each shard's `out/` and writes one batch under `OUT`:

    OUT/runs/                                   the union of the shards' runs (`<digest>/try<N>/...`), as one batch
    OUT/shards/<index>/campaign-snapshot.sqlite each shard's snapshot of its store, as the merge took it
    OUT/merge.json                              `expcore-merge/1`: what was merged, and each snapshot's SHA-256

A shard's snapshot is made from its store with the online backup (`take_snapshot`), so a shard with attempts still
running is refused, and everything the checks read is read from the copy the batch keeps. A shard's `out/` is only
read: its store is backed up from a private copy of its files, under a reader's hold of its lock, so nothing is
written beside it, a read-only `out/` is merged as it is, and a shard a dispatcher still holds is refused (R16). Its
records are read from a private copy too (`copy_runs`, #304): the replay check settles what a kill left of a declared
side file in that copy (R15), never in the shard, and the batch is laid out from the copy that was verified.

**What a merge refuses, naming the shard and the reason (C29).** The checks run in the order that costs least, the
replay check (`study.merge`) last:

1. every shard is sharded, under the same rule and count, with indices exactly 1..N and distinct labels;
2. every shard has the same campaign fingerprint, which a start pins whole, so the same experiment;
3. the shards' run sets are disjoint, and every run belongs to its shard under their rule's assignment of the whole
   plan (`assign()`, or by model with the `[shards]` layout of their fingerprint, C32);
4. the union of the run sets is exactly the plan, so no run is missing and none is extra;
5. each shard passes the replay check `python -m expcore check` performs on a batch;
6. every shard records the same environment identity.

A shard whose `out/` holds `NOT-FINAL.json` (`shard.NOT_FINAL_FILE`) is refused first of all, naming it and the reason
the file gives: a driver that checks a shard's results before archiving them, as the HPC shard driver's post-run script
does (O63), marks them so when the check did not pass.

A campaign run in **selections** (`expcore-shard/4`, C38) is merged by the same command, with the checks of
`campaign.merge_selection` in place of 1, 3 and 4: the selections are disjoint and cover the plan, sequential ones ran
one after another, and their spend together stayed within the budget.

**merge.json** (`MERGE_FILE`, schema `MERGE_SCHEMA`) holds `fingerprint` (the SHA-256 of the campaign's fingerprint,
which every shard shares), `rule`, `count`, `environment_identity` and `shards`, each `{index, label, started_utc,
shares, runs, snapshot_sha256}` in index order, `runs` being how many runs the shard planned. It names no host or path:
a batch is published in a package. `check` binds the file by its SHA-256 and checks each snapshot against it, and the
analysis reads each snapshot once, compared with the same hash (`study.verified`). Neither trusts what the file says:
each rebuilds it from the snapshots and the study's plan, through the checks above, and refuses a file that differs
(`campaign.merged`).

The merge writes into a staging directory beside `OUT` and renames it into place, so a refused or killed merge leaves
`OUT` as it was, and `OUT` must not exist or must be empty.
"""

import dataclasses
import hashlib
import json
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.lock import CampaignLocked, LockMissing, LockUnavailable, ReaderLock
from expcore.campaign.manifest import Manifest, compare_fingerprints, fingerprint_sha256
from expcore.campaign.resume import FINGERPRINT_KEY, recorded_fingerprint
from expcore.campaign.scheduler import STATE_FILE
from expcore.campaign.selection import RULE_SELECTION, SelectionSpec, Spec, spec_from_json
from expcore.campaign.selection_ledger import Pricing, Spend, released_of, spend_of, window_of
from expcore.campaign.shard import (
    MERGE_FILE,
    MERGE_SCHEMA,
    META_ENVIRONMENT,
    META_SHARD,
    META_SHARES,
    META_STARTED,
    NOT_FINAL_FILE,
    ShardError,
    check_identity,
    in_shard,
    layout_of,
    pair_of,
    partition,
)
from expcore.campaign.snapshot import SNAPSHOT_FILE, AttemptsRunning, SnapshotError, take_snapshot

SHARDS_DIR = "shards"
RUNS_DIR = "runs"
CHECK_KEY = "merge"  # the key of the replay check that says which merge.json it was made under
_SKIPPED = frozenset({"replay-check.json", SNAPSHOT_FILE})  # a shard's own check of its batch: not part of the union
_SHARD_KEYS = {"index", "label", "started_utc", "shares", "runs", "snapshot_sha256"}
_SELECTION_KEYS = {"selection", "sequential", "window"}  # what a selection's entry has beyond a shard's (C38)
_DOCUMENT_KEYS = {"schema", "fingerprint", "rule", "count", "environment_identity", "shards"}
Query = Callable[..., Iterable[Sequence[Any]]]  # query(sql, args=()) on a store, or a snapshot of one: the rows


class MergeRefused(RuntimeError):
    """A merge, or a merged batch, is not usable; the message names the shard and the reason."""


@dataclass(frozen=True)
class ShardFacts:
    """What a shard's snapshot says of itself, read once from the copy the merge took."""

    out: Path
    snapshot: Path
    snapshot_sha256: str
    spec: Spec
    started_utc: str
    environment: Mapping[str, str]
    shares: Any
    fingerprint: Mapping[str, Any]
    runs: Mapping[str, Mapping[str, Any]]  # the factors of each run the shard's store holds, by run key
    window: tuple[str, str] | None = None  # a selection's: its first start and its last attempt's time (C38)
    released: Mapping[str, float] = dataclasses.field(default_factory=dict)  # a selection's releases, by scope
    spend: Spend | None = None  # a selection's spend, when the reader was given the study's prices

    @property
    def name(self) -> str:
        """How a message names the shard: its index and the operator's label, never its path; a selection by its
        label and levels."""
        if isinstance(self.spec, SelectionSpec):
            return self.spec.name
        return f"shard {self.spec.index} ({self.spec.label})"


def shard_snapshot_path(out: Path, index: int) -> Path:
    """Where the batch under `out` keeps shard `index`'s snapshot."""
    return out / SHARDS_DIR / str(index) / SNAPSHOT_FILE


def sha256_of(path: Path) -> str:
    """The SHA-256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _meta(query: Query, where: object) -> dict[str, str]:
    try:
        return {row[0]: row[1] for row in query("SELECT key, value FROM meta")}
    except sqlite3.Error as error:
        raise MergeRefused(f"{where}: its store has no meta table ({type(error).__name__}: {error})") from None


def _json(meta: Mapping[str, str], key: str, where: object) -> Any:
    """A meta value that is JSON; refused, naming the key, when the store lacks it or it is not JSON."""
    if key not in meta:
        raise MergeRefused(f"{where}: its store records no {key!r}, so it did not run as a shard of this core")
    try:
        return json.loads(meta[key])
    except ValueError:
        raise MergeRefused(f"{where}: {key!r} in its store is not JSON") from None


def read_shard(out: Path, scratch: Path, pricing: Pricing | None = None) -> ShardFacts:
    """Copy the store of the shard whose `out/` is `out` to `scratch` and read what the merge needs from the copy.

    The shard's `out/` is only read, and may be read-only (R16): its lock is held as a reader (`ReaderLock`, which
    never makes the file) while its store is read from a private copy (`take_snapshot`). Refuses a shard a dispatcher
    still holds, one whose lock file is missing (copy the shard's `out/` whole) or cannot be opened or locked, a store
    with attempts still running or that changes while it is copied, and a store that is not a shard's (`facts_of`).
    """
    store = out / STATE_FILE
    try:
        with ReaderLock(out):
            take_snapshot(store, scratch)
    except CampaignLocked as error:
        raise MergeRefused(f"{out}: {error}; a shard is merged when it has finished") from None
    except LockMissing as error:
        raise MergeRefused(f"{out}: {error}; copy the shard's out/ whole, with its lock file") from None
    except LockUnavailable as error:
        raise MergeRefused(f"{out}: {error}; the shard cannot be shown to have finished") from None
    except AttemptsRunning as error:
        raise MergeRefused(f"{out}: {error}; a shard is merged when it has finished") from None
    except SnapshotError as error:
        raise MergeRefused(str(error)) from None
    facts = facts_of_bytes(scratch.read_bytes(), out, out=out, snapshot=scratch, pricing=pricing)
    if os.path.lexists(out / NOT_FINAL_FILE):  # a driver's check of the shard's results did not pass (it says why)
        raise MergeRefused(f"{facts.name}: its results are marked not final ({_not_final_reason(out)})")
    return facts


def _not_final_reason(out: Path) -> str:
    """The reason the shard's `NOT-FINAL.json` gives, for a message; a fixed phrase when it cannot be read."""
    try:
        reason = json.loads((out / NOT_FINAL_FILE).read_text(encoding="utf-8")).get("reason")
    except (OSError, ValueError, AttributeError):
        reason = None
    return reason if isinstance(reason, str) and reason else f"{NOT_FINAL_FILE} says so"


def facts_of_bytes(
    data: bytes, where: object, *, out: Path, snapshot: Path, pricing: Pricing | None = None
) -> ShardFacts:
    """What the shard store whose bytes are `data` says of itself (`facts_of`), with the SHA-256 of those bytes."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    try:
        try:
            db.deserialize(data)
        except sqlite3.Error as error:
            raise MergeRefused(f"{where}: not a campaign store ({type(error).__name__}: {error})") from None
        query: Query = lambda sql, args=(): db.execute(sql, tuple(args)).fetchall()  # noqa: E731
        sha256 = hashlib.sha256(data).hexdigest()
        return facts_of(query, where, out=out, snapshot=snapshot, sha256=sha256, pricing=pricing)
    finally:
        db.close()


def facts_of(
    query: Query, where: object, *, out: Path, snapshot: Path, sha256: str, pricing: Pricing | None = None
) -> ShardFacts:
    """What a shard's store says of itself, read through `query` (SQL in, rows out); `where` names it in a message.

    The merge reads a shard's store this way, and so do the check and the analysis of a merged batch, each from the
    snapshot it verified, to rebuild merge.json (`campaign.merged`). Refuses a store that is not a shard's: no spec,
    start, environment identity or shares, or a fingerprint it does not record. A selection's store also gives its
    window and its releases, and its spend when `pricing` is given (`campaign.selection_ledger`).
    """
    try:
        meta = _meta(query, where)
        if META_SHARD not in meta:
            raise MergeRefused(f"{where}: not a shard: its store records no {META_SHARD!r}, so it ran unsharded")
        try:
            spec = spec_from_json(_json(meta, META_SHARD, where))
        except ShardError as error:
            raise MergeRefused(f"{where}: the shard spec in its store is unusable: {error}") from None
        started = meta.get(META_STARTED)
        if not isinstance(started, str) or not started:
            raise MergeRefused(f"{where}: its store records no {META_STARTED!r}")
        try:
            environment = check_identity(_json(meta, META_ENVIRONMENT, where))
        except ShardError as error:
            raise MergeRefused(f"{where}: the environment identity in its store is unusable: {error}") from None
        shares = _json(meta, META_SHARES, where)
        fingerprint = recorded_fingerprint([(meta[FINGERPRINT_KEY],)] if FINGERPRINT_KEY in meta else [])
        if fingerprint is None:
            raise MergeRefused(f"{where}: its store records no campaign fingerprint")
        runs = {row[0]: json.loads(row[1]) for row in query("SELECT run_key, factors FROM runs")}
        if not isinstance(spec, SelectionSpec):
            return ShardFacts(out, snapshot, sha256, spec, started, environment, shares, fingerprint, runs)
        window, released = window_of(query), released_of(query)
        spend = None if pricing is None else spend_of(query, pricing)
    except sqlite3.Error as error:
        raise MergeRefused(f"{where}: cannot read its store ({type(error).__name__}: {error})") from None
    return ShardFacts(
        out, snapshot, sha256, spec, started, environment, shares, fingerprint, runs, window, released, spend
    )


# --- the checks that need only what the snapshots say --------------------------------------------------------------


def check_shards(shards: Sequence[ShardFacts], plan: Manifest, pricing: Pricing | None = None) -> list[ShardFacts]:
    """Run checks 1 to 4 and 6 over the shards, return them in index order; raise MergeRefused for the first fault.

    Selections are checked by `merge_selection.check_selections`, and numbered there; `pricing` is what their spend
    is counted with, and without it their totals are not checked."""
    if any(isinstance(shard.spec, SelectionSpec) for shard in shards):
        from expcore.campaign.merge_selection import check_selections  # it builds on this module's checks

        return check_selections(shards, plan, pricing)
    _check_specs(shards)
    ordered = sorted(shards, key=lambda shard: shard.spec.index)
    check_fingerprints(ordered)
    _check_runs(ordered, plan)
    check_environment(ordered)
    return ordered


def _check_specs(shards: Sequence[ShardFacts]) -> None:
    """1: one rule and count, indices exactly 1..N each once, labels distinct."""
    first = shards[0]
    for shard in shards:
        if shard.spec.rule != first.spec.rule:
            raise MergeRefused(
                f"{shard.name}: partitions by rule {shard.spec.rule!r}, {first.name} by {first.spec.rule!r}"
            )
        if shard.spec.count != first.spec.count:
            raise MergeRefused(
                f"{shard.name}: its campaign was split into {shard.spec.count} shards, "
                f"but {first.name}'s into {first.spec.count}"
            )
    count = first.spec.count
    held: dict[int, ShardFacts] = {}
    for shard in sorted(shards, key=lambda s: (s.spec.index, s.spec.label)):
        if shard.spec.index in held:
            raise MergeRefused(
                f"{shard.name} and {held[shard.spec.index].name} are both shard {shard.spec.index} of {count}; "
                "give each shard's out/ once"
            )
        held[shard.spec.index] = shard
    missing = [index for index in range(1, count + 1) if index not in held]
    if missing:
        raise MergeRefused(
            f"the campaign was split into {count} shards and {len(shards)} were given: shard {missing[0]} of {count} "
            "is missing"
        )
    labels: dict[str, ShardFacts] = {}
    for index in sorted(held):
        shard = held[index]
        if shard.spec.label in labels:
            raise MergeRefused(f"{shard.name} and {labels[shard.spec.label].name} have the same label")
        labels[shard.spec.label] = shard


def check_fingerprints(shards: Sequence[ShardFacts]) -> None:
    """2: every shard has the campaign fingerprint of the first."""
    first = shards[0]
    for shard in shards[1:]:
        if shard.fingerprint != first.fingerprint:
            comparison = compare_fingerprints(first.fingerprint, shard.fingerprint)
            lines = [*comparison.refused, *(f"{item}: only in this shard's" for item in comparison.grown)]
            shown = "\n  ".join(lines[:6]) + ("\n  and more" if len(lines) > 6 else "")
            raise MergeRefused(
                f"{shard.name}: ran another campaign than {first.name}; its fingerprint differs:\n  {shown}"
            )


def _check_runs(shards: Sequence[ShardFacts], plan: Manifest) -> None:
    """3 and 4: disjoint run sets, each run in its own shard, and together exactly the plan."""
    fingerprint = shards[0].fingerprint  # every shard's, after check 2
    holder: dict[str, ShardFacts] = {}
    for shard in shards:
        for key in sorted(shard.runs):
            if key in holder:
                raise MergeRefused(f"run {key} is in both {holder[key].name} and {shard.name}: the shards overlap")
            holder[key] = shard
    try:  # the rule the shards ran under (check 1 made it one), by model with the layout their fingerprint declares
        assignment = partition({pair_of(row.key.factors) for row in plan.rows}, shards[0].spec, layout_of(fingerprint))
    except ShardError as error:
        raise MergeRefused(f"{shards[0].name}: its shards cannot be assigned the plan's pairs: {error}") from None
    for shard in shards:
        for key, factors in sorted(shard.runs.items()):
            if pair_of(factors) not in assignment:
                raise MergeRefused(f"{shard.name}: run {key} is not in the plan")
            if not in_shard(factors, shard.spec, assignment):
                owner = shards[assignment[pair_of(factors)] - 1]
                raise MergeRefused(f"{shard.name}: run {key} belongs to {owner.name}, not to this shard")
    planned = {row.key.text: row for row in plan.rows}
    extra = sorted(set(holder) - set(planned))
    if extra:
        raise MergeRefused(f"{holder[extra[0]].name}: run {extra[0]} is not in the plan ({len(extra)} such runs)")
    missing = sorted(set(planned) - set(holder))
    if missing:
        owner = shards[assignment[pair_of(planned[missing[0]].key.factors)] - 1]
        raise MergeRefused(
            f"run {missing[0]}, which belongs to {owner.name}, is in no shard "
            f"({len(missing)} of the plan's {len(planned)} runs are missing)"
        )


def check_environment(shards: Sequence[ShardFacts]) -> None:
    """6: every shard records the environment identity of the first."""
    first = shards[0]
    for shard in shards[1:]:
        if shard.environment != first.environment:
            names = sorted(
                name
                for name in set(shard.environment) | set(first.environment)
                if shard.environment.get(name) != first.environment.get(name)
            )
            raise MergeRefused(
                f"{shard.name}: ran against another environment than {first.name}; "
                f"its environment identity differs in {', '.join(names)}"
            )


# --- the layout -----------------------------------------------------------------------------------------------------


def merge_document(shards: Sequence[ShardFacts]) -> dict[str, Any]:
    """The content of merge.json for shards that passed `check_shards`, in index order; selections are numbered here
    as the merge numbers them, and each entry also holds the selection, its mode and its window (C38)."""
    selections = any(isinstance(shard.spec, SelectionSpec) for shard in shards)
    if selections:
        from expcore.campaign.merge_selection import numbered  # it builds on this module's checks

        shards = numbered(shards)
    first = shards[0]
    return {
        "schema": MERGE_SCHEMA,
        "fingerprint": fingerprint_sha256(first.fingerprint),
        "rule": first.spec.rule,
        "count": first.spec.count,
        "environment_identity": dict(first.environment),
        "shards": [
            {
                "index": shard.spec.index,
                "label": shard.spec.label,
                "started_utc": shard.started_utc,
                "shares": shard.shares,
                "runs": len(shard.runs),
                "snapshot_sha256": shard.snapshot_sha256,
                **(_selection_entry(shard) if selections else {}),
            }
            for shard in shards
        ],
    }


def _selection_entry(shard: ShardFacts) -> dict[str, Any]:
    """What a selection's entry in merge.json holds beyond a shard's: its levels, its mode and its window."""
    spec = shard.spec
    assert isinstance(spec, SelectionSpec)  # merge_document asks only for a selection's
    return {
        "selection": spec.selection.to_json(),
        "sequential": spec.sequential,
        "window": None if shard.window is None else list(shard.window),
    }


def merge_text(document: Mapping[str, Any]) -> str:
    """merge.json's text: sorted keys, indented, ending with a newline."""
    return json.dumps(document, indent=1, sort_keys=True) + "\n"


def copy_runs(source: Path, target: Path) -> None:
    """Copy the batch of a shard (its `runs/`) into `target`, a private copy that the merge may write in (#304).

    A shard's own `replay-check.json` and snapshot (`_SKIPPED`, the files of those names directly in `runs/`) are left
    behind: they are not part of the union. A directory of one of those names is copied and replayed like any other
    (`lay_out` then leaves it out of the batch). Every directory of the copy is writable whatever the shard's modes
    are, so a shard fetched read-only gives a copy that can be settled and removed. Raises OSError (`shutil.Error`
    included) for a file that cannot be read; a copy that failed halfway is the caller's to remove (`make_writable`,
    then `shutil.rmtree`).
    """

    def left_behind(directory: str, names: list[str]) -> list[str]:
        if Path(directory) != source:
            return []
        return [name for name in names if name in _SKIPPED and os.path.isfile(os.path.join(directory, name))]

    # links are copied as links (`symlinks=True`), never followed: a link in a shard is evidence the replay check
    # must see and refuse (R04), and following one would copy what it points at, outside the shard, into the batch
    shutil.copytree(source, target, ignore=left_behind, symlinks=True)
    make_writable(target)


def make_writable(root: Path) -> None:
    """Give the owner access to every directory below `root` (and `root`), so that it can be written and removed."""
    for directory, _, _ in os.walk(root):
        mode = os.stat(directory).st_mode
        if mode & 0o700 != 0o700:
            os.chmod(directory, mode | 0o700)


def lay_out(stage: Path, shards: Sequence[ShardFacts], *, move: bool = False) -> dict[str, Any]:
    """Write the merged batch into the empty directory `stage` and return merge.json's content.

    The runs are the shards' `runs/<digest>` directories, copied, or moved with `move` when each shard's `out/` is a
    private copy the merge made (`copy_runs`, which a rename out of it does not duplicate); two shards holding one
    directory is refused. A shard's own `replay-check.json` and snapshot are left behind: the merged batch is checked
    as a whole.
    """
    runs = stage / RUNS_DIR
    runs.mkdir(parents=True)
    for shard in shards:
        source = shard.out / RUNS_DIR
        if not source.is_dir():
            raise MergeRefused(f"{shard.name}: no {RUNS_DIR}/ directory of records in its out/")
        for entry in sorted(source.iterdir()):
            if not entry.is_dir() or entry.name.startswith(".") or entry.name in _SKIPPED:
                continue
            if (runs / entry.name).exists():
                raise MergeRefused(f"{shard.name}: {RUNS_DIR}/{entry.name} is already there from another shard")
            if move:
                os.replace(entry, runs / entry.name)
            else:
                shutil.copytree(entry, runs / entry.name)
        target = shard_snapshot_path(stage, shard.spec.index)
        target.parent.mkdir(parents=True)
        os.replace(shard.snapshot, target)
    document = merge_document(shards)
    (stage / MERGE_FILE).write_text(merge_text(document), encoding="utf-8")
    return document


# --- reading a merged batch back ------------------------------------------------------------------------------------


def read_merge(raw: bytes, where: object = MERGE_FILE) -> dict[str, Any]:
    """Parse and validate the bytes of a merge.json; raise MergeRefused naming what is wrong."""
    try:
        document = json.loads(raw)
    except ValueError:
        raise MergeRefused(f"{where}: not JSON") from None
    if not isinstance(document, dict) or document.get("schema") != MERGE_SCHEMA or set(document) != _DOCUMENT_KEYS:
        raise MergeRefused(f"{where}: not an {MERGE_SCHEMA} merge file")
    shards = document["shards"]
    count = document["count"]
    ok = isinstance(shards, list) and isinstance(count, int) and not isinstance(count, bool) and count >= 2
    keys = _SHARD_KEYS | (_SELECTION_KEYS if document["rule"] == RULE_SELECTION else set())
    ok = ok and all(isinstance(s, dict) and set(s) == keys for s in shards)
    ok = ok and [s["index"] for s in shards] == list(range(1, count + 1))
    ok = ok and all(isinstance(s["snapshot_sha256"], str) and len(s["snapshot_sha256"]) == 64 for s in shards)
    if not ok:
        raise MergeRefused(
            f"{where}: its list of shards is not the shards 1 to {count!r}, at least 2, each with its snapshot hash"
        )
    return document


def refuse_non_empty(out: Path) -> None:
    """A merge writes a new batch: `out` must not exist, or be an empty directory."""
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise MergeRefused(f"{out}: a merge writes a new batch, so this must not exist or must be an empty directory")
