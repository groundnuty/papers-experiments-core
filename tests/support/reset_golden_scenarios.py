"""The reset scenarios whose records, waits and provider calls under `[placement] wait_mode = "fixed"` are pinned to the
ones captured before the wait modes existed (`tests/data/reset-fixed-golden.json`): one space evicting files (with a
job to cancel), one evicting a tree with the quiet check skipped, and three spaces reset as one. Every request is sent
from one thread (`concurrency` 1), so the calls and the reads' tallies are the same on any host."""

import dataclasses
import json
from collections import Counter
from pathlib import Path
from typing import Any

from fake_reset import FakeReset
from qos_world import FILES, federation, two_providers

from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.reset import SpaceReset
from expcore_onedata.resetspaces import ResetPlan, SpacesReset
from expcore_onedata.testing import (
    ADMIN_ENV,
    ALPHA_ID,
    BASELINE,
    BETA_ID,
    DATA,
    SPACE_NAME,
    USERS,
    Ticker,
    write_platform,
)
from expcore_onedata.testing.world import SPACE_IDS, SPACE_LABELS, SPACE_NAMES, with_spaces

GOLDEN = Path(__file__).resolve().parents[1] / "data" / "reset-fixed-golden.json"
COUNT = 3  # spaces of the several-space scenario


def _provisioner(client, config, settings, ticker, log: Path, space: str | None = None) -> Provisioner:
    return Provisioner(
        client,
        config,
        settings,
        provider_ids=resolve_provider_ids(config),
        log=log,
        **({"space": space} if space is not None else {}),
        clock=ticker.clock,
        sleep=ticker.sleep,
    )


def _shape(record: Any, ticker: Ticker, fake) -> dict[str, Any]:
    """What is pinned: the record, every sleep in order, and the provider calls counted by route."""
    calls = Counter(f"{provider} {method} {route}" for provider, method, route in fake.provider_calls)
    return {
        "record": json.loads(json.dumps(record, sort_keys=True, default=sorted)),
        "slept": list(ticker.slept),
        "calls": dict(sorted(calls.items())),
    }


def one_space(monkeypatch, tmp_path, make_client, load_config, kind: str, **settings) -> dict[str, Any]:
    """One space whose beta holds every file of `study`: `files` evicts them by path with an outside job to cancel and
    the quiet check run; `tree` evicts the directory with the quiet check skipped."""
    ticker = Ticker()
    fake = federation(monkeypatch, ticker)
    platform_file = write_platform(tmp_path, two_providers)
    config = load_config(platform_file)
    client = make_client(config, fake)
    base = load_placement(platform_file, config).settings
    placed = dataclasses.replace(base, qos_scan="listing", **{"concurrency": 1, **settings})  # one thread: exact
    p = _provisioner(client, config, placed, ticker, tmp_path / "changes.jsonl")
    if kind == "files":
        tid = fake.add_transfer("replication", FILES[0], BETA_ID, scheduled=ticker.now, ended=False)
        assert tid
        record = SpaceReset(p, now=ticker.clock).run({"beta": FILES}, qos_rights=True)
    else:
        record = SpaceReset(p, now=ticker.clock).run({"beta": ["study"]}, qos_rights=False)
    return _shape(record, ticker, fake)


def several_spaces(monkeypatch, tmp_path, make_client, load_config, plans=None, **settings) -> dict[str, Any]:
    """Three spaces, each with beta holding every file of `study`, reset as one with the quiet check run; `plans`,
    given, are the spaces' plans instead."""
    ticker = Ticker()
    fake = FakeReset(space_name=SPACE_NAME)
    fake.clock = ticker.clock
    fake.add_provider(BETA_ID, "beta.test")
    for space_id, name in zip(SPACE_IDS[1:COUNT], SPACE_NAMES[1:COUNT], strict=True):
        fake.add_data_space(space_id, name, [ALPHA_ID, BETA_ID])
    monkeypatch.setenv(ADMIN_ENV, fake.admin_token)
    for login, (password, user_id) in USERS.items():
        fake.add_user(login, password, user_id)
        for space in SPACE_IDS[:COUNT]:
            fake.grant(user_id, BASELINE, space=space)
    for space in SPACE_IDS[:COUNT]:
        for path in FILES[:2]:
            fake.blocks[(fake.add_file(path, DATA, space=space), BETA_ID)] = [[0, len(DATA)]]

    def document(data):
        two_providers(data)
        with_spaces(data, COUNT)

    platform_file = write_platform(tmp_path, document)
    config = load_config(platform_file)
    client = make_client(config, fake)
    placed = dataclasses.replace(load_placement(platform_file, config).settings, **{"concurrency": 1, **settings})
    resets = [
        SpaceReset(_provisioner(client, config, placed, ticker, tmp_path / f"{label}.jsonl", label), now=ticker.clock)
        for label in SPACE_LABELS[:COUNT]
    ]
    plans = plans or [ResetPlan({"beta": ["study"]}, qos_rights=True) for _ in range(COUNT)]
    return _shape(SpacesReset(resets, concurrency=1).run(plans), ticker, fake)


SCENARIOS = ("files", "tree", "spaces")
NEW_KEYS = ("wait_records", "wait_mode")  # what the wait modes added to every record, fixed mode's included


def without_new(value: Any) -> Any:
    """A shape with what the wait modes added taken out: each stage's `wait_records`, each space's `wait_mode` and its
    `shared_waits` flag (the several-space record's own `shared_waits` table stays)."""
    if isinstance(value, dict):
        return {
            key: without_new(item)
            for key, item in value.items()
            if key not in NEW_KEYS and not (key == "shared_waits" and isinstance(item, bool))
        }
    if isinstance(value, list):
        return [without_new(item) for item in value]
    return value
