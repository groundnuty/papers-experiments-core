"""Range placement: bytes are placed by range reads through the receiving provider, confirmed by the block map (O02)."""

import pytest
from placement_support import ALPHA_ID, BETA_ID, DATA

from expcore_onedata.provision import NotPlaced, ProvisionError
from expcore_onedata.rangemap import RangeRow
from expcore_onedata.ranges import place_ranges

PATH = "study/data.bin"
ROW = RangeRow(PATH, "beta", ((0, 100), (2000, 500)))


def sent_to(fake, provider_id):
    return [call for call in fake.provider_calls if call[0] == provider_id]


def test_o02_a_range_placement_issues_range_reads_and_never_a_transfer_job(provisioner, seeded):
    """O02: the declared ranges arrive on the receiving provider through reads, and no transfer is ever scheduled."""
    record = place_ranges(provisioner, [ROW])
    assert [c for c in seeded.provider_calls if c[2].startswith("/transfers")] == []
    reads = [c for c in sent_to(seeded, BETA_ID) if c[2].endswith("/content")]
    assert len(reads) == 2  # one read per declared range
    assert [c for c in sent_to(seeded, ALPHA_ID) if c[2].endswith("/content")] == []  # the data provider is not read
    assert seeded.held(PATH, BETA_ID) == [[0, 100], [2000, 500]]  # exactly the declared bytes, not the whole file
    assert record["providers"]["beta"]["bytes_read"] == 600
    assert record["providers"]["beta"]["placement"]["fully_placed"] is True


def test_o02_the_placement_is_confirmed_by_the_block_map_after_it_lags(provisioner, seeded, ticker):
    """O02: bytes that show in the map only after some polls are waited for, within the bound."""
    seeded.fetch_lag = 4
    place_ranges(provisioner, [ROW])
    assert len(ticker.slept) >= 4
    assert seeded.held(PATH, BETA_ID) == [[0, 100], [2000, 500]]


def test_o02_reads_that_leave_the_block_map_short_are_not_placed(provisioner, seeded):
    """O02 with O01: a provider that answers the reads but never records the blocks has not placed anything."""
    seeded.onthefly_places = False
    with pytest.raises(NotPlaced, match="short") as raised:
        place_ranges(provisioner, [ROW])
    assert raised.value.report.bytes_short == 600


def test_o02_a_range_beyond_the_end_of_its_file_is_refused_before_any_read(provisioner, seeded):
    """O02: clipping a declared range would place less than declared, so the map is refused and nothing is read."""
    with pytest.raises(ProvisionError, match="beyond the end"):
        place_ranges(provisioner, [RangeRow(PATH, "beta", ((4000, 500),))])
    assert [c for c in seeded.provider_calls if c[2].endswith("/content")] == []


def test_o02_a_provider_that_must_start_empty_and_does_not_is_refused_before_any_read(provisioner, seeded):
    """O02: bytes cached by an earlier run would pass as placed, so the cold-start rule stops before reading."""
    place_ranges(provisioner, [ROW])
    before = len(seeded.provider_calls)
    with pytest.raises(NotPlaced, match="does not start empty"):
        place_ranges(provisioner, [ROW], start_empty=True)
    assert [c for c in seeded.provider_calls[before:] if c[2].endswith("/content")] == []


def test_o02_long_ranges_are_read_in_blocks_of_the_configured_size(provisioner, seeded):
    """O02: no single read is longer than chunk_bytes (1024 in these tests)."""
    place_ranges(provisioner, [RangeRow(PATH, "beta", ((0, 3000),))])
    reads = [r for r in seeded.requests if r.range]
    assert [r.range for r in reads] == ["bytes=0-1023", "bytes=1024-2047", "bytes=2048-2999"]
    assert seeded.held(PATH, BETA_ID) == [[0, 3000]]


def test_o02_the_bytes_read_are_the_files_bytes(client, provisioner, seeded):
    """The range read returns the file's own bytes at that offset."""
    place_ranges(provisioner, [ROW])
    entry = provisioner.resolve(PATH)
    data = client.read_range(f"/data/{entry.file_id}/content", provider="beta", offset=2000, length=500)
    assert data == DATA[2000:2500]


def test_placing_nothing_is_an_error(provisioner, seeded):
    """An empty map is a mistake, not a placement that trivially succeeded."""
    with pytest.raises(ProvisionError, match="no ranges"):
        place_ranges(provisioner, [])
