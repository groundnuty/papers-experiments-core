"""The mount-guard instrument for a run: a guard for a mount run, a statement of no mount for a run without one, decided
by the sandbox's own data path and never by a missing watcher (O60)."""

from types import SimpleNamespace

import pytest
from mountguard_logs import NONCE, log

from expcore.mountguard import NAMESPACE, MountGuard, NoMount, judge, mount_guard_for


class Box:
    """A sandbox as `mount_guard_for` reads it: its data path, its watcher's nonce and its log."""

    def __init__(self, kind, nonce=None, data=None):
        self.data_path_kind, self.mount_guard_nonce, self._data = kind, nonce, data

    def mount_watch_log(self):
        return self._data


def snapshot_of(box):
    instrument = mount_guard_for(box)
    instrument.start({})
    return instrument.finish()


def test_a_mount_run_gets_the_guard_over_its_watcher_log():
    box = Box("mount", NONCE, log())
    assert isinstance(mount_guard_for(box), MountGuard)
    assert (
        judge(SimpleNamespace(run_key="k", attempt=1, instruments={NAMESPACE: snapshot_of(box)}), "c").verdict
        == "clean"
    )


@pytest.mark.parametrize("kind", ["rest", "none"])
def test_a_run_whose_data_path_has_no_mount_gets_the_statement_that_the_guard_does_not_apply(kind):
    """O69: the sandbox's data path decides; the statement names it."""
    instrument = mount_guard_for(Box(kind))
    assert isinstance(instrument, NoMount)
    assert instrument.namespace == NAMESPACE
    assert snapshot_of(Box(kind)) == {"version": 1, "mount": False, "data_path": kind}


def test_a_mount_run_without_a_watcher_nonce_is_refused_never_recorded_as_not_applicable():
    """O69: the helper's lack of a watcher says nothing about the run's data path: a mount run it stays."""
    with pytest.raises(ValueError, match="nonce"):
        mount_guard_for(Box("mount", None))


def test_a_mount_run_whose_watcher_log_is_missing_is_unknown_never_not_applicable():
    """O69: a mount run with no log is unknown and left out, whatever else is missing."""
    box = Box("mount", NONCE, None)
    verdict = judge(SimpleNamespace(run_key="k", attempt=1, instruments={NAMESPACE: snapshot_of(box)}), "c")
    assert verdict.verdict == "unknown"
    assert verdict.not_applicable is None


@pytest.mark.parametrize("kind", ["Mount", "", None, "api", 3])
def test_a_data_path_this_code_does_not_know_is_refused(kind):
    with pytest.raises(ValueError, match="data path"):
        mount_guard_for(Box(kind, NONCE))


@pytest.mark.parametrize("kind", ["mount", "api", "", None])
def test_the_statement_cannot_be_made_for_a_mount_or_an_unknown_data_path(kind):
    with pytest.raises(ValueError, match="data path"):
        NoMount(kind)
