"""A provisioner in a space its data provider holds on a read-only imported storage sends nothing that would write to,
change or delete from that storage (O37), and still reads, places and evicts on the other providers."""

import pytest

from expcore_onedata.ingest import ProgressiveIngest
from expcore_onedata.ingestplan import load_ingest_plan
from expcore_onedata.provision import Refused
from expcore_onedata.testing import ALPHA_ID, BETA_ID, DATA, with_placement, write_platform

PATH = "study/data.bin"


def imported(readonly: bool):
    def edit(data):
        with_placement(data)
        data["support"]["providers"] = ["alpha", "beta"]
        data["space"]["import"] = {"readonly": readonly}

    return edit


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, imported(readonly=True))


@pytest.fixture
def fake(fake):
    fake.add_provider(BETA_ID, "beta.test")
    fake.add_file(PATH, DATA)
    return fake


def mutating(fake):
    return [call for call in fake.provider_calls if call[1] in ("POST", "PUT", "DELETE")]


WRITES = {
    "upload": lambda p, source: p.upload("study/new.bin", source),
    "put_file": lambda p, source: p.put_file("study/new.bin", b"new"),
    "make_dir": lambda p, source: p.make_dir("fresh"),
    "delete": lambda p, source: p.mutate("DELETE", f"/data/{p.resolve(PATH).file_id}", provider="alpha"),
    "write through a mirror": lambda p, source: p.mutate(
        "PUT", f"/data/{p.resolve(PATH).file_id}/content", provider="beta", content=b"x"
    ),
    "replicate onto the data provider": lambda p, source: p.schedule_transfer(
        "replication", p.resolve(PATH).file_id, "alpha"
    ),
}


@pytest.mark.parametrize("case", list(WRITES))
def test_o37_a_read_only_imported_space_is_never_written_and_a_refusal_sends_nothing(case, provisioner, fake, tmp_path):
    """O37: op-worker refuses a write, a creation and a replication onto a read-only storage (EROFS), and a delete there
    leaves the file on the storage while removing it from the space; a write through another provider takes the data
    provider's blocks from it for good. So the provisioner refuses every such request before anything is sent."""
    source = tmp_path / "new.bin"
    source.write_bytes(b"new")
    with pytest.raises(Refused, match="read-only imported storage"):
        WRITES[case](provisioner, source)
    assert mutating(fake) == []
    assert fake.held(PATH, ALPHA_ID) == [[0, len(DATA)]]


def test_o37_a_read_only_imported_space_is_still_read_replicated_to_a_mirror_and_evicted_from_it(provisioner, fake):
    """O37: the data provider's storage is only read: a read, a replication onto a mirror's regular storage and an
    eviction from it, confirmed by the block maps, all work, and the data provider keeps every block."""
    assert provisioner.read_file(provisioner.resolve(PATH).file_id).size == len(DATA)
    provisioner.replicate(PATH, "beta")
    assert fake.held(PATH, BETA_ID) == [[0, len(DATA)]]
    assert provisioner.evict(PATH, "beta")["empty"] is True
    assert fake.held(PATH, ALPHA_ID) == [[0, len(DATA)]]


def test_o37_a_writable_imported_space_takes_the_provisioners_writes(tmp_path, fake, client, ticker):
    """O37: a writable imported storage is written like a regular one; only `readonly` refuses."""
    from expcore_onedata.config import load_platform
    from expcore_onedata.provision import Provisioner, resolve_provider_ids
    from expcore_onedata.rangemap import load_placement

    platform = write_platform(tmp_path, imported(readonly=False))
    config = load_platform(platform)
    writable = Provisioner(
        client,
        config,
        load_placement(platform, config).settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        clock=ticker.clock,
        sleep=ticker.sleep,
    )
    assert writable.read_only is False
    writable.put_file("study/new.bin", b"new")
    assert fake.exists("study/new.bin")


def test_o37_a_progressive_ingest_is_refused_on_a_read_only_imported_space_before_it_claims_anything(
    provisioner, config, tmp_path
):
    """O37, O27: the ingest removes files and writes them back through the data provider, which a read-only storage
    refuses; it is refused when built, with nothing claimed or sent."""
    staging = tmp_path / "staged"
    staging.mkdir()
    (staging / "data.bin").write_bytes(DATA)
    (tmp_path / "plan.toml").write_text(
        '[ingest]\nstaging = "staged"\n\n[[ingest.files]]\nlabel = "day"\npath = "study/data.bin"\n'
        'source = "data.bin"\nat_s = 1\nbaseline = "complete"\n',
        encoding="utf-8",
    )
    plan = load_ingest_plan(tmp_path / "plan.toml", config)
    with pytest.raises(Refused, match="read-only imported storage"):
        ProgressiveIngest(provisioner, plan, journal=tmp_path / "journal.jsonl")
    assert not (tmp_path / "changes.jsonl").exists()
