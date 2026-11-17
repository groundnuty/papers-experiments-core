"""O57: the data of an ingested space is checked after the ingest, and a shard is planned only on verified data.

`up` checks neither an ingested space (it holds nothing yet) nor takes its data on a claim: it records, in the state
file, each ingested space with the digest of the manifest it is to be verified against. `hpcfed.py verify-data` reads
each back through its owner once the ingest is done, compares every file's path, size and SHA-256 and every directory
with that manifest, fails loudly on any difference and records a marker bound to the manifest's digest on a pass.
`shards.py plan` and `lines` refuse a federation whose ingested spaces have no marker. Slurm is the fake of
hpc_fed_support, the bootstrap and the space reads are stand-ins, and nothing reaches a cluster."""

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fingerprint_support import standin_python
from hpc_fed_support import LAYOUT, hpc_on_path
from hpc_ingested_support import (
    ONE,
    OTHER,
    artifacts,
    driver_over,
    ingested_state,
    manifest_of,
    site_env,
    study_layout,
    tree_of,
    up,
    verify_stand_in,
)

from expcore_onedata.importmanifest import Manifest, write_manifest

SCRIPT = LAYOUT.parents[1] / "shards.py"


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import fedlinks
        import hpcdata

        modules.fedlinks, modules.data = fedlinks, hpcdata
        yield modules


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_verify", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_verify"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        # these manifests name an interpreter that does not exist; the core it imports is asked in
        # test_hpc_shards_core_identity.py (C06), with real interpreters
        module.identify_core = lambda plan: {"known": True, "stale": False, "why": None}
        yield module
    finally:
        sys.modules.pop("hpc_shards_verify", None)


def brought_up(hpc, tmp_path, ca, name="fed", art=None):
    """A federation of the pilot's layout whose ingested space declares its manifest (ONE), brought up."""
    art = art or artifacts(tmp_path / f"art-{name}")
    return up(hpc, tmp_path / name, study_layout(tmp_path / f"layout-{name}"), ca, artifacts_dir=art, label=name)


# -- what up records, and what verify-data does ----------------------------------------------------------------


def test_o57_up_records_each_ingested_space_unverified_with_its_manifests_digest(hpc, tmp_path):
    """O57: the state file names the spaces awaiting verification, from the layout alone, each with the digest of the
    manifest it will be verified against and no verdict yet; an undeclared one with the empty manifest's."""
    declared = driver_over(hpc, tmp_path / "a", study_layout(tmp_path / "la"))
    declared.driver.up(hpc.settings.site_from_environment(site_env(tmp_path / "a", artifacts(tmp_path / "art"))))
    assert ingested_state(declared) == {"delta-space": {"manifest": ONE.digest(), "declared": True, "verified": None}}
    plain = driver_over(hpc, tmp_path / "b", LAYOUT)
    plain.driver.up(hpc.settings.site_from_environment(site_env(tmp_path / "b", None)))
    empty = Manifest((), ()).digest()
    assert ingested_state(plain) == {"delta-space": {"manifest": empty, "declared": False, "verified": None}}


def test_o57_verify_data_passes_on_matching_data_and_records_the_marker_bound_to_the_manifest(hpc, tmp_path, ca):
    """O57: after the ingest, each ingested space read through its owner holds exactly the declared manifest: the
    state file gets a marker holding the manifest's digest and the counts, and the phase is unchanged."""
    fed = brought_up(hpc, tmp_path, ca)
    verify_stand_in(hpc, fed, {"delta-space": ONE})
    printed = fed.driver.verify_data()
    assert "every ingested space holds the data its manifest declares (1 space(s), 3 file(s))" in printed
    assert fed.driver.store.read()["phase"] == "linked"
    assert ingested_state(fed)["delta-space"]["verified"] == {"manifest": ONE.digest(), "files": 3, "bytes": 14}


@pytest.mark.parametrize(
    ("data", "said"),
    [
        (OTHER, "1 file(s) of another size or other bytes"),
        (manifest_of({"records/day-01.csv": b"alpha", "notes.txt": b"gamma"}), "1 file(s) missing"),
        (manifest_of({**{f.path: b"x" * f.size for f in ONE.files}, "more.txt": b"m"}), "file(s) not in it"),
    ],
    ids=["one file's bytes", "one file missing", "one file extra"],
)
def test_o57_verify_data_fails_loudly_on_one_difference_naming_the_space_and_counts_never_a_path(
    hpc, tmp_path, ca, data, said
):
    """O57: a space whose data differs from its declared manifest by one file fails `verify-data`, naming the space and
    how many files differ and never a path, and records no marker."""
    fed = brought_up(hpc, tmp_path, ca)
    verify_stand_in(hpc, fed, {"delta-space": data})
    with pytest.raises(RuntimeError, match=r"^delta-space does not hold the data its manifest declares") as refused:
        fed.driver.verify_data()
    assert said in str(refused.value)
    assert "records/" not in str(refused.value)
    assert str(tmp_path) not in str(refused.value)
    assert ingested_state(fed)["delta-space"]["verified"] is None


def test_o57_a_verify_that_fails_after_one_that_passed_clears_the_marker(hpc, tmp_path, ca):
    """O57: data changed since a pass (one more ingest): the next verify-data fails and the earlier marker goes, so
    plan never reads a pass the data no longer deserves."""
    fed = brought_up(hpc, tmp_path, ca)
    verify_stand_in(hpc, fed, {"delta-space": ONE})
    fed.driver.verify_data()
    assert ingested_state(fed)["delta-space"]["verified"] is not None
    verify_stand_in(hpc, fed, {"delta-space": OTHER})
    with pytest.raises(RuntimeError):
        fed.driver.verify_data()
    assert ingested_state(fed)["delta-space"]["verified"] is None


def test_o57_a_verify_that_dies_mid_read_leaves_no_marker_from_an_earlier_pass(hpc, tmp_path, ca):
    """O57: a REST error while reading a space raises before any verdict: the earlier pass's marker is already gone."""
    fed = brought_up(hpc, tmp_path, ca)
    verify_stand_in(hpc, fed, {"delta-space": ONE})
    fed.driver.verify_data()

    def dies(layout, owner, manifests):
        raise OSError("the owner did not answer")

    fed.driver.verify = dies
    with pytest.raises(OSError, match="did not answer"):
        fed.driver.verify_data()
    assert ingested_state(fed)["delta-space"]["verified"] is None


def test_o57_an_undeclared_ingested_space_is_verified_as_empty_so_ingested_bytes_fail_with_a_hint(hpc, tmp_path, ca):
    """O57, C06: a space that declares no manifest fingerprints as empty, so any byte the study ingested into it fails
    `verify-data`: the message says it declares nothing, and where to declare it."""
    fed = up(hpc, tmp_path, LAYOUT, ca, artifacts_dir=None)
    verify_stand_in(hpc, fed, {})
    assert "(1 space(s), 0 file(s))" in fed.driver.verify_data()
    verify_stand_in(hpc, fed, {"delta-space": ONE})
    with pytest.raises(RuntimeError, match=r"declares no data_manifest.*spaces\.delta-space\.data_manifest"):
        fed.driver.verify_data()


def test_o57_verify_data_refuses_a_manifest_that_is_not_the_one_up_recorded(hpc, tmp_path, ca):
    """O57: the manifest file edited between `up` (whose digest the state holds, and whose hash the fingerprint will
    carry) and the verification: refused, never verified against another file than the one up recorded."""
    art = artifacts(tmp_path / "art")
    fed = brought_up(hpc, tmp_path, ca, art=art)
    write_manifest(OTHER, art / "manifests" / "delta.json")
    verify_stand_in(hpc, fed, {"delta-space": OTHER})
    with pytest.raises(RuntimeError, match="delta-space: its manifest is not the one up recorded"):
        fed.driver.verify_data()
    assert ingested_state(fed)["delta-space"]["verified"] is None


def test_o57_verify_data_needs_a_federation_that_is_up(hpc, tmp_path, ca):
    """O57: no state file, or an up that did not finish (its phase not `linked`), and there is nothing to verify."""
    fed = driver_over(hpc, tmp_path / "none", study_layout(tmp_path / "layout"))
    fed.root.mkdir(parents=True)
    with pytest.raises(hpc.state.StateError, match="the federation is not up"):
        fed.driver.verify_data()
    done = brought_up(hpc, tmp_path, ca, "done")
    done.driver.set(lambda s: s.update(phase="bootstrapped"))
    with pytest.raises(RuntimeError, match=r"up has not finished \(phase bootstrapped\)"):
        done.driver.verify_data()


def test_o57_the_verify_data_command_is_its_own_branch_and_never_takes_the_federation_down(hpc, tmp_path, ca):
    """O57: `hpcfed.py verify-data` exits 0 on a pass and 1 on a difference, and neither cancels a job, stops a
    service nor removes the root (the command line's last branch is `down`)."""
    fed = brought_up(hpc, tmp_path, ca)
    words = ["--layout", str(fed.driver.layout_path), "--root", str(fed.root)]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.cli, "make_driver", lambda *args, **kwargs: fed.driver)
        verify_stand_in(hpc, fed, {"delta-space": ONE})
        assert hpc.cli.main([*words, "verify-data"], {}, runner=fed.fake) == 0
        verify_stand_in(hpc, fed, {"delta-space": OTHER})
        assert hpc.cli.main([*words, "verify-data"], {}, runner=fed.fake) == 1
    assert (fed.root / "hpc-federation.json").is_file()
    assert not [c for c in fed.fake.calls if c[0] == "scancel"]


def test_o57_verify_reads_every_space_through_the_ids_up_wrote_into_facts_json(hpc, tmp_path, ca):
    """O57: `verify-data` is a fresh process, so the bootstrap holds no ids: the space ids are read from facts.json,
    where up wrote them, before the owner is asked for a listing."""
    fed = brought_up(hpc, tmp_path, ca)
    layout = fed.driver.placed_layout(fed.driver.store.read())
    seen = {}

    def bootstrap(client, lay, owner):
        return SimpleNamespace(layout=lay, spec=lay.spec, ids=SimpleNamespace(spaces={}))

    def reader(boot):
        seen.update(boot.ids.spaces)
        return lambda space: tree_of(ONE)

    result = hpc.boot.verify_ingested(
        layout, (5001, 5001), {"delta-space": ONE}, rest=lambda f, **kwargs: SimpleNamespace(close=lambda: None),
        bootstrap=bootstrap, data=reader,
    )  # fmt: skip
    facts = json.loads((fed.root / "facts.json").read_text(encoding="utf-8"))["spaces"]
    assert seen == {name: space["id"] for name, space in facts.items()}
    assert result == {"delta-space": ""}


def test_o57_a_state_file_with_a_malformed_data_record_is_refused_when_it_is_read(hpc, tmp_path):
    """O57: the record is read by `verify-data` and by plan: a shape other than the driver's is not this one's state."""
    fed = driver_over(hpc, tmp_path, LAYOUT)
    fed.driver.up(hpc.settings.site_from_environment(site_env(tmp_path, None)))
    path = fed.root / "hpc-federation.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    state["data"] = {"ingested": {"delta-space": {"manifest": "nope", "declared": True, "verified": None}}}
    path.write_text(json.dumps(state), encoding="utf-8")
    with pytest.raises(hpc.state.StateError, match=r"data\.ingested: a space's manifest is not a SHA-256"):
        fed.driver.store.read()


# -- shards.py plan ---------------------------------------------------------------------------------------------------


def shard_manifest(tmp_path: Path, feds: list) -> Path:
    (tmp_path / "study").mkdir(exist_ok=True)  # the campaign fingerprint is taken on a copy of it (O63)
    lines = [f'{k} = "{tmp_path / k}"' for k in ("study", "core_src", "results", "tmpfs")]
    python = standin_python(tmp_path / "venv", real_platform=True)  # the federations' own rendered files
    lines += [f'python = "{python}"', 'step_path = "/usr/bin:/bin"', "cpus = 6"]
    lines += ['memory = "18g"']
    for n, fed in enumerate(feds):
        lines += ["[[shards]]", f'label = "fed-{n}"', f'root = "{fed.root}"']
    (tmp_path / "shards.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path / "shards.toml"


def first_shard(shards, fed):
    """The first shard of a manifest of two shards, on `fed` (`federation()` reads the one shard it is given)."""
    return shards.Shard(1, 2, "fed-0", fed.root)


def test_o57_plan_refuses_a_federation_whose_ingested_space_has_no_verified_marker(hpc, shards, tmp_path, ca, capsys):
    """O57: a shard planned on data nothing has shown to be the declared data is a shard that spends on the wrong
    experiment: `federation()` (which `plan` and `lines` both call) refuses it, naming the shard's space and the verb
    that records the marker; once both federations are verified, both commands go through."""
    feds = [brought_up(hpc, tmp_path, ca, name) for name in ("a", "b")]
    manifest = shard_manifest(tmp_path, feds)
    for command in ("plan", "lines"):
        assert shards.main([command, "--manifest", str(manifest)]) == 2
        err = capsys.readouterr().err
        assert "shard fed-0: its federation's ingested space(s) have no verified data: delta-space" in err
        assert "verify-data" in err
    for fed in feds:
        verify_stand_in(hpc, fed, {"delta-space": ONE})
        fed.driver.verify_data()
    assert shards.main(["plan", "--manifest", str(manifest)]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2
    assert shards.main(["lines", "--manifest", str(manifest)]) == 0


def test_o57_plan_refuses_a_marker_for_another_manifest_than_the_platform_file_now_declares(hpc, shards, tmp_path, ca):
    """O57: verified data is data verified against one manifest: the file the platform's `data_manifest` names edited
    after the verification (its hash, and so the fingerprint, changes) leaves the pass for another experiment."""
    art = artifacts(tmp_path / "art")
    fed = brought_up(hpc, tmp_path, ca, art=art)
    verify_stand_in(hpc, fed, {"delta-space": ONE})
    fed.driver.verify_data()
    shard = first_shard(shards, fed)
    assert shards.federation(shard)["job"]
    write_manifest(OTHER, art / "manifests" / "delta.json")
    with pytest.raises(shards.ShardsError, match=r"ingested space\(s\) have no verified data: delta-space"):
        shards.federation(shard)


def test_o57_plan_refuses_a_marker_that_is_for_another_manifest_than_the_one_up_recorded(hpc, shards, tmp_path, ca):
    """O57: a marker whose manifest digest is not the recorded one is no marker."""
    fed = brought_up(hpc, tmp_path, ca)
    verify_stand_in(hpc, fed, {"delta-space": ONE})
    fed.driver.verify_data()
    fed.driver.set(lambda s: s["data"]["ingested"]["delta-space"]["verified"].update(manifest=OTHER.digest()))
    with pytest.raises(shards.ShardsError, match="no verified data: delta-space"):
        shards.federation(first_shard(shards, fed))


def test_o57_plan_refuses_a_state_that_holds_no_record_of_its_ingested_spaces(hpc, shards, tmp_path, ca):
    """O57: a federation brought up by a driver that did not record them cannot be shown verified: down and up again."""
    fed = brought_up(hpc, tmp_path, ca)
    fed.driver.set(lambda s: s.pop("data"))
    with pytest.raises(shards.ShardsError, match="no record of its ingested spaces"):
        shards.federation(first_shard(shards, fed))


def test_o57_plan_takes_a_federation_with_no_ingested_space_as_it_always_did(hpc, shards, tmp_path, ca):
    """O57: a federation of baseline spaces and shared imports alone has nothing to verify: its record is empty and plan
    asks nothing more of it."""
    fed = brought_up(hpc, tmp_path, ca)
    fed.driver.set(lambda s: s["data"].update(ingested={}))
    assert shards.federation(first_shard(shards, fed))["job"]
