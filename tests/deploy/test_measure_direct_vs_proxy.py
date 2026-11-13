"""The proxy-against-direct comparison of deploy/compose-federation/measure_direct_vs_proxy.py, checked without a
federation (#223, O37): its passes in order, its comparison of what each read placed, its verdict, the platform
tables it derives, and what its help says. The live run is the coordinator's."""

import json

import pytest

TAG = "buffered-A"


@pytest.fixture(scope="module")
def script(fed):
    import measure_direct_vs_proxy

    return measure_direct_vs_proxy


def replay(*gains, placed=None, ended=True, **read_fields):
    """One pass's replay of a file: each read's gained ranges, and what the local provider held at the end."""
    reads = [{"i": i, "gained": [list(g) for g in gained], "settled": True, **read_fields} for i, gained in
             enumerate(gains)]  # fmt: skip
    every = [list(g) for gained in gains for g in gained]
    return {"reads": reads, "placed": every if placed is None else placed, "ended": ended}


def test_o37_each_round_runs_both_modes_from_the_same_state_and_alternates_which_goes_first(script):
    """O37: round 0 runs proxy then direct, round 1 direct then proxy, so an order effect does not pass for a mode's;
    the control is a second proxy pass in each round, compared with the first: the noise floor."""
    plan = script.pass_plan(2, control=False)
    assert [(p["round"], p["mode"], p["label"]) for p in plan] == [
        (0, "proxy", "proxy"), (0, "direct", "direct"), (1, "direct", "direct"), (1, "proxy", "proxy"),
    ]  # fmt: skip
    with_control = script.pass_plan(1, control=True)
    assert [p["label"] for p in with_control] == ["proxy", "direct", "proxy-control"]
    assert with_control[-1]["mode"] == "proxy"


def test_o37_reads_that_place_the_same_bytes_compare_the_same(script):
    """O37: the same gains read by read, and the same placement at the end, are the same."""
    base = {TAG: replay([(0, 1 << 20)], [(1 << 20, 1 << 20)])}
    other = {TAG: replay([(0, 1 << 20)], [(1 << 20, 1 << 20)])}
    [found] = script.compare(base, other)
    assert found["same"] is True
    assert found["differing_reads"] == []
    assert found["incomplete"] is None
    assert found["placed_same"] is True


def test_o37_a_read_that_placed_other_bytes_is_named_with_the_bytes_only_each_pass_placed(script):
    """O37: compared read by read, never only in total: the second read placed 1 MiB under proxy and 4 MiB under
    direct, and the totals of a sequence could still agree when two reads trade bytes."""
    base = {TAG: replay([(0, 1 << 20)], [(1 << 20, 1 << 20)])}
    other = {TAG: replay([(0, 1 << 20)], [(1 << 20, 4 << 20)])}
    [found] = script.compare(base, other)
    assert found["same"] is False
    assert found["differing_reads"] == [
        {"i": 1, "base_bytes": 1 << 20, "other_bytes": 4 << 20, "only_base_bytes": 0, "only_other_bytes": 3 << 20}
    ]
    assert found["placed_same"] is False
    assert found["placed_only_other_bytes"] == 3 << 20


def test_o37_equal_totals_with_reads_that_trade_bytes_are_a_difference(script):
    """O37: two reads that trade bytes differ even when the sequence's total agrees."""
    base = {TAG: replay([(0, 2 << 20)], [])}
    other = {TAG: replay([(0, 1 << 20)], [(1 << 20, 1 << 20)])}
    [found] = script.compare(base, other)
    assert found["placed_same"] is True
    assert found["same"] is False
    assert [d["i"] for d in found["differing_reads"]] == [0, 1]


@pytest.mark.parametrize(
    ("base", "other", "why"),
    [
        (replay([(0, 10)]), replay([(0, 10)], [(10, 10)]), "read counts differ"),
        (replay([(0, 10)], error="EIO"), replay([(0, 10)]), "a read failed or gave no answer"),
        (replay([(0, 10)], no_answer_after_s=300), replay([(0, 10)]), "a read failed or gave no answer"),
        (replay([(0, 10)], settled=False), replay([(0, 10)]), "a block map did not settle"),
        (replay([(0, 10)], ended=False), replay([(0, 10)]), "a replay did not end"),
        (None, replay([(0, 10)]), "missing in a pass"),
    ],
)
def test_o37_a_file_whose_replay_is_not_complete_in_both_passes_is_incomplete_never_the_same(script, base, other, why):
    """O37: a failed read, a block map that never settled or a replay that never ended leaves the comparison unknown,
    never `same`, and never a difference either."""
    [found] = script.compare({TAG: base} if base else {}, {TAG: other})
    assert found["incomplete"] == why
    assert found["same"] is None


def test_o37_the_same_reads_with_a_different_final_placement_are_not_the_same(script):
    """O37: each read gained the same, but a late prefetch placed more under one mode before the final read of the map:
    the sequence did not place the same bytes."""
    base = {TAG: replay([(0, 10)], placed=[[0, 10]])}
    other = {TAG: replay([(0, 10)], placed=[[0, 20]])}
    [found] = script.compare(base, other)
    assert found["differing_reads"] == []
    assert found["placed_same"] is False
    assert found["same"] is False


def test_o37_every_file_of_the_measurement_is_compared_even_when_both_passes_failed_to_start(script):
    """O37: a round whose two passes both failed gives nothing to compare; its files are still each incomplete, so the
    round can never vanish from the verdict and leave a yes."""
    found = script.compare({}, {}, [TAG, "pread-A"])
    assert [(f["file"], f["incomplete"]) for f in found] == [
        (TAG, "missing in a pass"),
        ("pread-A", "missing in a pass"),
    ]
    good = script.compare({TAG: replay([(0, 10)])}, {TAG: replay([(0, 10)])}, [TAG])
    assert script.verdict(good + script.compare({}, {}, [TAG]))["same_placed_bytes_per_read_sequence"] is None


def test_o37_a_direct_pass_whose_reads_went_through_the_provider_is_incomplete_never_the_same(script):
    """O37: a direct pass whose sidecar logged a fallback (or whose log could not be read) after its reads did not
    read directly, so its files compare as incomplete, never as the same placement."""
    base = {TAG: replay([(0, 10)])}
    other = {TAG: {**replay([(0, 10)]), "not_direct": ["proxy_fallback"]}}
    [found] = script.compare(base, other)
    assert found["same"] is None
    assert found["incomplete"] == "a direct pass did not read directly, or could not be shown to"


def test_o37_the_verdict_is_yes_only_when_every_file_compared_and_none_differs(script):
    """O37: unknown is never yes, and one difference is a no."""
    same = {"file": "a", "same": True, "incomplete": None}
    differs = {"file": "b", "same": False, "incomplete": None}
    unknown = {"file": "c", "same": None, "incomplete": "a read failed or gave no answer"}
    assert script.verdict([same])["same_placed_bytes_per_read_sequence"] is True
    assert script.verdict([same, differs])["same_placed_bytes_per_read_sequence"] is False
    assert script.verdict([same, unknown])["same_placed_bytes_per_read_sequence"] is None
    assert script.verdict([differs, unknown])["same_placed_bytes_per_read_sequence"] is False
    assert script.verdict([])["same_placed_bytes_per_read_sequence"] is None
    assert script.verdict([same, differs, unknown]) == {
        "files": 3, "same": 1, "differing": 1, "incomplete": 1, "same_placed_bytes_per_read_sequence": False,
    }  # fmt: skip


def test_o37_a_storage_argument_is_an_id_with_an_optional_host_path(script, tmp_path):
    """O37: the storage's id and host directory, the Compose layout's by default."""
    platform = tmp_path / "platform.toml"
    assert script.storage_of("0a1b2c", None, platform, "oak") == ("0a1b2c", str(tmp_path / "oak" / "storage"))
    assert script.storage_of("0a1b2c", "/srv/other", platform, "oak") == ("0a1b2c", "/srv/other")
    with pytest.raises(SystemExit, match="--storage"):
        script.storage_of("with:colon", None, platform, "oak")
    with pytest.raises(SystemExit, match="--storage-path"):
        script.storage_of("0a1b2c", "relative", platform, "oak")


def test_o37_the_direct_pass_maps_the_storage_and_names_the_check_file_and_the_proxy_pass_neither(script):
    """O37: each pass's mount keys fit its mode, as the loader requires."""
    proxy = script.mount_keys("proxy", ("0a1b2c", "/srv/s"), "space/run/check.bin")
    direct = script.mount_keys("direct", ("0a1b2c", "/srv/s"), "space/run/check.bin")
    assert proxy == {"io_mode": "proxy"}
    assert direct == {
        "io_mode": "direct", "storages": {"0a1b2c": "/srv/s"}, "direct_check_file": "space/run/check.bin",
    }  # fmt: skip


def test_o37_the_help_says_what_it_compares_and_what_it_changes(script, capsys):
    """O37: the help names the comparison, the state it starts from and what it changes."""
    with pytest.raises(SystemExit) as stop:
        script.main(["--help"])
    assert stop.value.code == 0
    shown = " ".join(capsys.readouterr().out.split())
    for words in (
        "same files",
        "evicted",
        "read sequence by read sequence",
        "--storage",
        "distributionPerStorageBackend",
        "--control",
        "deletes them",
    ):
        assert words in shown, words


def test_o37_the_comparison_lines_hold_numbers_and_tags_never_a_path(script):
    """O37, O12: each line the comparison writes holds a tag, offsets, lengths and counts."""
    base = {TAG: replay([(0, 10)])}
    other = {TAG: replay([(0, 20)])}
    text = json.dumps(script.compare(base, other))
    assert "/" not in text
