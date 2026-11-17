"""O63: `hpcfed.py down` gates on evidence PRESENT in the root, not on a shard step having run. A root that only ran
Day-0 measurements (a non-empty fault journal, the relay's journals, the pilot's wiring) holds evidence the study's
post-check reads, and `down` refuses it without a collected-evidence marker, unless `--discard-evidence`; a root that
`up` alone made (nothing run) holds none, and an emergency teardown of a failed `up` stays unblocked. What `up` writes
and what a run leaves are two named sets (`hpcevidence.UP_FILES`, `RUN_FILES` and their trees), every file of the
allow-list in exactly one. Against the fake Slurm of hpc_fed_support; nothing reaches a cluster."""

import ast
import os
import re
from pathlib import Path

import pytest
from down_rig_support import Rig, make_site
from hpc_fed_support import LAYOUT, hpc_on_path
from root_evidence_support import EVIDENCE, REQUEST_ID, TAKEN_ID, leave_what_up_leaves, make_root, record_step

ROOT_OF_REPO = Path(__file__).resolve().parents[2]
JOURNAL_LINE = b'{"event": "injecting", "target": "aspen"}\n'
RELAY_LINE = b'{"verb": "status", "service": "aspen"}\n'


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcevidence

        modules.evidence = hpcevidence
        yield modules


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def _write(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _day0_rig(hpc, tmp_path, site):
    """A rig whose root is what a Day-0-only run leaves: everything `up` leaves, then the injector's journal, the
    relay's requests and answers, and the pilot's wiring. No shard step ran: there is no `shard-steps/`."""
    rig = Rig(hpc, tmp_path, site)
    leave_what_up_leaves(rig.root)
    _write(rig.root, "fault-journal/injections.jsonl", JOURNAL_LINE)
    _write(rig.root, "fault-journal/shared-holds.jsonl", EVIDENCE["fault-journal/shared-holds.jsonl"])
    _write(rig.root, "fault-journal/federation.lock", b"")
    _write(rig.root, f"fault-relay/requests/{REQUEST_ID}.json", RELAY_LINE)
    _write(rig.root, f"fault-relay/answers/{REQUEST_ID}.json", EVIDENCE[f"fault-relay/answers/{REQUEST_ID}.json"])
    _write(rig.root, "day0-wiring.toml", EVIDENCE["day0-wiring.toml"])
    assert not (rig.root / "shard-steps").exists()
    return rig


def _gated(rig, **options):
    """The state after down's gate: down is stopped right after it (the state goes with the root at the end of a whole
    down, so it is read before)."""

    def stopped():
        raise RuntimeError("stopped for the test, after the gate")

    rig.driver.take_down = stopped
    with pytest.raises(RuntimeError, match="stopped for the test"):
        rig.driver.down(**options)
    return rig.driver.store.read()


# ------------------------------------------------------------------------------------------------ a Day-0-only root
def test_down_refuses_a_day0_only_root_with_no_marker_before_it_stops_anything(hpc, tmp_path, site):
    """O63: a root with a fault journal, the relay's files and the wiring, and no shard step, is refused: the message
    names what is there (never a file or a path) and the remedies, nothing is cancelled or stopped, the state is
    untouched and the root stays, so a collect can still read it."""
    rig = _day0_rig(hpc, tmp_path, site)
    state_before = (rig.root / "hpc-federation.json").read_bytes()
    with pytest.raises(RuntimeError) as refused:
        rig.driver.down()
    message = str(refused.value)
    assert re.search(
        r"holds evidence.*the fault journal.*the relay's journals.*the day-0 wiring.*not collected", message
    )
    assert "collect --roots-only" in message
    assert "--discard-evidence" in message
    assert "shard results are recorded" not in message
    assert "injections.jsonl" not in message
    assert str(rig.root) not in message
    assert rig.fake.cancelled == []
    assert rig.fake.calls == []
    assert (rig.root / "hpc-federation.json").read_bytes() == state_before
    assert rig.root.exists()


def test_discard_evidence_lets_down_remove_a_day0_only_root_and_records_what_was_discarded(hpc, tmp_path, site):
    """O63: with `--discard-evidence` down goes on, says what it discarded (the kinds of evidence, no shard to name),
    and records it in the state: the time, no shards, the kinds, that it had not been collected, and the reason."""
    rig = _day0_rig(hpc, tmp_path, site)
    said = []
    rig.driver.say = said.append
    (entry,) = _gated(rig, discard_evidence=True, discard_reason="the pilot was abandoned")["evidence_discarded"]
    assert entry["shards"] == []
    assert entry["evidence"] == ["the fault journal", "the relay's journals", "the day-0 wiring"]
    assert entry["changed_since_collect"] is False
    assert entry["reason"] == "the pilot was abandoned"
    assert any("--discard-evidence" in line and "the fault journal" in line and "recorded in the state file" in line
               for line in said)  # fmt: skip


def test_discard_evidence_removes_a_day0_only_root_and_the_output_says_what_went(hpc, tmp_path, site):
    """O63: the output's first line names the kinds of evidence discarded and that they were not collected, and the
    whole down follows: the job cancelled, the root removed."""
    rig = _day0_rig(hpc, tmp_path, site)
    printed = rig.driver.down(discard_evidence=True)
    assert printed.startswith("down: --discard-evidence: the root's evidence (the fault journal")
    assert "was not collected and is removed with it" in printed.splitlines()[0]
    assert "down: the held job 4242 is gone, and so is the root" in printed
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_a_root_after_up_alone_goes_down_as_it_always_did(hpc, tmp_path, site):
    """O63: everything `up` leaves (the platform file, the render identity, a data manifest, the link check's record
    and lock, the relay's empty directories, its heartbeat and lock, the empty fault journal) is no evidence a run
    left: down proceeds, says nothing of evidence, and is not refused. A failed `up`'s root is thinner still."""
    rig = Rig(hpc, tmp_path, site)
    leave_what_up_leaves(rig.root)
    assert hpc.evidence.present_evidence(rig.root) == ()
    assert list((rig.root / "fault-journal").iterdir()) == []  # up writes no line of the fault journal
    printed = rig.driver.down()
    assert printed.startswith("down: the held job 4242 is gone, and so is the root")
    assert "evidence" not in printed
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_a_failed_ups_root_goes_down_too(hpc, tmp_path, site):
    """O63: an emergency teardown stays unblocked: a root with the state and the secrets `up` made before it failed,
    none of the files a finished `up` writes, and the relay not started."""
    rig = Rig(hpc, tmp_path, site)
    (rig.root / "facts.json").unlink()
    assert hpc.evidence.present_evidence(rig.root) == ()
    assert rig.driver.down().startswith("down: the held job 4242 is gone, and so is the root")
    assert not rig.root.exists()


def test_a_shard_step_root_is_refused_as_it_was_naming_the_shard(hpc, tmp_path, site):
    """O63: a root with a step record is refused as before, naming its shard, though a journal is there too."""
    rig = _day0_rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    recorded = r"shard results are recorded in this root \(shard-1\) and its evidence was not collected"
    with pytest.raises(RuntimeError, match=recorded):
        rig.driver.down()
    (entry,) = _gated(rig, discard_evidence=True)["evidence_discarded"]
    assert entry["shards"] == ["shard-1"]
    assert entry["evidence"] == ["step records", "the fault journal", "the relay's journals", "the day-0 wiring"]


# ------------------------------------------------------------------------------------------------ what is evidence
PRESENT = {  # one evidence file, alone, in a root `up` made: the kind of evidence down names for it
    "a journal line": ("fault-journal/injections.jsonl", JOURNAL_LINE, "the fault journal"),
    "a hold record": ("fault-journal/shared-holds.jsonl", b'{"event": "held"}\n', "the fault journal"),
    "a link epoch": ("fault-journal/link-epoch.json", b'{"epoch": 3}\n', "the fault journal"),
    "a file below a subdirectory": ("fault-journal/old/one.jsonl", b'{"event": "earlier"}\n', "the fault journal"),
    "the relay's late steps": (
        "fault-relay/late-steps.jsonl",
        b'{"verb": "stop", "late": []}\n',
        "the relay's journals",
    ),
    "a relay request": (f"fault-relay/requests/{REQUEST_ID}.json", RELAY_LINE, "the relay's journals"),
    "a request the relay took": (f"fault-relay/taken/{TAKEN_ID}.json", RELAY_LINE, "the relay's journals"),
    "a relay answer": (f"fault-relay/answers/{REQUEST_ID}.json", b'{"status": "ok"}\n', "the relay's journals"),
    "the shard claim": ("shard-claim.json", b'{"held_by": "the pre-step guard"}\n', "a shard claim"),
    "the pilot's wiring": ("day0-wiring.toml", b'[wiring]\nzone = "zone"\n', "the day-0 wiring"),
}


@pytest.mark.parametrize("case", list(PRESENT), ids=list(PRESENT))
def test_each_kind_of_evidence_alone_refuses_down_without_a_marker_and_is_named(hpc, tmp_path, site, case):
    """O63: whichever of the run's files is there, non-empty, in a root `up` made, down refuses and names its kind."""
    name, content, kind = PRESENT[case]
    rig = Rig(hpc, tmp_path, site)
    leave_what_up_leaves(rig.root)
    _write(rig.root, name, content)
    assert hpc.evidence.present_evidence(rig.root) == (kind,)
    with pytest.raises(RuntimeError, match=rf"holds evidence.*\({re.escape(kind)}\).*not collected"):
        rig.driver.down()
    assert rig.fake.cancelled == []
    assert rig.root.exists()


@pytest.mark.parametrize(
    "name",
    ["fault-journal/federation.lock", "fault-journal/journal.lock", "fault-journal/aspen.lock", "shard-claim.lock"],
)
def test_a_lock_file_with_nothing_in_it_is_no_evidence(hpc, tmp_path, site, name):
    """O63: the injector's and the claim's lock files are empty: a process that took a lock and wrote no line left no
    evidence, so down is not refused for them."""
    rig = Rig(hpc, tmp_path, site)
    leave_what_up_leaves(rig.root)
    _write(rig.root, name, b"")
    assert hpc.evidence.present_evidence(rig.root) == ()
    assert rig.driver.down().startswith("down: the held job 4242 is gone, and so is the root")


def test_what_up_writes_and_a_link_check_the_federation_rewrites_are_no_evidence_however_they_change(
    hpc, tmp_path, site
):
    """O63: the state, the link check, the platform file, the facts, the render identity and the data manifests are the
    federation's own or `up`'s: a root of them alone has no evidence present, whatever they hold."""
    rig = Rig(hpc, tmp_path, site)
    leave_what_up_leaves(rig.root)
    _write(rig.root, "link-check.json", b'{"format": "expcore-link-check/1", "ok": false}\n')
    _write(rig.root, "facts.json", EVIDENCE["facts.json"])
    _write(rig.root, "data-manifests/beta-space.json", EVIDENCE["data-manifests/alpha-space.json"])
    assert hpc.evidence.present_evidence(rig.root) == ()


@pytest.mark.parametrize("what", ["a link", "a directory nested too deep", "a fifo"])
def test_a_thing_in_the_evidence_trees_that_cannot_be_read_as_a_file_counts_as_evidence(hpc, tmp_path, site, what):
    """O63: a link, a fifo, or a tree nested past the bound, where the fault journal is, is no proof that nothing ran
    (and no collect could archive it): it counts, so down refuses and the operator decides with `--discard-evidence`."""
    rig = Rig(hpc, tmp_path, site)
    leave_what_up_leaves(rig.root)
    journal = rig.root / "fault-journal"
    if what == "a link":
        (journal / "x.jsonl").symlink_to(rig.root / "facts.json")
    elif what == "a fifo":
        os.mkfifo(journal / "x.jsonl")
    else:
        deep = journal.joinpath(*["a"] * 7)
        deep.mkdir(parents=True)
    assert hpc.evidence.present_evidence(rig.root) == ("the fault journal",)
    with pytest.raises(RuntimeError, match=r"holds evidence.*the fault journal"):
        rig.driver.down()
    assert rig.fake.cancelled == []


def test_the_relays_directory_being_a_link_counts_as_evidence(hpc, tmp_path, site):
    """O63: `fault-relay/answers` a link: nothing can be said of what it holds, so it counts."""
    rig = Rig(hpc, tmp_path, site)
    leave_what_up_leaves(rig.root)
    (rig.root / "fault-relay" / "answers").rmdir()
    (rig.root / "fault-relay" / "answers").symlink_to(rig.root / "secrets")
    assert hpc.evidence.present_evidence(rig.root) == ("the relay's journals",)


def test_uncollected_names_no_shard_for_a_day0_only_root(hpc, tmp_path, site):
    """O63: `uncollected` is (the shards with records, whether the marker is stale): none for a root with no step
    record, and a root that is gone or holds no evidence is None, as before."""
    rig = _day0_rig(hpc, tmp_path, site)
    assert hpc.evidence.uncollected(rig.root) == ([], False)
    assert hpc.evidence.uncollected(tmp_path / "gone") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert hpc.evidence.uncollected(plain) is None
    record_step(rig.root, "shard-1")
    assert hpc.evidence.uncollected(rig.root) == (["shard-1"], False)


# ------------------------------------------------------------------------------------------------ the marker
def test_down_proceeds_on_a_day0_only_root_once_collect_roots_only_has_marked_it(hpc, tmp_path, site):
    """O63: `collect --roots-only` marks a root with no step record (it works from the plan, never from the records):
    the marker names each archived file's hash, and down goes as before, with nothing to discard."""
    rig = _day0_rig(hpc, tmp_path, site)
    rig.collected()
    printed = rig.driver.down()
    assert printed.startswith("down: the held job 4242 is gone, and so is the root")
    assert "discard" not in printed
    assert not rig.root.exists()


def test_a_line_appended_to_the_journal_after_the_collect_of_a_day0_only_root_refuses_down(hpc, tmp_path, site):
    """O63: the marker is compared file by file even with no step record: a journal that grew since, a relay answer that
    did not exist at the collect, and a changed wiring file make it stale, and the message names the files; collected
    again, down proceeds."""
    rig = _day0_rig(hpc, tmp_path, site)
    rig.collected()
    journal = rig.root / "fault-journal" / "injections.jsonl"
    journal.write_bytes(journal.read_bytes() + b'{"event": "cleared"}\n')
    with pytest.raises(RuntimeError, match=r"the evidence \(the fault journal.*changed after it was collected") as late:
        rig.driver.down()
    assert "fault-journal/injections.jsonl" in str(late.value)
    assert rig.fake.cancelled == []
    rig.collected()
    _write(rig.root, f"fault-relay/answers/{TAKEN_ID}.json", b'{"status": "timed_out"}\n')
    with pytest.raises(RuntimeError, match="changed after it was collected") as another:
        rig.driver.down()
    assert f"fault-relay/answers/{TAKEN_ID}.json" in str(another.value)
    rig.collected()
    assert rig.driver.down().startswith("down: the held job 4242 is gone")


def test_a_link_check_rewritten_after_the_collect_of_a_day0_only_root_is_named_not_refused(hpc, tmp_path, site):
    """O63: the federation's own rewritten files keep their tolerance with no step record: down goes on, names the file
    and records it, with no shard to name."""
    rig = _day0_rig(hpc, tmp_path, site)
    rig.collected()
    _write(rig.root, "link-check.json", b'{"format": "expcore-link-check/1", "ok": true, "later": "again"}\n')
    said = []
    rig.driver.say = said.append
    (entry,) = _gated(rig)["evidence_rewritten_after_collect"]
    assert entry["files"] == ["link-check.json"]
    assert entry["shards"] == []
    assert any("link-check.json changed after the evidence (the fault journal" in line for line in said)


def test_the_command_refuses_a_day0_only_root_and_takes_the_flag(hpc, tmp_path, site, capsys):
    """O63: `hpcfed.py down` exits 1 with the refusal for a Day-0-only root and 0 with `--discard-evidence`."""
    rig = _day0_rig(hpc, tmp_path, site)
    words = ["--layout", str(LAYOUT), "--root", str(rig.root)]
    assert hpc.cli.main([*words, "down"], {}, runner=rig.fake) == 1
    assert "holds evidence" in capsys.readouterr().err
    assert rig.root.exists()
    assert (
        hpc.cli.main([*words, "down", "--discard-evidence", "--discard-reason", "abandoned"], {}, runner=rig.fake) == 0
    )
    assert "reason given: abandoned" in capsys.readouterr().out
    assert not rig.root.exists()


def test_the_state_loader_takes_a_discard_record_with_and_without_the_kinds_of_evidence(hpc, tmp_path, site):
    """O63: `evidence` is an added key of an `evidence_discarded` entry: an entry an earlier down wrote (without it) is
    still of the form, one with it must be a list of strings, and anything else is refused."""
    rig = Rig(hpc, tmp_path, site)
    base = {"at": "2026-11-17T00:00:00Z", "shards": [], "changed_since_collect": False, "reason": None}
    for entry in (base, {**base, "evidence": ["the fault journal"]}):
        rig.driver.store.update(lambda state, entry=entry: {**state, "evidence_discarded": [entry]})
        assert rig.driver.store.read()["evidence_discarded"] == [entry]
    for bad in ({**base, "evidence": "the fault journal"}, {**base, "evidence": [1]}, {**base, "kinds": []}):
        rig.driver.store.update(lambda state, bad=bad: {**state, "evidence_discarded": [bad]})
        with pytest.raises(hpc.state.StateError, match="evidence_discarded"):
            rig.driver.store.read()


# ------------------------------------------------------------------------------------------------ the two sets
def test_every_file_of_the_allow_list_is_one_up_writes_or_one_a_run_leaves(hpc):
    """O63: the allow-list is split in two, disjoint, and nothing is in neither: a file a later change adds to the
    allow-list must be put in one of them, or this fails (and with it the question whether a plain up-then-down is
    refused for it)."""
    found = hpc.evidence
    assert set(found.UP_FILES).isdisjoint(found.RUN_FILES)
    assert set(found.UP_FILES) | set(found.RUN_FILES) == set(found.EVIDENCE_FILES)
    trees = {tree for tree, _ in found.EVIDENCE_TREES} | set(found.WHOLE_TREES)
    assert set(found.UP_TREES).isdisjoint(found.RUN_TREES)
    assert set(found.UP_TREES) | set(found.RUN_TREES) == trees


def test_every_file_a_collect_archives_is_one_of_the_two_and_every_run_file_is_counted(hpc, tmp_path):
    """O63: from a root holding every kind of evidence file: what `archive_root` takes is each of `up`'s or a run's
    (`kind_of`), every non-empty run file alone makes the root count, and every one of `up`'s alone does not: the set
    that gates `down` is a subset of what a collect can archive, so a marker can vouch for each of its members, and
    none of it is what `up` writes."""
    found = hpc.evidence
    full = make_root(tmp_path / "full")
    archived = found.archive_root(full, "x").names
    assert archived
    assert {found.kind_of(name) for name in archived} == {"up", "run"}
    held = {"hpc-federation.json": EVIDENCE["hpc-federation.json"], "facts.json": EVIDENCE["facts.json"]}
    for number, name in enumerate(archived):
        alone = make_root(tmp_path / f"alone-{number}", evidence={**held, name: EVIDENCE[name]}, extras={})
        counted = found.present_evidence(alone)
        if found.kind_of(name) == "up" or not EVIDENCE[name]:
            assert counted == (), name
        else:
            assert len(counted) == 1, name


def test_a_root_of_every_run_file_names_every_kind_in_a_fixed_order(hpc, tmp_path):
    """O63: the kinds are named in one order, whatever the order the files were made in."""
    full = make_root(tmp_path / "full")
    assert hpc.evidence.present_evidence(full) == (
        "step records",
        "the fault journal",
        "the relay's journals",
        "a shard claim",
        "the day-0 wiring",
    )


UP_PATH = (
    "deploy/hpc-federation/hpcdriver.py",
    "deploy/hpc-federation/hpcboot.py",
    "deploy/hpc-federation/hpcdata.py",
    "deploy/hpc-federation/hpcimport.py",
    "deploy/hpc-federation/hpcidentity.py",
    "deploy/hpc-federation/hpcnames.py",
    "deploy/hpc-federation/hpcplacement.py",
    "deploy/hpc-federation/hpcpins.py",
    "deploy/hpc-federation/hpcstorage.py",
    "deploy/compose-federation/fedboot.py",
    "deploy/compose-federation/fedcontent.py",
    "deploy/compose-federation/fedfacts.py",
    "deploy/compose-federation/fedimport.py",
    "deploy/compose-federation/fedlayout.py",
    "deploy/compose-federation/fedlinks.py",
)


def test_nothing_on_the_up_path_takes_an_admission_or_writes_the_fault_journal():
    """O63: `up` writes no line of the fault journal, so a root after `up` alone has none to count. Every module on its
    path (the driver, the bootstrap, the data and import checks, the facts, the link check) imports nothing that takes
    the federation's admission or writes the journal's files (`expcore_onedata`'s `admission*`, `fault_*` but the
    settings, `faults`, `shared_holds`, `tool_admission` and the reset locks), so a later change that makes `up` take
    one fails here, and the baseline must be recorded at its end instead. The link check goes through `linkrecord`,
    which writes `link-check.json` and its lock in the root, never in the journal (the test above)."""
    package = ROOT_OF_REPO / "src" / "expcore_onedata"
    forbidden = {p.stem for p in package.glob("*.py") if p.stem.startswith(("fault", "admission"))}
    forbidden |= {"tool_admission", "shared_holds", "resetlock", "resetgrant", "resetheld"}
    forbidden -= {"fault_settings"}
    assert {"fault_gate", "admission", "faults", "fault_journal", "shared_holds"} <= forbidden
    found = {}
    for relative in UP_PATH:
        tree = ast.parse((ROOT_OF_REPO / relative).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "expcore_onedata":
                parts = node.module.split(".")
                names |= {parts[1]} if len(parts) > 1 else {alias.name for alias in node.names}
            elif isinstance(node, ast.Import):
                names |= {a.name.split(".")[1] for a in node.names if a.name.startswith("expcore_onedata.")}
        if names & forbidden:
            found[relative] = sorted(names & forbidden)
    assert found == {}
