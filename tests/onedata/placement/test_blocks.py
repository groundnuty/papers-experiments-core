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
