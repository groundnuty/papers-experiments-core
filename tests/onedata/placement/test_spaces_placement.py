"""Several spaces in provisioning, placement, eviction, consistency waits and probes: each is bound to one space by its
label, acts in that space alone, and names it by its position; and every operation over many files lists each
directory once."""

import pytest
from fake_reset import FakeReset
from sandbox_support import add_sandbox

from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.eviction import evict_tree
from expcore_onedata.health import PlatformHealth
from expcore_onedata.precampaign import content_type_check
from expcore_onedata.probes import NAMESPACE, PlatformProbes
from expcore_onedata.provision import Provisioner, ProvisionError, Refused, resolve_provider_ids
from expcore_onedata.rangemap import RangeRow, load_placement
from expcore_onedata.ranges import place_ranges
from expcore_onedata.reset import SpaceReset
from expcore_onedata.testing import (
    ADMIN_ENV,
    ALPHA_ID,
    BASELINE,
    BETA_ID,
    DATA,
    SPACE_NAME,
    USERS,
    with_placement,
    write_platform,
)
from expcore_onedata.testing.world import SPACE_IDS, SPACE_LABELS, SPACE_NAMES, with_spaces
from expcore_onedata.tokens import TokenIssuer

OWNED = SPACE_LABELS[1]  # the second space, whose own data provider is beta


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeReset:
    """The study space and a second space, both supported by alpha and beta, and the users in both."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    federation.add_data_space(SPACE_IDS[1], SPACE_NAMES[1], [ALPHA_ID, BETA_ID])
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        for space in SPACE_IDS[:2]:
            federation.grant(user_id, BASELINE, space=space)
    return federation


def two_spaces(data, rows=None):
    with_placement(data, transfer_settle_s=5.0, transfer_settle_polls=3)
    with_spaces(data, 2)
    data["spaces"][1]["holder"] = "beta"
    if rows is not None:
        data["placement"]["map"] = rows


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_spaces)


def bound(client, config, placement, ticker, tmp_path, space=OWNED):
    return Provisioner(
        client,
        config,
        placement.settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / f"{space}.jsonl",
        space=space,
        clock=ticker.clock,
        sleep=ticker.sleep,
    )


@pytest.fixture
def owned(client, config, placement, ticker, tmp_path):
    return bound(client, config, placement, ticker, tmp_path)


@pytest.fixture
def provisioner(client, config, placement, ticker, tmp_path):
    """The study space's provisioner, named by its label as several spaces require."""
    return bound(client, config, placement, ticker, tmp_path, space=SPACE_LABELS[0])


def test_o09_a_provisioner_bound_to_a_space_creates_and_tears_down_in_that_space_alone(owned, fake):
    """O09: a file put through the second space's provisioner lands under that space's root, on its own data
    provider, and its teardown leaves the study space untouched."""
    fake.add_file("study/keep.bin", DATA)
    owned.put_file("scratch/new.bin", DATA)
    assert fake.exists("scratch/new.bin", SPACE_IDS[1])
    assert not fake.exists("scratch/new.bin")
    assert fake.held("scratch/new.bin", BETA_ID, SPACE_IDS[1]) == [[0, len(DATA)]]
    assert owned.root() == f"root-{SPACE_IDS[1]}"
    assert owned.teardown().ok
    assert not fake.exists("scratch", SPACE_IDS[1])
    assert fake.exists("study/keep.bin")


def test_o12_with_several_spaces_a_provisioner_must_name_its_space_and_its_messages_name_it_by_position(
    client, config, placement, ticker, tmp_path, owned, fake
):
    """O12: no label, or one not declared, is refused naming no space; a bound provisioner's refusal starts with the
    space's position, never its label or name."""
    for label in (None, "nowhere"):
        with pytest.raises(ConfigError):
            bound(client, config, placement, ticker, tmp_path, space=label)
    fake.add_file("study/a.bin", DATA, space=SPACE_IDS[1])
    with pytest.raises(ProvisionError) as caught:
        owned.put_file("study/a.bin", DATA)
    assert str(caught.value).startswith("spaces[1]: the file already exists")


def test_o09_each_spaces_own_data_provider_is_never_evicted_there(owned, fake):
    """O09: in the second space beta holds the source copy, so an eviction from beta is refused there by name, and an
    eviction from alpha, the study space's data provider, is not."""
    file_id = fake.add_file("study/a.bin", DATA, holder=BETA_ID, space=SPACE_IDS[1])
    fake.blocks[(file_id, ALPHA_ID)] = [[0, len(DATA)]]
    with pytest.raises(Refused, match=r"^spaces\[1\]: refusing to evict"):
        evict_tree(owned, "study", "beta")
    assert evict_tree(owned, "study", "alpha")["files"] == 1
    assert fake.held("study/a.bin", ALPHA_ID, SPACE_IDS[1]) == []
    assert fake.held("study/a.bin", BETA_ID, SPACE_IDS[1]) == [[0, len(DATA)]]


def test_o09_a_space_is_checked_by_its_own_live_name_before_anything_changes(owned, fake):
    """O09: the second space renamed out of its declared name refuses its provisioner, naming it by position, and
    sends nothing; the study space's name plays no part."""
    fake.other_spaces[SPACE_IDS[1]] = "study-a-renamed"
    with pytest.raises(Refused, match=r"^spaces\[1\]: the space's live name is not the configured one"):
        owned.put_file("scratch/new.bin", DATA)
    assert [c for c in fake.provider_calls if c[1] in ("POST", "PUT", "DELETE")] == []


def test_o08_probes_of_a_space_have_a_namespace_of_their_own_and_list_that_spaces_transfers(
    owned, client, fake, ticker
):
    """O08: one instrument per space: the second space's probes are `platform_probes_space1`, the snapshot names the
    space by position, and its transfer listing is the second space's."""
    fake.add_file("study/a.bin", DATA, holder=BETA_ID, space=SPACE_IDS[1])
    probes = PlatformProbes(
        client, owned, {"data": "study/a.bin"}, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock
    )
    assert probes.namespace == f"{NAMESPACE}_space1"
    probes.start({})
    snapshot = probes.finish()
    assert snapshot["space"] == "spaces[1]"
    assert snapshot["files"]["data"]["entry"]["probe_ok"] is True
    listings = {c[2] for c in fake.provider_calls if c[2].endswith("/transfers")}
    assert listings == {f"/spaces/{SPACE_IDS[1]}/transfers"}


def test_o08_probes_list_each_directory_once_and_retry_a_listing_that_failed(client, provisioner, fake, ticker):
    """O08: a reading resolves its files with one listing of each directory; a listing that failed is not kept, so
    the next file's probe reads it afresh, and only the file whose own read failed is failed."""
    files = {f"f{i}": f"study/deep/part-{i}.bin" for i in range(6)}
    for path in files.values():
        fake.add_file(path, DATA)
    probes = PlatformProbes(client, provisioner, files, ["alpha"], sleep=ticker.sleep, now=ticker.clock)
    probes.start({})
    deep = fake.id_of("study/deep")
    assert len([c for c in fake.provider_calls if c[2] == f"/data/{deep}/children"]) == 1
    fake.statuses.append([f"/data/{deep}/children", 1, 500])
    snapshot = probes.finish()
    entries = [snapshot["files"][label]["entry"]["probe_ok"] for label in files]
    assert entries == [False, True, True, True, True, True]


def test_o02_a_range_placement_lists_each_directory_once_whatever_the_number_of_files(provisioner, fake):
    """O02: placing ranges of many files in one directory resolves them with one listing of each directory on the
    way, where resolving file by file listed the directory again for each."""
    paths = [f"study/many/part-{i:02d}.bin" for i in range(15)]
    for path in paths:
        fake.add_file(path, DATA)
    place_ranges(provisioner, [RangeRow(path, "beta", ((0, 64),), SPACE_LABELS[0]) for path in paths])
    many = fake.id_of("study/many")
    assert len([c for c in fake.provider_calls if c[2] == f"/data/{many}/children"]) == 1
    assert all(fake.held(path, BETA_ID) == [[0, 64]] for path in paths)


def test_o02_range_rows_name_their_space_and_a_provisioner_places_its_own_spaces_rows_only(
    tmp_path, owned, fake, config
):
    """O02: with several spaces a row names its space by label, or the map is refused naming the row and the key; a
    provisioner refuses rows of another space before anything is read, and places its own space's rows."""
    for sub in ("x", "y"):
        (tmp_path / sub).mkdir()
    unnamed = write_platform(
        tmp_path / "x", lambda d: two_spaces(d, [{"file": "a", "provider": "beta", "ranges": [[0, 1]]}])
    )
    with pytest.raises(ConfigError, match=r"row 1: key 'space'"):
        load_placement(unnamed, load_platform(unnamed))
    fake.add_file("study/a.bin", DATA, holder=BETA_ID, space=SPACE_IDS[1])
    rows = [{"file": "study/a.bin", "provider": "alpha", "ranges": [[0, 64]], "space": OWNED}]
    named = write_platform(tmp_path / "y", lambda d: two_spaces(d, rows))
    placement = load_placement(named, load_platform(named))
    assert placement.map.for_space(OWNED) == (RangeRow("study/a.bin", "alpha", ((0, 64),), OWNED),)
    with pytest.raises(ProvisionError, match=r"^spaces\[1\]: a row lies in another space"):
        place_ranges(owned, [RangeRow("study/a.bin", "alpha", ((0, 64),), SPACE_LABELS[0])])
    assert [c for c in fake.provider_calls if c[2].endswith("/content")] == []
    place_ranges(owned, placement.map.for_space(OWNED))
    assert fake.held("study/a.bin", ALPHA_ID, SPACE_IDS[1]) == [[0, 64]]


def test_o11_a_reset_of_one_space_reads_and_changes_that_space_alone(owned, fake, ticker):
    """O11: the second space's reset lists that space's transfers on its providers, cancels its running job, and
    leaves the study space's running job alone."""
    fake.add_file("study/a.bin", DATA, holder=BETA_ID, space=SPACE_IDS[1])
    fake.add_file("study/b.bin", DATA)
    mine = fake.add_transfer("replication", "study/b.bin", ALPHA_ID, scheduled=ticker.now, ended=False)
    fake.transfers[mine].file_id = fake.id_of("study/a.bin", SPACE_IDS[1])
    theirs = fake.add_transfer("replication", "study/b.bin", BETA_ID, scheduled=ticker.now, ended=False)
    fake.transfer_flip_after = 10**6
    record = SpaceReset(owned, now=ticker.clock).run()
    assert record["space"] == "spaces[1]"
    assert fake.transfers[mine].cancelled
    assert not fake.transfers[theirs].ended
    assert {c[2] for c in fake.provider_calls if c[2].endswith("/transfers")} == {f"/spaces/{SPACE_IDS[1]}/transfers"}


def test_o05_the_content_type_check_reads_its_file_in_the_first_space_the_data_path_reaches(tmp_path, fake, ticker):
    """O05: with several spaces and a REST data path, the pre-campaign check finds its file under the root of the first
    space the data path's token reaches, here the second, through the agent's local provider; and the platform's
    health check, which composes it, reads every space without refusing on the several-space configuration."""

    def document(data):
        two_spaces(data)
        add_sandbox(data)
        data["sandbox"]["content_check_file"] = "study/probing.bin"
        data["spaces"][0]["tokens"] = ["tool_server"]

    config = load_platform(write_platform(tmp_path, document))
    client = OnezoneClient(config, http=fake.client_http())
    fake.add_file("study/probing.bin", DATA, space=SPACE_IDS[1])
    verdict = content_type_check(config, client)
    assert verdict.ok, verdict.detail
    listed = {c[2] for c in fake.provider_calls if c[2].endswith("/children")}  # the gate reads every space's root
    assert f"/data/{fake.space_roots[SPACE_IDS[1]]}/children" in listed
    assert f"/data/{fake.space_roots[SPACE_IDS[0]]}/children" not in listed
    health = PlatformHealth(config, client, TokenIssuer(client, config, clock=ticker.clock), clock=ticker.clock)
    assert health().ok
