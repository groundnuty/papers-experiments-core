"""Block-map arithmetic on hand-written maps."""

import pytest

from expcore_onedata import placement as P


def distribution(**providers):
    return {
        "distributionPerProvider": {
            name: {"distributionPerStorageBackend": {"backend-1": {"blocks": blocks}}}
            for name, blocks in providers.items()
        }
    }


def test_blocks_of_a_provider_are_read_sorted_and_a_missing_provider_holds_nothing():
    answer = distribution(a=[[100, 50], [0, 10]])
    assert P.provider_blocks(answer, "a") == [(0, 10), (100, 50)]
    assert P.provider_blocks(answer, "b") == []


def test_a_distribution_of_the_wrong_shape_is_refused_not_read_as_empty():
    with pytest.raises(ValueError, match="distributionPerProvider"):
        P.provider_blocks({}, "a")
    with pytest.raises(ValueError, match="offset, length"):
        P.provider_blocks(distribution(a=[[1, 2, 3]]), "a")
    with pytest.raises(ValueError, match="not a table"):
        P.provider_blocks({"distributionPerProvider": {"a": {"distributionPerStorageBackend": {"s": 5}}}}, "a")


UNKNOWN = [
    pytest.param({}, id="entry without a backend table"),
    pytest.param(None, id="entry that is null"),
    pytest.param([], id="entry that is a list"),
    pytest.param({"distributionPerStorageBackend": None}, id="null backend table"),
    pytest.param({"distributionPerStorageBackend": {}}, id="empty backend table"),
    pytest.param({"distributionPerStorageBackend": {"s": {}}}, id="backend without blocks"),
    pytest.param({"distributionPerStorageBackend": {"s": {"blocks": None}}}, id="backend with null blocks"),
    pytest.param({"distributionPerStorageBackend": {"s": {"blocks": "0-10"}}}, id="blocks that are text"),
    pytest.param({"distributionPerStorageBackend": {"s": {"blocks": {"0": 10}}}}, id="blocks that are a table"),
    pytest.param(
        {"distributionPerStorageBackend": {"s": {"blocks": [[0, 10]]}, "t": {"physicalSize": 0}}},
        id="one backend of two without blocks",
    ),
]


@pytest.mark.parametrize("entry", UNKNOWN)
def test_o08_a_missing_or_malformed_blocks_field_is_unknown_and_never_an_empty_list(entry):
    """O01 and O08: a listed provider whose entry or backend gives no list of blocks has an unknown holding, which
    raises; reading it as [] would let an eviction be confirmed by an answer that says nothing."""
    with pytest.raises(ValueError, match=r"distributionPerStorageBackend|blocks|not a table"):
        P.provider_blocks({"distributionPerProvider": {"a": entry}}, "a")


def test_o01_an_empty_list_of_blocks_is_an_answer_and_means_nothing_is_held():
    """O01: only an explicit empty list says a provider holds nothing."""
    assert P.provider_blocks(distribution(a=[]), "a") == []
    assert P.provider_blocks(distribution(a=[], b=[[0, 5]]), "b") == [(0, 5)]


def test_merging_joins_overlapping_and_adjacent_blocks_and_drops_empty_ones():
    assert P.merged([(10, 5), (0, 10), (12, 10), (50, 0)]) == [(0, 22)]
    assert P.held_bytes([(0, 10), (5, 10)]) == 15


def test_the_uncovered_part_of_a_range_is_what_the_blocks_do_not_hold():
    assert P.uncovered([(0, 10), (20, 10)], [(0, 30)]) == [(10, 10)]
    assert P.uncovered([(0, 30)], [(5, 10), (20, 5)]) == []
    assert P.uncovered([], [(5, 10)]) == [(5, 10)]


def test_covers_needs_at_least_one_range_and_every_byte_of_it():
    assert P.covers([(0, 100)], [(10, 20)])
    assert not P.covers([(0, 100)], [(90, 20)])
    assert not P.covers([(0, 100)], [])  # nothing asked for is not a placement
    assert P.file_fully_placed([(0, 100)], 100)
    assert P.file_fully_placed([], 0)
    assert not P.file_fully_placed([(0, 99)], 100)
