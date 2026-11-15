# Provenance: new
"""The earlier selections a later sequential one runs after (`--after`, C38): read, verified, and what they spent.

`run --select ... --sequential --after EARLIER_OUT ...` names every selection of the campaign that ran before this one,
each by its `out/` (or its store). Each is read from a snapshot of its store, taken while its dispatch lock is held,
so a driver still running it is refused, and it must be:

- a sequential selection (`--sequential`) of the same campaign: the same fingerprint and environment identity;
- stopped: no attempt still running, and no driver holding its store;
- disjoint from this selection: no run of the plan is selected by both;
- with every earlier selection it ran after also given here, and its record of each still what that one's store says
  (one run again since has spent more, which this selection's budget would not know).

The earlier selections are returned in the order they ran (each after all those before it), each as the entry this
selection's shares keep: `{label, select, started_utc, spent_usd, per_model_spent_usd}`, the spend the budget gate's
own figure (`selection_ledger.spend_of`). The core cannot tell a first selection from a later one whose `--after` was
left out; the merge refuses that chain (`merge_selection`).
"""

import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from expcore.campaign.lock import CampaignLocked, LockMissing, ReaderLock
from expcore.campaign.manifest import Manifest
from expcore.campaign.merge import MergeRefused, ShardFacts, facts_of_bytes
from expcore.campaign.scheduler import STATE_FILE
from expcore.campaign.selection import Selection, SelectionSpec
from expcore.campaign.selection_ledger import Pricing, spend_json
from expcore.campaign.shard import ShardError
from expcore.campaign.snapshot import AttemptsRunning, SnapshotError, take_snapshot


def store_of(path: Path) -> Path:
    """The store an `--after` names: the file itself, or `state.sqlite` in the directory (an `out/`) or its `out/`."""
    for candidate in (path, path / STATE_FILE, path / "out" / STATE_FILE):
        if candidate.is_file():
            return candidate
    raise ShardError(f"--after {path}: no campaign store there (give an earlier selection's out/ or its store)")


def _read(path: Path, scratch: Path, pricing: Pricing) -> ShardFacts:
    """A snapshot of the store an `--after` names, read as the merge reads one, and nothing written beside it (R16).

    The earlier selection must have stopped: its existing lock file is opened read-only and locked shared
    (`ReaderLock`, required), which is refused while its dispatcher runs, and refused where there is no lock file,
    since a finished selection's dispatcher made one. The lock is held across the copy of the store, so that
    selection cannot start again mid-copy; the store is backed up from a private copy of its files, so its
    directory may be read-only.
    """
    store = store_of(path)
    lock = ReaderLock(store.parent, required=True)
    try:
        lock.acquire()
    except CampaignLocked as error:
        raise ShardError(f"--after {path}: {error}; an earlier selection is given once it has stopped") from None
    except LockMissing as error:
        raise ShardError(f"--after {path}: {error}; not a finished selection") from None
    try:
        take_snapshot(store, scratch)
    except AttemptsRunning as error:
        raise ShardError(f"--after {path}: {error}; an earlier selection is given once it has stopped") from None
    except SnapshotError as error:
        raise ShardError(f"--after {path}: {error}") from None
    finally:
        lock.release()
    try:
        return facts_of_bytes(scratch.read_bytes(), path, out=store.parent, snapshot=scratch, pricing=pricing)
    except MergeRefused as error:
        raise ShardError(f"--after {path}: {error}") from None


def entry_of(facts: ShardFacts) -> dict[str, Any]:
    """What a later selection keeps of an earlier one: its label, levels, start and spend."""
    spec = facts.spec
    assert isinstance(spec, SelectionSpec)  # checked by `earlier_selections`
    assert facts.spend is not None
    return {
        "label": spec.label,
        "select": spec.selection.to_json(),
        "started_utc": facts.started_utc,
        **spend_json(facts.spend),
    }


def earlier_selections(
    paths: Sequence[Path],
    *,
    selection: Selection,
    label: str,
    fingerprint: Mapping[str, Any],
    environment: Mapping[str, str],
    plan: Manifest,
    pricing: Pricing,
    budget: bool,
) -> list[ShardFacts]:
    """The earlier selections `paths` name, verified (see the module docstring), in the order they ran; `budget`
    says the campaign has one, so each one's spend must be known."""
    found: list[ShardFacts] = []
    with tempfile.TemporaryDirectory(prefix="expcore-after-") as scratch:
        for n, path in enumerate(paths):
            facts = _read(Path(path), Path(scratch) / f"after-{n}.sqlite", pricing)
            _check_one(Path(path), facts, selection, fingerprint, environment, plan, budget)
            found.append(facts)
    labels = [facts.spec.label for facts in found]
    for facts in found:
        if labels.count(facts.spec.label) > 1 or facts.spec.label == label:
            raise ShardError(f"--after names {facts.spec.name} twice, or this selection's own label")
    by_label = {facts.spec.label: facts for facts in found}
    ordered = sorted(found, key=lambda f: (len(_after_of(f)), f.started_utc, f.spec.label))
    for position, facts in enumerate(ordered):
        recorded = {entry["label"]: entry for entry in _after_of(facts)}
        missing = sorted(set(recorded) - set(by_label))
        if missing:
            raise ShardError(
                f"{facts.spec.name} ran after selection {missing[0]}, which --after does not name: give every earlier "
                "selection of the campaign, so that this one's budget is what they all left"
            )
        if set(recorded) != {f.spec.label for f in ordered[:position]}:
            raise ShardError(
                f"the selections --after names did not run one after another: {facts.spec.name} ran after "
                f"{', '.join(sorted(recorded)) or 'none of them'}"
            )
        for name, entry in recorded.items():
            if entry != entry_of(by_label[name]):
                raise ShardError(
                    f"{facts.spec.name} recorded {by_label[name].spec.name} as it was when it started, and its store "
                    f"now says otherwise: {name} ran again after a later selection started"
                )
    return ordered


def _after_of(facts: ShardFacts) -> list[Mapping[str, Any]]:
    after = facts.shares.get("after") if isinstance(facts.shares, Mapping) else None
    if not isinstance(after, list) or not all(isinstance(e, Mapping) and "label" in e for e in after):
        raise ShardError(f"{facts.spec.name}: its shares record no list of the selections it ran after")
    return after


def _check_one(
    path: Path,
    facts: ShardFacts,
    selection: Selection,
    fingerprint: Mapping[str, Any],
    environment: Mapping[str, str],
    plan: Manifest,
    budget: bool,
) -> None:
    """One earlier selection: sequential, of this campaign and environment, disjoint from this one, and with a budget
    its spend known."""
    spec = facts.spec
    if not isinstance(spec, SelectionSpec) or not spec.sequential:
        raise ShardError(f"--after {path}: not a selection run with --sequential ({facts.name})")
    if facts.fingerprint != fingerprint:
        raise ShardError(f"--after {path}: {spec.name} ran another campaign: its fingerprint differs from this one's")
    if dict(facts.environment) != dict(environment):
        raise ShardError(f"--after {path}: {spec.name} ran against another environment identity than this one")
    both = next((row for row in plan.rows if selection.selects(row.key.factors) and spec.selection.selects(
        row.key.factors)), None)  # fmt: skip
    if both is not None:
        raise ShardError(
            f"--after {path}: {spec.name} also selects run {both.key.text}: the selections of a campaign are disjoint"
        )
    known = facts.spend is not None and facts.spend.total is not None and None not in facts.spend.per_model.values()
    if budget and not known:
        raise ShardError(f"--after {path}: what {spec.name} spent is unknown (an attempt of a model without a price)")


def spent_before(earlier: Sequence[ShardFacts]) -> tuple[float, dict[str, float]]:
    """What the earlier selections spent together, in total and per model; each spend was checked known."""
    total, per_model = 0.0, {}
    for facts in earlier:
        if facts.spend is None or facts.spend.total is None or None in facts.spend.per_model.values():
            raise ShardError(f"what {facts.spec.name} spent is unknown (an attempt of a model without a price)")
        total += facts.spend.total
        for model, usd in facts.spend.per_model.items():
            per_model[model] = per_model.get(model, 0.0) + (usd or 0.0)
    return total, per_model


__all__ = ["earlier_selections", "entry_of", "spent_before", "store_of"]
