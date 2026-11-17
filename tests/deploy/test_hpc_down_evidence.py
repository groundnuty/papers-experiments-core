"""O63: `hpcfed.py down` does not remove a federation's root while evidence a campaign left in it is uncollected. A root
that has shard results recorded against it (a step ran: `shards.py step` leaves `shard-steps/<label>.json` there) and
no collected-evidence marker (`shards.py collect` writes one, naming the root archive's hash) is refused before
anything is stopped, unless `--discard-evidence` is given, which down's output records. A root where no step ever ran
goes as before. Against the fake Slurm of hpc_fed_support; nothing reaches a cluster."""

import json
import os
import re

import pytest
from down_rig_support import Rig, make_site
from hpc_fed_support import LAYOUT, hpc_on_path
from root_evidence_support import record_step


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcevidence

        modules.evidence = hpcevidence
        yield modules


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def test_down_refuses_a_root_with_a_step_record_and_no_marker_before_it_stops_anything(hpc, tmp_path, site):
    """O63: the refusal names the shard and the remedies (collect, or `--discard-evidence`); no job is cancelled, no
    service is stopped, the state is not touched, and the root stays, so a collect can still read it."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    state_before = (rig.root / "hpc-federation.json").read_bytes()
    with pytest.raises(RuntimeError, match=r"shard-1.*collect.*--discard-evidence") as refused:
        rig.driver.down()
    assert "evidence" in str(refused.value)
    assert rig.fake.cancelled == []
    assert rig.fake.calls == []  # not a squeue, a scancel or an srun
    assert (rig.root / "hpc-federation.json").read_bytes() == state_before
    assert rig.root.exists()


def test_down_names_every_shard_with_results_and_no_marker(hpc, tmp_path, site):
    """O63: two shards recorded in one root (a resume on another shard's label never happens, but a root may hold
    more than one record): both are named."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    record_step(rig.root, "shard-2")
    with pytest.raises(RuntimeError, match=r"shard-1.*shard-2"):
        rig.driver.down()


def test_down_proceeds_once_collect_has_written_the_marker(hpc, tmp_path, site):
    """O63: a marker for the step records that are there lets down go as before: the job cancelled, the root removed."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    printed = rig.driver.down()
    assert printed.startswith("down: the held job 4242 is gone, and so is the root")
    assert "discard" not in printed
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def test_down_refuses_when_a_step_started_after_the_evidence_was_collected(hpc, tmp_path, site):
    """O63: a resume after `collect` adds to the journals the archive lacks: the marker names the step records it was
    collected against, so a changed record makes it stale and down refuses, saying to collect again."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    record_step(rig.root, "shard-1", starts=2)
    with pytest.raises(RuntimeError, match=r"evidence of shard-1 changed after it was collected"):
        rig.driver.down()
    assert rig.fake.cancelled == []
    assert rig.root.exists()


def test_down_refuses_when_a_second_shard_started_after_the_evidence_was_collected(hpc, tmp_path, site):
    """O63: a record that did not exist at the collect is as stale as one that changed."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    record_step(rig.root, "shard-2")
    with pytest.raises(RuntimeError, match=r"shard-2"):
        rig.driver.down()


def test_down_refuses_when_the_evidence_changed_after_it_was_collected_though_no_step_started(hpc, tmp_path, site):
    """O63: a step that was running when `collect --roots-only` ran goes on writing the fault journal and the relay's
    files until its job is cancelled: the marker names the hash of each file archived, and a root where a journal is no
    longer what it names is refused, so nothing a running step wrote after the collect goes with the root."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    (rig.root / "fault-journal").mkdir(exist_ok=True)
    (rig.root / "fault-journal" / "injections.jsonl").write_text('{"event": "injecting"}\n')
    with pytest.raises(RuntimeError, match=r"changed after it was collected.*end the shard's step"):
        rig.driver.down()
    assert rig.fake.cancelled == []
    assert rig.root.exists()
    rig.collected()  # collected again, once the step has ended
    assert rig.driver.down().startswith("down: the held job 4242 is gone")


def test_down_refuses_a_changed_evidence_file_and_one_that_can_no_longer_be_archived(hpc, tmp_path, site):
    """O63: an edited `facts.json`, or a link where an evidence file was, is no evidence of what was collected."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    (rig.root / "facts.json").write_text('{"changed": true}')
    with pytest.raises(RuntimeError, match="changed after it was collected"):
        rig.driver.down()
    rig.collected()
    (rig.root / "link-check.json").symlink_to(rig.root / "facts.json")
    with pytest.raises(RuntimeError, match="changed after it was collected"):
        rig.driver.down()


def test_a_down_that_began_is_not_gated_again(hpc, tmp_path, site):
    """O63: a first down passed the gate and set the state's phase, which is part of the evidence; run again after a
    failure halfway, it is not refused for its own change."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    rig.driver.set(lambda state: state.update(phase="stopping"))
    assert rig.driver.down().startswith("down: the held job 4242 is gone")


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"format": "something else"}',
                                     json.dumps({"format": "expcore-hpc-evidence-collected/1"})])  # fmt: skip
def test_down_refuses_a_marker_it_cannot_read_as_collects(hpc, tmp_path, site, content):
    """O63: a marker is a file of a fixed form (format, the root archive's SHA-256, the step digest): anything else
    is no evidence that anything was collected."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    (rig.root / "evidence-collected.json").write_text(content)
    with pytest.raises(RuntimeError, match="evidence"):
        rig.driver.down()
    assert rig.fake.cancelled == []


def test_down_refuses_a_marker_that_is_a_link(hpc, tmp_path, site):
    """O63: a marker is read as a file of the root, never through a link (here to a file that would pass)."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    real = rig.root / "evidence-collected.json"
    kept = tmp_path / "kept.json"
    real.rename(kept)
    real.symlink_to(kept)
    with pytest.raises(RuntimeError, match="evidence"):
        rig.driver.down()


def test_discard_evidence_lets_down_remove_an_uncollected_root_and_the_output_records_it(hpc, tmp_path, site):
    """O63: down goes as before, and what it prints (and says while it works) names the shards whose evidence was not
    collected and is removed with the root."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    said = []
    rig.driver.say = said.append
    printed = rig.driver.down(discard_evidence=True)
    assert printed.startswith("down: --discard-evidence")
    assert "shard-1" in printed.splitlines()[0]
    assert "down: the held job 4242 is gone, and so is the root" in printed
    assert any("--discard-evidence" in line and "shard-1" in line for line in said)
    assert rig.fake.cancelled == ["4242"]
    assert not rig.root.exists()


def _discarded(rig):
    """What a `down --discard-evidence` left in the state file: down is stopped right after its gate, so the state,
    which goes with the root at the end of a whole down, is still there to read."""

    def stopped():
        raise RuntimeError("stopped for the test, after the gate")

    rig.driver.take_down = stopped
    with pytest.raises(RuntimeError, match="stopped for the test"):
        rig.driver.down(discard_evidence=True, discard_reason=rig.reason)
    return rig.driver.store.read().get("evidence_discarded")


def test_every_use_of_discard_evidence_is_recorded_in_the_state_with_its_time_and_reason(hpc, tmp_path, site):
    """O63: the shards whose evidence was not collected, when (UTC), and the reason given on the command line; a second
    use adds a second entry. The output says it was recorded, with the reason."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    said = []
    rig.driver.say = said.append
    rig.reason = "the pilot's batch was abandoned"
    (entry,) = _discarded(rig)
    assert entry["shards"] == ["shard-1"]
    assert entry["changed_since_collect"] is False
    assert entry["reason"] == "the pilot's batch was abandoned"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", entry["at"])
    assert any(
        "reason given: the pilot's batch was abandoned" in line and "recorded in the state file" in line
        for line in said
    )
    rig.reason = None
    entries = _discarded(rig)
    assert [e["reason"] for e in entries] == ["the pilot's batch was abandoned", None]


def test_a_discard_of_a_marker_made_stale_by_a_later_step_says_so_in_the_state(hpc, tmp_path, site):
    """O63: the evidence was collected, and a step started since: recorded as such, not as never collected."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    rig.collected()
    record_step(rig.root, "shard-1", starts=2)
    rig.reason = None
    (entry,) = _discarded(rig)
    assert entry["changed_since_collect"] is True


def test_nothing_is_recorded_when_there_was_nothing_to_discard(hpc, tmp_path, site):
    """O63: the flag on a root with no step record, or whose evidence is collected, records nothing."""
    rig = Rig(hpc, tmp_path, site)

    def stopped():
        raise RuntimeError("stopped for the test")

    rig.driver.take_down = stopped
    with pytest.raises(RuntimeError, match="stopped for the test"):
        rig.driver.down(discard_evidence=True, discard_reason="not needed")
    assert "evidence_discarded" not in rig.driver.store.read()


def test_the_state_loader_refuses_a_discard_record_that_is_not_of_the_form(hpc, tmp_path, site):
    """O63: the record is part of the state file's checked form."""
    rig = Rig(hpc, tmp_path, site)
    rig.driver.store.update(lambda state: {**state, "evidence_discarded": [{"at": 1}]})
    with pytest.raises(hpc.state.StateError, match="evidence_discarded"):
        rig.driver.store.read()


def test_the_discard_reason_goes_with_the_flag_and_is_bounded(hpc, tmp_path, site, capsys):
    """O63: `--discard-reason` without `--discard-evidence`, or one over 500 characters, is an error (exit 2) before
    anything is touched; with the flag it is accepted and recorded."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    words = ["--layout", str(LAYOUT), "--root", str(rig.root), "down"]
    assert hpc.cli.main([*words, "--discard-reason", "why"], {}, runner=rig.fake) == 2
    assert hpc.cli.main([*words, "--discard-evidence", "--discard-reason", "x" * 501], {}, runner=rig.fake) == 2
    assert rig.root.exists()
    capsys.readouterr()
    assert hpc.cli.main([*words, "--discard-evidence", "--discard-reason", "why"], {}, runner=rig.fake) == 0
    assert "reason given: why" in capsys.readouterr().out


def test_the_state_file_holds_no_secret_of_the_root(hpc, tmp_path, site):
    """O63: the archive of the root takes the state file, so it must hold none: no content of any file of `secrets/`
    (the zone's passphrase and CA key `up` wrote, and a planted token and password) is in it."""
    rig = Rig(hpc, tmp_path, site)
    (rig.root / "secrets").mkdir(exist_ok=True)
    (rig.root / "secrets" / "planted.token").write_text("tok-7c1e93b2a5d84f60e3a1b9c2")
    (rig.root / "secrets" / "users").mkdir(exist_ok=True)
    (rig.root / "secrets" / "users" / "planted.pass").write_text("pass-1f0e4d8a73b65c92")
    state = (rig.root / "hpc-federation.json").read_bytes()
    descriptor = os.open(rig.root, os.O_RDONLY)
    try:
        values = hpc.evidence.secret_values(descriptor)
    finally:
        os.close(descriptor)
    assert len(values) >= 3  # the planted ones, and what up wrote
    assert not [value for value in values if value in state]


def test_a_root_where_no_step_ever_ran_goes_down_as_it_always_did(hpc, tmp_path, site):
    """O63: no record, no marker, no flag: nothing changes, and the output is the old output."""
    rig = Rig(hpc, tmp_path, site)
    printed = rig.driver.down()
    assert printed.startswith("down: the held job 4242 is gone, and so is the root")
    assert "evidence" not in printed
    assert not rig.root.exists()


def test_discard_evidence_on_a_root_with_nothing_to_discard_says_nothing_of_it(hpc, tmp_path, site):
    """O63: the flag records a discard only when there was something to discard."""
    rig = Rig(hpc, tmp_path, site)
    printed = rig.driver.down(discard_evidence=True)
    assert "evidence" not in printed


def test_the_command_takes_the_flag_for_down_alone(hpc, tmp_path, site):
    """O63: `hpcfed.py down --discard-evidence` is accepted; the flag on any other command is an error, never ignored
    (a discard is down's alone), and without it a root with uncollected evidence exits 1 with the refusal."""
    rig = Rig(hpc, tmp_path, site)
    record_step(rig.root, "shard-1")
    words = ["--layout", str(LAYOUT), "--root", str(rig.root)]
    assert hpc.cli.main([*words, "describe", "--discard-evidence"], {}, runner=rig.fake) == 2
    assert hpc.cli.main([*words, "down"], {}, runner=rig.fake) == 1
    assert rig.root.exists()
    assert hpc.cli.main([*words, "down", "--discard-evidence"], {}, runner=rig.fake) == 0
    assert not rig.root.exists()
