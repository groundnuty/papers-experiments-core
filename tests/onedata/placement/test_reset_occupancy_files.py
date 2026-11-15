"""The reset's occupancy verdict comes from each file's own block map, summed per provider, never from the space's
directory-statistics counter alone (O46). Op-worker f118ed9 showed both directions: a space root crediting a mirror
1,048,576 B for good while its only file held nothing there, and a placement of 1,572,864 B that the counter showed only
9.534 s later, reading 0 meanwhile. So every provider that can hold the space's bytes has its files read whatever the
counter says, the counter may skip only a provider declared never to hold them, and each disagreement is an event."""

import pytest
from qos_world import FILES, federation, provisioner_with, two_providers

from expcore_onedata import occupancy, resetqos
from expcore_onedata.reset import ResetFailed, SpaceReset
from expcore_onedata.testing import BETA_ID, DATA, write_platform
from expcore_onedata.testing.fake_data import ROOT_ID

MIB = 1_048_576  # what the live space root credited the mirror beyond its files


@pytest.fixture
def fake(monkeypatch, ticker):
    return federation(monkeypatch, ticker)


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture(params=["summaries", "listing"])
def scanned(request, client, config, platform_file, ticker, tmp_path, fake):
    return provisioner_with(client, config, platform_file, ticker, tmp_path, scan=request.param)


def file_reads_in_the_occupancy_stage(monkeypatch, fake) -> list:
    """Collect the block-map reads of regular files on beta that the occupancy stage's reading makes."""
    seen: list = []
    original = SpaceReset._occupancy_read

    def occupancy_reading(self, *args):
        start = len(fake.provider_calls)
        try:
            return original(self, *args)
        finally:
            seen.extend(
                c
                for c in fake.provider_calls[start:]
                if c[0] == BETA_ID and c[2].endswith("/distribution") and ROOT_ID not in c[2]
            )

    monkeypatch.setattr(SpaceReset, "_occupancy_read", occupancy_reading)
    return seen


@pytest.mark.parametrize("rights", [True, False])
def test_o46_a_counter_stale_high_while_every_file_is_empty_passes_with_the_event(scanned, fake, ticker, rights):
    """O46: the live failure. The eviction empties beta's files, the space root goes on crediting beta 1 MiB, and a
    reset that allows beta nothing failed at its occupancy stage with no figure. The files' block maps say beta holds
    nothing: the reset passes, and each stage that read the occupancy records the disagreement with both figures."""
    fake.counter_offset[BETA_ID] = MIB
    record = SpaceReset(scanned, now=ticker.clock).run({"beta": ["study"]}, qos_rights=rights)
    stage = record["stages"]["occupancy"]
    assert record["ok"]
    assert (stage["physical_bytes"]["providers[1]"], stage["files_bytes"]) == (MIB, {"providers[1]": 0})
    event = {"event": occupancy.COUNTER_DISAGREES, "provider": "providers[1]", "counter_bytes": MIB, "files_bytes": 0}
    assert stage["events"] == [event]
    quiet = record["stages"]["quiet"]
    assert quiet["ran"] is rights
    if rights:
        assert quiet["events"] == [event]


def test_o46_a_counter_reading_0_while_a_file_holds_bytes_fails_naming_the_bytes(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the other direction. Beta holds 1,024 bytes of one file, from the middle, as a partial read places them,
    and the counter reads 0. The reset fails at its occupancy stage, and the message states what beta's files hold,
    what the reset allows and what the counter read, by position alone; the record has the event."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))
    fake.blocks[(fake.id_of(FILES[2]), BETA_ID)] = [[1024, 1024]]
    fake.counter_offset[BETA_ID] = -1024
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage") as caught:
        SpaceReset(provisioner, now=ticker.clock).run(None, qos_rights=False)
    message = str(caught.value)
    assert "providers[1] holds 1024 bytes of the space in 1 file(s), beyond what the reset allows (0 bytes" in message
    assert "the space root's directory statistics read 0 bytes" in message
    assert "files[2] 1024 bytes" in message
    assert "study" not in message
    assert fake.id_of(FILES[2]) not in message
    stage = caught.value.record["stages"]["occupancy"]
    assert stage["events"] == [
        {"event": occupancy.COUNTER_DISAGREES, "provider": "providers[1]", "counter_bytes": 0, "files_bytes": 1024}
    ]
    assert stage["over"]["providers[1]"] == {
        "files_bytes": 1024,
        "allowed_bytes": 0,
        "counter_bytes": 0,
        "links_bytes": 0,
        "files": {"files[2]": 1024},
    }


def test_o46_a_file_holding_bytes_beyond_what_is_allowed_fails_whatever_the_counter_says(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: beta keeps every file whole and the counter agrees; the reset allows beta one file's size. The files hold
    six times that: the reset fails, stating the bytes held and the figure allowed."""
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage") as caught:
        SpaceReset(provisioner, now=ticker.clock).run(None, {"beta": len(DATA)}, qos_rights=False)
    held = len(FILES) * len(DATA)
    assert f"providers[1] holds {held} bytes of the space in {len(FILES)} file(s)" in str(caught.value)
    assert f"beyond what the reset allows ({len(DATA)} bytes" in str(caught.value)
    assert caught.value.record["stages"]["occupancy"]["events"] == []


def test_o46_a_counter_of_0_does_not_skip_a_provider_that_can_hold_the_spaces_bytes(
    monkeypatch, client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the counter under-counts (a placement showed 9.534 s late, reading 0 meanwhile), so a provider that can hold
    the space's bytes has every file read, though its counter reads 0 after the eviction."""
    seen = file_reads_in_the_occupancy_stage(monkeypatch, fake)
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    record = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=False)
    assert record["stages"]["occupancy"]["physical_bytes"]["providers[1]"] == 0
    assert record["stages"]["occupancy"]["files_read"] == len(FILES) == len(seen)


def test_o46_a_counter_of_0_skips_the_file_reads_of_a_provider_declared_never_to_hold(
    monkeypatch, client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the fast path. A provider the caller declares never holds the space's bytes, whose counter reads 0, has no
    file read; the record says so."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))
    seen = file_reads_in_the_occupancy_stage(monkeypatch, fake)
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    record = SpaceReset(provisioner, now=ticker.clock).run(None, qos_rights=False, never_holds=["beta"])
    stage = record["stages"]["occupancy"]
    assert (stage["files_read"], stage["files_bytes"], seen) == (0, {}, [])


def test_o46_a_provider_declared_never_to_hold_is_still_read_when_its_counter_is_not_0(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the counter decides nothing but the skip. Declared never to hold, beta's counter reads 1 MiB with every
    file empty: its files are read, and the reset passes with the event."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))
    fake.counter_offset[BETA_ID] = MIB
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    record = SpaceReset(provisioner, now=ticker.clock).run(None, qos_rights=False, never_holds=["beta"])
    stage = record["stages"]["occupancy"]
    assert (stage["files_read"], stage["files_bytes"]) == (len(FILES), {"providers[1]": 0})
    assert [e["counter_bytes"] for e in stage["events"]] == [MIB]


def test_o46_a_never_holds_that_names_the_data_provider_is_refused(provisioner, fake, ticker):
    """O46: the data provider's holdings are never judged, so naming it never to hold is a mistake, refused before any
    stage runs."""
    with pytest.raises(Exception, match="own copy"):
        SpaceReset(provisioner, now=ticker.clock).run(None, never_holds=["alpha"])
    assert fake.provider_calls == []


def test_o46_with_a_stale_counter_a_growth_after_the_occupancy_stage_fails_the_quiet_stage(
    client, config, platform_file, ticker, tmp_path, fake, monkeypatch
):
    """O46, O33: the quiet stage holds the files' block maps, summed per provider, to the occupancy stage's sums, not
    to the counter: with the counter stale by 1 MiB, a file that lands on beta after the occupancy stage, smaller than
    that, fails the quiet stage with the figures."""
    fake.counter_offset[BETA_ID] = MIB
    late = fake.add_file("other/late.bin", DATA)
    original = SpaceReset._occupancy_kept  # the occupancy stage's last step, its reading kept

    def then_land(self, *args):
        facts = original(self, *args)
        fake.blocks[(late, BETA_ID)] = [[0, len(DATA)]]
        return facts

    monkeypatch.setattr(SpaceReset, "_occupancy_kept", then_land)
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"quiet stage.*more of the space after the quiet wait") as caught:
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=True)
    assert f"its files hold {len(DATA)} bytes, 0 at the occupancy stage" in str(caught.value)


def test_o46_the_file_reads_are_bounded_and_fail_closed_with_the_figures(
    monkeypatch, client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the reads are bounded by a request budget: past it the stage fails, closed, naming how many reads it
    needed, rather than fall back on the counter."""
    monkeypatch.setattr(occupancy, "READS_MAX", len(FILES) - 1)
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage") as caught:
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=False)
    assert f"needs {len(FILES)} block-map read(s), past its budget of {len(FILES) - 1}" in str(caught.value)


ERROR_BACKEND = {"success": True, "distributionPerStorageBackend": {"storage-1": {"success": False, "error": {}}}}
NULL_BACKEND = {"success": True, "distributionPerStorageBackend": {"storage-1": {"blocks": None, "physicalSize": None}}}


@pytest.mark.parametrize("shape", ["error", "null", "missing"])
def test_o46_a_block_map_that_is_unknown_never_counts_as_0(
    client, config, platform_file, ticker, tmp_path, fake, shape
):
    """O46, O08: a file's distribution that answers beta's storage as failed, with no size, gives its blocks and size
    as null, or leaves beta out says nothing of what beta holds there (data_distribution_translator.erl): the reset
    fails at its occupancy stage, naming the file's position and the provider, and never passes beta as holding 0."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))
    if shape == "missing":
        fake.omit_providers = {BETA_ID}
    else:
        fake.distribution_entries[(fake.id_of(FILES[2]), BETA_ID)] = ERROR_BACKEND if shape == "error" else NULL_BACKEND
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage") as caught:
        SpaceReset(provisioner, now=ticker.clock).run(None, qos_rights=False)
    position = "files[0]" if shape == "missing" else "files[2]"
    assert f"the block map of {position} at providers[1] is unknown" in str(caught.value)


def test_o46_a_counter_that_is_null_is_unknown_never_0(client, config, platform_file, ticker, tmp_path, fake):
    """O46, O08: the space root's distribution can answer a provider's physical size as null; that is unknown, and the
    reset fails at its occupancy stage rather than read it as 0, though every file's map was read."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))
    original = fake._distribution

    def null_root(node):
        answer = original(node)
        if node.file_id == ROOT_ID:
            backends = answer["distributionPerProvider"][BETA_ID]["distributionPerStorageBackend"]
            backends["storage-1"]["physicalSize"] = None
        return answer

    fake._distribution = null_root
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage.*physical size of the space is unknown"):
        SpaceReset(provisioner, now=ticker.clock).run(None, qos_rights=False)


def test_o46_a_file_a_providers_own_listing_missed_is_still_read_there(
    monkeypatch, client, config, platform_file, ticker, tmp_path, fake
):
    """O46: beta's listing misses a file alpha's shows, and beta holds it whole. Every listed file is read at every
    provider, so the reset fails on it rather than pass beta on the files its own listing showed."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))
    missed = fake.id_of(FILES[4])
    fake.blocks[(missed, BETA_ID)] = [[0, len(DATA)]]
    original = resetqos.tree

    def tree(p, provider, reads):
        root, leaves = original(p, provider, reads)
        return root, [leaf for leaf in leaves if provider != "beta" or leaf[0] != missed]

    monkeypatch.setattr(resetqos, "tree", tree)
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage") as caught:
        SpaceReset(provisioner, now=ticker.clock).run(None, qos_rights=False)
    assert f"providers[1] holds {len(DATA)} bytes of the space in 1 file(s)" in str(caught.value)


def test_o46_a_provider_no_declaration_fits_fails_the_occupancy_whatever_its_counter(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: a provider no declaration fits supports the space, its counter at 0. Its files cannot be read there, and
    the counter is no verdict, so the reset fails, closed, rather than pass it on that 0."""
    fake.add_provider("gamma-id", "gamma.test")
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage.*no declaration fits supports the space"):
        SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]}, qos_rights=False)


def test_o46_a_provider_declared_never_to_hold_that_the_counter_leaves_out_is_read(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the fast path needs a 0 read. Declared never to hold, beta is left out of the space root's answer, and the
    reset lists alpha alone: beta is read all the same, and the files it holds fail the reset."""
    original = fake._distribution

    def without_beta(node):
        answer = original(node)
        if node.file_id == ROOT_ID:
            answer["distributionPerProvider"].pop(BETA_ID)
        return answer

    fake._distribution = without_beta
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage") as caught:
        SpaceReset(provisioner, providers=["alpha"], now=ticker.clock).run(None, never_holds=["beta"])
    assert f"providers[1] holds {len(FILES) * len(DATA)} bytes" in str(caught.value)


def test_o46_bytes_in_a_hard_link_are_counted_and_the_refusal_says_so(
    client, config, platform_file, ticker, tmp_path, fake
):
    """O46: a hard link is listed with its own id and shares its target's blocks. Its bytes are counted, so a target
    deleted under its link never hides them; a refusal with bytes in a link says they may be counted twice."""
    for path in FILES:
        fake.blocks.pop((fake.id_of(path), BETA_ID))
    link = fake._create(fake.id_of("study"), "link.bin", "LNK")
    fake.nodes[link].content = bytearray(DATA)
    fake.blocks[(link, BETA_ID)] = [[0, len(DATA)]]
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage") as caught:
        SpaceReset(provisioner, now=ticker.clock).run(None, qos_rights=False)
    assert f"{len(DATA)} bytes of it are in hard link(s)" in str(caught.value)


def test_o46_the_time_bound_is_looked_at_after_the_last_batch(
    monkeypatch, client, config, platform_file, ticker, tmp_path, fake
):
    """O46: a reading whose only batch ends past the bound fails, closed, rather than return a verdict read too
    slowly."""
    monkeypatch.setattr(occupancy, "READ_BOUND_S", 0.5)
    original = fake._distribution

    def slow(node):
        if node.file_id != ROOT_ID:
            ticker.sleep(1.0)
        return original(node)

    fake._distribution = slow
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    with pytest.raises(ResetFailed, match=r"occupancy stage.*past its bound"):
        SpaceReset(provisioner, now=ticker.clock).run(None, {"beta": len(FILES) * len(DATA)}, qos_rights=False)


def test_o46_one_budget_covers_every_call_of_a_reading(
    monkeypatch, client, config, platform_file, ticker, tmp_path, fake
):
    """O46: the request budget is the reading's, its calls together: a second call that would pass it fails."""
    monkeypatch.setattr(occupancy, "READS_MAX", len(FILES) + 2)
    provisioner = provisioner_with(client, config, platform_file, ticker, tmp_path)
    ids = sorted(fake.id_of(path) for path in FILES)
    budget = occupancy.Budget(ticker.now, retry=provisioner.reading_retry())
    occupancy.read_files(provisioner, {"beta": ids}, provisioner.client.provider_label, budget)
    with pytest.raises(occupancy.ProvisionError, match=rf"needs {2 * len(FILES)} block-map read\(s\)"):
        occupancy.read_files(provisioner, {"beta": ids}, provisioner.client.provider_label, budget)
