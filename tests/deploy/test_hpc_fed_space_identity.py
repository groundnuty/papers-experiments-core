"""C06, O63: what `verify-data` verified is bound to the space it read, by its Onezone ID, not to a manifest alone.

The marker `verify-data` leaves was bound to the digest of a manifest and to nothing else, and the core's phase makes a
space that is missing again by its name (`ensure_spaces`): a space deleted out of band after `up` and `verify-data`, and
then a rerun of the core's phase by hand, came back empty under a new ID, the rerun linked the federation again, and
`shards.py plan` accepted it on the old marker (Codex third pass, 17 Nov 2026, item 423). `up` now records each
ingested space's ID in the state (`data.ingested.<space>.space_id`), `verify-data` records the ID it read in the marker,
and a rerun that finds another ID than the recorded one does not link the federation again and clears the marker; `plan`
refuses a marker whose ID is not the one the platform file now names. The zone is a fake that keeps its spaces by name
and makes a missing one again with another ID; Slurm is the fake of hpc_fed_support and nothing reaches a cluster."""

import functools
import importlib.util
import json
import sys
from types import SimpleNamespace

import pytest
from fingerprint_support import standin_python
from hpc_fed_support import LAYOUT, hpc_on_path
from hpc_ingested_support import ONE, artifacts, driver_over, held, ingested_state, site_env, study_layout
from hpc_rehearsal_support import BootStandIn, link_check_stand_in

from expcore_onedata.linkcheck import LinkReport, PairResult

SCRIPT = LAYOUT.parents[1] / "shards.py"


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import fedlinks

        modules.fedlinks = fedlinks
        yield modules


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_identity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_identity"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        module.identify_core = lambda plan: {"known": True, "stale": False, "why": None}
        yield module
    finally:
        sys.modules.pop("hpc_shards_identity", None)


class FakeZone:
    """The zone's spaces by name, as the bootstrap finds them: one that exists keeps its ID, one that does not is made
    (empty) under a new ID, which is what `ensure_spaces` does for a space deleted out of band. Used as the bootstrap of
    both the core's phase and `verify-data`."""

    def __init__(self, label: str) -> None:
        self.label, self.made, self.count = label, {}, 0

    def delete(self, space: str) -> None:
        del self.made[space]

    def __call__(self, rest, layout, owner):
        boot = BootStandIn(self.label)(rest, layout, owner)
        for space in layout.spec.spaces:
            if space.name not in self.made:
                self.count += 1
                self.made[space.name] = f"sid-{space.name}-{self.label}-{self.count}"
        boot.ids.spaces = {space.name: self.made[space.name] for space in layout.spec.spaces}
        return boot


def wire(hpc, fed, zone, data) -> None:
    """The driver's core's phase and `verify-data`'s read, over `zone` and the stand-in data."""

    def rest(ca_file, **kwargs):
        return SimpleNamespace(close=lambda: None)

    fed.driver.core = functools.partial(
        hpc.boot.core_phase, rest=rest, bootstrap=zone, steps=lambda b: None,
        content=lambda b: SimpleNamespace(reset=lambda timeout: None), data=held({}),
    )  # fmt: skip
    fed.driver.verify = functools.partial(hpc.boot.verify_ingested, rest=rest, bootstrap=zone, data=data)


def brought_up(hpc, tmp_path, ca):
    """A federation brought up over the fake zone, its ingested space (delta-space) declaring the manifest ONE."""
    fed = driver_over(hpc, tmp_path / "fed", study_layout(tmp_path / "layout"), "fed-a", ca)
    fed.site = site_env(tmp_path / "fed", artifacts(tmp_path / "art"))
    fed.zone = FakeZone("fed-a")
    wire(hpc, fed, fed.zone, held({"delta-space": ONE}))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
        fed.driver.up(hpc.settings.site_from_environment(fed.site))
    return fed


def verified(hpc, tmp_path, ca):
    fed = brought_up(hpc, tmp_path, ca)
    fed.driver.verify_data()
    return fed


def facts_id(fed, space="delta-space") -> str:
    return json.loads((fed.root / "facts.json").read_text(encoding="utf-8"))["spaces"][space]["id"]


def rerun(hpc, fed) -> list:
    """`hpcfed.py core` on the root: the driver's own core's phase again, its link check passing; what it said."""
    said: list = []
    fed.driver.say = said.append
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
        fed.driver.core_here()
    return said


def phase(fed) -> str:
    return fed.driver.store.read()["phase"]


def shard_of(shards, tmp_path, fed):
    (tmp_path / "study").mkdir(exist_ok=True)  # the campaign fingerprint is taken on a copy of it (O63)
    lines = [f'{k} = "{tmp_path / k}"' for k in ("study", "core_src", "results", "tmpfs")]
    python = standin_python(tmp_path / "venv", real_platform=True)
    lines += [f'python = "{python}"', 'step_path = "/usr/bin:/bin"', "cpus = 6", 'memory = "18g"']
    roots = (
        fed.root,
        tmp_path / "other" / "root",
    )  # a campaign needs two shards, one root each; only the first is read
    for n, root in enumerate(roots):
        lines += ["[[shards]]", f'label = "fed-{n}"', f'root = "{root}"']
    (tmp_path / "shards.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return shards.load_plan(tmp_path / "shards.toml").shards[0]


# -- what up and verify-data record ------------------------------------------------------------------------------------


def test_c06_up_records_each_ingested_spaces_onezone_id_from_the_facts_it_wrote(hpc, tmp_path, ca):
    """C06, O63: the state names each ingested space with the ID the zone gave it, as facts.json holds it, and every
    space of the record has one."""
    fed = brought_up(hpc, tmp_path, ca)
    assert ingested_state(fed)["delta-space"]["space_id"] == facts_id(fed) == fed.zone.made["delta-space"]
    assert all(record["space_id"] for record in ingested_state(fed).values())


def test_c06_verify_data_records_the_id_of_the_space_it_read_in_its_marker(hpc, tmp_path, ca):
    """C06, O63: the marker holds the manifest's digest, the counts and the ID of the space that was read."""
    fed = verified(hpc, tmp_path, ca)
    assert ingested_state(fed)["delta-space"]["verified"] == {
        "manifest": ONE.digest(), "files": 3, "bytes": 14, "space_id": fed.zone.made["delta-space"]}  # fmt: skip


# -- a space made again after up and verify-data ---------------------------------------------------------------------


def test_c06_a_rerun_after_a_space_was_deleted_does_not_link_again_and_clears_the_marker(hpc, tmp_path, ca):
    """C06, O63 (Codex, 17 Nov 2026): the space is deleted out of band, `hpcfed.py core` makes it again by name (empty,
    under a new ID), its link check passes, and it linked the federation over the old marker. The rerun compares the ID
    in the facts it wrote with the one `up` recorded: the federation stays `bootstrapped`, the rerun says which space
    and why, and the marker is gone."""
    fed = verified(hpc, tmp_path, ca)
    old = facts_id(fed)
    fed.zone.delete("delta-space")
    said = rerun(hpc, fed)
    assert facts_id(fed) != old  # the fake zone made it again, empty, under another ID
    assert phase(fed) == "bootstrapped"
    assert any("delta-space" in line and "not the one up recorded" in line for line in said)
    assert ingested_state(fed)["delta-space"]["verified"] is None
    assert ingested_state(fed)["delta-space"]["space_id"] == old  # up's record is never rewritten


def failing_links(config, client):
    """`fedlinks.check_links` finding a pair that does not transfer."""
    pair = PairResult("providers[0]", "providers[1]", False, reason="the fetch failed")
    return LinkReport(False, "providers[0] <- providers[1]: the fetch failed", (pair,), 1.0, 2.0)


def test_c06_a_rerun_that_fails_after_the_space_was_made_again_still_clears_the_marker(hpc, tmp_path, ca):
    """C06, O63 (Codex, 17 Nov 2026, review of this fix): the core's phase made the space again and wrote its new ID,
    then its link check failed: the failure bypassed the comparison, so the old marker stayed in the state. The marker
    of a space whose ID is not the recorded one is cleared however the phase ends."""
    fed = verified(hpc, tmp_path, ca)
    fed.zone.delete("delta-space")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", failing_links)
        with pytest.raises(RuntimeError, match="the link check failed"):
            fed.driver.core_here()
    assert phase(fed) == "bootstrapped"
    assert ingested_state(fed)["delta-space"]["verified"] is None


def test_c06_a_later_rerun_that_passes_clears_a_marker_the_failed_one_could_not(hpc, tmp_path, ca):
    """C06, O63: the comparison is made after every core phase that ends, not only a relinking one: a federation that is
    already `bootstrapped` whose space was made again loses the marker it still holds when a rerun passes."""
    fed = verified(hpc, tmp_path, ca)
    marker = ingested_state(fed)["delta-space"]["verified"]
    fed.driver.set(lambda s: s.update(phase="bootstrapped"))  # as a failed rerun left it
    fed.zone.delete("delta-space")
    rerun(hpc, fed)
    assert phase(fed) == "bootstrapped"
    assert marker is not None
    assert ingested_state(fed)["delta-space"]["verified"] is None


def test_c06_a_rerun_that_finds_every_space_as_it_was_links_again_and_the_marker_stays(hpc, tmp_path, ca):
    """C06, O63: nothing is refused where nothing moved: the same zone, the same IDs, so the rerun that passes its
    link check links the federation again and the marker is the one `verify-data` left."""
    fed = verified(hpc, tmp_path, ca)
    marker = ingested_state(fed)["delta-space"]["verified"]
    said = rerun(hpc, fed)
    assert phase(fed) == "linked"
    assert any("linked again" in line for line in said)
    assert ingested_state(fed)["delta-space"]["verified"] == marker


def test_c06_verify_data_on_a_space_made_again_since_up_fails_naming_it_and_leaves_no_marker(hpc, tmp_path, ca):
    """C06, O63: `verify-data` compares the ID in the facts with the one `up` recorded before it reads anything: a
    space made again since is refused naming it, however the state came to say `linked`."""
    fed = verified(hpc, tmp_path, ca)
    fed.zone.delete("delta-space")
    rerun(hpc, fed)  # stays bootstrapped: the state is put back as an operator's edit or an older driver left it
    fed.driver.set(lambda s: s.update(phase="linked"))
    with pytest.raises(RuntimeError, match=r"^delta-space: the space is not the one up recorded") as refused:
        fed.driver.verify_data()
    assert "not verified" in str(refused.value)
    assert ingested_state(fed)["delta-space"]["verified"] is None


def test_c06_verify_data_and_plan_refuse_a_state_that_records_no_id_for_a_space(hpc, shards, tmp_path, ca):
    """C06, O63: a state an older `up` wrote has no ID to bind a marker to: `verify-data` says to go down and up again,
    and `plan` takes a marker with no ID for none."""
    fed = verified(hpc, tmp_path, ca)
    fed.driver.set(lambda s: s["data"]["ingested"]["delta-space"].pop("space_id"))
    with pytest.raises(RuntimeError, match=r"delta-space: its state records no ID for the space.*down, then up"):
        fed.driver.verify_data()
    assert ingested_state(fed)["delta-space"]["verified"] is None
    with pytest.raises(shards.ShardsError, match=r"ingested space\(s\) have no verified data: delta-space"):
        shards.federation(shard_of(shards, tmp_path, fed))


# -- plan --------------------------------------------------------------------------------------------------------------


def test_c06_plan_takes_a_federation_whose_marker_is_for_the_space_the_platform_file_names(hpc, shards, tmp_path, ca):
    """C06, O63: the ordinary case is unchanged: the marker's ID, the state's and the platform file's are one."""
    fed = verified(hpc, tmp_path, ca)
    assert shards.federation(shard_of(shards, tmp_path, fed))["job"]


def test_c06_plan_refuses_a_marker_for_another_space_than_the_platform_file_now_names(hpc, shards, tmp_path, ca):
    """C06, O63: the space was made again, the rerun's platform file names the new ID, and the state is put back to
    `linked` with the old marker (an operator's edit, or a driver that did not clear it): `plan` compares the marker's
    ID with the platform file's and refuses, naming the space and saying it is not the one that was verified."""
    fed = verified(hpc, tmp_path, ca)
    marker = ingested_state(fed)["delta-space"]["verified"]
    fed.zone.delete("delta-space")
    rerun(hpc, fed)
    fed.driver.set(lambda s: (s.update(phase="linked"), s["data"]["ingested"]["delta-space"].update(verified=marker)))
    with pytest.raises(shards.ShardsError, match=r"ingested space\(s\) are not the ones verified: delta-space"):
        shards.federation(shard_of(shards, tmp_path, fed))


def test_c06_a_state_with_a_malformed_space_id_is_refused_when_it_is_read(hpc, tmp_path, ca):
    """C06, O63: the record is read by the driver and by plan: an ID that is not a non-empty string, in the record or
    in the marker, is not this one's."""
    fed = verified(hpc, tmp_path, ca)
    path = fed.root / "hpc-federation.json"
    good = json.loads(path.read_text(encoding="utf-8"))
    for where, bad in [("record", 7), ("record", ""), ("record", ["sid"]), ("marker", 7), ("marker", "")]:
        state = json.loads(json.dumps(good))
        record = state["data"]["ingested"]["delta-space"]
        (record if where == "record" else record["verified"])["space_id"] = bad
        path.write_text(json.dumps(state), encoding="utf-8")
        with pytest.raises(hpc.state.StateError, match=r"a space's ID is not the driver's"):
            fed.driver.store.read()
    path.write_text(json.dumps(good), encoding="utf-8")
    assert fed.driver.store.read()["phase"] == "linked"
