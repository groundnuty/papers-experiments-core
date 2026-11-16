"""A reset of a space on a shared imported tree touches none of the tree's files (O56).

The reset evicts replicas from the mirrors, cancels transfers and removes the run's own QoS requirements. None of those
reads or changes the data provider's storage, and the one thing that would (an eviction from the data provider, which
takes its blocks for good on a read-only storage) is refused before any stage. What a reset cannot do for such a space
is delete or rewrite files, and it never tries: it has no such stage."""

import pytest
from fake_reset import FakeReset

from expcore_onedata.eviction import evict_ids, evict_tree
from expcore_onedata.provision import Refused
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

FILES = [f"study/part-{i:02d}.bin" for i in range(6)]


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeReset:
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        federation.grant(user_id, BASELINE)
    return federation


def document(data):
    """Alpha holds the space on a read-only imported storage that other federations import too; beta mirrors it."""
    with_placement(data, transfer_settle_s=5.0, transfer_settle_polls=3, concurrency=4)
    data["support"]["providers"] = ["alpha", "beta"]
    data["space"]["import"] = {"readonly": True, "shared": True}


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, document)


@pytest.fixture
def stocked(fake) -> FakeReset:
    """Six files the data provider holds whole, as an import makes them, and a mirror holds replicas of."""
    for path in FILES:
        file_id = fake.add_file(path, DATA)
        fake.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]
    return fake


def writes_to_files(fake):
    """Every request that could create, change or delete a file or directory of the space, through any provider."""
    return [c for c in fake.provider_calls if c[1] in ("POST", "PUT", "PATCH", "DELETE") and c[2].startswith("/data/")]


def evictions(fake):
    return [c for c in fake.provider_calls if c[1:] == ("POST", "/transfers")]


def test_o56_a_reset_of_a_shared_imported_space_evicts_the_mirror_and_writes_no_file(provisioner, stocked, ticker):
    """O56: every stage runs; the mirror's replicas leave; no request to `/data/` that writes or deletes is made, and
    the data provider holds every file whole, as the import made it."""
    assert provisioner.read_only is True
    record = SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})
    assert record["ok"] is True
    assert writes_to_files(stocked) == []
    assert not any(stocked.held(path, BETA_ID) for path in FILES)
    assert all(stocked.held(path, ALPHA_ID) == [[0, len(DATA)]] for path in FILES)


def test_o56_every_eviction_a_reset_sends_names_the_mirror_and_none_the_data_provider(provisioner, stocked, ticker):
    """O56: the eviction jobs of the reset carry the mirror's id as the evicting provider; the data provider's is in
    none, so none takes the imported tree's blocks."""
    SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})
    assert evictions(stocked)
    jobs = [transfer for transfer in stocked.transfers.values() if transfer.kind == "eviction"]
    assert jobs
    assert {transfer.provider_id for transfer in jobs} == {BETA_ID}


def test_o56_a_reset_plan_that_names_the_data_provider_is_refused_before_any_stage(provisioner, stocked, ticker):
    """O56, O09: on an imported tree the data provider's copy is the only one, and an eviction of it cannot be undone
    (nothing is replicated back onto a read-only storage): refused with nothing sent, not even a read."""
    with pytest.raises(Refused, match="own copy"):
        SpaceReset(provisioner, now=ticker.clock).run({"alpha": ["study"]})
    assert stocked.provider_calls == []


def test_o56_a_bulk_eviction_from_the_data_provider_is_refused_on_a_shared_imported_space(provisioner, stocked):
    """O56, O09: by name, for a directory and for files alike."""
    with pytest.raises(Refused, match="own copy"):
        evict_tree(provisioner, "study", "alpha")
    with pytest.raises(Refused, match="own copy"):
        evict_ids(provisioner, [stocked.id_of(FILES[0])], "alpha")
    assert evictions(stocked) == []


def test_o56_a_reset_removes_the_runs_own_requirement_without_touching_a_file(provisioner, stocked, ticker):
    """O56: a QoS requirement the agent added is removed (a requirement is metadata, not a file), through the gate and
    nothing under `/data/`; the files stay as the import made them."""
    stocked.add_qos(FILES[2])
    SpaceReset(provisioner, now=ticker.clock).run({"beta": ["study"]})
    assert stocked.qos == {}
    assert writes_to_files(stocked) == []
    assert all(stocked.held(path, ALPHA_ID) == [[0, len(DATA)]] for path in FILES)


def test_o56_the_teardown_of_a_provisioner_that_created_nothing_deletes_nothing(provisioner, stocked):
    """O56, O09: a teardown deletes only what its own log says it created; in a space whose every file an import made,
    that is nothing, so no request leaves and the report says everything is in place."""
    report = provisioner.teardown()
    assert report.ok is True
    assert writes_to_files(stocked) == []
    assert all(stocked.held(path, ALPHA_ID) == [[0, len(DATA)]] for path in FILES)
