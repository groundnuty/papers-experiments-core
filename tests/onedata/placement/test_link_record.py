"""O35: a restore's link check and a heal's every read of the pairs write the link-check record, as `up` does.

Both go through the one probe the injector's driver is given (`ServingProbe.matrix`, which `links` uses too), so the
record on disk is the verdict of the latest check made, passing or failing, and never an older one: after a heal the
operator's pre-check, which wants a check under an hour old, finds the heal's own re-check instead of re-running `up`.
These tests run the platform layer's real link check over the fake federation; the heal and the restore themselves are
in `tests/onedata/test_fault_link_record.py`.
"""

import contextlib
import json
import logging
from functools import partial
from pathlib import Path

import pytest
from fault_support import SERVICES, faults_table
from link_support import PROVIDERS, add_providers, three_providers

from expcore_onedata import fault_serves
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_serves import provider_serves
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.linkcheck import check_links
from expcore_onedata.linkrecord import RECORD, record_text
from expcore_onedata.testing import write_platform

STORES = {**SERVICES, "gamma": "store-three"}  # the Compose service of each provider a restore can concern
SERVICE = STORES["beta"]


@pytest.fixture(autouse=True)
def checks_on_the_tickers_clock(monkeypatch, ticker):
    """The probe's check runs on the test's `Ticker`, as `test_link_check_pairs` runs the check: a wait is instant, the
    window is the ticker's, and the pairs go one at a time, since the fake federation's clock is every thread's."""
    monkeypatch.setattr(
        fault_serves,
        "check_links",
        partial(check_links, clock=ticker.clock, sleep=ticker.sleep, now=ticker.clock, workers=1),
    )


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(
        tmp_path, lambda d: (three_providers(d), d.__setitem__("faults", faults_table(services=STORES)))
    )


@pytest.fixture
def fake(fake):
    return add_providers(fake)


@pytest.fixture
def serves(platform_file, config, fake):
    client = OnezoneClient(config, http=fake.client_http())
    return provider_serves(config, load_faults(platform_file, config), client)


def stick(fake, ticker, reader, source):
    """The link of `reader` to `source` is stuck: a read through `reader` of bytes only `source` holds never answers."""
    fake.links[(PROVIDERS[reader], PROVIDERS[source])] = "hang"
    fake.hang_hook = ticker.sleep


def record(tmp_path):
    return json.loads((tmp_path / RECORD).read_text(encoding="utf-8"))


def test_o35_a_passing_check_writes_its_verdict_in_the_federations_root_in_ups_format(tmp_path, serves, ticker):
    """O35: the matrix of a restore's or a heal's check leaves the report it was made from, as `up` writes it, in the
    federation's root (the journal's parent: here the platform.toml's own directory): every pair, its seconds and
    bytes, and the check's window on the host's epoch clock."""
    assert not (tmp_path / RECORD).exists()
    before = ticker.clock()
    matrix = serves.matrix(SERVICE)
    after = ticker.clock()
    assert matrix is not None
    assert matrix.ok
    written = record(tmp_path)
    assert written["ok"] is True
    assert len(written["pairs"]) == 6  # every ordered pair of the three providers
    assert all(pair["ok"] and pair["bytes"] for pair in written["pairs"])
    assert before <= written["window"]["start_epoch_s"] <= written["window"]["end_epoch_s"] <= after
    assert written["window"]["end_epoch_s"] > written["window"]["start_epoch_s"]  # the check's own span, not a stamp
    text = (tmp_path / RECORD).read_text(encoding="utf-8")
    assert text == json.dumps(written, indent=2, sort_keys=True) + "\n"  # `up`'s bytes


def test_o35_the_restores_link_check_writes_it_too(tmp_path, serves):
    """O35: `links`, the restore's check by its detail alone (`ComposeDriver.restored_links`), writes the record."""
    assert serves.links(SERVICE) is None
    assert record(tmp_path)["ok"] is True


def test_o35_a_failing_check_writes_its_own_failing_verdict_over_a_passing_record(tmp_path, serves, fake, ticker):
    """O35: the latest verdict on disk is the latest check's: a check that finds a pair stuck replaces the passing
    record of the check before it with a failing one naming the pair, never leaves the pass, and the next passing
    check replaces that."""
    serves.matrix(SERVICE)
    assert record(tmp_path)["ok"] is True
    stick(fake, ticker, "gamma", "alpha")
    matrix = serves.matrix(SERVICE)
    assert matrix is not None
    assert not matrix.ok
    failed = record(tmp_path)
    assert failed["ok"] is False
    (stuck_pair,) = [p for p in failed["pairs"] if not p["ok"]]
    assert (stuck_pair["reader"], stuck_pair["source"]) == ("providers[2]", "providers[0]")
    assert "restart the rtransfer link on providers[2]" in stuck_pair["remedy"]
    del fake.links[(PROVIDERS["gamma"], PROVIDERS["alpha"])]  # the link recovered
    assert serves.matrix(SERVICE) is not None
    assert record(tmp_path)["ok"] is True


def test_o35_a_check_that_cannot_run_removes_the_older_record_and_still_raises(tmp_path, serves, monkeypatch):
    """O35: a check that raises answers nothing, so the passing record of an older check must not stand as the latest
    verdict: it is removed (a pre-check then finds none and runs `up`), and the error still reaches the caller, which
    fails closed."""
    serves.matrix(SERVICE)
    assert (tmp_path / RECORD).exists()

    def broken(config, client):
        raise RuntimeError("the check could not run")

    monkeypatch.setattr(fault_serves, "check_links", broken)
    with pytest.raises(RuntimeError, match="could not run"):
        serves.matrix(SERVICE)
    assert not (tmp_path / RECORD).exists()


def test_o35_a_record_that_cannot_be_written_removes_the_older_one_and_leaves_the_check_whole(
    tmp_path, serves, monkeypatch, caplog
):
    """O35: the record is advisory, so a disk that refuses it never fails the check, the restore or the heal: the matrix
    is returned as made. But the older record must not stand as the latest verdict, so it is removed, and a warning
    names the error's type (never a path or a provider)."""
    serves.matrix(SERVICE)
    assert (tmp_path / RECORD).exists()

    def refused(report, path):
        raise PermissionError(13, "read-only file system")

    monkeypatch.setattr(fault_serves, "write_record", refused)
    with caplog.at_level(logging.WARNING, logger=fault_serves.__name__):
        matrix = serves.matrix(SERVICE)
    assert matrix is not None
    assert matrix.ok
    assert not (tmp_path / RECORD).exists()
    (warning,) = [r for r in caplog.records if r.name == fault_serves.__name__ and r.levelno == logging.WARNING]
    assert "PermissionError" in warning.getMessage()
    assert str(tmp_path) not in warning.getMessage()


def test_o35_a_record_in_a_directory_that_refuses_both_is_emptied_in_place_so_no_old_pass_stays(
    tmp_path, serves, monkeypatch
):
    """O35: a read-only directory takes neither a new record nor the removal of the old one, but the old file is still
    writable: the old pass is emptied in place and dated to the epoch, so it is no record, and the check answers as
    made."""
    serves.matrix(SERVICE)
    assert record(tmp_path)["ok"] is True

    def refused(*args, **kwargs):
        raise PermissionError(13, "read-only directory")

    monkeypatch.setattr(fault_serves, "write_record", refused)
    monkeypatch.setattr(Path, "unlink", refused)
    matrix = serves.matrix(SERVICE)
    monkeypatch.undo()
    assert matrix is not None
    assert matrix.ok
    assert (tmp_path / RECORD).read_bytes() == b""
    assert (tmp_path / RECORD).stat().st_mtime == 0


def test_o35_a_check_that_cannot_take_the_lock_publishes_no_verdict_and_removes_the_older_one(
    tmp_path, serves, monkeypatch, caplog
):
    """O35: without the record's lock the check's verdict could be overtaken by, or overtake, another's, so no record is
    written at all: the older one is removed, a warning says why, and the check still answers as made."""
    serves.matrix(SERVICE)
    assert (tmp_path / RECORD).exists()

    @contextlib.contextmanager
    def unlocked(path):
        yield False

    monkeypatch.setattr(fault_serves, "recording", unlocked)
    with caplog.at_level(logging.WARNING, logger=fault_serves.__name__):
        matrix = serves.matrix(SERVICE)
    assert matrix is not None
    assert matrix.ok
    assert not (tmp_path / RECORD).exists()
    assert any("lock could not be taken" in r.getMessage() for r in caplog.records)


def test_o35_a_record_that_can_be_neither_written_nor_removed_is_said_twice_and_changes_nothing_else(
    tmp_path, serves, monkeypatch, caplog
):
    """O35: when even the removal fails, the check still answers as made and each failure is logged."""
    refused = PermissionError(13, "denied")

    def write(report, path):
        raise refused

    def discard(path):
        raise refused

    monkeypatch.setattr(fault_serves, "write_record", write)
    monkeypatch.setattr(fault_serves, "discard_record", discard)
    with caplog.at_level(logging.WARNING, logger=fault_serves.__name__):
        matrix = serves.matrix(SERVICE)
    assert matrix is not None
    assert matrix.ok
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_o35_without_a_link_check_nothing_is_checked_and_no_record_is_written(tmp_path, fake):
    """O35: a platform with `[faults]` and no `[link_check]` has no check to record."""

    def edit(data):
        three_providers(data, link_check=False)
        data["faults"] = faults_table(services=STORES)

    platform = write_platform(tmp_path, edit)
    config = load_platform(platform)
    client = OnezoneClient(config, http=fake.client_http())
    probe = provider_serves(config, load_faults(platform, config), client)
    assert probe.matrix(SERVICE) is None
    assert not (tmp_path / RECORD).exists()


def test_o35_the_file_is_the_record_text_of_the_report_the_check_made(tmp_path, serves, monkeypatch):
    """O35: what the probe hands the writer is the platform layer's own report, so the file is `record_text` of it."""
    made = []
    real = fault_serves.check_links

    def spy(config, client, **options):
        report = real(config, client, **options)
        made.append(report)
        return report

    monkeypatch.setattr(fault_serves, "check_links", spy)
    serves.matrix(SERVICE)
    (report,) = made
    assert (tmp_path / RECORD).read_text(encoding="utf-8") == record_text(report)
