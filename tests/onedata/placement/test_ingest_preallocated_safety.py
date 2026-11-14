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
