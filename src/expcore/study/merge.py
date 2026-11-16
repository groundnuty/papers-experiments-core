# Provenance: new
"""`python -m expcore merge OUT SHARD_OUT...`: the outputs of a campaign's shards, merged into one batch (#154).

The study is the one above `OUT` (or `--study`), and `OUT` is normally its `out/`. The merge loads the study for the
whole plan, so that "every run belongs to its shard" and "no run is missing" are checked against it, and replays each
shard's records the way `check` does (`verify_batch`, under the code and environment in use), then writes the batch
(`campaign.merge`). Nothing is written to `OUT` unless every check passes. A shard's `out/` is only read: its store is
copied with the online backup, and its records are copied into the batch.

The one thing the merge may write into a shard is the finish of a kill, the way `check` settles it (R15, #304): a side
file left under its temporary name, which a record beside it names by its SHA-256, is renamed to its declared name
before the shard's records are replayed, so that a shard copied back from another host is merged, not refused as
missing a file. It removes nothing a record does not name: a partial file of a killed attempt stays in the shard, where
`check` removes it under the campaign's lock. A shard whose directory cannot be written is not written to: the merge
says what it left and, if the replay then fails for it, what to do (`_unsettled`).
"""

import os
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.merge import (
    RUNS_DIR,
    MergeRefused,
    ShardFacts,
    check_shards,
    lay_out,
    read_shard,
    refuse_non_empty,
)
from expcore.errors import study_kinds
from expcore.provenance.codestamp import require_known
from expcore.provenance.environment import differences
from expcore.replay.check import BatchNotVerified, verify_batch
from expcore.replay.side_file_placement import FINISHED, Settled, settle_side_files
from expcore.study.config import StudyConfig, find_study_root, load_study
from expcore.study.pipeline import (
    HASHED_ONLY,
    campaign_environment,
    import_what_the_records_need,
    study_environment,
)
from expcore.study.selection_start import pricing_of

_SHOWN_FAILURES = 3


@dataclass(frozen=True)
class Merged:
    """What a merge wrote: the batch's directory, its number of shards and of planned runs."""

    out: Path
    shards: int
    runs: int


def merge_shards(
    out: str | os.PathLike[str],
    shard_outs: Sequence[str | os.PathLike[str]],
    *,
    study_root: str | os.PathLike[str] | None = None,
    allow_dirty: bool = False,
    code_root: str | os.PathLike[str] | None = None,
) -> Merged:
    """Merge the shards whose `out/` directories are `shard_outs` into the batch `out`; see the module docstring.

    Raises MergeRefused, naming the shard and the reason, for the first check a shard fails.
    """
    out = Path(out)
    study = load_study(find_study_root(study_root if study_root is not None else out))
    with study_kinds(study.kinds):  # a replayed record may end in one of the study's own kinds
        return _merge(study, out, [Path(path) for path in shard_outs], allow_dirty, code_root)


def _merge(study: StudyConfig, out: Path, shard_outs: list[Path], allow_dirty: bool, code_root: Any) -> Merged:
    refuse_non_empty(out)
    code = require_known("merge", code_root or study.root, study.scope, allow_dirty=allow_dirty)
    env = study_environment(study)
    out.parent.mkdir(parents=True, exist_ok=True)
    stage = out.parent / f".{out.name}.merge-{os.getpid()}"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    try:
        pricing = pricing_of(study)  # what a selection's spend is counted with (C38); a shard's is not read
        facts = [read_shard(path, stage / f"read-{n}.sqlite", pricing) for n, path in enumerate(shard_outs)]
        shards = check_shards(facts, study.manifest, pricing)
        for shard in shards:
            _replay(study, shard, code, env)
        lay_out(stage, shards)
        try:
            os.replace(stage, out)
        except OSError as error:
            raise MergeRefused(f"{out}: cannot be written ({type(error).__name__}: {error})") from None
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return Merged(out, len(shards), sum(len(shard.runs) for shard in shards))


def _replay(study: StudyConfig, shard: ShardFacts, code: Any, env: Any) -> None:
    """5: the shard's own records pass the replay check `check` makes, under the environment its campaign ran in.

    The shard's side files are settled first, as `check` does (`settle_side_files`, R15, #304), but never removed: the
    merge holds no campaign lock, and without it a file no record names may be an attempt's own, staging. A rename
    changes the listing of the shard's `runs/`, so a `replay-check.json` the shard already holds stops vouching for it;
    the merge reads no check of a shard (`lay_out` leaves it behind), and the merged batch is checked as a whole.
    """
    changed = differences(campaign_environment(shard.fingerprint), env)
    if changed:
        raise MergeRefused(
            f"{shard.name}: its campaign ran under another environment than this merge would; merge under the "
            "campaign's environment:\n  " + "\n  ".join(changed)
        )
    batch = shard.out / RUNS_DIR
    if not batch.is_dir():
        raise MergeRefused(f"{shard.name}: no {RUNS_DIR}/ directory of records in its out/")
    import_what_the_records_need(study, batch)
    left = _settle(study, shard, batch)
    try:
        check = verify_batch(
            batch, code=code, hashed_only=list(HASHED_ONLY), environment=env, side_files=study.side_files
        )
    except BatchNotVerified as error:
        raise MergeRefused(f"{shard.name}: {error}") from None
    failures = check["failures"]
    if failures:
        shown = "; ".join(f"{f['file']} [{f['stage']}] {f['error']}" for f in failures[:_SHOWN_FAILURES])
        more = " and more" if len(failures) > _SHOWN_FAILURES else ""
        raise MergeRefused(
            f"{shard.name}: {len(failures)} of {check['n_records']} records fail the replay check: {shown}{more}"
            f"{_unsettled(left)}"
        )


def _settle(study: StudyConfig, shard: ShardFacts, batch: Path) -> list[Settled]:
    """Finish what a kill left of the placement of the shard's declared side files, say what was done on stderr, and
    return what could not be done because the shard's directory cannot be written."""
    settled = settle_side_files(batch, study.side_files)
    for done in settled:
        print(f"merge: {shard.name}: {done}", file=sys.stderr)
    return [done for done in settled if done.left]


def _unsettled(left: Sequence[Settled]) -> str:
    """What the refusal of a shard adds when a rename it could not make may be the cause: the files and the fix.

    A removal left undone (a redundant copy of a file in place) refuses nothing, so only a rename is named."""
    renames = [done.path for done in left if done.action == FINISHED]
    if not renames:
        return ""
    listed = ", ".join(renames[:_SHOWN_FAILURES]) + (" and more" if len(renames) > _SHOWN_FAILURES else "")
    return (
        f"; the merge did not settle {len(renames)} side file(s) that a kill left under a temporary name a record "
        f"names ({listed}), because the shard's out/ is read-only: make the shard's out/ writable (chmod -R u+w on "
        "it), or copy it to a disk that is, and merge again"
    )
