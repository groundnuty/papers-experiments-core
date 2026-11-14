"""Helpers for tests of selection shards (C35): batches of the example study's campaign, run for real.

`run_selection` runs a copy of the example with `python -m expcore run --select ... --shard-label L`, as a study
running its campaign in batches does, so the stores, records and shares are what the run side writes. The example's
declared factor `access` (`direct`, `relay`) splits its conditions: `access=direct` holds `baseline` and `extended`,
`access=relay` holds `relay-down`, so the contrast of `relay-down` with `baseline` pairs runs of two selections.

`make_selection_study` gives every copy the same campaign (the unit caps are not in the fingerprint, the budget is):
caps of 4 per unit, so two concurrent selections each get a part of every cap they run on.
"""

import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from shard_support import IDENTITY, UNIT_CAPS, declaring
from study_support import HEAD, Campaign, cli, make_copy

from expcore.campaign.selection_ledger import spend_of
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.selection_start import pricing_of

DIRECT, RELAY = "access=direct", "access=relay"
SELECTION_ONLY = ("selection",)
"""What a merge of selections adds to each run entry of the export, beyond a merge of shards: the selection's label."""


def make_selection_study(base: Path, name: str, *, caps: int = 4, budget: float | None = None, extra: str = "") -> Path:
    """A stamped copy of the example with unit caps of `caps`, `max_usd` replaced by `budget` when given, and `extra`
    appended to its campaign.toml."""
    root = make_copy(base, name)
    text = (root / "campaign.toml").read_text(encoding="utf-8")
    for cap in UNIT_CAPS:
        assert cap in text, f"the example no longer caps {cap.split(' ')[0]} at 1"
        text = text.replace(cap, cap.replace("= 1", f"= {caps}"))
    if budget is not None:
        assert "max_usd = 5.0" in text, "the example's budget is no longer 5.0"
        text = text.replace("max_usd = 5.0", f"max_usd = {budget}")
    (root / "campaign.toml").write_text(text + extra, encoding="utf-8")
    write_stamp(root, HEAD, load_study(root).scope)
    return root


def select_args(selects: Sequence[str], label: str, *, sequential: bool = False, after: Sequence[Path] = ()) -> list:
    """The `run` options of a selection."""
    args = [option for select in selects for option in ("--select", select)] + ["--shard-label", label]
    if sequential:
        args.append("--sequential")
    return args + [option for path in after for option in ("--after", str(path))]


def run_selection(
    base: Path,
    name: str,
    env: dict[str, str],
    selects: Sequence[str],
    label: str,
    *,
    sequential: bool = False,
    after: Sequence[Campaign] = (),
    identity: Mapping[str, str] = IDENTITY,
    **study: object,
) -> Campaign:
    """Run one selection of the example in a copy named `name` under `base`, to the end, and return it."""
    root = make_selection_study(base, name, **study)  # type: ignore[arg-type]
    outs = [campaign.root / "out" for campaign in after]
    with pytest.MonkeyPatch.context() as patch:
        declaring(patch, identity)
        code, errors = cli(env, "run", str(root), *select_args(selects, label, sequential=sequential, after=outs))
    assert code == 0, errors
    return Campaign(root)


def run_refused(env: dict[str, str], root: Path, *args: str, identity: Mapping[str, str] = IDENTITY) -> tuple[int, str]:
    """Run `run` on `root` with `args` and return its exit status and stderr, the plugin declaring `identity`."""
    with pytest.MonkeyPatch.context() as patch:
        declaring(patch, identity)
        return cli(env, "run", str(root), *args)


def spent(campaign: Campaign) -> float:
    """What a finished selection spent, as the budget gate counts it."""
    connection = sqlite3.connect(campaign.root / "out" / "state.sqlite")
    connection.row_factory = sqlite3.Row
    try:
        total = spend_of(
            lambda sql, args=(): connection.execute(sql, tuple(args)).fetchall(), pricing_of(load_study(campaign.root))
        ).total
    finally:
        connection.close()
    assert total is not None
    return total


def execute(store: Path, sql: str, *args: object) -> None:
    """Run one statement on a store (a copy, to plant a fault in)."""
    connection = sqlite3.connect(store)
    try:
        connection.execute(sql, args)
        connection.commit()
    finally:
        connection.close()
