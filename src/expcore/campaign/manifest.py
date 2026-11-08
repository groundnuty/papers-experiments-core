# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/manifest.py, blob aac5bf109fa04b7a080706e27db2833d118bd18f
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The run manifest: one row per (block, instance, option, layout, model, seed),
in a fixed randomised order (#16)."""
from __future__ import annotations

import collections
import dataclasses
import fractions
import itertools
import random
from typing import Sequence

from dataset_76.records import Instance, RunId

from .config import BlockSpec, CampaignConfig, stage_label


# Amendments 27(b) and 29(b): the order is seed-first, and no launch setting changes it. The registered order stays in the code for the tests that are about it
# (they set this False); nothing in a real launch does.
SEED_FIRST = True

# Amendments 39 and 40: in the LOWEST seed among the staged groups, these groups are ONE stage, merged per model by the fraction of each list taken so far (taken over planned),
# a tie to the earlier list in this tuple: block 1 (main S1 and the pagination cell), then tier A, then tier B. Each list keeps its own seeded order; every run keeps its own
# group, so the caps and the reports by group are unchanged. The tuple's order is the tie order. A later seed, and any group not named here, is emitted as before. Naming fewer than two
# groups present in that seed changes nothing, and `()` is amendment 27(b)'s stage-by-stage order. No launch setting touches it: this is the registered order.
MERGED_SEED1_GROUPS: tuple[str, ...] = ("condition_e", "condition_f", "condition_g")


@dataclasses.dataclass(frozen=True)
class ManifestRow:
    seq: int
    block: str
    run: RunId
    exclude_from_analysis: bool = False
    group: str = ""   # priority group (BlockSpec.interleave); groups run in manifest order
    stage: str = ""   # seed-first: the run's priority group within its seed; "" for the pilot, calibration and the registered order

    @property
    def run_key(self) -> str:
        return f"{self.block}__{self.run.key()}"


def block_rows(spec: BlockSpec, instances: Sequence[Instance]) -> list[tuple[str, RunId]]:
    insts = list(instances)
    if spec.instances == "pilot":
        insts = [i for i in insts if i.pilot]
    elif spec.instances == "condition_h":
        # the calibration instance is the one #5 flags, never "the first pilot instance":
        # instance order says nothing about which pilot instance meets the rule
        flagged = [i for i in insts if i.calibration]
        if len(flagged) != 1:
            raise ValueError(f"expected exactly one instance flagged calibration, got "
                             f"{[i.instance_id for i in flagged]}")
        if not flagged[0].pilot:
            raise ValueError(f"calibration instance {flagged[0].instance_id} is not a pilot instance")
        insts = flagged
    return [
        (spec.name, RunId(i.instance_id, o, l, m, s))
        for i, o, l, m, s in itertools.product(
            insts, spec.option_enums(), spec.layout_enums(), spec.models, spec.seeds)
    ]


def build_manifest(cfg: CampaignConfig, instances: Sequence[Instance]) -> list[ManifestRow]:
    """Order: priority groups in the order they first appear in cfg.blocks, each shuffled inside
    with the campaign seed (blocks sharing a group, such as main S1 and the pagination cell, are
    shuffled together so they share time of day and provider load). Blocks with interleave "after"
    form one group each, last, in config order. A disabled block adds no rows and moves nothing, so
    enabling an "after" block later only appends.
    """
    groups: dict[str, list[tuple[str, RunId]]] = {}
    excluded = {s.name for s in cfg.blocks if s.exclude_from_analysis}
    afters: list[list[tuple[str, RunId]]] = []
    for spec in cfg.blocks:
        if not spec.enabled:
            continue
        rows = block_rows(spec, instances)
        if spec.interleave == "after":
            afters.append(rows)
        else:
            groups.setdefault(spec.interleave, []).extend(rows)

    if SEED_FIRST:
        return _seed_first(cfg, groups, afters, excluded)
    ordered: list[tuple[str, str, RunId]] = []
    for name, rows in list(groups.items()) + [(f"after{i}", r) for i, r in enumerate(afters)]:
        rows = sorted(rows, key=lambda br: (br[0], br[1].key()))  # canonical, then shuffle
        random.Random(f"{cfg.rng_seed}:{name}").shuffle(rows)
        ordered.extend((name, b, r) for b, r in rows)
    out = [ManifestRow(i, b, r, b in excluded, g) for i, (g, b, r) in enumerate(ordered)]
    keys = [r.run_key for r in out]
    assert len(set(keys)) == len(keys), "duplicate run keys in the manifest"
    return out


def _merge_stage(lists: Sequence[Sequence[tuple[str, RunId]]]) -> list[tuple[int, tuple[str, RunId]]]:
    """Amendments 39 and 40: merge ordered lists of (block, run) into one order, per model by the greedy rule "the next run comes from the list with the smaller fraction taken so
    far (taken over planned, planned being that model's runs in the list), a tie to the earlier list". Returns (list index, item) in the merged order. The rule is a sort: the
    run with rank j (0-based) among a model's runs of a list is taken when that list's fraction is j / planned, so ordering by (j / planned, list index) is the greedy order, each
    list's own order kept (j rises along it). Across models the third key, the place in the list, makes the same fractions proportional. Fractions are exact."""
    keyed = []
    for li, rows in enumerate(lists):
        planned = collections.Counter(r.model for _, r in rows)
        taken: collections.Counter[str] = collections.Counter()
        for pos, item in enumerate(rows):
            m = item[1].model
            keyed.append(((fractions.Fraction(taken[m], planned[m]), li, pos), li, item))
            taken[m] += 1
    keyed.sort(key=lambda t: t[0])
    return [(li, item) for _, li, item in keyed]


def _seed_first(cfg: CampaignConfig, groups: dict[str, list[tuple[str, RunId]]], afters: list[list[tuple[str, RunId]]],
                excluded: set[str]) -> list[ManifestRow]:
    """Amendment 27(b): every planned run stays, only the order changes. Groups that hold a barrier block (pilot, calibration) come first, as registered, with
    the same shuffle. The others go in stages for each seed, lowest seed first: within a seed the groups in the order they first appear in cfg.blocks (block 1,
    which is main S1 with the pagination cell, then S2 tier A, then S2 tier B), each stage shuffled on its own with the campaign seed. Groups of "after"
    blocks stay last.

    Amendments 39 and 40: in the lowest seed the groups named in MERGED_SEED1_GROUPS (all three by default) are one stage (`_merge_stage`), where the first of them in that tuple would
    have been. Each is first shuffled as its own stage would be (the same seed string, so each keeps its registered order); the merged stage's label is the first merged group's, block
    1's `seed<n>:condition_e`, so block 1's runs keep the stage name they had; every run keeps its own group."""
    barrier = set(cfg.barrier_blocks)
    first = [g for g, rows in groups.items() if any(b in barrier for b, _ in rows)]
    staged = [g for g in groups if g not in first]
    seeds = sorted({r.seed for g in staged for _, r in groups[g]})
    out: list[tuple[str, str, str, RunId]] = []          # (group, stage, block, run)

    def shuffled(stage: str, rows: list[tuple[str, RunId]]) -> list[tuple[str, RunId]]:
        rows = sorted(rows, key=lambda br: (br[0], br[1].key()))      # canonical, then shuffle, as registered
        random.Random(f"{cfg.rng_seed}:{stage}").shuffle(rows)
        return rows

    def emit(group: str, stage: str, rows: list[tuple[str, RunId]]) -> None:
        out.extend((group, stage, b, r) for b, r in shuffled(stage or group, rows))
    for g in first:
        emit(g, "", groups[g])
    merged: list[str] = []                               # the groups merged in the lowest seed, in MERGED_SEED1_GROUPS order
    if seeds:
        merged = [g for g in MERGED_SEED1_GROUPS if g in staged and any(r.seed == seeds[0] for _, r in groups[g])]
        if len(merged) < 2:
            merged = []                                  # one group, or none: there is nothing to merge
    for sd in seeds:
        for g in staged:
            rows = [(b, r) for b, r in groups[g] if r.seed == sd]
            if sd == seeds[0] and g in merged:
                if g == next(x for x in staged if x in merged):      # at the place of the first merged group in the config's order
                    lists = [shuffled(stage_label(x, sd), [(b, r) for b, r in groups[x] if r.seed == sd]) for x in merged]
                    label = stage_label(merged[0], sd)
                    out.extend((merged[li], label, b, r) for li, (b, r) in _merge_stage(lists))
            elif rows:
                emit(g, stage_label(g, sd), rows)
    for i, rows in enumerate(afters):
        emit(f"after{i}", "", rows)
    res = [ManifestRow(i, b, r, b in excluded, g, st) for i, (g, st, b, r) in enumerate(out)]
    keys = [r.run_key for r in res]
    assert len(set(keys)) == len(keys), "duplicate run keys in the manifest"
    return res
