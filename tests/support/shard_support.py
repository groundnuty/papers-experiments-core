"""Helpers for tests of merged shards: finished shard campaigns of the example study, made without the run side.

`run_shard` runs a stamped copy of the example with its plan filtered to one shard's runs, the way a driver of that
shard does: the filtered plan keeps each run's place in the whole plan, and the stub models run for real, so the
records, the store and the dispatch sequences are a shard's own. Only what the run side of #154 adds is written
here by hand: the shard's store meta (`META_*`), in the shapes the frozen interface names. `drop` leaves runs out of
the plan and `add` puts runs of another shard in, to make the shard sets the merge must refuse.
"""

import hashlib
import json
import sqlite3
from collections.abc import Collection, Mapping
from pathlib import Path

import pytest
from study_support import Campaign, cli, make_copy

from expcore.campaign import resume
from expcore.campaign.manifest import Manifest
from expcore.campaign.shard import (
    META_ENVIRONMENT,
    META_SHARD,
    META_SHARES,
    META_STARTED,
    ShardSpec,
    assign,
    in_shard,
    pair_of,
)
from expcore.study.config import StudyConfig, load_study

IDENTITY = {
    "layout": hashlib.sha256(b"a federation of two providers").hexdigest(),
    "platform": hashlib.sha256(b"a platform configuration").hexdigest(),
}
"""An environment identity, in the form `check_identity` accepts: names mapped to SHA-256 digests."""


def started(index: int) -> str:
    """A start time that differs from shard to shard."""
    return f"2026-03-0{index}T10:00:00+00:00"


def shard_meta(index: int, count: int, label: str, identity: Mapping[str, str] = IDENTITY) -> dict[str, str]:
    """The store meta a shard pins at its first start, in the shapes the frozen interface gives."""
    return {
        META_SHARD: json.dumps(ShardSpec(index, count, label).to_json(), sort_keys=True, separators=(",", ":")),
        META_STARTED: started(index),
        META_ENVIRONMENT: json.dumps(dict(identity), sort_keys=True, separators=(",", ":")),
        META_SHARES: json.dumps(
            {"unit_caps": {"alpha-stub": index}, "budget": {"max_usd": 1.0, "per_model_usd": {}}}, sort_keys=True
        ),
    }


def write_meta(store: Path, meta: Mapping[str, str]) -> None:
    """Write meta rows into a campaign store, replacing a row of the same key."""
    connection = sqlite3.connect(store)
    try:
        for key, value in meta.items():
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value, set_utc) VALUES(?,?,?)", (key, value, "2026-03-01T00:00:00Z")
            )
        connection.commit()
    finally:
        connection.close()


def plan_for(root: Path, count: int) -> tuple[Manifest, dict[tuple[str, str, int], int]]:
    """The whole plan of the study at `root`, and its assignment of every pair to one of `count` shards."""
    manifest = load_study(root).manifest
    return manifest, assign({pair_of(row.key.factors) for row in manifest.rows}, count)


def keys_of_shard(root: Path, index: int, count: int = 2) -> list[str]:
    """The run keys of shard `index`, in plan order."""
    manifest, assignment = plan_for(root, count)
    spec = ShardSpec(index, count, f"shard-{index}")
    return [row.key.text for row in manifest.rows if in_shard(row.key.factors, spec, assignment)]


def run_shard(
    base: Path,
    name: str,
    env: dict[str, str],
    index: int,
    *,
    count: int = 2,
    label: str | None = None,
    drop: Collection[str] = (),
    add: Collection[str] = (),
    identity: Mapping[str, str] = IDENTITY,
) -> Campaign:
    """Run shard `index` of `count` of the example study in a copy named `name` under `base`, and return it.

    The copy runs only its shard's runs, less those in `drop` and plus those in `add`, each at its place in the whole
    plan, then gets its shard meta. It is not checked: the merge does that.
    """
    root = make_copy(base, name)
    manifest, assignment = plan_for(root, count)
    spec = ShardSpec(index, count, label or f"shard-{index}")
    rows = tuple(
        row
        for row in manifest.rows
        if (in_shard(row.key.factors, spec, assignment) or row.key.text in add) and row.key.text not in drop
    )
    filtered = Manifest(manifest.order_seed, rows)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(StudyConfig, "manifest", property(lambda self: filtered))
        patch.setattr(resume, "plan_problem", lambda schema, manifest, fingerprint: None)
        code, errors = cli(env, "run", str(root))
    assert code == 0, errors
    write_meta(root / "out" / "state.sqlite", shard_meta(index, count, spec.label, identity))
    return Campaign(root)


def merge_into(env: dict[str, str], target: Campaign, *shards: Campaign | Path) -> tuple[int, str]:
    """Merge shard campaigns (or their out directories) into `target`'s out/, as the command line does."""
    outs = [str(shard / "out" if isinstance(shard, Path) else shard.root / "out") for shard in shards]
    return cli(env, "merge", str(target.root / "out"), *outs)
