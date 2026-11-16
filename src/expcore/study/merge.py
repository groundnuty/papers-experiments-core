# Provenance: new
"""`python -m expcore merge OUT SHARD_OUT...`: the outputs of a campaign's shards, merged into one batch (#154).

The study is the one above `OUT` (or `--study`), and `OUT` is normally its `out/`. The merge loads the study for the
whole plan, so that "every run belongs to its shard" and "no run is missing" are checked against it, and replays each
shard's records the way `check` does (`verify_batch`, under the code and environment in use), then writes the batch
(`campaign.merge`). Nothing is written to `OUT` unless every check passes. A shard's `out/` is only read: its store is
copied with the online backup, and its records are copied into a private staging directory, inside the directory
beside `OUT` that the merge writes the batch in, which nothing but this merge can write to.

**The records are replayed in that copy, never in the shard** (R15, #304). The merge settles the copy's declared side
files the way `check` settles a batch (`settle_side_files`): a file a kill left under its temporary name, which the one
record beside it names by its SHA-256, is renamed to its declared name, and a partial file no record names is removed
(no attempt can be writing in a copy that only this merge holds). So a shard copied back from another host, read-only
as it may be, is merged and not refused as missing a file, and it keeps every byte it had, the partial files included,
as the evidence they are. The batch is laid out from the copy that was verified.
"""

import dataclasses
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
    copy_runs,
    lay_out,
    make_writable,
    read_shard,
    refuse_non_empty,
)
from expcore.errors import study_kinds
from expcore.provenance.codestamp import require_known
from expcore.provenance.environment import differences
from expcore.replay.check import BatchNotVerified, verify_batch
from expcore.replay.side_file_placement import settle_side_files
from expcore.study.config import StudyConfig, find_study_root, load_study
from expcore.study.pipeline import (
    HASHED_ONLY,
    campaign_environment,
    import_what_the_records_need,
    study_environment,
)
from expcore.study.selection_start import pricing_of

_SHOWN_FAILURES = 3
REPLAY_DIR = "replay"  # the private copies of the shards' runs/, in the merge's staging directory


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
        copies = stage / REPLAY_DIR
        copies.mkdir(mode=0o700)
        staged = [_replay(study, shard, code, env, copies / str(n)) for n, shard in enumerate(shards)]
        lay_out(stage, staged, move=True)  # what was verified is what is laid out: a rename, never a second copy
        shutil.rmtree(copies)
        try:
            os.replace(stage, out)
        except OSError as error:
            raise MergeRefused(f"{out}: cannot be written ({type(error).__name__}: {error})") from None
    finally:
        if stage.exists():  # a halfway copy of a read-only shard has directories no removal can enter
            make_writable(stage)
        shutil.rmtree(stage, ignore_errors=True)
    return Merged(out, len(shards), sum(len(shard.runs) for shard in shards))


def _replay(study: StudyConfig, shard: ShardFacts, code: Any, env: Any, copy: Path) -> ShardFacts:
    """5: the shard's own records pass the replay check `check` makes, under the environment its campaign ran in.

    The check runs on a private copy of the shard's `runs/` (`copy` is its directory, whose `runs/` is made here), after
    its side files are settled there as `check` settles a batch (R15, #304), so the shard itself is never written, and
    what is verified is what `lay_out` moves into the merged batch. Returns the shard with `out` set to that copy. A
    rename changes the listing of a `runs/`, so a `replay-check.json` the shard holds would stop vouching for it; the
    merge leaves it behind (`copy_runs`) and the merged batch is checked as a whole.
    """
    changed = differences(campaign_environment(shard.fingerprint), env)
    if changed:
        raise MergeRefused(
            f"{shard.name}: its campaign ran under another environment than this merge would; merge under the "
            "campaign's environment:\n  " + "\n  ".join(changed)
        )
    source = shard.out / RUNS_DIR
    if not source.is_dir():
        raise MergeRefused(f"{shard.name}: no {RUNS_DIR}/ directory of records in its out/")
    batch = copy / RUNS_DIR
    try:
        copy_runs(source, batch)
    except OSError as error:  # shutil.Error too: a file that cannot be read cannot be replayed either
        reason = getattr(error, "strerror", None) or type(error).__name__
        raise MergeRefused(f"{shard.name}: its {RUNS_DIR}/ cannot be copied to be replayed ({reason})") from None
    import_what_the_records_need(study, batch)
    _settle(study, shard, batch)
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
        )
    return dataclasses.replace(shard, out=copy)


def _settle(study: StudyConfig, shard: ShardFacts, batch: Path) -> None:
    """Finish or remove what a kill left of the placement of the shard's declared side files, in the merge's copy, and
    say so on stderr.

    The copy is private, so no attempt can be staging a file in it: a partial file no record names is removed from the
    copy, as `check` removes it under the campaign's lock (`remove_unnamed`), and the shard keeps it. A file that could
    not be settled (the staging directory beside `OUT` cannot be written) refuses the merge, naming it.
    """
    settled = settle_side_files(batch, study.side_files, remove_unnamed=True)
    for done in settled:
        if not done.left:
            note = "in the merge's copy: the shard's out/ is not written"
            print(f"merge: {shard.name}: {done} ({note})", file=sys.stderr)
    stuck = [done.path for done in settled if done.left]
    if stuck:
        listed = ", ".join(stuck[:_SHOWN_FAILURES]) + (" and more" if len(stuck) > _SHOWN_FAILURES else "")
        raise MergeRefused(
            f"{shard.name}: {len(stuck)} side file(s) that a kill left under a temporary name cannot be settled "
            f"({listed}): the staging directory the merge copies into, beside OUT, cannot be written"
        )
