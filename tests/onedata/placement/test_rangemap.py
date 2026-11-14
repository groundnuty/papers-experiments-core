"""The declared range map and the `[placement]` settings: validation names the file and the key, never the value."""

import dataclasses

import pytest

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.rangemap import PlacementSettings, RangeRow, load_placement, safe_path, trace_reads
from expcore_onedata.testing import write_platform, write_with_placement

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
    from expcore_onedata.testing import write_platform

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
        ("fresh_connection_bytes", 0, r"key 'fresh_connection_bytes'.*integer of at least 1"),
        ("fresh_connection_bytes", 1.5, r"key 'fresh_connection_bytes'.*integer"),
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


def test_o07_the_transfer_listing_is_waited_out_by_default_for_30_s_and_up_to_4_observations(tmp_path):
    """O07: the wait between two observations of the transfer listing defaults to 30 s, at least the listing's lag,
    and the observations to 4; both can be set."""
    settings = load(tmp_path).settings
    assert (settings.transfer_settle_s, settings.transfer_settle_polls) == (30.0, 4)
    custom = load(tmp_path, transfer_settle_s=60, transfer_settle_polls=6).settings
    assert (custom.transfer_settle_s, custom.transfer_settle_polls) == (60.0, 6)


def test_o08_the_allowance_for_clock_skew_defaults_to_5_s_and_cannot_be_negative(tmp_path):
    """O08: the run's window is widened by the providers' estimated clock offsets plus an allowance of 5 s by
    default, which can be set and is never negative."""
    assert load(tmp_path).settings.clock_skew_allowance_s == 5.0
    assert load(tmp_path, clock_skew_allowance_s=12).settings.clock_skew_allowance_s == 12.0
    with pytest.raises(ConfigError, match=r"key 'clock_skew_allowance_s'"):
        load(tmp_path, clock_skew_allowance_s=-1)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("transfer_settle_s", -1, r"key 'transfer_settle_s'"),
        ("transfer_settle_polls", 1, r"key 'transfer_settle_polls'.*integer of at least 2"),
        ("transfer_settle_polls", 2.5, r"key 'transfer_settle_polls'.*integer"),
    ],
)
def test_o07_a_listing_settle_that_could_not_show_two_alike_observations_is_refused(tmp_path, key, value, message):
    """O07: one observation is not convergence, so fewer than two is refused, as is a negative wait."""
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, **{key: value})


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


def test_o25_the_default_chunk_is_16_mib_and_every_full_default_chunk_goes_on_a_new_connection(tmp_path):
    """O25: on the 25.0 development build a 64 MiB or 32 MiB chunk stalled on a reused connection (3 of 289 such PUTs
    against 0 of 336 on new ones) and 16 MiB never did, so the default is 16 MiB, in the dataclass and as the loader
    reads it from a table that names neither key, and the threshold from which a content write is sent on a new
    connection is 16 MiB too: with the defaults every full chunk is above it."""
    bare = write_platform(tmp_path, lambda data: data.update(placement={"holder": "alpha"}))
    loaded = load_placement(bare, load_platform(bare)).settings
    for settings in (PlacementSettings(holder="alpha"), loaded):
        assert (settings.chunk_bytes, settings.fresh_connection_bytes) == (16777216, 16777216)
    assert loaded.chunk_bytes >= loaded.fresh_connection_bytes


def test_o25_a_positional_construction_from_before_the_connection_threshold_still_means_what_it_did():
    """O25: `fresh_connection_bytes` (#196) was inserted before `probe_attempts`, so a caller passing the settings
    positionally had every later argument shift one field, `probe_attempts` become a float and a probe fail with a
    TypeError. The earlier order is restored and the new field comes last."""
    settings = PlacementSettings("data", 2.5, 3600.0, 300.0, 2, 900.0, 16 << 20, 3, 1.5, 30.0, 4, 5.0, 16)
    assert (settings.holder, settings.poll_s, settings.transfer_timeout_s, settings.settle_timeout_s) == (
        "data",
        2.5,
        3600.0,
        300.0,
    )
    assert (settings.consecutive_polls, settings.request_timeout_s, settings.chunk_bytes) == (2, 900.0, 16 << 20)
    assert (settings.probe_attempts, settings.probe_backoff_s) == (3, 1.5)
    assert (settings.transfer_settle_s, settings.transfer_settle_polls) == (30.0, 4)
    assert (settings.clock_skew_allowance_s, settings.concurrency) == (5.0, 16)
    assert settings.fresh_connection_bytes == 16 << 20  # not given: the default
    assert isinstance(settings.probe_attempts, int)
    assert dataclasses.fields(PlacementSettings)[-1].name == "fresh_connection_bytes"


def test_o25_the_threshold_for_a_new_connection_can_be_set(tmp_path):
    """O25: `[placement] fresh_connection_bytes` is the smallest content write sent on a connection of its own."""
    assert load(tmp_path, fresh_connection_bytes=4 << 20).settings.fresh_connection_bytes == 4 << 20


def test_c35_the_settings_a_resume_compares_are_every_effective_value_of_the_table_but_the_providers_name(tmp_path):
    """C35: `PlacementSettings.reset_settings()` is what the platform gives the core to record and compare at a resume:
    each field after its default, under `placement.<name>`, numbers and text only. The data provider's name is left
    out, since the record is shipped with a replication package, and so is the map."""
    from expcore.campaign.reset_settings import canonical

    placement = load(tmp_path, ROWS, qos_scan="summaries", concurrency=4)
    watched = placement.settings.reset_settings()
    fields = {field.name for field in dataclasses.fields(PlacementSettings)} - {"holder"}
    assert set(watched) == {f"placement.{name}" for name in fields}
    assert watched["placement.qos_scan"] == "summaries"
    assert watched["placement.concurrency"] == 4
    assert watched["placement.qos_fulfil_timeout_s"] == 120.0  # not declared: its default, the effective value
    assert all(isinstance(value, str | int | float) for value in watched.values())
    text = canonical(watched)
    assert "alpha" not in text
    assert "study/data.bin" not in text
    assert "beta" not in text


def test_c35_a_default_that_changes_under_a_file_that_does_not_declare_it_shows_in_the_settings(tmp_path):
    """C35: the record is of the effective value, so a file that never declared `qos_scan` differs from one that
    declares `summaries` (the default before #258), and a resume that finds the other says so."""
    declared = load(tmp_path, qos_scan="summaries").settings.reset_settings()
    (tmp_path / "bare").mkdir()
    bare = load(tmp_path / "bare").settings.reset_settings()
    assert declared["placement.qos_scan"] == "summaries"
    assert bare["placement.qos_scan"] == "listing"
    assert {key for key in bare if bare[key] != declared[key]} == {"placement.qos_scan"}
