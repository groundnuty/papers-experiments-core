"""The declared range map and the `[placement]` settings: validation names the file and the key, never the value."""

import pytest
from placement_support import write_with_placement

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.rangemap import RangeRow, load_placement, safe_path, trace_reads

ROWS = """
[[placement.map]]
file = "study/data.bin"
provider = "beta"
ranges = [[100, 50], [0, 10], [5, 10]]
"""


@pytest.fixture(autouse=True)
def _admin_credential(fake):
    """platform.toml names the admin credential's environment variable, which the fake sets."""


def load(tmp_path, rows="", **override):
    path = write_with_placement(tmp_path, rows, **override)
    return load_placement(path, load_platform(path))


def test_inline_rows_are_read_merged_and_sorted(tmp_path):
    placement = load(tmp_path, ROWS)
    assert placement.map.rows == (RangeRow("study/data.bin", "beta", ((0, 15), (100, 50))),)
    assert placement.map.total_bytes() == 65
    assert placement.map.files() == ("study/data.bin",)
    assert placement.settings.holder == "alpha"
    assert placement.map.for_provider("alpha") == ()


def test_a_study_file_can_hold_the_rows(tmp_path):
    (tmp_path / "ranges.toml").write_text(
        '[[map]]\nfile = "a/b.bin"\nprovider = "beta"\nranges = [[0, 4096]]\n', encoding="utf-8"
    )
    path = write_with_placement(tmp_path, map="ranges.toml")
    placement = load_placement(path, load_platform(path))
    assert placement.map.rows == (RangeRow("a/b.bin", "beta", ((0, 4096),)),)


def test_a_platform_without_a_placement_table_is_refused_naming_the_key(tmp_path):
    from platform_support import write_platform

    path = write_platform(tmp_path)
    with pytest.raises(ConfigError, match=r"platform\.toml: key 'placement' is required"):
        load_placement(path, load_platform(path))


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ('file = "../x"\nprovider = "beta"\nranges = [[0, 1]]', r"key 'file'.*relative to the space root"),
        ('file = "/x"\nprovider = "beta"\nranges = [[0, 1]]', r"key 'file'.*relative to the space root"),
        ('file = "x"\nprovider = "nowhere"\nranges = [[0, 1]]', r"key 'provider'.*declared in \[providers\]"),
        ('file = "x"\nprovider = "alpha"\nranges = [[0, 1]]', r"key 'provider'.*must not be the data provider"),
        ('file = "x"\nprovider = "beta"\nranges = []', r"key 'ranges'.*non-empty"),
        ('file = "x"\nprovider = "beta"\nranges = [[0, 0]]', r"key 'ranges'.*length of 1 or more"),
        ('file = "x"\nprovider = "beta"\nranges = [[-1, 5]]', r"key 'ranges'.*offset of 0 or more"),
        ('file = "x"\nprovider = "beta"\nranges = [[0.5, 5]]', r"key 'ranges'"),
        ('file = "x"\nprovider = "beta"\nranges = [[0, 1]]\nextra = 1', r"key 'extra'.*not a known key"),
    ],
)
def test_a_bad_row_is_refused_naming_the_key_and_not_echoing_the_value(tmp_path, rows, message):
    with pytest.raises(ConfigError, match=message) as raised:
        load(tmp_path, "\n[[placement.map]]\n" + rows + "\n")
    assert "nowhere" not in str(raised.value)
    assert "../x" not in str(raised.value)


def test_a_file_on_a_provider_named_twice_is_refused(tmp_path):
    with pytest.raises(ConfigError, match="more than once"):
        load(tmp_path, ROWS + ROWS)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("holder", "nowhere", r"key 'holder'.*declared in \[providers\]"),
        ("poll_s", 0, r"key 'poll_s'.*at least"),
        ("consecutive_polls", 0, r"key 'consecutive_polls'.*integer"),
        ("chunk_bytes", 1.5, r"key 'chunk_bytes'.*integer"),
        ("settle_timeout_s", -1, r"key 'settle_timeout_s'"),
        ("probe_attempts", True, r"key 'probe_attempts'"),
    ],
)
def test_a_bad_setting_is_refused(tmp_path, key, value, message):
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, **{key: value})


@pytest.mark.parametrize("value", [1, 0, -2, 2.0, True, "2"])
def test_o07_one_poll_is_not_convergence_so_fewer_than_two_consecutive_polls_is_refused_at_load(tmp_path, value):
    """O07: a block map is believed only when two polls in a row agree, so a configuration that asks for one poll (or
    anything that is not an integer of two or more) is refused naming the key."""
    with pytest.raises(ConfigError, match=r"key 'consecutive_polls'.*integer of at least 2"):
        load(tmp_path, consecutive_polls=value)


def test_o07_two_consecutive_polls_and_more_are_accepted(tmp_path):
    """O07: the smallest accepted count is the default, two."""
    assert load(tmp_path, consecutive_polls=2).settings.consecutive_polls == 2
    assert load(tmp_path, consecutive_polls=5).settings.consecutive_polls == 5


def test_safe_path_accepts_plain_relative_paths_only():
    assert safe_path("a/b.bin") == ("a", "b.bin")
    for bad in ("", "/a", "a/", "a//b", "a/../b", "./a", "a\\b"):
        with pytest.raises(ValueError, match="relative to the space root"):
            safe_path(bad)


def test_trace_reads_returns_the_merged_ranges_a_reader_read(tmp_path):
    """The map of a study can be derived from its own reader: every byte it read, joined."""
    file = tmp_path / "local.bin"
    file.write_bytes(bytes(range(256)) * 8)

    def reader(handle):
        handle.seek(10)
        handle.read(20)
        handle.seek(30)
        handle.read(10)  # adjacent to the first read
        handle.seek(1000)
        handle.read(5)

    assert trace_reads(file, reader) == [(10, 30), (1000, 5)]
