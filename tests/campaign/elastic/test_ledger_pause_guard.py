"""C55: `pause` is written only to a controller that says it honours operator pauses. A core without the change would
open the ledger and keep leasing the model, so a controller holding the ledger declares itself (a second `flock`
beside the dispatcher lock, held until it closes, dropped by the kernel with the process), and `pause` reads the two
locks: an older live controller is refused with nothing written, a current one is accepted, none running is written
with a warning, and a lock that cannot be read is written with its own."""

import sqlite3
import subprocess
import sys
import warnings
from pathlib import Path

import pytest
from elastic_support import FakeClock, ledger_path, make, plan

from expcore.campaign.elastic import Ledger, LedgerReader
from expcore.campaign.elastic.__main__ import main
from expcore.campaign.elastic.awareness import AWARE, NONE, OLDER, UNKNOWN, controller_of, mark_path
from expcore.campaign.lock import DispatchLock, LockUnavailable

WHY = "provider 429 burst"
PAUSE = ["--model", "m1", "--reason", WHY, "--actor", "op-1"]


def _closed(tmp_path):
    """A ledger no controller holds: made, then closed; its path and the clock."""
    ledger, clock = make(tmp_path, runs=plan(models=("m1", "m2"), tasks=2, conditions=("api-none",)))
    ledger.close()
    return str(ledger_path(tmp_path)), clock


def _events(path):
    reader = LedgerReader(Path(path))
    try:
        return reader.events()
    finally:
        reader.close()


def test_c55_a_controller_of_this_core_declares_itself_while_it_holds_the_ledger_and_not_after(tmp_path):
    """C55: a closed ledger has no controller; an open one reads as aware (its dispatcher lock and its declaration);
    the declaration goes with `close`, and a holder of the dispatcher lock alone (what an older core does) reads as
    older."""
    _, clock = _closed(tmp_path)
    root = ledger_path(tmp_path).parent
    file = ledger_path(tmp_path)
    assert controller_of(file) == NONE
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    assert controller_of(file) == AWARE
    ledger.close()
    assert controller_of(file) == NONE
    with DispatchLock(root):
        assert controller_of(file) == OLDER


def test_c55_the_declaration_cannot_outlive_a_controller_the_kernel_drops_it_with_the_process(tmp_path):
    """C55: a controller process takes the ledger and is killed outright: the dispatcher lock and the declaration go
    with it, so a stale declaration can never make an older controller read as aware."""
    _closed(tmp_path)
    root = ledger_path(tmp_path).parent
    file = ledger_path(tmp_path)
    code = (
        "import sys, time; from pathlib import Path; from expcore.campaign.elastic import Ledger; "
        "Ledger.open(Path(sys.argv[1]), clock=time.time); print('open', flush=True); time.sleep(600)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(ledger_path(tmp_path))], stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "open"
        assert controller_of(file) == AWARE
    finally:
        child.kill()
        child.wait()
    assert mark_path(file).exists()  # the file stays; only the hold is gone
    assert controller_of(file) == NONE
    with DispatchLock(root):  # an older controller now: the file left behind must not make it look aware
        assert controller_of(file) == OLDER


def test_c55_pause_is_refused_and_writes_nothing_while_the_live_controller_is_older(tmp_path, capsys):
    """C55: an older controller holds the dispatcher lock and never declares itself: `pause` exits 1 naming the cause
    and the restart, the ledger's events are as they were, and `status` has no pause."""
    path, _ = _closed(tmp_path)
    before = _events(path)
    with DispatchLock(ledger_path(tmp_path).parent):
        assert main(["pause", path, *PAUSE]) == 1
    err = capsys.readouterr().err
    assert "does not say it honours operator pauses" in err
    assert "Restart it on this commit or later" in err
    assert _events(path) == before


def test_c55_pause_is_written_without_a_warning_to_a_live_controller_that_declares_itself(tmp_path, capsys):
    """C55: the current controller holds the ledger: `pause` writes, says the controller honours it from its next
    lease, warns of nothing, and the controller's next lease obeys."""
    path, clock = _closed(tmp_path)
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    try:
        assert main(["pause", path, *PAUSE]) == 0
        captured = capsys.readouterr()
        assert "honours it from its next lease" in captured.out
        assert captured.err == ""
        assert list(ledger.operator_pauses()) == ["m1"]
    finally:
        ledger.close()


def test_c55_pause_with_no_controller_is_written_with_a_warning_that_the_next_one_must_honour_it(tmp_path, capsys):
    """C55: nobody holds the ledger: `pause` writes, and warns that whichever controller starts next must run this
    commit or later; a controller of this core that then starts leases none of the paused model."""
    path, _ = _closed(tmp_path)
    assert main(["pause", path, *PAUSE]) == 0
    captured = capsys.readouterr()
    assert "paused model m1" in captured.out
    assert "no controller holds this ledger" in captured.err
    assert "must run this commit or later" in captured.err
    ledger = Ledger.open(ledger_path(tmp_path), clock=FakeClock())
    try:
        assert list(ledger.operator_pauses()) == ["m1"]
    finally:
        ledger.close()


def test_c55_pause_with_locks_that_cannot_be_read_is_written_with_its_own_warning(tmp_path, capsys, monkeypatch):
    """C55: a file system that refuses `flock` cannot say whether a controller is there: `pause` writes and warns
    that an older controller would ignore it."""
    path, _ = _closed(tmp_path)

    class Refusing:
        def __init__(self, root):
            pass

        def __enter__(self):
            raise LockUnavailable("no flock here")

        def __exit__(self, *exc):
            return None

    monkeypatch.setattr("expcore.campaign.elastic.awareness.ReaderLock", Refusing)
    assert controller_of(ledger_path(tmp_path)) == UNKNOWN
    assert main(["pause", path, *PAUSE]) == 0
    assert "could not tell whether a controller holds this ledger" in capsys.readouterr().err


def test_c55_status_warns_when_an_older_controller_holds_a_ledger_that_has_a_pause_in_force(tmp_path, capsys):
    """C55: the pause was written while nobody held the ledger; an older controller then starts: `status` says it
    ignores the pause. A ledger with no pause in force gets no such line, and neither does one a current controller
    holds."""
    path, clock = _closed(tmp_path)
    root = ledger_path(tmp_path).parent
    with DispatchLock(root):
        assert main(["status", path]) == 0
        assert "operator pauses" not in capsys.readouterr().out  # nothing paused: nothing to warn of
    assert main(["pause", path, *PAUSE]) == 0
    capsys.readouterr()
    with DispatchLock(root):
        assert main(["status", path]) == 0
        assert "does not say it honours operator pauses" in capsys.readouterr().out
    ledger = Ledger.open(ledger_path(tmp_path), clock=clock)
    try:
        assert main(["status", path]) == 0
        assert "operator pauses" not in capsys.readouterr().out
    finally:
        ledger.close()


def test_c55_declaring_the_last_undeclared_value_says_when_an_operator_pause_still_holds_the_model(tmp_path, capsys):
    """C55: m1 is paused by an operator, and declaring a fingerprint prints that the operator's pause stays (declaring
    lifts only C48's)."""
    ledger, _ = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)), declared={"m1": ["fp-a"]})
    path = str(ledger_path(tmp_path))
    ledger.pause_model("m1", WHY, "op-1")
    declare = ["declare", path, "--model", "m1", "--fingerprint", "fp-new", "--reason", "r", "--actor", "op-1"]
    assert main(declare) == 0
    assert f"model m1 stays paused by an operator: {WHY}" in capsys.readouterr().out
    ledger.close()


def test_c55_a_pause_moves_only_sqlites_own_counter_beyond_the_new_event_row(tmp_path):
    """C55: appending an event moves SQLite's autoincrement counter of the events table, as every event of the ledger
    does, and nothing else of `sqlite_sequence`."""
    ledger, _ = make(tmp_path, runs=plan(models=("m1", "m2"), tasks=2, conditions=("api-none",)))
    before = dict(ledger.db.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
    entry = ledger.pause_model("m1", WHY, "op-1")
    after = dict(ledger.db.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
    assert after.pop("events") == entry.event_seq == before.pop("events", 0) + 1
    assert after == before
    ledger.close()


def test_c55_a_controller_that_fails_to_open_or_to_close_never_strands_the_dispatcher_lock(tmp_path, monkeypatch):
    """C55: a declaration that cannot be made (a warning turned into an error), a clock that raises while the ledger
    opens, and a declaration whose release raises each leave the dispatcher lock free for the next controller."""
    _closed(tmp_path)
    file = ledger_path(tmp_path)
    root = file.parent

    def lock_is_free():
        with DispatchLock(root):
            return True

    mark_path(file).unlink()
    mark_path(file).mkdir()  # a directory where the declaration file goes: it cannot be made
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(UserWarning, match="operator pauses are refused"):
            Ledger.open(file, clock=FakeClock())
    assert lock_is_free()
    mark_path(file).rmdir()

    def broken_clock():
        raise RuntimeError("no clock")

    with pytest.raises(RuntimeError, match="no clock"):
        Ledger.open(file, clock=broken_clock)
    assert lock_is_free()
    assert controller_of(file) == NONE

    ledger = Ledger.open(file, clock=FakeClock())

    def refuse(self):
        raise OSError("cannot unlock")

    monkeypatch.setattr("expcore.campaign.elastic.awareness.PauseAwareMark.release", refuse)
    with pytest.raises(OSError, match="cannot unlock"):
        ledger.close()
    monkeypatch.undo()
    assert lock_is_free()


def test_c55_pause_of_a_file_that_is_not_a_ledger_is_a_usage_error_even_beside_an_older_controller(tmp_path, capsys):
    """C55: the file is checked before the controller: a name that holds no ledger exits 2, as every command does,
    where an older controller in the directory would have made it a refusal (1)."""
    path, _ = _closed(tmp_path)
    missing = str(ledger_path(tmp_path).with_name("other.sqlite"))
    with DispatchLock(ledger_path(tmp_path).parent):
        assert main(["pause", missing, *PAUSE]) == 2
    assert "no ledger there" in capsys.readouterr().err
    assert path


def test_c55_a_declaration_stays_a_success_when_the_look_for_an_operators_pause_fails(tmp_path, capsys, monkeypatch):
    """C55: `declare` has committed when it looks for an operator's pause; a read that fails then is reported as a
    note and the command still exits 0, since the declaration is durable."""
    ledger, _ = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)), declared={"m1": ["fp-a"]})
    ledger.close()

    class Failing:
        def __init__(self, path):
            raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr("expcore.campaign.elastic.__main__.LedgerReader", Failing)
    path = str(ledger_path(tmp_path))
    declare = ["declare", path, "--model", "m1", "--fingerprint", "fp-new", "--reason", "r", "--actor", "op-1"]
    assert main(declare) == 0
    captured = capsys.readouterr()
    assert "declared model m1: fp-new" in captured.out
    assert "the declaration is recorded" in captured.err
