"""A release file counts once, and only if it is newer than the stop it ends (C13)."""

import os
import stat

from expcore.campaign.release import SLACK_S, epoch_of, take, take_listed, written_after

STOP = 1_000_000.0


def release_file(tmp_path, text="go\n", mtime=STOP + 10, name="release"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def test_c13_a_release_written_after_the_stop_ends_it_and_is_used_up(tmp_path):
    """C13: a release newer than the stop ends it once; the file is removed, so a reused file ends nothing."""
    path = release_file(tmp_path, "checked the figure\n")
    said = take(path, STOP)
    assert said is not None
    assert said.endswith("checked the figure")
    assert not path.exists()
    assert take(path, STOP) is None
    path.write_text("again\n", encoding="utf-8")
    os.utime(path, (STOP + 20, STOP + 20))
    assert take(path, STOP + 100) is None  # a new file is older than a later stop
    assert path.exists()


def test_c13_a_release_written_before_the_stop_does_not_end_it(tmp_path):
    """C13: a stale file says nothing about a stop nobody had seen when it was written; it is left where it is."""
    path = release_file(tmp_path, mtime=STOP - 100)
    assert take(path, STOP) is None
    assert path.exists()


def test_c13_a_file_a_moment_before_the_stop_counts_because_file_clocks_are_coarse(tmp_path):
    """C13: file times come from a coarser clock, so a file within the slack of the stop counts as written after it."""
    assert take(release_file(tmp_path, mtime=STOP - SLACK_S / 2), STOP) is not None
    assert take(release_file(tmp_path, mtime=STOP - SLACK_S * 3, name="r2"), STOP) is None


def test_c13_a_release_that_cannot_be_removed_releases_nothing(tmp_path):
    """C13: if the word cannot be used up it would end the next stop as well, so the stop holds."""
    directory = tmp_path / "locked"
    directory.mkdir()
    path = release_file(directory)
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        if os.access(directory, os.W_OK):  # running as a user the mode does not bind: nothing to show
            return
        assert take(path, STOP) is None
        assert path.exists()
    finally:
        directory.chmod(stat.S_IRWXU)


def test_c13_a_listed_release_ends_only_the_stops_it_names_and_that_were_older_than_it(tmp_path):
    """C13: each named stop ends once; a name with no stop in force and a name stopped later are left in the file."""
    path = release_file(tmp_path, "alpha\nbeta\ngamma\nALL\n", mtime=STOP + 10)
    stops = {"alpha": STOP, "gamma": STOP + 50, "ALL": STOP + 5}
    taken = take_listed(path, stops)
    assert set(taken) == {"alpha", "ALL"}  # gamma was stopped after the file was written
    assert path.read_text(encoding="utf-8").split() == ["beta", "gamma"]
    assert os.stat(path).st_mtime == STOP + 10  # what is left stays as old as it was
    assert take_listed(path, {"alpha": STOP}) == {}  # alpha was used up
    later = take_listed(path, {"gamma": STOP + 10})
    assert set(later) == {"gamma"}
    assert path.read_text(encoding="utf-8").split() == ["beta"]


def test_c13_the_file_goes_with_its_last_line(tmp_path):
    """C13: when every listed name is taken the file is removed, so nothing is left to end a later stop."""
    path = release_file(tmp_path, "alpha\n")
    assert set(take_listed(path, {"alpha": STOP})) == {"alpha"}
    assert not path.exists()
    assert take_listed(path, {"alpha": STOP}) == {}


def test_c13_with_no_stop_in_force_a_listed_file_is_left_alone(tmp_path):
    """C13: a release with nothing to end is not used up, and a missing file ends nothing."""
    path = release_file(tmp_path, "alpha\n")
    assert take_listed(path, {}) == {}
    assert path.exists()
    assert take_listed(tmp_path / "absent", {"alpha": STOP}) == {}


def test_c13_a_standing_decision_is_looked_at_and_kept(tmp_path):
    """C13: written_after only reports who wrote the file when it is newer than the figure it answers; it stays."""
    path = release_file(tmp_path, mtime=STOP + 10)
    assert written_after(path, STOP) is not None
    assert written_after(path, STOP) is not None  # still there, still counts
    assert written_after(path, STOP + 100) is None
    assert written_after(tmp_path / "absent", STOP) is None


def test_c13_a_stop_time_that_cannot_be_read_lets_a_release_count(tmp_path):
    """C13: the stop itself is not in doubt when its time is garbled, so epoch 0 lets any release count."""
    assert epoch_of("not a time") == 0.0
    assert epoch_of("2026-01-01T00:00:00Z") > 0
    assert written_after(release_file(tmp_path), epoch_of("garbled")) is not None
