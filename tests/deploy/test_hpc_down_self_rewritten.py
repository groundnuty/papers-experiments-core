"""O63: `hpcfed.py down` compares STRICTLY only the evidence a step or the study writes (the fault journal, the relay's
journals, the shard claims, the step records, the data manifests, the files `up` writes once) and refuses on any change
to it since `collect`. The federation's own self-rewritten files (the state, the link check) post-date every run once
`collect` has run, so a change to them is no reason to refuse and no second archive: `down` records which of them
changed, by name only, in the state file and in its output; the collect-time copy stays what the post-check reads.
Against the fake Slurm of hpc_fed_support; nothing reaches a cluster."""

import json
import re

import pytest
from down_rig_support import Rig, make_site
from hpc_fed_support import hpc_on_path
from root_evidence_support import EVIDENCE, REQUEST_ID, make_root, record_step

LINK = b'{"format": "expcore-link-check/1", "ok": true}\n'
LINK_LATER = b'{"format": "expcore-link-check/1", "ok": true, "later": "check-b3f1c0"}\n'
APPENDED = b'{"event": "appended after collect"}\n'
JOB = "424242"


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcevidence

        modules.evidence = hpcevidence
        yield modules


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def _collected_rig(hpc, tmp_path, site, files=None):
    """A rig whose root holds a step record and `files` (name -> bytes, besides a link check), collected."""
    rig = Rig(hpc, tmp_path, site)
    for name, content in {"link-check.json": LINK, **(files or {})}.items():
        path = rig.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    record_step(rig.root, "shard-1")
    rig.collected()
    return rig


def _gated(rig):
    """The state after down's gate: down is stopped right after it (the state goes with the root at the end of a
    whole down, so it is read before)."""

    def stopped(node_checked=None):
        raise RuntimeError("stopped for the test, after the gate")

    rig.driver.take_down = stopped
    with pytest.raises(RuntimeError, match="stopped for the test"):
        rig.driver.down()
    return rig.driver.store.read()


def _rewrite_state(rig):
    """The state file rewritten the way an operator's command after `collect` does (`resume-line` records a node)."""
    entry = {"job": JOB, "node": "node-a", "label": "shard-1", "name": "step-a"}
    rig.driver.set(lambda state: state.setdefault("shard_nodes", []).append(entry))


# ------------------------------------------------------------------------------------------------ self-rewritten files
def test_a_link_check_rewritten_after_collect_lets_down_proceed_and_the_output_names_it(hpc, tmp_path, site):
    """O63: the link check is rewritten by the federation's own checks, so a root whose link check changed since
    `collect` and nothing else is torn down as collected, and down's output says which file changed (and says so while
    it works)."""
    rig = _collected_rig(hpc, tmp_path, site)
    (rig.root / "link-check.json").write_bytes(LINK_LATER)
    said = []
    rig.driver.say = said.append
    printed = rig.driver.down()
    assert "link-check.json" in printed.splitlines()[0]
    assert "down: the held job 4242 is gone, and so is the root" in printed
    assert any("link-check.json" in line for line in said)
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_the_state_records_the_self_rewritten_files_that_changed_by_name_only(hpc, tmp_path, site):
    """O63: `evidence_rewritten_after_collect`: when (UTC), the shards the root was collected for and the names of the
    files, never a value of one."""
    rig = _collected_rig(hpc, tmp_path, site)
    (rig.root / "link-check.json").write_bytes(LINK_LATER)
    state = _gated(rig)
    (entry,) = state["evidence_rewritten_after_collect"]
    assert set(entry) == {"at", "shards", "files"}
    assert entry["shards"] == ["shard-1"]
    assert entry["files"] == ["link-check.json"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", entry["at"])
    assert "check-b3f1c0" not in json.dumps(state)


def test_a_state_file_rewritten_after_collect_does_not_refuse_down_and_is_named(hpc, tmp_path, site):
    """O63: the state is rewritten by state updates (a resumed step's node, a data verification) after `collect`; it is
    named as changed and is no reason to refuse."""
    rig = _collected_rig(hpc, tmp_path, site)
    _rewrite_state(rig)
    (entry,) = _gated(rig)["evidence_rewritten_after_collect"]
    assert entry["files"] == ["hpc-federation.json"]


def test_both_self_rewritten_files_changed_are_named_together_sorted(hpc, tmp_path, site):
    rig = _collected_rig(hpc, tmp_path, site)
    (rig.root / "link-check.json").write_bytes(LINK_LATER)
    _rewrite_state(rig)
    (entry,) = _gated(rig)["evidence_rewritten_after_collect"]
    assert entry["files"] == ["hpc-federation.json", "link-check.json"]


@pytest.mark.parametrize("how", ["appears", "vanishes"])
def test_a_link_check_that_appears_or_vanishes_after_collect_is_named_not_a_crash(hpc, tmp_path, site, how):
    """O63: the heal's `discard_record` removes the record, and a check after the collect makes one: either is a change
    of a file the federation rewrites, named in the state."""
    rig = _collected_rig(hpc, tmp_path, site)
    (rig.root / "link-check.json").unlink()
    if how == "appears":  # collected again with no record, then a check writes one
        rig.collected()
        (rig.root / "link-check.json").write_bytes(LINK)
    (entry,) = _gated(rig)["evidence_rewritten_after_collect"]
    assert entry["files"] == ["link-check.json"]


def test_nothing_is_recorded_when_no_self_rewritten_file_changed(hpc, tmp_path, site):
    rig = _collected_rig(hpc, tmp_path, site)
    assert "evidence_rewritten_after_collect" not in _gated(rig)


def test_a_second_down_after_a_failure_halfway_keeps_the_record_and_adds_none(hpc, tmp_path, site):
    """O63: a down that began is not gated again, so the first one's record is the one that stays."""
    rig = _collected_rig(hpc, tmp_path, site)
    (rig.root / "link-check.json").write_bytes(LINK_LATER)
    assert len(_gated(rig)["evidence_rewritten_after_collect"]) == 1
    rig.driver.set(lambda state: state.update(phase="stopping"))
    (rig.root / "link-check.json").write_bytes(b"{}\n")
    assert rig.driver.evidence_gate(False) == ""
    assert len(rig.driver.store.read()["evidence_rewritten_after_collect"]) == 1


# ------------------------------------------------------------------------------------------------ strict files
STRICT_CHANGES = {
    "a journal line appended": "fault-journal/injections.jsonl",
    "a reset hold appended": "fault-journal/reset-holds.jsonl",
    "the relay's late steps": "fault-relay/late-steps.jsonl",
    "a relay answer": f"fault-relay/answers/{REQUEST_ID}.json",
    "the shard claim": "shard-claim.json",
    "the shard claim's lock": "shard-claim.lock",
    "a data manifest": "data-manifests/alpha-space.json",
    "the step record": "shard-steps/shard.json",
    "the facts": "facts.json",
    "the platform file": "platform.toml",
    "the render identity": "render-identity.json",
    "the pilot's wiring": "day0-wiring.toml",
    "the ingest's evidence": "ingest-evidence.json",
}


@pytest.mark.parametrize("name", list(STRICT_CHANGES.values()), ids=list(STRICT_CHANGES))
def test_a_change_to_evidence_a_step_or_the_study_writes_refuses_down_naming_the_file(hpc, tmp_path, site, name):
    """O63: the journals, the relay's files, the claims, the step records, the manifests and what `up` wrote once are
    compared strictly: one more line, or any change, since `collect` and down refuses, before it stops anything."""
    present = {key: EVIDENCE[key] for key in (name,) if key in EVIDENCE}
    rig = _collected_rig(hpc, tmp_path, site, files=present)
    path = rig.root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(path.read_bytes() + APPENDED if path.exists() else APPENDED)
    with pytest.raises(RuntimeError, match=r"changed after it was collected") as refused:
        rig.driver.down()
    assert name in str(refused.value)
    assert rig.fake.cancelled == []
    assert rig.fake.calls == []
    assert rig.root.exists()


def test_a_strict_change_refuses_even_when_a_self_rewritten_file_changed_too_and_names_only_the_strict_one(
    hpc, tmp_path, site
):
    rig = _collected_rig(hpc, tmp_path, site, files={"shard-claim.json": EVIDENCE["shard-claim.json"]})
    (rig.root / "link-check.json").write_bytes(LINK_LATER)
    _rewrite_state(rig)
    (rig.root / "shard-claim.json").write_bytes(b'{"held_by": "another shard"}\n')
    with pytest.raises(RuntimeError, match=r"changed after it was collected") as refused:
        rig.driver.down()
    assert "shard-claim.json" in str(refused.value)
    assert "link-check.json" not in str(refused.value)
    assert "hpc-federation.json" not in str(refused.value)


def test_a_refused_down_leaves_the_state_as_it_was_and_records_nothing(hpc, tmp_path, site):
    """O63: the comparison comes first and the record only when down goes on: a refusal touches no file."""
    rig = _collected_rig(hpc, tmp_path, site, files={"fault-journal/injections.jsonl": b'{"event": "a"}\n'})
    (rig.root / "link-check.json").write_bytes(LINK_LATER)
    (rig.root / "fault-journal" / "injections.jsonl").write_bytes(b'{"event": "a"}\n' + APPENDED)
    before = (rig.root / "hpc-federation.json").read_bytes()
    with pytest.raises(RuntimeError, match="changed after it was collected"):
        rig.driver.down()
    assert (rig.root / "hpc-federation.json").read_bytes() == before


def test_a_link_where_a_self_rewritten_file_was_still_refuses_for_it_cannot_be_archived(hpc, tmp_path, site):
    """O63: a link is never followed, so a root that no longer archives is refused whichever file the link replaced."""
    rig = _collected_rig(hpc, tmp_path, site)
    (rig.root / "link-check.json").unlink()
    (rig.root / "link-check.json").symlink_to(rig.root / "facts.json")
    with pytest.raises(RuntimeError, match="changed after it was collected"):
        rig.driver.down()


# ------------------------------------------------------------------------------------------------ the marker
def test_a_marker_without_the_per_file_hashes_compares_the_whole_archive(hpc, tmp_path, site):
    """O63: a marker an earlier `collect` wrote has no `files`: nothing can be told apart, so any change refuses, a
    self-rewritten file's included, and an unchanged root goes as it did."""
    rig = Rig(hpc, tmp_path, site)
    (rig.root / "link-check.json").write_bytes(LINK)
    record_step(rig.root, "shard-1")
    held = hpc.evidence.archive_root(rig.root, "shard-1")
    hpc.evidence.write_marker(rig.root, "shard-1", held.sha256, held.steps_sha256)
    assert "files" not in json.loads((rig.root / "evidence-collected.json").read_text())
    (rig.root / "link-check.json").write_bytes(LINK_LATER)
    with pytest.raises(RuntimeError, match="changed after it was collected"):
        rig.driver.down()
    (rig.root / "link-check.json").write_bytes(LINK)
    assert rig.driver.down().startswith("down: the held job 4242 is gone")


@pytest.mark.parametrize(
    "files", ["x", [], {"link-check.json": "not a hash"}, {"../up/x": "a" * 64}, {"a b": "a" * 64}]
)
def test_a_marker_whose_file_hashes_are_not_of_the_form_is_no_marker(hpc, tmp_path, site, files):
    rig = _collected_rig(hpc, tmp_path, site)
    marker = rig.root / "evidence-collected.json"
    marker.write_text(json.dumps({**json.loads(marker.read_text()), "files": files}))
    with pytest.raises(RuntimeError, match="was not collected"):
        rig.driver.down()


def test_the_marker_names_a_hash_for_each_file_of_the_archive_and_no_content(hpc, tmp_path, site):
    rig = _collected_rig(hpc, tmp_path, site)
    held = hpc.evidence.archive_root(rig.root, "shard-1")
    found = json.loads((rig.root / "evidence-collected.json").read_text())
    assert found["files"] == held.hashes
    assert {"hpc-federation.json", "link-check.json", "facts.json", "shard-steps/shard-1.json"} <= set(found["files"])
    assert "check-b3f1c0" not in json.dumps(found)


# ------------------------------------------------------------------------------------------------ the flag
def test_discard_evidence_with_only_self_rewritten_files_changed_discards_nothing_and_still_names_them(
    hpc, tmp_path, site
):
    """O63: there is no evidence to discard, so no discard is recorded; the rewritten files are named all the same."""
    rig = _collected_rig(hpc, tmp_path, site)
    (rig.root / "link-check.json").write_bytes(LINK_LATER)

    def stopped(node_checked=None):
        raise RuntimeError("stopped for the test, after the gate")

    rig.driver.take_down = stopped
    with pytest.raises(RuntimeError, match="stopped for the test"):
        rig.driver.down(discard_evidence=True)
    state = rig.driver.store.read()
    assert "evidence_discarded" not in state
    assert state["evidence_rewritten_after_collect"][0]["files"] == ["link-check.json"]


def test_a_discard_of_a_changed_strict_file_records_both_the_discard_and_the_rewritten_files(hpc, tmp_path, site):
    rig = _collected_rig(hpc, tmp_path, site, files={"shard-claim.json": EVIDENCE["shard-claim.json"]})
    (rig.root / "link-check.json").write_bytes(LINK_LATER)
    (rig.root / "shard-claim.json").write_bytes(b"{}\n")

    def stopped(node_checked=None):
        raise RuntimeError("stopped for the test, after the gate")

    rig.driver.take_down = stopped
    with pytest.raises(RuntimeError, match="stopped for the test"):
        rig.driver.down(discard_evidence=True)
    state = rig.driver.store.read()
    assert state["evidence_discarded"][0]["changed_since_collect"] is True
    assert state["evidence_rewritten_after_collect"][0]["files"] == ["link-check.json"]


def test_the_state_loader_refuses_a_rewritten_record_that_is_not_of_the_form(hpc, tmp_path, site):
    rig = Rig(hpc, tmp_path, site)
    rig.driver.store.update(lambda state: {**state, "evidence_rewritten_after_collect": [{"at": 1}]})
    with pytest.raises(hpc.state.StateError, match="evidence_rewritten_after_collect"):
        rig.driver.store.read()


# ------------------------------------------------------------------------------------------------ the two sets
def test_every_allow_listed_file_is_in_exactly_one_of_the_two_sets(hpc):
    """O63: the allow-list is the two sets side by side, so a file added to it must be placed in one of them."""
    evidence = hpc.evidence
    assert not set(evidence.STRICT_FILES) & set(evidence.SELF_REWRITTEN_FILES)
    assert sorted(evidence.EVIDENCE_FILES) == sorted([*evidence.STRICT_FILES, *evidence.SELF_REWRITTEN_FILES])
    assert len(set(evidence.EVIDENCE_FILES)) == len(evidence.EVIDENCE_FILES)
    assert {evidence.set_of(name) for name in evidence.STRICT_FILES} == {evidence.STRICT}
    assert {evidence.set_of(name) for name in evidence.SELF_REWRITTEN_FILES} == {evidence.SELF_REWRITTEN}


def test_every_directory_of_the_allow_list_is_strict_and_holds_no_self_rewritten_name(hpc):
    evidence = hpc.evidence
    trees = {directory for directory, _ in evidence.EVIDENCE_TREES} | set(evidence.WHOLE_TREES)
    assert trees == set(evidence.STRICT_TREES)
    assert not [name for name in evidence.SELF_REWRITTEN_FILES if "/" in name]


def test_every_file_a_full_root_archives_is_in_one_of_the_two_sets_and_the_self_rewritten_ones_are_two(hpc, tmp_path):
    """O63: a root holding every kind of evidence file: none is placed in neither set, so none compares by default."""
    evidence = hpc.evidence
    held = evidence.archive_root(make_root(tmp_path / "root"), "s")
    assert held.hashes
    assert set(held.hashes) == set(held.names)
    assert [name for name in held.hashes if evidence.set_of(name) is None] == []
    assert sorted(name for name in held.hashes if evidence.set_of(name) == evidence.SELF_REWRITTEN) == [
        "hpc-federation.json",
        "link-check.json",
    ]


def test_a_name_in_neither_set_compares_strictly(hpc):
    """O63: the default for a file nobody placed is the safe one."""
    assert hpc.evidence.set_of("something-new.json") is None
    assert hpc.evidence.compared_strictly("something-new.json")
    assert not hpc.evidence.compared_strictly("link-check.json")
