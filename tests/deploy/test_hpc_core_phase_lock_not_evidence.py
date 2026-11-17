"""O63, C06: the root's core-phase lock is no evidence of a run, so `down`'s gate (which counts the evidence present in
a root) never refuses, names or archives it. Two changes meet here: a core phase, and each rerun of it by
`hpcfed.py core`, holds `<root>/core-phase.lock` (`StateStore.core_phase`), a file that stays in the root after the
phase ends and holds nothing; and `down` refuses a root that holds evidence a run left
(`hpcevidence.present_evidence`) and no current marker. Against the fake Slurm of hpc_fed_support; nothing reaches a
cluster."""

import pytest
from down_rig_support import Rig, make_site
from hpc_fed_support import hpc_on_path
from root_evidence_support import EVIDENCE, leave_what_up_leaves

LOCK = "core-phase.lock"
JOURNAL_LINE = b'{"event": "injecting", "target": "aspen"}\n'


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcevidence

        modules.evidence = hpcevidence
        yield modules


@pytest.fixture
def site(tmp_path):
    return make_site(tmp_path)


def _rerun(rig):
    """A core phase that ends: the lock taken, as `hpcfed.py core` takes it, and released."""
    with rig.driver.store.core_phase("this test's core phase"):
        assert (rig.root / LOCK).is_file()


def _day0_root(rig):
    """What a Day-0-only run leaves on top of `up`'s files: lines in the injector's journal, the kind of evidence."""
    leave_what_up_leaves(rig.root)
    journal = rig.root / "fault-journal"
    journal.mkdir(exist_ok=True)
    (journal / "injections.jsonl").write_bytes(JOURNAL_LINE)
    (journal / "shared-holds.jsonl").write_bytes(EVIDENCE["fault-journal/shared-holds.jsonl"])


@pytest.mark.parametrize("content", [b"", b"a line in the lock file\n"], ids=["empty", "with a line"])
def test_o63_the_core_phase_lock_is_no_evidence_held_or_left_and_whatever_it_holds(hpc, tmp_path, site, content):
    """O63: with the lock held by a core phase, and with the file left behind holding nothing or a line, a root after
    `up` alone has no evidence present and nothing uncollected: the file is in none of the sets of `hpcevidence`."""
    rig = Rig(hpc, tmp_path, site)
    leave_what_up_leaves(rig.root)
    with rig.driver.store.core_phase("this test's core phase"):
        assert hpc.evidence.present_evidence(rig.root) == ()
        assert hpc.evidence.uncollected(rig.root) is None
    (rig.root / LOCK).write_bytes(content)
    assert hpc.evidence.present_evidence(rig.root) == ()
    assert hpc.evidence.uncollected(rig.root) is None


def test_o63_the_core_phase_lock_is_in_neither_set_and_never_archived(hpc, tmp_path, site):
    """O63: the lock is no file `up` writes and none a run leaves (`kind_of` knows neither), and a collect of a root
    that holds it archives every file of the allow-list but it."""
    rig = Rig(hpc, tmp_path, site)
    _day0_root(rig)
    _rerun(rig)
    assert hpc.evidence.kind_of(LOCK) is None
    assert LOCK not in hpc.evidence.EVIDENCE_FILES
    archived = hpc.evidence.archive_root(rig.root, "x")
    assert archived.names
    assert LOCK not in archived.names
    assert all(LOCK not in name for name in archived.hashes)


def test_o63_a_root_after_a_core_rerun_alone_goes_down_unrefused(hpc, tmp_path, site):
    """O63: a federation whose core phase was rerun by hand and which ran nothing goes down as a root after `up` alone
    does: not refused, and the output says nothing of evidence."""
    rig = Rig(hpc, tmp_path, site)
    leave_what_up_leaves(rig.root)
    _rerun(rig)
    assert hpc.evidence.present_evidence(rig.root) == ()
    printed = rig.driver.down()
    assert printed.startswith("down: the held job 4242 is gone, and so is the root")
    assert "evidence" not in printed
    assert not rig.root.exists()


def test_o63_a_day0_root_with_the_core_phase_lock_is_refused_for_its_journal_alone(hpc, tmp_path, site):
    """O63: the refusal names the fault journal and nothing of the lock, whatever order the evidence and the lock came
    in; nothing is cancelled or stopped, and the root stays."""
    rig = Rig(hpc, tmp_path, site)
    _day0_root(rig)
    _rerun(rig)
    assert hpc.evidence.present_evidence(rig.root) == ("the fault journal",)
    with pytest.raises(RuntimeError) as refused:
        rig.driver.down()
    message = str(refused.value)
    assert "the fault journal" in message
    assert "core-phase" not in message
    assert "lock" not in message
    assert rig.fake.cancelled == []
    assert rig.root.exists()


def test_o63_a_core_rerun_after_the_collect_does_not_make_the_collected_evidence_stale(hpc, tmp_path, site):
    """O63: a root collected and marked, whose core phase is then rerun (the lock file is made or taken again), is still
    collected: no strict file changed, so down goes on, naming no change since the collect."""
    rig = Rig(hpc, tmp_path, site)
    _day0_root(rig)
    rig.collected()
    assert hpc.evidence.uncollected(rig.root) is None
    _rerun(rig)
    assert hpc.evidence.uncollected(rig.root) is None
    printed = rig.driver.down()
    assert printed.startswith("down: the held job 4242 is gone, and so is the root")
    assert "discard" not in printed
    assert not rig.root.exists()
