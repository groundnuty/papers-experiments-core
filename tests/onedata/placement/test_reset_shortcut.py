"""O51: a shortcut reset re-reads only the files the planner's reading showed held, the files a transfer touched since,
and those a QoS requirement or a hard link may reach; by default every file is read.

The planner's reading and the reset are made inside the reset lock's exclusive section (`SpaceReset.exclusive`), under
the run's shared admission. Beta holds one of the space's six files when the planner reads; the federation is the fake
one, and the locks are real, on the fixture's journal directory.
"""

import dataclasses
import threading

import pytest
from fault_support import faults_table
from qos_world import FILES, federation, two_providers

from expcore.config.loading import ConfigError
from expcore_onedata import resetshortcut
from expcore_onedata.admission import FederationLock
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.faults import load_faults
from expcore_onedata.occupancy import Listing
from expcore_onedata.provision import Provisioner, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.reset import SpaceReset
from expcore_onedata.resetgrant import ResetLockBusy
from expcore_onedata.resetspaces import ResetPlan, SpacesReset
from expcore_onedata.testing import BETA_ID, DATA, write_platform
from expcore_onedata.tool_admission import shared_admission

HELD = FILES[1]  # the one file beta holds when the planner reads
LEFTOVER = FILES[3]  # the file a job begun before the reading replicates into beta after it
UNTOUCHED = [FILES[0], FILES[2], FILES[4], FILES[5]]


def with_faults(data) -> None:
    two_providers(data)
    data["faults"] = faults_table(admission_timeout_s=10)


@pytest.fixture
def fake(monkeypatch, ticker):
    world = federation(monkeypatch, ticker)
    for path in FILES:
        if path != HELD:
            world.blocks.pop((world.id_of(path), BETA_ID))
    return world


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, with_faults)


@pytest.fixture
def lock(platform_file):
    config = load_platform(platform_file, require_secrets=False)
    return FederationLock(load_faults(platform_file, config))


def provisioner(client, config, platform_file, ticker, tmp_path, *, shortcut=True, wait_s=5.0):
    """A provisioner on the test's clock that finds QoS requirements by the listing, with the shortcut on or off."""
    settings = dataclasses.replace(
        load_placement(platform_file, config).settings,
        qos_scan="listing",
        reset_shortcut=shortcut,
        reset_exclusive_wait_s=wait_s,
    )
    return Provisioner(
        client,
        config,
        settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )


def distribution_reads(fake, path) -> int:
    file_id = fake.id_of(path)
    return sum(1 for c in fake.provider_calls if c[1] == "GET" and c[2] == f"/data/{file_id}/distribution")


def reset_with_leftover(fake, reset, ticker, *, inside=True):
    """Plan inside the exclusive section, let a job begun before the reading replicate LEFTOVER into beta after it, and
    run the reset; return the record and the distribution reads each file had during the run."""
    with reset.exclusive():
        holdings = reset.plan_scan()
        fake.add_transfer("replication", LEFTOVER, BETA_ID, scheduled=ticker.clock())
        before = {path: distribution_reads(fake, path) for path in FILES}
        if inside:
            record = reset.run(holdings.plan(), allowed={"beta": len(DATA)}, planned=holdings, qos_rights=True)
    if not inside:
        record = reset.run(holdings.plan(), allowed={"beta": len(DATA)}, planned=holdings, qos_rights=True)
    return record, {path: distribution_reads(fake, path) - before[path] for path in FILES}


def test_o51_the_shortcut_rereads_only_the_held_files_and_those_a_transfer_touched(
    client, config, platform_file, ticker, tmp_path, fake, lock
):
    """O51: inside the exclusive section the occupancy and quiet stages read the file the reading showed held and the
    file a transfer replicated after the reading, and carry every other file as empty: none of them is read at all."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    with lock.held(exclusive=False, timeout_s=1.0):
        record, reads = reset_with_leftover(fake, SpaceReset(p, now=ticker.clock), ticker)
    assert {path: reads[path] for path in UNTOUCHED} == dict.fromkeys(UNTOUCHED, 0)
    assert reads[LEFTOVER] == 2  # the occupancy stage's reading and the quiet stage's
    occupancy = record["stages"]["occupancy"]
    assert (occupancy["files_read"], occupancy["files_carried"]) == (2, 4)
    assert occupancy["files_bytes"] == {"providers[1]": len(DATA)}
    mode = record["occupancy_reads"]
    assert (mode["mode"], mode["why"]) == ("shortcut", None)
    assert mode["grant"]["mode"] == "exclusive"
    assert mode["reading"]["files"] == len(FILES)
    assert mode["reading"]["held_pairs"] == 1
    assert mode["reread"]["transferred"] == 1


def test_o51_by_default_every_file_is_read_twice_after_the_eviction(
    client, config, platform_file, ticker, tmp_path, fake, lock
):
    """O51: with `reset_shortcut` off (the default) the occupancy and quiet stages read every file, the planner's
    reading given or not, and the record says why."""
    p = provisioner(client, config, platform_file, ticker, tmp_path, shortcut=False)
    with lock.held(exclusive=False, timeout_s=1.0):
        record, reads = reset_with_leftover(fake, SpaceReset(p, now=ticker.clock), ticker)
    assert {path: reads[path] for path in UNTOUCHED} == dict.fromkeys(UNTOUCHED, 2)
    assert "files_carried" not in record["stages"]["occupancy"]
    assert (record["occupancy_reads"]["mode"], record["occupancy_reads"]["why"]) == ("full", "off")


def test_o51_a_reading_used_after_its_exclusive_section_ended_gets_the_full_reads(
    client, config, platform_file, ticker, tmp_path, fake, lock
):
    """O51: the section ended before the reset ran, so readers may have placed bytes since the reading: every file is
    read (`outside_grant`)."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    with lock.held(exclusive=False, timeout_s=1.0):
        record, reads = reset_with_leftover(fake, SpaceReset(p, now=ticker.clock), ticker, inside=False)
    assert {path: reads[path] for path in UNTOUCHED} == dict.fromkeys(UNTOUCHED, 2)
    assert (record["occupancy_reads"]["mode"], record["occupancy_reads"]["why"]) == ("full", "outside_grant")


def test_o51_a_tools_hold_past_the_bound_refuses_the_reset_naming_the_holder_and_sends_nothing(ticker, tmp_path, fake):
    """O51: a tool outside a campaign holds the federation past `[faults] admission_timeout_s`: the reset is refused
    (`ResetLockBusy`, the hold `federation_busy`, uncharged) before the planner reads anything, naming the holder by
    its process and never by its reason (O66: it used to give the full reads, `shortcut_unavailable`, beside it)."""

    def short_bound(data):
        two_providers(data)
        data["faults"] = faults_table(admission_timeout_s=0.3)

    (tmp_path / "short").mkdir()
    path = write_platform(tmp_path / "short", short_bound)
    config = load_platform(path)
    lock = FederationLock(load_faults(path, config))
    client = OnezoneClient(config, http=fake.client_http())
    p = provisioner(client, config, path, ticker, tmp_path)
    holding, release = threading.Event(), threading.Event()

    def tool():
        with shared_admission(config, reason="smoke_warmup"):
            holding.set()
            release.wait(10)

    thread = threading.Thread(target=tool)
    thread.start()
    try:
        assert holding.wait(10)
        before = {name: distribution_reads(fake, name) for name in FILES}
        with lock.held(exclusive=False, timeout_s=1.0), pytest.raises(ResetLockBusy) as refused:
            reset_with_leftover(fake, SpaceReset(p, now=ticker.clock), ticker)
    finally:
        release.set()
        thread.join(10)
    assert {name: distribution_reads(fake, name) for name in FILES} == before
    assert refused.value.wait_s >= 0.3
    (holder,) = refused.value.holders
    assert holder["status"] == "live"
    assert "smoke_warmup" not in str(refused.value)


def test_o51_a_transfer_since_the_reading_on_a_directory_gives_the_full_reads(
    client, config, platform_file, ticker, tmp_path, fake, lock
):
    """O51: a job since the reading whose record names no listed file (a directory, the root) cannot be narrowed to
    the files it touched: every file is read (`transfer_not_on_a_file`)."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    reset = SpaceReset(p, now=ticker.clock)
    with lock.held(exclusive=False, timeout_s=1.0), reset.exclusive():
        holdings = reset.plan_scan()
        fake.add_transfer("eviction", "study", BETA_ID, scheduled=ticker.clock())
        before = {path: distribution_reads(fake, path) for path in UNTOUCHED}
        record = reset.run(holdings.plan(), planned=holdings, qos_rights=True)
    assert {path: distribution_reads(fake, path) - before[path] for path in UNTOUCHED} == dict.fromkeys(UNTOUCHED, 2)
    assert (record["occupancy_reads"]["mode"], record["occupancy_reads"]["why"]) == ("full", "transfer_not_on_a_file")


def test_o51_a_requirement_the_scan_found_keeps_its_files_reread(
    client, config, platform_file, ticker, tmp_path, fake, lock
):
    """O51: a QoS replication leaves no transfer record and a kept requirement may still replicate, so every file a
    requirement the planner's scan found applies to is read again, kept or removed."""
    qid = fake.add_qos(FILES[4])
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    reset = SpaceReset(p, now=ticker.clock)
    with lock.held(exclusive=False, timeout_s=1.0), reset.exclusive():
        holdings = reset.plan_scan()
        before = distribution_reads(fake, FILES[4])
        record = reset.run(holdings.plan(), planned=holdings, qos_baseline=[qid], qos_rights=True)
    assert distribution_reads(fake, FILES[4]) - before >= 2
    assert record["occupancy_reads"]["mode"] == "shortcut"


def test_o51_a_reset_of_several_spaces_takes_one_exclusive_section_for_all(
    client, config, platform_file, ticker, tmp_path, fake, lock
):
    """O51: `SpacesReset.exclusive` is the one section of the federation's reset lock: every reading made inside it
    carries its grant, and the driver's reset takes the shortcut."""
    p = provisioner(client, config, platform_file, ticker, tmp_path)
    reset = SpaceReset(p, now=ticker.clock)
    driver = SpacesReset([reset])
    with lock.held(exclusive=False, timeout_s=1.0), driver.exclusive() as grant:
        holdings = reset.plan_scan()
        assert holdings.grant is grant
        before = {path: distribution_reads(fake, path) for path in UNTOUCHED}
        record = driver.run([ResetPlan(holdings.plan(), planned=holdings, qos_rights=True)])
    assert {path: distribution_reads(fake, path) - before[path] for path in UNTOUCHED} == dict.fromkeys(UNTOUCHED, 0)
    assert record["spaces"][0]["occupancy_reads"]["mode"] == "shortcut"


def test_o51_every_hard_link_and_every_file_the_reading_did_not_read_is_reread():
    """O51: a hard link shares its target's blocks under its own id, so a transfer of the target places bytes the link
    shows: every link is read again; so is a file the reading has no figure for (a later scan's)."""
    listing = Listing(regular=frozenset({"f1", "f2", "f3", "f4"}), links=frozenset({"l1"}))
    reading = {"beta": {"f1": 0, "f2": 5, "f3": 0, "l1": 0}}
    only = resetshortcut.files_to_read(listing, reading, touched=frozenset({"f3"}))
    assert only == {"beta": frozenset({"f2", "f3", "f4", "l1"})}


def placement_of(tmp_path, edit):
    path = write_platform(tmp_path, edit)
    return load_placement(path, load_platform(path, require_secrets=False)).settings


def shortcut_without_faults(data):
    two_providers(data)
    data["placement"]["reset_shortcut"] = True


def shortcut_not_a_flag(data):
    with_faults(data)
    data["placement"]["reset_shortcut"] = "yes"


def negative_wait(data):
    with_faults(data)
    data["placement"]["reset_exclusive_wait_s"] = -1


@pytest.mark.parametrize(
    ("edit", "key"),
    [
        (shortcut_without_faults, "reset_shortcut"),
        (shortcut_not_a_flag, "reset_shortcut"),
        (negative_wait, "reset_exclusive_wait_s"),
    ],
)
def test_o51_a_shortcut_declared_without_faults_or_not_as_a_flag_or_a_negative_wait_is_refused(tmp_path, edit, key):
    """O51: the shortcut rests on the federation's reset lock, which a platform without `[faults]` has not: declaring
    it there is refused, naming the key, as is a value that is not true or false, and a negative wait."""
    with pytest.raises(ConfigError, match=key):
        placement_of(tmp_path, edit)


def test_o51_the_shortcut_is_off_by_default_with_a_wait_of_a_minute(tmp_path):
    """O51: a platform that declares neither key has the full reads, and `reset_exclusive_wait_s` defaults to 60 s,
    a value that no longer bounds the wait (O66: every reset waits within `admission_timeout_s`)."""
    settings = placement_of(tmp_path, with_faults)
    assert (settings.reset_shortcut, settings.reset_exclusive_wait_s) == (False, 60.0)
