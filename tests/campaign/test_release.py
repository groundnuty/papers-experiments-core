"""A release line counts once, and only when it names the serial of the stop it ends (C13)."""

import logging
import os
import re
import stat

import pytest
from campaign_support import NOW, open_store

from expcore.campaign.release import (
    SERIAL_KEY,
    epoch_of,
    line_for,
    next_serial,
    say_refused,
    serial_of,
    take,
    take_listed,
    told,
    written_after,
)

STOP = 1_000_000.0
SERIAL = re.compile(r"[0-9]+\.[a-z2-7]{8}")


def release_file(tmp_path, text="go\n", mtime=STOP + 10, name="release"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_c13_a_line_naming_the_serial_of_the_stop_in_force_ends_it_once_and_is_used_up(tmp_path):
    """C13: the stop's own line ends it; the line is removed, so the same file ends nothing the next time."""
    path = release_file(tmp_path, "m1 4.abcdefgh\n")
    taken, refused = take_listed(path, {"m1": "4.abcdefgh"})
    assert set(taken) == {"m1"}
    assert refused == []
    assert not path.exists()
    assert take_listed(path, {"m1": "4.abcdefgh"}) == ({}, [])


def test_c13_a_line_naming_an_earlier_serial_ends_nothing_and_names_the_line_that_would(tmp_path):
    """C13: a stale line, written for a stop that has ended since, ends nothing and stays; the refusal says which line
    ends the stop in force."""
    path = release_file(tmp_path, "m1 3.abcdefgh\n")
    taken, [refused] = take_listed(path, {"m1": "4.ijklmnop"})
    assert taken == {}
    assert path.read_text(encoding="utf-8") == "m1 3.abcdefgh\n"
    assert (refused.line, refused.name, refused.serial) == ("m1 3.abcdefgh", "m1", "4.ijklmnop")
    assert "the line 'm1 4.ijklmnop' ends it" in refused.sentence("stop")


def test_c13_a_bare_name_ends_nothing_and_its_refusal_names_the_form_and_the_serial(tmp_path):
    """C13: the form before serials is refused with the new form and the line to write, so its writer learns it."""
    path = release_file(tmp_path, "m1\n")
    taken, [refused] = take_listed(path, {"m1": "7.abcdefgh"})
    assert taken == {}
    assert path.exists()
    sentence = refused.sentence("harness-error stop")
    assert "'<name> <serial>'" in sentence
    assert "harness-error stop on m1 now in force is stop 7.abcdefgh" in sentence
    assert "the line 'm1 7.abcdefgh' ends it" in sentence


@pytest.mark.parametrize("offset", [-3600.0, -0.0015, 0.0, 1.0], ids=["behind", "a-tick-behind", "same-tick", "ahead"])
def test_c13_the_files_time_decides_nothing(tmp_path, offset):
    """C13: the stop's own line counts whatever time the file carries (a coarse clock, a server's behind or ahead), and
    a bare name or another stop's line ends nothing at any time."""
    path = release_file(tmp_path, "m1\nm2 1.abcdefgh\nALL 2.abcdefgh\n", mtime=STOP + offset)
    taken, refused = take_listed(path, {"m1": "3.abcdefgh", "m2": "4.abcdefgh", "ALL": "2.abcdefgh"})
    assert set(taken) == {"ALL"}
    assert sorted(r.line for r in refused) == ["m1", "m2 1.abcdefgh"]
    assert path.read_text(encoding="utf-8").split("\n")[:2] == ["m1", "m2 1.abcdefgh"]


def test_c13_runs_of_blanks_count_as_one_space_and_other_lines_stay(tmp_path):
    """C13: a line is matched by its words; the lines left keep the file's time, which is shown, never compared."""
    path = release_file(tmp_path, "  m1\t 5.abcdefgh  \nnotes about it\nm9 1.abcdefgh\n", mtime=STOP)
    taken, refused = take_listed(path, {"m1": "5.abcdefgh"})
    assert set(taken) == {"m1"}
    assert refused == []  # neither of the other lines names a stop in force
    assert path.read_text(encoding="utf-8").split("\n")[:2] == ["notes about it", "m9 1.abcdefgh"]
    assert os.stat(path).st_mtime == STOP


def test_c13_with_no_stop_in_force_a_listed_file_is_left_alone(tmp_path):
    """C13: a release with nothing to end is not used up and refuses nothing, and a missing file ends nothing."""
    path = release_file(tmp_path, "m1 1.abcdefgh\n")
    assert take_listed(path, {}) == ({}, [])
    assert path.exists()
    assert take_listed(tmp_path / "absent", {"m1": "1.abcdefgh"}) == ({}, [])


def test_c13_a_release_file_that_cannot_be_rewritten_ends_nothing(tmp_path):
    """C13: if the line cannot be used up it would end nothing twice, so the stop holds and the file stays."""
    directory = tmp_path / "locked"
    directory.mkdir()
    path = release_file(directory, "m1 1.abcdefgh\nm2 2.abcdefgh\n")
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(directory, os.W_OK):  # running as a user the mode does not bind: nothing to show
            return
        assert take_listed(path, {"m1": "1.abcdefgh"})[0] == {}
        assert path.exists()
    finally:
        directory.chmod(stat.S_IRWXU)


def test_c13_a_serial_has_a_count_and_a_random_part_and_none_is_given_twice(tmp_path):
    """C13: the count goes up by one per stop and is kept in the store; two serials never share the random part."""
    store = open_store(tmp_path)
    first, second = next_serial(store, NOW), next_serial(store, NOW)
    assert SERIAL.fullmatch(first)
    assert SERIAL.fullmatch(second)
    assert (first.split(".")[0], second.split(".")[0]) == ("1", "2")
    assert first.split(".")[1] != second.split(".")[1]
    assert store.get_meta(SERIAL_KEY) == "2"
    store.close()
    reopened = open_store(tmp_path)
    assert next_serial(reopened, NOW).split(".")[0] == "3"


def test_c13_a_serial_taken_in_a_transaction_that_rolls_back_is_not_counted(tmp_path):
    """C13: the count commits with the stop that took it, or not at all."""
    store = open_store(tmp_path)

    def stop_then_fail():
        next_serial(store, NOW)
        raise RuntimeError("the stop's transaction fails")

    with pytest.raises(RuntimeError):
        store.transact(stop_then_fail)
    assert store.get_meta(SERIAL_KEY) is None
    assert next_serial(store, NOW).split(".")[0] == "1"


def test_c13_a_count_that_cannot_be_read_goes_on_past_every_event(tmp_path):
    """C13: each serial was given with an event, so a garbled count starts past the events' highest id."""
    store = open_store(tmp_path)
    for index in range(5):
        store.event(NOW, "something", None, str(index))
    store.put_meta(SERIAL_KEY, "garbled", NOW)
    assert next_serial(store, NOW).split(".")[0] == "6"


def test_c13_a_stop_without_a_readable_serial_has_none(tmp_path):
    """C13: a stop recorded before serials, or with a garbled one, carries none, and is given one before any release."""
    assert serial_of({"serial": "3.abcdefgh"}) == "3.abcdefgh"
    for stop in ({}, {"serial": 3}, {"serial": "3"}, {"serial": "3.ABCDEFGH"}, {"serial": "3.abcdefgh x"}, "text"):
        assert serial_of(stop) is None


def test_c13_a_hold_says_its_stop_and_the_line_that_ends_it(tmp_path):
    """C13: the hold names the serial, and with a release file the exact line to write."""
    assert told("m1", "3.abcdefgh", "the release file") == (
        " (stop 3.abcdefgh; to end it, write the line 'm1 3.abcdefgh' in the release file)"
    )
    assert told("m1", "3.abcdefgh", None) == " (stop 3.abcdefgh)"
    assert told("m1", None, "the release file") == ""
    assert line_for("ALL", "3.abcdefgh") == "ALL 3.abcdefgh"


def test_c13_a_refused_line_is_said_once_in_the_log_and_as_an_event_without_the_files_path(tmp_path, caplog):
    """C13: a refusal is said once per stop in force, not at every pass; the event, kept in the store, names no path."""
    store = open_store(tmp_path)
    path = release_file(tmp_path, "m1\n")
    said: set = set()
    for _ in range(3):
        _, refused = take_listed(path, {"m1": "2.abcdefgh"})
        with caplog.at_level(logging.WARNING, logger="expcore.campaign.release"):
            say_refused(store, path, refused, said, "release_refused", "stop", NOW)
    assert len([r for r in caplog.records if "ends nothing" in r.getMessage()]) == 1
    assert str(path) in caplog.records[0].getMessage()
    [event] = store.q("SELECT model, detail FROM events WHERE kind='release_refused'")
    assert event["model"] == "m1"
    assert "'m1 2.abcdefgh'" in event["detail"]
    assert str(tmp_path) not in event["detail"]
    _, refused = take_listed(path, {"m1": "3.abcdefgh"})  # a new stop: the same line is said again, with its serial
    say_refused(store, path, refused, said, "release_refused", "stop", NOW)
    assert len(store.q("SELECT 1 FROM events WHERE kind='release_refused'")) == 2


def test_c13_the_older_forms_still_compare_the_files_time(tmp_path):
    """C13: `take` and `written_after`, which nothing in the core uses, keep their strict time rule."""
    path = release_file(tmp_path, "checked the figure\n")
    assert written_after(path, STOP) is not None
    assert written_after(path, STOP + 100) is None
    assert take(path, STOP + 100) is None
    said = take(path, STOP)
    assert said is not None
    assert said.endswith("checked the figure")
    assert not path.exists()


def test_c13_a_stored_time_that_cannot_be_read_is_shown_as_0():
    """C13: the time of a stop is for display only."""
    assert epoch_of("not a time") == 0.0
    assert epoch_of("2026-01-01T00:00:00Z") > 0
