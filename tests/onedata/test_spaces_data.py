"""C06: a space is the same experimental condition across federations when its declared identity and its data identity
match. An entry of `[[spaces]]` that declares its data (`data_manifest`) is fingerprinted by its declared identity and
its data's content hash, without its Onezone id, which every federation's up makes anew; one that does not keeps the
id-bearing digest it always had, so its federations never agree."""

import hashlib
import json

import pytest
from campaign_support import NOW, make_fingerprint, make_manifest, make_schema
from platform_support import leaked_values

from expcore.campaign.merge import MergeRefused, ShardFacts, check_fingerprints
from expcore.campaign.resume import FingerprintMismatch, open_campaign, read_fingerprint
from expcore.campaign.shard import ShardSpec
from expcore.config.loading import ConfigError
from expcore.study.platform_legacy import legacy_fingerprint_of
from expcore_onedata.config import load_platform
from expcore_onedata.importmanifest import Manifest, ManifestFile, write_manifest
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.testing.world import with_spaces

FILES = (("records/day-01.csv", b"a,b\n1,2\n"), ("records/day-02.csv", b"a,b\n3,4\n"))


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")


def manifest(files=FILES) -> Manifest:
    entries = tuple(ManifestFile(path, len(data), hashlib.sha256(data).hexdigest()) for path, data in files)
    return Manifest(entries, ("records",))


def federation(tmp_path, name, *, data=None, ids=("sp-1", "sp-2"), declare=(0, 1)):
    """One federation's platform file: two spaces, with the Onezone ids its up made, each entry in `declare` naming
    its data manifest (written beside the file, its own copy)."""
    directory = tmp_path / name
    directory.mkdir(parents=True)
    write_manifest(data or manifest(), directory / "data-manifest.json")

    def edit(document):
        with_spaces(document, 2)
        for index, entry in enumerate(document["spaces"]):
            entry["id"] = ids[index]
            if index in declare:
                entry["data_manifest"] = "data-manifest.json"

    return load_platform(write_platform(directory, edit))


def old_digest(entry) -> str:
    """The digest every entry had before this change, written out as it was."""
    facts = {
        "label": entry.label,
        "space": [entry.space.name, entry.space.id, list(entry.space.prefixes)],
        "support": [list(entry.support.providers), entry.support.local],
        "holder": entry.holder,
        "tokens": list(entry.tokens),
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


# -- the digest -------------------------------------------------------------------------------------------------------


def test_c06_two_federations_holding_the_same_data_are_one_condition_whatever_their_onezone_ids(tmp_path):
    """C06: the same declared spaces and the same data, at two federations whose up made other ids: one platform
    fingerprint, holding neither id nor path."""
    one = federation(tmp_path, "one", ids=("sp-1", "sp-2"))
    two = federation(tmp_path, "two", ids=("sp-9", "sp-8"))
    assert one.fingerprint() == two.fingerprint()
    assert one.spaces[0].data == manifest().digest()
    text = json.dumps(one.fingerprint())
    assert "sp-1" not in text
    assert leaked_values(text, tmp_path / "one") == []


def test_c06_one_file_different_is_another_condition(tmp_path):
    """C06: one file of the data changed by one byte: another data identity, another digest, another fingerprint."""
    changed = manifest(((FILES[0][0], b"a,b\n1,3\n"), FILES[1]))
    one = federation(tmp_path, "one")
    two = federation(tmp_path, "two", data=changed, ids=("sp-9", "sp-8"))
    assert one.spaces[0].digest() != two.spaces[0].digest()
    assert one.fingerprint() != two.fingerprint()


def test_c06_a_space_that_declares_no_data_keeps_its_old_id_bearing_digest(tmp_path):
    """C06: without `data_manifest` an entry's digest is exactly the one it always had, the Onezone id in it: two
    federations never agree on it, and a store started before the key existed resumes unchanged."""
    one = federation(tmp_path, "one", declare=())
    two = federation(tmp_path, "two", declare=(), ids=("sp-9", "sp-8"))
    assert [entry.digest() for entry in one.spaces] == [old_digest(entry) for entry in one.spaces]
    assert [entry.digest() for entry in one.spaces] == [entry.legacy_digest() for entry in one.spaces]
    assert one.fingerprint() != two.fingerprint()
    assert one.legacy_fingerprint() is None


def test_c06_the_legacy_fingerprint_is_every_entry_by_its_id_bearing_digest(tmp_path):
    """C06: with a space that declares its data, the earlier form of the platform's half has every entry by its old
    digest, as a store started before the declaration recorded it."""
    config = federation(tmp_path, "one", declare=(0,))
    legacy = config.legacy_fingerprint()
    assert legacy["spaces"] == [old_digest(entry) for entry in config.spaces]
    assert config.fingerprint()["spaces"][1] == legacy["spaces"][1]  # the entry with no data: one digest in both
    assert config.fingerprint()["spaces"][0] != legacy["spaces"][0]
    assert {k: v for k, v in legacy.items() if k != "spaces"} == {
        k: v for k, v in config.fingerprint().items() if k != "spaces"
    }


@pytest.mark.parametrize("case", ["missing", "not json", "not a manifest", "not a string"])
def test_c06_a_data_manifest_that_is_no_manifest_is_refused_naming_the_key_never_the_path(tmp_path, case):
    directory = tmp_path / "fed"
    directory.mkdir()
    if case == "not json":
        (directory / "data-manifest.json").write_text("{")
    elif case == "not a manifest":
        (directory / "data-manifest.json").write_text(json.dumps({"version": 1}))
    value = 7 if case == "not a string" else "data-manifest.json"

    def edit(document):
        with_spaces(document, 2)
        document["spaces"][0]["data_manifest"] = value

    with pytest.raises(ConfigError, match=r"spaces\[0\]: key 'data_manifest'") as refused:
        load_platform(write_platform(directory, edit))
    assert "data-manifest.json" not in str(refused.value)
    assert leaked_values(str(refused.value), directory) == []


# -- the merge and the legacy rule --------------------------------------------------------------------------------


def shard(index: int, fingerprint) -> ShardFacts:
    return ShardFacts(out=None, snapshot=None, snapshot_sha256="0" * 64, spec=ShardSpec(index, 2, f"fed-{index}"),
                      started_utc=NOW, environment={}, shares={}, fingerprint=fingerprint, runs={})  # fmt: skip


def campaign(config_fingerprint):
    return make_fingerprint(make_schema(), platform=config_fingerprint)


def test_c06_the_merge_takes_shards_of_two_federations_holding_the_same_data_and_refuses_one_file_different(tmp_path):
    """C06: the merge compares the shards' recorded fingerprints: two federations with the same data give one, so the
    shards merge; one file different, and the merge refuses, naming the platform's spaces."""
    one = federation(tmp_path, "one")
    same = federation(tmp_path, "same", ids=("sp-9", "sp-8"))
    other = federation(tmp_path, "other", data=manifest(((FILES[0][0], b"x"), FILES[1])), ids=("sp-7", "sp-6"))
    check_fingerprints([shard(1, campaign(one.fingerprint())), shard(2, campaign(same.fingerprint()))])
    with pytest.raises(MergeRefused, match=r"ran another campaign .*\n  platform\.spaces"):
        check_fingerprints([shard(1, campaign(one.fingerprint())), shard(2, campaign(other.fingerprint()))])


def test_c06_the_merge_never_takes_a_store_of_the_earlier_form_beside_one_of_the_later(tmp_path):
    """C06: a store that resumed under the earlier form keeps it; beside a shard of the later form, of the same
    federation and the same data, the merge refuses."""
    config = federation(tmp_path, "one")
    with pytest.raises(MergeRefused, match="ran another campaign"):
        check_fingerprints([shard(1, campaign(config.legacy_fingerprint())), shard(2, campaign(config.fingerprint()))])


def test_c06_a_store_of_the_earlier_form_resumes_unchanged_and_nothing_else_does(tmp_path):
    """C06: a store started before its spaces declared their data recorded the id-bearing form; the start presents
    that recorded fingerprint when it is exactly the campaign's with the earlier platform half, and the store resumes
    with its record unchanged. The later form alone is refused, and so is a store that differs in anything else."""
    config = federation(tmp_path, "one")
    schema, plan = make_schema(), make_manifest(make_schema())
    earlier, later = campaign(config.legacy_fingerprint()), campaign(config.fingerprint())
    path = tmp_path / "campaign.db"

    def resume(fingerprint):
        store, _ = open_campaign(path, schema, plan, fingerprint, now=NOW, max_attempts=3)
        store.close()

    resume(earlier)  # the store started before the declaration
    with pytest.raises(FingerprintMismatch, match=r"platform\.spaces"):
        resume(later)
    presented = legacy_fingerprint_of(read_fingerprint(path), later, config.legacy_fingerprint())
    assert presented == earlier
    resume(presented)
    assert read_fingerprint(path) == earlier
    other_tls = {**config.legacy_fingerprint(), "tls": {"mode": "insecure"}}
    assert legacy_fingerprint_of(read_fingerprint(path), later, other_tls) == later
    assert legacy_fingerprint_of(read_fingerprint(path), later, None) == later
    assert legacy_fingerprint_of(None, later, config.legacy_fingerprint()) == later
