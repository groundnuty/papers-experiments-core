"""The preallocated ingest's safety conditions (O44, #107): every staged copy checked before the first delete, the
reset restoring each file whole at its path and the mirror's replica of it, the old-to-new id map, and a failure
mid-start or mid-reset marking the federation unhealthy until a verified recovery."""

import json

import pytest

from expcore.errors import InfraError
from expcore_onedata.ingest import ProgressiveIngest
from expcore_onedata.ingestplan import load_ingest_plan
from expcore_onedata.sandbox.kinds import INGEST_FAILURE
from expcore_onedata.testing import ALPHA_ID, BETA_ID, with_placement, write_platform

DAY = bytes((i * 31 + i // 97) % 256 for i in range(10240))
KEPT = b"an earlier day, never touched\n" * 20
DAY_PATH, KEPT_PATH = "records/day-13.csv", "records/day-12.csv"
PREFIX = 1024
ROW = {
    "label": "day-13",
    "path": DAY_PATH,
    "source": "day-13.csv",
    "at_s": 30,
    "baseline": "complete",
    "prefix": PREFIX,
    "parts": 3,
    "over_s": 60,
}
KEPT_ROW = {
    "label": "day-12",
    "path": KEPT_PATH,
    "source": "day-12.csv",
    "at_s": 10,
    "baseline": "complete",
    "prefix": 100,
}
CHANGES = ("POST", "PUT", "DELETE")


def two_providers(data):
    with_placement(data)
    data["support"]["providers"] = ["alpha", "beta"]


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture
def fake(fake):
    """beta supports the space too; the dataset's baseline is two days, both held whole by the data provider."""
    fake.add_provider(BETA_ID, "beta.test")
    fake.add_file(KEPT_PATH, KEPT)
    fake.add_file(DAY_PATH, DAY)
    return fake


def write_plan(tmp_path, rows):
    staging = tmp_path / "staged"
    staging.mkdir(exist_ok=True)
    (staging / "day-13.csv").write_bytes(DAY)
    (staging / "day-12.csv").write_bytes(KEPT)
    body = "".join(
        "\n[[ingest.files]]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in row.items()) for row in rows
    )
    path = tmp_path / "ingest.toml"
    path.write_text('[ingest]\nstaging = "staged"\nmode = "preallocated"\n' + body, encoding="utf-8")
    return path


@pytest.fixture
def make(provisioner, config, tmp_path):
    """Build an instrument over a preallocated plan; every one a test builds shares the journal and the change log."""
    built = []

    def build(*rows):
        plan = load_ingest_plan(write_plan(tmp_path, rows), config)
        ingest = ProgressiveIngest(provisioner, plan, journal=tmp_path / "journal.jsonl", max_wait_s=0.001)
        built.append(ingest)
        return ingest

    yield build
    for ingest in built:
        ingest.stop()


def changes(fake, since=0):
    return [c for c in fake.provider_calls[since:] if c[1] in CHANGES]


def content(fake, path):
    return bytes(fake.nodes[fake.id_of(path)].content)


# -- 1. every staged copy before the first delete ----------------------------------------------------------------------


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_o44_a_damaged_or_missing_staged_copy_of_any_file_refuses_the_start_before_anything_is_removed(
    make, fake, tmp_path, damage
):
    """O44: the second file's staged copy no longer has the plan's hash (or is gone), so the start is refused before
    any file is removed, the first one included: nothing is created, written or deleted, and both days stay whole."""
    ingest = make(ROW, KEPT_ROW)
    staged = tmp_path / "staged" / "day-12.csv"
    if damage == "missing":
        staged.unlink()
    else:
        staged.write_bytes(KEPT[:-1] + b"?")
    with pytest.raises(InfraError) as refused:
        ingest.start({})
    assert refused.value.kind == INGEST_FAILURE
    assert changes(fake) == []
    assert (content(fake, DAY_PATH), content(fake, KEPT_PATH)) == (DAY, KEPT)
    assert (fake.held(DAY_PATH, ALPHA_ID), fake.held(DAY_PATH, BETA_ID)) == ([[0, len(DAY)]], [])


# -- 2. the reset restores each file whole, and the mirror's replica of it -------------------------------------------


MIRRORED = {**ROW, "replicas": ["beta"]}


@pytest.fixture
def mirrored(fake, ticker):
    """beta, the mirror, holds a replica of the day in the baseline, as an outage's mirror variant reads it."""
    fake.add_transfer("replication", DAY_PATH, BETA_ID, scheduled=ticker.now)
    assert fake.held(DAY_PATH, BETA_ID) == [[0, len(DAY)]]
    return fake


def test_o44_the_reset_restores_the_mirrors_replica_of_the_file_the_start_made_again(make, mirrored, ticker):
    """O44: the start removed the day with its mirror's replica and made it again, so the mirror holds nothing of the
    new file during the run; the reset completes it in place at the data provider, by its hash and the owner's block
    map, and replicates it to the mirror, whose own block map then covers it, so a run that reads through the mirror
    next starts from the baseline's mirror state."""
    ingest = make(MIRRORED)
    ingest.start({})
    assert (mirrored.held(DAY_PATH, ALPHA_ID), mirrored.held(DAY_PATH, BETA_ID)) == ([[0, PREFIX]], [])
    ingest.at_first_turn({"clock_s": 0.0})
    snapshot = ingest.finish()
    ingest.check()
    assert content(mirrored, DAY_PATH) == DAY
    assert (mirrored.held(DAY_PATH, ALPHA_ID), mirrored.held(DAY_PATH, BETA_ID)) == ([[0, len(DAY)]], [[0, len(DAY)]])
    assert snapshot["restore"]["files"]["day-13"]["replicas"] == {"providers[1]": "replicated"}
    assert snapshot["start"]["restore"]["day-13"]["replicas"] == {"providers[1]": "held"}


def test_o44_a_reset_whose_replication_places_nothing_on_the_mirror_fails(make, mirrored):
    """O44: the replication reports completed and the mirror's block map shows nothing of the file: the map judges,
    so the reset fails and the post-run check refuses the attempt; the data provider's copy is whole all the same."""
    ingest = make(MIRRORED)
    ingest.start({})
    ingest.at_first_turn({"clock_s": 0.0})
    mirrored.transfers_place_blocks = False
    snapshot = ingest.finish()
    assert snapshot["restore"]["ok"] is False
    assert snapshot["restore"]["files"]["day-13"]["ok"] is False
    with pytest.raises(InfraError, match="restore"):
        ingest.check()
    assert (content(mirrored, DAY_PATH), mirrored.held(DAY_PATH, BETA_ID)) == (DAY, [])


def test_o44_a_replica_the_plan_does_not_declare_refuses_the_start_before_anything_is_removed(make, mirrored):
    """O44: the mirror holds the day but the plan declares no replica: the start would remove it and the reset would
    not bring it back, so the start is refused with nothing removed, the mirror's replica kept."""
    ingest = make(ROW)
    deletes = sum(1 for c in mirrored.provider_calls if c[1] == "DELETE")
    with pytest.raises(InfraError, match="replicas"):
        ingest.start({})
    assert sum(1 for c in mirrored.provider_calls if c[1] == "DELETE") == deletes
    assert (content(mirrored, DAY_PATH), mirrored.held(DAY_PATH, BETA_ID)) == (DAY, [[0, len(DAY)]])


def test_o44_a_replica_on_the_data_provider_is_refused_when_the_instrument_is_built(
    provisioner, config, fake, tmp_path
):
    """O44: the data provider holds the file whole by definition; naming it as a replica is a plan the reset cannot
    keep apart from the owner's copy, so it is refused before anything is read."""
    plan = load_ingest_plan(write_plan(tmp_path, [{**ROW, "replicas": ["alpha"]}]), config)
    with pytest.raises(ValueError, match="data provider"):
        ProgressiveIngest(provisioner, plan, journal=tmp_path / "journal.jsonl")


def test_o44_a_reset_is_verified_by_the_owners_block_map_as_well_as_by_the_hash(make, fake):
    """O44: the reset writes the rest and the bytes read back have the plan's hash, but the data provider's block map
    keeps showing only the prefix: the file is not whole there, so the reset fails rather than pass on the hash."""
    ingest = make(ROW)
    ingest.start({})
    ingest.at_first_turn({"clock_s": 0.0})
    blocks = {"physicalSize": PREFIX, "blocks": [[0, PREFIX]]}
    fake.distribution_entries[(fake.id_of(DAY_PATH), ALPHA_ID)] = {
        "virtualSize": len(DAY),
        "distributionPerStorageBackend": {"storage-1": blocks},
    }
    snapshot = ingest.finish()
    assert content(fake, DAY_PATH) == DAY
    assert snapshot["restore"]["ok"] is False
    assert "block map" in snapshot["restore"]["files"]["day-13"]["detail"]
