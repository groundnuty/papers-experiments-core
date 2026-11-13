"""Helpers for tests of merged shards: finished shard campaigns of the example study, run for real.

`run_shard` runs a copy of the example as shard `i` of `N` with `python -m expcore run --shard i/N --shard-label L`, as
a host of a campaign does: the run side of #154 plans the shard's pairs at their places in the whole plan, runs them on
the shard's shares of the caps and the budget, and writes the shard's store meta and export itself. Nothing of a shard
is written here by hand. The one thing the example cannot do alone is declare an environment identity (it has none),
so `run_shard` has the plugin declare one, the way a study does.

`plant_run` and `without_run` plant a fault in a copy of a finished shard: a run of another shard in its store, or a
run of its own taken out, to make the shard sets the merge must refuse. `write_meta` changes one row of a store's meta
for the same purpose. Faults are planted in copies; the shards the tests merge are never touched.
"""

import dataclasses
import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from study_support import HEAD, Campaign, cli, copy_campaign, make_copy

from expcore.campaign.shard import RULE, RULE_BY_MODEL, ShardSpec, in_shard, pair_of, partition
from expcore.provenance.codestamp import write_stamp
from expcore.study import pipeline
from expcore.study.config import load_study

IDENTITY = {
    "layout": hashlib.sha256(b"a federation of two providers").hexdigest(),
    "platform": hashlib.sha256(b"a platform configuration").hexdigest(),
}
"""An environment identity, in the form `check_identity` accepts: names mapped to SHA-256 digests."""

UNIT_CAPS = ("alpha-stub = 1", "beta-stub = 1")

MERGED_ONLY = ("shard_retries_by_kind", "shards")
"""The top-level keys only a merged batch's export has, in their order at its end (C29): each shard's own
`retries_by_kind` rows tagged with the shard, which one driver has no shards to break down by, and merge.json's list."""

MERGE_TIMES = ("wall_s", "turn_latency_s")
"""What a merged export adds to each analysed entry (C29, `study.merged_times`): the attempt's wall time from its
shard's snapshot and its turns' latencies from its record. One driver's export has neither, and both are clock readings
no two drivers share; tests/study/test_merge.py checks them against the store and the record they come from."""

SHARD_SECTION = "## Shards"  # the report's section only a merged batch has (C29), after the conditions


def make_study(base: Path, name: str, extra: str = "", declare: Sequence[str] = ()) -> Path:
    """A stamped copy of the example whose unit caps (1 each) are 2, which two shards can share one of each, with
    `extra` appended to its campaign.toml (a `[shards]` table, say) and the models in `declare` given a set of
    accepted fingerprints (B18) in models.toml.

    Every copy of a test (the shards, the whole campaign they are compared with, the study they merge into) is made
    here, so they plan the same campaign: the caps are in the campaign's fingerprint.
    """
    root = make_copy(base, name)
    text = (root / "campaign.toml").read_text(encoding="utf-8")
    for cap in UNIT_CAPS:
        assert cap in text, f"the example no longer caps {cap.split(' ')[0]} at 1"
        text = text.replace(cap, cap.replace("= 1", "= 2"))
    (root / "campaign.toml").write_text(text + extra, encoding="utf-8")
    models = (root / "models.toml").read_text(encoding="utf-8")
    for model in declare:
        header = f"[models.{model}]\n"
        assert header in models, f"the example no longer has a model {model}"
        models = models.replace(header, header + f'accepted_fingerprints = ["fp-{model}"]\n')
    (root / "models.toml").write_text(models, encoding="utf-8")
    write_stamp(root, HEAD, load_study(root).scope)  # the study's files changed: stamp the copy again
    return root


def declaring(patch: pytest.MonkeyPatch, identity: Mapping[str, str]) -> None:
    """Make the plugin the pipeline loads declare `identity` as its environment identity (the example declares none)."""
    real = pipeline.load_plugin
    patch.setattr(
        pipeline,
        "load_plugin",
        lambda context: dataclasses.replace(real(context), environment_identity=lambda: dict(identity)),
    )


def run_shard(
    base: Path,
    name: str,
    env: dict[str, str],
    index: int,
    *,
    count: int = 2,
    label: str | None = None,
    identity: Mapping[str, str] = IDENTITY,
    extra: str = "",
    declare: Sequence[str] = (),
) -> Campaign:
    """Run shard `index` of `count` of the example study in a copy named `name` under `base`, and return it.

    It is run by the command line, `run --shard index/count --shard-label label`, to the end. It is not checked: the
    merge does that. `extra` is appended to the copy's campaign.toml, and `declare` names the models that declare
    accepted fingerprints, as `make_study` takes them.
    """
    root = make_study(base, name, extra, declare)
    with pytest.MonkeyPatch.context() as patch:
        declaring(patch, identity)
        code, errors = cli(
            env, "run", str(root), "--shard", f"{index}/{count}", "--shard-label", label or f"shard-{index}"
        )
    assert code == 0, errors
    return Campaign(root)


def keys_of_shard(root: Path, index: int, count: int = 2) -> list[str]:
    """The run keys of shard `index`, in plan order, under the study's rule: by model when it declares `[shards]`."""
    study = load_study(root)
    layout = study.campaign.shards
    spec = ShardSpec(index, count, f"shard-{index}", RULE if layout is None else RULE_BY_MODEL)
    assignment = partition({pair_of(row.key.factors) for row in study.manifest.rows}, spec, layout)
    return [row.key.text for row in study.manifest.rows if in_shard(row.key.factors, spec, assignment)]


def meta_of(campaign: Campaign) -> dict[str, str]:
    """A finished shard's store meta, as the run side wrote it."""
    return {row["key"]: row["value"] for row in campaign.sql("SELECT key, value FROM meta")}


def write_meta(store: Path, meta: Mapping[str, str]) -> None:
    """Write meta rows into a campaign store (a copy, to plant a fault in), replacing a row of the same key."""
    connection = sqlite3.connect(store)
    try:
        for key, value in meta.items():
            connection.execute(
                "INSERT OR REPLACE INTO meta(key, value, set_utc) VALUES(?,?,?)", (key, value, "2026-03-01T00:00:00Z")
            )
        connection.commit()
    finally:
        connection.close()


def plant_run(shard: Campaign, source: Campaign, key: str, base: Path, name: str) -> Campaign:
    """A copy of `shard` whose store also holds run `key` of `source`, a shard it does not belong to."""
    copy = copy_campaign(shard, base, name)
    donor = sqlite3.connect(source.root / "out" / "state.sqlite")
    target = sqlite3.connect(copy.root / "out" / "state.sqlite")
    try:
        row = donor.execute(
            "SELECT run_key, digest, seq, factors, status FROM runs WHERE run_key = ?", (key,)
        ).fetchone()
        assert row is not None, f"{key} is not a run of the shard it is taken from"
        target.execute("INSERT INTO runs(run_key, digest, seq, factors, status) VALUES(?,?,?,?,?)", row)
        target.commit()
    finally:
        donor.close()
        target.close()
    return copy


def without_run(shard: Campaign, key: str, base: Path, name: str) -> Campaign:
    """A copy of `shard` whose store holds no run `key`, nor any attempt at it."""
    copy = copy_campaign(shard, base, name)
    connection = sqlite3.connect(copy.root / "out" / "state.sqlite")
    try:
        connection.execute("DELETE FROM attempts WHERE run_key = ?", (key,))
        connection.execute("DELETE FROM runs WHERE run_key = ?", (key,))
        connection.commit()
    finally:
        connection.close()
    return copy


def merge_into(env: dict[str, str], target: Campaign, *shards: Campaign | Path) -> tuple[int, str]:
    """Merge shard campaigns (or their out directories) into `target`'s out/, as the command line does."""
    outs = [str(shard / "out" if isinstance(shard, Path) else shard.root / "out") for shard in shards]
    return cli(env, "merge", str(target.root / "out"), *outs)


def json_meta(campaign: Campaign, key: str) -> object:
    """One JSON row of a finished shard's store meta, parsed."""
    return json.loads(meta_of(campaign)[key])


def without_shard_section(report: str) -> str:
    """A merged batch's report without its `## Shards` section, from that heading up to the next `## ` one or the end:
    the one section only a merged batch's report has. Refuses a report without it."""
    head, heading, rest = report.partition(f"\n{SHARD_SECTION}\n")
    assert heading, "the report has no shards section"
    _, boundary, tail = rest.partition("\n## ")
    return head + boundary + tail
