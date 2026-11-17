"""The data path of each level of a study's `data_path` factor, as the campaign's own fingerprint records it (O72)."""

import pytest

from expcore.mountguard.campaign import recorded_data_paths


def fingerprint(paths):
    return {"schema": {}, "platform": {"tls": {}, "data_paths": paths}}


def test_o72_the_kind_of_each_level_is_read_from_the_platforms_half_of_the_fingerprint():
    found = recorded_data_paths(
        fingerprint(
            {
                "api": {"kind": "rest", "listing": "paged"},
                "mnt": {"kind": "mount", "io_mode": "proxy"},
                "bare": {"kind": "none"},
            }
        )
    )
    assert found == {"api": "rest", "mnt": "mount", "bare": "none"}


@pytest.mark.parametrize(
    "recorded",
    [
        None,
        {},
        {"platform": None},
        {"platform": {}},
        {"platform": {"data_paths": None}},
        {"platform": []},
        {"platform": {"data_paths": []}},
    ],
)
def test_o72_a_fingerprint_that_records_no_data_paths_gives_none(recorded):
    assert recorded_data_paths(recorded) is None


def test_o72_an_entry_that_is_not_a_known_kind_is_left_out_so_its_level_is_unknown():
    found = recorded_data_paths(
        fingerprint(
            {"ok": {"kind": "rest"}, "odd": {"kind": "api"}, "none-kind": {}, "flat": "rest", 3: {"kind": "rest"}}
        )
    )
    assert found == {"ok": "rest"}
