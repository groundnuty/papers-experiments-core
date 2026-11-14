"""A reset planner's reading (`resetplan.plan_scan`): one recursive listing gives the files and the QoS scan, each
file's holdings are read at every provider that can hold the space's bytes, and the reset given the reading lists the
space no second time (O33, O46)."""

import dataclasses

import pytest
from qos_world import FILES, calls, federation, provisioner_with, two_providers

from expcore_onedata.reset import SpaceReset
from expcore_onedata.testing import BETA_ID, DATA, write_platform


@pytest.fixture
def fake(monkeypatch, ticker):
    return federation(monkeypatch, ticker)


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture
def listing(client, config, platform_file, ticker, tmp_path, fake):
    return provisioner_with(client, config, platform_file, ticker, tmp_path, scan="listing")


def listings(fake) -> list:
    """The pages of the space root's recursive listing read, on any provider."""
    return [c for c in calls(fake, "/files") if "/data/" in c[2]]


def test_o46_one_listing_gives_the_files_and_each_files_holdings_at_every_provider_that_can_hold(listing, fake, ticker):
    """O51: the planner lists each provider once and reads each regular file's block map at beta, the one provider
    that can hold the space's bytes; the data provider is never read. The plan names only the files beta holds."""
    fake.blocks.pop((fake.id_of(FILES[0]), BETA_ID))  # beta holds every file of `study` but the first
    holdings = SpaceReset(listing, now=ticker.clock).plan_scan()
    assert len(listings(fake)) == 2  # one page on each provider
    assert set(holdings.held) == {"beta"}
    assert holdings.held["beta"] == {fake.id_of(path): (0 if path == FILES[0] else len(DATA)) for path in FILES}
    assert holdings.plan() == {"beta": sorted(FILES[1:])}
    figures = holdings.facts(listing.client.provider_label)
    assert figures["providers"]["providers[1]"] == {
        "files_read": len(FILES),
        "held_files": len(FILES) - 1,
        "held_bytes": (len(FILES) - 1) * len(DATA),
    }


def test_o46_a_provider_declared_never_to_hold_has_no_file_read_and_the_counter_never_skips_one(listing, fake, ticker):
    """O51: a provider the caller declares never holds the space's bytes is not read; one that can hold them is read
    whatever the space root's counter says, since the counter under-counts."""
    fake.counter_offset[BETA_ID] = -(len(FILES) * len(DATA))  # the counter reads 0 for beta
    reset = SpaceReset(listing, now=ticker.clock)
    assert reset.plan_scan(never_holds=["beta"]).held == {}
    assert len(reset.plan_scan().held["beta"]) == len(FILES)


def test_o33_a_reset_given_the_planners_reading_lists_the_space_no_second_time(listing, fake, ticker):
    """O51: the planner's scan is the qos stage's first: the reset reads no page of the recursive listing of its own
    when it removes nothing, and records that the scan was given, with its age."""
    reset = SpaceReset(listing, now=ticker.clock)
    holdings = reset.plan_scan()
    ticker.sleep(3.0)
    before = len(listings(fake))
    record = reset.run(holdings.plan(), planned=holdings, qos_rights=False)
    assert len(listings(fake)) == before
    assert (record["stages"]["qos"]["scan"], record["stages"]["qos"]["scan_age_s"]) == ("given", 3.0)
    assert fake.held(FILES[1], BETA_ID) == []


def test_o33_a_requirement_the_planners_scan_found_is_removed_and_the_second_scan_is_the_resets_own(
    listing, fake, ticker
):
    """O51: the planner's scan finds a requirement the run added; the reset removes it and makes the scan after the
    removal itself, which lists the space again."""
    fake.add_qos(FILES[2])
    reset = SpaceReset(listing, now=ticker.clock)
    holdings = reset.plan_scan()
    before = len(listings(fake))
    record = reset.run(holdings.plan(), planned=holdings)
    assert record["stages"]["qos"]["removed"] == 1
    assert fake.qos == {}
    assert len(listings(fake)) == before + 2  # the second scan's page on each provider


def test_o51_a_planners_reading_of_another_space_is_refused_by_the_spaces_id_before_any_stage(listing, fake, ticker):
    """O51: a reading is bound to the space it listed by the space's Onezone id, never by a position a configuration
    gives, so a scan of another space, which another configuration may put at the same position, stands for no scan
    of this one: it is refused before anything is sent."""
    holdings = SpaceReset(listing, now=ticker.clock).plan_scan()
    elsewhere = dataclasses.replace(holdings, scan=dataclasses.replace(holdings.scan, space="sp-another-0009"))
    calls_before = len(fake.provider_calls)
    with pytest.raises(ValueError, match="of its own space"):
        SpaceReset(listing, now=ticker.clock).run(planned=elsewhere)
    assert len(fake.provider_calls) == calls_before


def test_o09_a_planners_reading_of_another_space_or_providers_is_refused_before_any_stage(listing, fake, ticker):
    """O51: a reading made through a reset of other providers stands for no scan of this reset; it is refused before
    anything is sent."""
    holdings = SpaceReset(listing, providers=["alpha"], now=ticker.clock).plan_scan()
    calls_before = len(fake.provider_calls)
    with pytest.raises(ValueError, match="providers the reset reads"):
        SpaceReset(listing, now=ticker.clock).run(planned=holdings)
    assert len(fake.provider_calls) == calls_before
