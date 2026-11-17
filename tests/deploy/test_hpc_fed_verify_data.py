"""O57: the data of an ingested space is checked after the ingest, and a shard is planned only on verified data.

`up` checks neither an ingested space (it holds nothing yet) nor takes its data on a claim: it records, in the state
file, each ingested space with the digest of the manifest it is to be verified against. `hpcfed.py verify-data` reads
each back through its owner once the ingest is done, compares every file's path, size and SHA-256 and every directory
with that manifest, fails loudly on any difference and records a marker bound to the manifest's digest on a pass.
`shards.py plan` and `lines` refuse a federation whose ingested spaces have no marker. Slurm is the fake of
hpc_fed_support, the bootstrap and the space reads are stand-ins, and nothing reaches a cluster."""

import contextlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
from hpc_rehearsal_support import link_check_stand_in

from expcore_onedata.importmanifest import Manifest, write_manifest
from expcore_onedata.linkcheck import LinkReport, PairResult

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
    manifest it will be verified against and no verdict yet; an undeclared one with the empty manifest's. Each has no
    Onezone ID here (`space_id`): a core's phase that writes no facts file, as this stand-in, gives `up` none to record
    (test_hpc_fed_space_identity.py runs `up` over a core's phase that does)."""
    declared = driver_over(hpc, tmp_path / "a", study_layout(tmp_path / "la"))
    declared.driver.up(hpc.settings.site_from_environment(site_env(tmp_path / "a", artifacts(tmp_path / "art"))))
    assert ingested_state(declared) == {
        "delta-space": {"manifest": ONE.digest(), "declared": True, "space_id": None, "verified": None}
    }
    plain = driver_over(hpc, tmp_path / "b", LAYOUT)
    plain.driver.up(hpc.settings.site_from_environment(site_env(tmp_path / "b", None)))
    empty = Manifest((), ()).digest()
    assert ingested_state(plain) == {
        "delta-space": {"manifest": empty, "declared": False, "space_id": None, "verified": None}
    }


def test_o57_verify_data_passes_on_matching_data_and_records_the_marker_bound_to_the_manifest(hpc, tmp_path, ca):
    """O57: after the ingest, each ingested space read through its owner holds exactly the declared manifest: the
    state file gets a marker holding the manifest's digest, the counts and the ID of the space that was read, and the
    phase is unchanged."""
    fed = brought_up(hpc, tmp_path, ca)
    verify_stand_in(hpc, fed, {"delta-space": ONE})
    printed = fed.driver.verify_data()
    assert "every ingested space holds the data its manifest declares (1 space(s), 3 file(s))" in printed
    assert fed.driver.store.read()["phase"] == "linked"
    space_id = json.loads((fed.root / "facts.json").read_text(encoding="utf-8"))["spaces"]["delta-space"]["id"]
    marker = {"manifest": ONE.digest(), "files": 3, "bytes": 14, "space_id": space_id}
    assert ingested_state(fed)["delta-space"]["verified"] == marker


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


# -- a rerun of the core's phase on a federation that is linked (C06, O63) --------------------------------------------


def failing_links(config: Any, client: Any) -> LinkReport:
    """`fedlinks.check_links` finding a pair that does not transfer."""
    pair = PairResult("providers[0]", "providers[1]", False, reason="the fetch failed")
    return LinkReport(False, "providers[0] <- providers[1]: the fetch failed", (pair,), 1.0, 2.0)


def rerun(hpc, fed, links, said=None) -> str:
    """`hpcfed.py core` on the root of `fed`: the driver's own core's phase again, `links` its link check."""
    fed.driver.say = (said if said is not None else []).append
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", links)
        return fed.driver.core_here()


def verified(hpc, tmp_path, ca, name="fed", art=None):
    """A federation brought up and whose ingested space has been verified: plan takes it, and is its only gate left."""
    fed = brought_up(hpc, tmp_path, ca, name, art)
    verify_stand_in(hpc, fed, {"delta-space": ONE})
    fed.driver.verify_data()
    return fed


def phase(fed) -> str:
    return fed.driver.store.read()["phase"]


@pytest.fixture
def other(hpc, tmp_path, ca):
    """A second federation, brought up and verified: a manifest's shards never share a root (O63, #414), so `plan` is
    given two."""
    return verified(hpc, tmp_path, ca, name="other")


def plan(shards, tmp_path, fed, other) -> int:
    return shards.main(["plan", "--manifest", str(shard_manifest(tmp_path, [fed, other]))])


def test_c06_a_core_rerun_on_a_linked_federation_whose_link_check_passes_leaves_it_linked_and_plan_takes_it(
    hpc, shards, tmp_path, ca, other, capsys
):
    """C06, O63 (live, 17 Nov 2026): `hpcfed.py core` on a finished federation ends in the core's own phase, which set
    `bootstrapped`, and only `up` set `linked`, so `shards.py plan` refused a federation whose rerun had passed every
    check. A rerun whose link check passes restores `linked`, and says so."""
    fed = verified(hpc, tmp_path, ca)
    assert plan(shards, tmp_path, fed, other) == 0
    capsys.readouterr()
    said: list = []
    printed = rerun(hpc, fed, link_check_stand_in([]), said)
    assert "link check: every pair transferred" in printed
    assert phase(fed) == "linked"
    assert any("linked again" in line for line in said)
    assert plan(shards, tmp_path, fed, other) == 0


def test_c06_a_core_rerun_on_a_linked_federation_whose_link_check_fails_leaves_it_bootstrapped_and_plan_refuses(
    hpc, shards, tmp_path, ca, other, capsys
):
    """C06, O63: a rerun that fails its link check leaves no `linked` it did not earn: the federation is `bootstrapped`,
    the failure is raised as it was, and `plan` refuses the federation, saying its up has not finished."""
    fed = verified(hpc, tmp_path, ca)
    said: list = []
    with pytest.raises(RuntimeError, match="the link check failed, so the federation is not ready"):
        rerun(hpc, fed, failing_links, said)
    assert phase(fed) == "bootstrapped"
    assert any("bootstrapped" in line for line in said)
    capsys.readouterr()
    assert plan(shards, tmp_path, fed, other) == 2
    assert "its federation's up has not finished (phase bootstrapped)" in capsys.readouterr().err


def test_c06_a_core_rerun_on_a_linked_federation_is_not_linked_while_it_runs(hpc, tmp_path, ca):
    """C06, O63: the demotion comes before the core's phase, not after its failure: a rerun that dies mid-way (a step
    killed, a login node's session lost) never raises past a `linked` it has not re-earned."""
    fed = verified(hpc, tmp_path, ca)
    seen: list = []
    real = fed.driver.core

    def watching(*args: Any, **kwargs: Any) -> str:
        seen.append(phase(fed))
        return real(*args, **kwargs)

    fed.driver.core = watching
    rerun(hpc, fed, link_check_stand_in([]))
    assert seen == ["bootstrapped"]


def test_c06_a_core_rerun_does_not_overwrite_a_phase_that_changed_under_it(hpc, tmp_path, ca):
    """C06, O63: the core's phase takes no lifecycle lock, so a `down` may start while it runs: its `stopping` is not
    overwritten with `linked` when the rerun ends."""
    fed = verified(hpc, tmp_path, ca)
    real = fed.driver.core

    def stopped_meanwhile(*args: Any, **kwargs: Any) -> str:
        fed.driver.set(lambda s: s.update(phase="stopping"))
        return real(*args, **kwargs)

    fed.driver.core = stopped_meanwhile
    rerun(hpc, fed, link_check_stand_in([]))
    assert phase(fed) == "stopping"


def stays_bootstrapped(shards, tmp_path, fed, other, said: list, why: str, capsys) -> None:
    """The rerun passed its own checks and the federation is not `linked`: `why` was said, and `plan` refuses it."""
    assert phase(fed) == "bootstrapped"
    assert any(why in line for line in said)
    capsys.readouterr()
    assert plan(shards, tmp_path, fed, other) == 2
    assert "its federation's up has not finished (phase bootstrapped)" in capsys.readouterr().err


def test_c06_a_core_rerun_after_the_facts_file_was_removed_does_not_link_the_federation_again(
    hpc, shards, tmp_path, ca, other, capsys
):
    """C06, O63: with no facts file the core's phase resets every space to its baseline, which empties an ingested space
    whose `verified` marker the state still holds: the rerun passes its own checks and cannot vouch for that data, so
    the federation stays `bootstrapped` and the rerun says why."""
    fed = verified(hpc, tmp_path, ca)
    fed.driver.layout.facts.unlink()
    said: list = []
    assert "link check: every pair transferred" in rerun(hpc, fed, link_check_stand_in([]), said)
    stays_bootstrapped(shards, tmp_path, fed, other, said, "its facts file was missing", capsys)


def test_c06_a_core_rerun_whose_layout_has_other_ingested_spaces_than_up_recorded_does_not_link_it_again(
    hpc, shards, tmp_path, ca, other, capsys
):
    """C06, O63: `up` recorded the ingested spaces the layout declared, and `plan` checks only those: a rerun over a
    layout that declares others (here the state records none) would leave a study space neither read nor verified, so
    the federation stays `bootstrapped` and the rerun says why."""
    fed = verified(hpc, tmp_path, ca)
    fed.driver.set(lambda s: s["data"].update(ingested={}))
    said: list = []
    assert "link check: every pair transferred" in rerun(hpc, fed, link_check_stand_in([]), said)
    stays_bootstrapped(shards, tmp_path, fed, other, said, "ingested spaces are not the ones this up recorded", capsys)


def test_c06_a_core_rerun_over_another_layout_than_up_recorded_does_not_link_it_again(
    hpc, shards, tmp_path, ca, other, capsys
):
    """C06, O63: the rerun renders the platform file from the layout it is given, and `up` built the federation from
    its own: a rerun over another layout (a wrong `--layout`, a layout edited since, one with no platform template and
    so no link check to run) passes its own checks and cannot vouch for the federation, so it stays `bootstrapped`
    and says why."""
    fed = verified(hpc, tmp_path, ca)
    fed.driver.set(lambda s: s["layout"].update(digest="ab" * 32))  # the digest `up` recorded of its layout's bytes
    said: list = []
    assert "link check: every pair transferred" in rerun(hpc, fed, link_check_stand_in([]), said)
    stays_bootstrapped(shards, tmp_path, fed, other, said, "its layout is not the one up recorded", capsys)


def test_c06_a_core_rerun_that_cannot_compare_the_ingested_spaces_does_not_link_it_again_and_names_no_path(
    hpc, shards, tmp_path, ca, other, capsys
):
    """C06, O63: whatever goes wrong while the rerun compares the layout's ingested spaces with the recorded ones (here
    the template cannot be read) is a refusal to link again, in fixed words that name no path: the rerun still runs, and
    never ends in a traceback before it does."""
    fed = verified(hpc, tmp_path, ca)
    real, asked = hpc.data.ingested, []

    def unreadable_once(layout: Any) -> Any:
        if not asked:
            asked.append(True)
            raise OSError("/no/such/place/template.toml: unreadable")
        return real(layout)

    said: list = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.data, "ingested", unreadable_once)
        assert "link check: every pair transferred" in rerun(hpc, fed, link_check_stand_in([]), said)
    stays_bootstrapped(
        shards, tmp_path, fed, other, said, "its ingested spaces could not be compared with the layout", capsys
    )
    assert not any("/no/such/place" in line for line in said)


def test_c06_a_core_rerun_with_another_platform_template_than_up_rendered_does_not_link_it_again(
    hpc, shards, tmp_path, ca, other, capsys
):
    """C06, O63: the layout's digest covers the layout file and not the platform template beside it. A template edited
    since `up` (here by a comment; in the worst case so that no provider pair is left and no link check runs) is
    rendered again by the rerun, which would then vouch for a render `up` never made: it stays `bootstrapped` and says
    why, comparing the template with the hash `up` recorded in `render-identity.json`."""
    fed = verified(hpc, tmp_path, ca)
    template = fed.driver.spec.platform_template
    template.write_text(template.read_text(encoding="utf-8") + "\n# edited since up\n", encoding="utf-8")
    said: list = []
    assert "link check: every pair transferred" in rerun(hpc, fed, link_check_stand_in([]), said)
    stays_bootstrapped(shards, tmp_path, fed, other, said, "its platform template is not the one up rendered", capsys)


def test_c06_a_core_rerun_with_no_render_identity_to_compare_does_not_link_it_again(
    hpc, shards, tmp_path, ca, other, capsys
):
    """C06, O63: a root whose render identity record is gone (or not a record) gives the rerun nothing to compare the
    template with: it stays `bootstrapped`, in fixed words that name no path."""
    fed = verified(hpc, tmp_path, ca)
    (fed.root / "render-identity.json").write_text("[]", encoding="utf-8")
    said: list = []
    assert "link check: every pair transferred" in rerun(hpc, fed, link_check_stand_in([]), said)
    stays_bootstrapped(shards, tmp_path, fed, other, said, "its platform template could not be compared", capsys)
    assert not any(str(tmp_path) in line for line in said)


def test_c06_up_does_not_say_linked_while_a_rerun_by_hand_holds_the_core_phase_lock(hpc, tmp_path):
    """C06, O63: `up` says `linked` after its core phase has ended (under `[hpc] core = "step"` the node's step has
    released the lock by then). A rerun by hand that started in that window reads the `bootstrapped` the step left and
    is no relink, so `up`'s own write takes the lock too: refused while that rerun runs, `up` fails and the federation
    stays `bootstrapped`, never `linked` over a rerun whose link check has not ended."""
    art = artifacts(tmp_path / "art")
    fed = driver_over(hpc, tmp_path / "fed", study_layout(tmp_path / "layout"))
    fed.site = site_env(tmp_path / "fed", art)
    held, real = contextlib.ExitStack(), fed.driver.run_core

    def core_then_a_rerun_starts() -> str:
        out = real()
        held.enter_context(fed.driver.store.core_phase())  # the rerun by hand, started once the step released the lock
        return out

    fed.driver.run_core = core_then_a_rerun_starts
    with held, pytest.raises(RuntimeError, match=REFUSED):
        fed.driver.up(hpc.settings.site_from_environment(fed.site))
    assert phase(fed) == "bootstrapped"


def test_c06_a_core_rerun_whose_ingested_manifest_changed_since_up_does_not_link_it_again(
    hpc, shards, tmp_path, ca, capsys
):
    """C06, O63: the manifest an ingested space declares was edited after `up` recorded its digest: what `verify-data`
    read is no longer what the layout declares, so the federation stays `bootstrapped` and the rerun says why. The
    second shard (two shards never share a root, #414) is up over the same artifacts and rerun after the edit too, so
    that both platform files are rendered from the edited manifest and the shards' fingerprints agree: `plan` then
    refuses on the phase, which is what the test is about."""
    art = artifacts(tmp_path / "art")
    fed = verified(hpc, tmp_path, ca, art=art)
    other = verified(hpc, tmp_path, ca, name="other", art=art)
    write_manifest(OTHER, art / "manifests" / "delta.json")
    said: list = []
    assert "link check: every pair transferred" in rerun(hpc, fed, link_check_stand_in([]), said)
    rerun(hpc, other, link_check_stand_in([]))
    stays_bootstrapped(shards, tmp_path, fed, other, said, "its manifest is not the one up recorded", capsys)


def test_c06_the_core_phase_of_a_federation_that_was_not_linked_does_not_overwrite_a_phase_that_changed_under_it(
    hpc, tmp_path, ca
):
    """C06, O63: the same on a state that was not `linked` (a rerun after a failed one, or `up`'s own step): a `down`
    that wrote `stopping` while the core's phase ran keeps it, instead of having `bootstrapped` written over it."""
    fed = brought_up(hpc, tmp_path, ca)
    fed.driver.set(lambda s: s.update(phase="providers"))
    real = fed.driver.core

    def stopped_meanwhile(*args: Any, **kwargs: Any) -> str:
        fed.driver.set(lambda s: s.update(phase="stopping"))
        return real(*args, **kwargs)

    fed.driver.core = stopped_meanwhile
    rerun(hpc, fed, link_check_stand_in([]))
    assert phase(fed) == "stopping"


REFUSED = "a core phase is already running on this federation"


def attempt_meanwhile(fed, found: list):
    """The driver's core wrapped so that, once, a second `core_here` is attempted from inside it: `found` gets what that
    attempt raised (None if it ran) and the state before and after it."""
    real, attempted = fed.driver.core, []

    def with_a_second(*args: Any, **kwargs: Any) -> str:
        if not attempted:
            attempted.append(True)
            before = fed.driver.store.read()
            try:
                fed.driver.core_here()
                raised = None
            except RuntimeError as error:
                raised = str(error)
            found.append((raised, before, fed.driver.store.read()))
        return real(*args, **kwargs)

    fed.driver.core = with_a_second


def test_c06_a_core_rerun_is_refused_while_another_core_phase_runs_and_changes_nothing(hpc, tmp_path, ca):
    """C06, O63: `hpcfed.py core` takes no lifecycle lock, so two reruns could overlap: the second would read the
    `bootstrapped` the first left, not be a rerun of a `linked` federation, and the first would then link it over the
    second's failed check. The core's phase holds an exclusive lock on the root, never waited for: a second one is
    refused at once, naming the state, and writes nothing; the first goes on and ends `linked`."""
    fed = verified(hpc, tmp_path, ca)
    found: list = []
    attempt_meanwhile(fed, found)
    rerun(hpc, fed, link_check_stand_in([]))
    ((raised, before, after),) = found
    assert raised is not None
    assert REFUSED in raised
    assert "phase bootstrapped" in raised  # the state the first run left while it works
    assert after == before
    assert phase(fed) == "linked"


def test_c06_the_core_phase_lock_is_held_by_a_rerun_that_fails_and_released_when_it_ends(hpc, tmp_path, ca):
    """C06, O63: a rerun whose link check fails holds the lock to its end, so a second one is refused meanwhile; it
    releases it however it ends, so a rerun after it is not refused (and, the state not being `linked`, ends
    `bootstrapped`)."""
    fed = verified(hpc, tmp_path, ca)
    found: list = []
    attempt_meanwhile(fed, found)  # once: a second `core_here` from inside the first rerun
    with pytest.raises(RuntimeError, match="the link check failed"):
        rerun(hpc, fed, failing_links)
    assert found[0][0] is not None
    assert REFUSED in found[0][0]
    assert phase(fed) == "bootstrapped"
    rerun(hpc, fed, link_check_stand_in([]))  # raises if the lock were still held
    assert phase(fed) == "bootstrapped"


def test_c06_a_core_rerun_by_hand_is_refused_while_ups_own_core_phase_runs(hpc, tmp_path):
    """C06, O63: `up`'s core phase (here, or the node's step under `[hpc] core = "step"`) takes the same lock: a rerun
    started by hand meanwhile is refused, and `up` ends `linked` all the same."""
    art = artifacts(tmp_path / "art")
    fed = driver_over(hpc, tmp_path / "fed", study_layout(tmp_path / "layout"))
    fed.site = site_env(tmp_path / "fed", art)
    seen: list = []

    def core_while_up_runs(layout: Any, owner: Any, values: Any, environ: Any = None) -> str:
        if not seen:
            seen.append(None)
            try:
                fed.driver.core_here()
            except RuntimeError as error:
                seen[0] = str(error)
        return "core: linked"

    fed.driver.core = core_while_up_runs
    fed.driver.up(hpc.settings.site_from_environment(fed.site))
    assert seen[0] is not None
    assert REFUSED in seen[0]
    assert phase(fed) == "linked"


@pytest.mark.parametrize("before", ["providers", "bootstrapped"])
def test_c06_the_core_phase_of_a_federation_that_was_not_linked_ends_bootstrapped_as_up_needs(
    hpc, tmp_path, ca, before
):
    """C06, O63: `up` is unchanged. Brought up, the federation is `linked` (it is `up` that says so); the core's phase
    (the node's step under `[hpc] core = "step"`) on a state that is not `linked` ends `bootstrapped` and never
    `linked`, so a rerun cannot link a federation whose up did not finish."""
    fed = brought_up(hpc, tmp_path, ca)
    assert phase(fed) == "linked"
    fed.driver.set(lambda s: s.update(phase=before))
    rerun(hpc, fed, link_check_stand_in([]))
    assert phase(fed) == "bootstrapped"
