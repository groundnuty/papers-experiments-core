"""Reservations: what a process beside the dispatcher holds back, and a lock that cannot be read reads as held (C19)."""

import signal
import subprocess
import sys
import textwrap

import pytest

from expcore.campaign.reservation import (
    ReservationError,
    Reservations,
    ReservationSpec,
    hold,
    look,
    probe,
    problems,
)

SPECS = {
    "calibration": ReservationSpec(count=2, keys=("k1", "k2")),
    "validation": ReservationSpec(count=1, keys=("k3",)),
}


def test_c19_a_reservation_is_held_while_its_process_holds_the_lock(tmp_path):
    """C19: held while the process is up, free when it releases; no file means the process has not started."""
    assert probe(tmp_path, "calibration") == (False, None)
    reservation = hold("calibration", tmp_path)
    try:
        assert probe(tmp_path, "calibration") == (True, None)
    finally:
        reservation.release()
    assert probe(tmp_path, "calibration") == (False, None)


def test_c19_a_reservation_cannot_be_taken_twice(tmp_path):
    """C19: a second process of one name would use twice what is reserved, so it is refused."""
    first = hold("calibration", tmp_path)
    try:
        with pytest.raises(ReservationError, match="twice what the campaign counts"):
            hold("calibration", tmp_path, retry_s=0.0)
    finally:
        first.release()


def test_c19_a_reservation_whose_process_was_killed_is_dropped_by_the_kernel(tmp_path):
    """C19: nothing is cleaned up and nothing stays reserved after a kill."""
    code = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from expcore.campaign.reservation import hold
        hold("calibration", Path(sys.argv[1]))
        print("held", flush=True)
        sys.stdin.read()
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "held"
        assert probe(tmp_path, "calibration") == (True, None)
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
    finally:
        child.kill()
        child.wait()
    assert probe(tmp_path, "calibration") == (False, None)


def test_c19_an_unreadable_reservation_reads_as_held(tmp_path):
    """C19: a missing directory, no directory at all, or a lock that cannot be opened reads as held, with the reason."""
    held, why = look(tmp_path / "absent", ["calibration"])
    assert held == {"calibration"}
    assert "FileNotFoundError" in why["calibration"]
    held, why = look(None, ["calibration", "validation"])
    assert held == {"calibration", "validation"}
    assert set(why) == {"calibration", "validation"}
    not_a_directory = tmp_path / "file"
    not_a_directory.write_text("", encoding="utf-8")
    held, why = look(not_a_directory, ["calibration"])
    assert held == {"calibration"}
    assert "NotADirectoryError" in why["calibration"]


def test_c19_held_reservations_take_their_keys_and_sandboxes_out_of_the_campaign(tmp_path):
    """C19: while a process is up its keys leave the lease pool and its sandboxes count in the caps."""
    reservations = Reservations(tmp_path, SPECS)
    assert reservations.excluded_keys() == set()
    assert reservations.reserved_count() == 0
    reservation = hold("calibration", tmp_path)
    try:
        assert reservations.excluded_keys() == {"k1", "k2"}
        assert reservations.reserved_count() == 2
        resolve = reservations.without_reserved(lambda group: ["k1", "k2", "k3"])
        assert resolve("g") == ["k3"]
    finally:
        reservation.release()
    assert reservations.without_reserved(lambda group: ["k1", "k3"])("g") == ["k1", "k3"]


def test_c19_with_no_directory_every_reservation_is_taken_as_held():
    """C19: a campaign that cannot look keeps every reserved key out of its pool, never lets them in."""
    reservations = Reservations(None, SPECS)
    assert reservations.excluded_keys() == {"k1", "k2", "k3"}
    assert reservations.reserved_count() == 3


def test_c19_misdeclared_reservations_are_all_reported_together():
    """C19: a bad count, a repeated key, an unknown key and a key in two reservations are named in one message."""
    bad = {
        "a": ReservationSpec(count=0, keys=("k1", "k1", "nope")),
        "b": ReservationSpec(count=1, keys=("k2",)),
        "c": ReservationSpec(count=1, keys=("k2",)),
    }
    found = problems(bad, known_keys={"k1", "k2"})
    assert len(found) == 4
    assert any("count" in x for x in found)
    assert any("repeats" in x for x in found)
    assert any("'nope'" in x for x in found)
    assert any("two reservations" in x for x in found)
    with pytest.raises(ReservationError, match="count"):
        Reservations(None, bad)
    assert problems(SPECS, known_keys={"k1", "k2", "k3"}) == []
