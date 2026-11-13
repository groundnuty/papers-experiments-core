"""The world the reset's QoS tests share: a federation whose second provider holds every file of one directory, a
platform file with two supporting providers, and a provisioner on the test's clock that finds QoS requirements by a
chosen scan (O33)."""

import dataclasses

from fake_reset import FakeReset

from expcore_onedata import resetqos
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.testing import (
    ADMIN_ENV,
    BASELINE,
    BETA_ID,
    DATA,
    SPACE_NAME,
    USERS,
    with_placement,
)

FILES = [f"study/part-{i:02d}.bin" for i in range(6)]
WAIT_S = resetqos.quiet_wait_s(2)  # the quiet wait for files one directory below the root: twice 10 s + 5 s


def federation(monkeypatch, ticker) -> FakeReset:
    """The placement fixture's federation, with a reset's calls: beta supports the space and holds every file."""
    fake = FakeReset(space_name=SPACE_NAME)
    fake.clock = ticker.clock
    fake.add_provider(BETA_ID, "beta.test")
    monkeypatch.setenv(ADMIN_ENV, fake.admin_token)
    for login, (password, user_id) in USERS.items():
        fake.add_user(login, password, user_id)
        fake.grant(user_id, BASELINE)
    for path in FILES:
        file_id = fake.add_file(path, DATA)
        fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]
    return fake


def two_providers(data) -> None:
    """The platform document's placement section: both providers support the space."""
    with_placement(data, transfer_settle_s=5.0, transfer_settle_polls=3, concurrency=4)
    data["support"]["providers"] = ["alpha", "beta"]


def provisioner_with(client, config, platform_file, ticker, tmp_path, *, scan="summaries", sleep=None) -> Provisioner:
    """A provisioner on the test's clock, finding QoS requirements by `scan`, sleeping through `sleep`."""
    settings = dataclasses.replace(load_placement(platform_file, config).settings, qos_scan=scan)
    return Provisioner(
        client,
        config,
        settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=sleep or ticker.sleep,
    )


def calls(fake, part: str) -> list:
    """The provider GETs whose route holds `part`."""
    return [c for c in fake.provider_calls if c[1] == "GET" and part in c[2]]
