"""The public testing module, used the way a study uses it: wired from its public names alone, no test support file.

The core's own tests import the same fakes from the same place, so these tests are about the module's contract: what it
exports, what it says of itself, and that a probe run against it reports a transfer as the core does.
"""

import importlib
import inspect
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

import expcore_onedata.testing as testing
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.probes import PlatformProbes
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.testing import (
    ADMIN_ENV,
    BETA_ID,
    DATA,
    FakeData,
    Ticker,
    make_federation,
    write_with_placement,
)

ROOT = Path(__file__).resolve().parents[2]
TRACKED = "study/data.bin"
MORE = "study/more.bin"
OUTSIDE_S = 60  # how long before the run a scripted job starts: far outside the window and its clock-skew bands
INSIDE_S = 10  # how far into the run one starts: far from the window's edges


@dataclass
class World:
    """What a study's own fixture builds from the public names: the clock, the federation and the probes on it."""

    ticker: Ticker
    fake: FakeData
    probes: PlatformProbes


@pytest.fixture
def world(monkeypatch, tmp_path) -> World:
    ticker = Ticker()
    fake = make_federation(clock=ticker.clock)
    fake.add_file(TRACKED, DATA)
    fake.add_file(MORE, DATA)
    monkeypatch.setenv(ADMIN_ENV, fake.admin_token)
    platform = write_with_placement(tmp_path)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    provisioner = Provisioner(
        client,
        config,
        load_placement(platform, config).settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
    probes = PlatformProbes(
        client, provisioner, {"data": TRACKED, "more": MORE}, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock
    )
    return World(ticker, fake, probes)


def test_o08_a_transfer_that_started_before_the_run_is_reported_carried_over_and_never_counted(world):
    """O08: a replication into beta that started a minute before the run and ended inside it placed bytes during the
    run, so beta gained them, but the core reports the job as carried over: it is not a transfer of the run, its bytes
    are not in `transfer_bytes_in`, and beta's on-demand bound is unknown rather than a number. A stand-in that sums
    every job it holds would count the 4096 bytes and answer a bound of zero."""
    started = world.fake.add_transfer("replication", MORE, BETA_ID, scheduled=world.ticker.now - OUTSIDE_S, ended=False)
    world.probes.start({})
    world.ticker.sleep(INSIDE_S)
    world.fake.end_transfer(started)
    world.ticker.sleep(INSIDE_S)
    snapshot = world.probes.finish()
    assert snapshot["ok"] is True
    assert snapshot["space_transfers"]["records"] == []
    assert snapshot["space_transfers"]["carried_over"] == 1
    assert snapshot["on_demand"]["beta"] == {
        "gained_bytes": len(DATA),
        "transfer_bytes_in": 0,
        "difference": len(DATA),
        "on_demand_lower_bound": None,
        "unknown": ["transfer_carried_over"],
    }
    assert snapshot["on_demand"]["alpha"]["on_demand_lower_bound"] == 0


def test_o08_the_same_transfer_started_inside_the_run_is_counted(world):
    """O08: the one difference from the carried-over case is when the job started. Started inside the run, it is a
    record of the run, its 4096 bytes are transfer bytes into beta, and the bound is known and zero."""
    world.probes.start({})
    world.ticker.sleep(INSIDE_S)
    started = world.fake.add_transfer("replication", MORE, BETA_ID, scheduled=world.ticker.now, ended=False)
    world.fake.end_transfer(started)
    world.ticker.sleep(INSIDE_S)
    snapshot = world.probes.finish()
    assert [record["transfer_id"] for record in snapshot["space_transfers"]["records"]] == [started]
    assert snapshot["space_transfers"]["carried_over"] == 0
    assert snapshot["on_demand"]["beta"]["transfer_bytes_in"] == len(DATA)
    assert snapshot["on_demand"]["beta"]["on_demand_lower_bound"] == 0
    assert snapshot["on_demand"]["beta"]["unknown"] is None


def test_o08_a_transfer_that_started_and_ended_before_the_run_is_outside_it_and_not_carried_over(world):
    """O08: carried over means still running when the run began. A job that ended a minute before is outside the
    window: not a record, not counted, not carried over, and it does not make the bound unknown. Its bytes are already
    on beta when the run starts, so beta gained nothing."""
    now = world.ticker.now
    world.fake.add_transfer("replication", MORE, BETA_ID, scheduled=now - 2 * OUTSIDE_S, finished=now - OUTSIDE_S)
    world.probes.start({})
    world.ticker.sleep(INSIDE_S)
    snapshot = world.probes.finish()
    assert snapshot["space_transfers"]["records"] == []
    assert snapshot["space_transfers"]["carried_over"] == 0
    assert snapshot["on_demand"]["beta"] == {
        "gained_bytes": 0,
        "transfer_bytes_in": 0,
        "difference": 0,
        "on_demand_lower_bound": 0,
        "unknown": None,
    }


PUBLIC = sorted(testing.__all__)


def test_every_public_name_is_importable_and_listed_in_the_interfaces_document():
    document = (ROOT / "docs" / "interfaces.md").read_text(encoding="utf-8")
    section = document.split("## The platform layer's testing module", 1)[1].split("\n## ", 1)[0]
    assert PUBLIC, "the module exports nothing"
    for name in PUBLIC:
        assert hasattr(testing, name), name
        assert f"`{name}`" in section, f"{name} is exported but not listed in docs/interfaces.md"
    for module in ("fake_mcp_server", "fake_onedata_mcp"):
        assert f"expcore_onedata.testing.{module}" in section, module


@pytest.mark.parametrize("name", [n for n in PUBLIC if inspect.isclass(getattr(testing, n))])
def test_every_exported_class_has_a_docstring(name):
    assert inspect.getdoc(getattr(testing, name)), f"{name} has no docstring"


@pytest.mark.parametrize("fake", [testing.MockOnedata, testing.FakeFederation, testing.FakeData, testing.Ticker])
def test_every_fake_says_what_it_models_and_what_it_does_not(fake):
    doc = inspect.getdoc(fake) or ""
    assert "Models:" in doc, f"{fake.__name__}: the docstring does not say what it models"
    assert "Does not model:" in doc, f"{fake.__name__}: the docstring does not say what it does not model"


@pytest.mark.parametrize("module", ["fake_mcp_server", "fake_onedata_mcp"])
def test_the_stand_in_servers_say_what_they_model_and_what_they_do_not(module):
    doc = inspect.getdoc(importlib.import_module(f"expcore_onedata.testing.{module}")) or ""
    assert "Models:" in doc, module
    assert "Does not model:" in doc, module


def test_importing_the_testing_package_loads_no_pytest():
    """The package is for a study's own tests and holds no fixture: a helper that needs pytest is the study's."""
    program = "import sys, expcore_onedata.testing\nprint('pytest' in sys.modules, 'conftest' in sys.modules)\n"
    done = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip() == "False False"
