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


def test_blocks_of_a_provider_are_read_sorted():
    assert P.provider_blocks(distribution(a=[[100, 50], [0, 10]]), "a") == [(0, 10), (100, 50)]


def test_o01_a_provider_the_answer_does_not_list_is_unknown_and_never_an_empty_holding():
    """O01 and O08: the answer may be partial, so a declared provider absent from it is not proof that it holds
    nothing; it raises, as a missing blocks field does."""
    answer = distribution(a=[[0, 10]])
    with pytest.raises(ValueError, match="does not list the provider"):
        P.provider_blocks(answer, "b")
    with pytest.raises(ValueError, match="does not list the provider"):
        P.provider_blocks({"distributionPerProvider": {}}, "a")


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


@pytest.mark.parametrize(
    "block",
    [
        [0, -1],  # a negative length
        [-1, 5],  # a negative offset
        [0, 0],  # an empty block places nothing and is not a block
        ["a", 1],
        [1, "5"],
        [True, 1],  # a bool is not an integer here
        [0, False],
        [1.5, 2],
        [0],
        [0, 1, 2],
        None,
        "05",
    ],
)
def test_o01_a_block_that_is_not_a_pair_of_integers_with_a_valid_offset_and_length_makes_the_answer_unknown(block):
    """O01: every block is validated. A bad one among good ones makes the provider's whole answer unknown; it is never
    dropped, clipped to zero or counted, since a block map is evidence of placement."""
    for blocks in ([block], [[0, 5], block], [block, [10, 5]]):
        with pytest.raises(ValueError, match="block"):
            P.provider_blocks(distribution(a=blocks), "a")


def test_o01_a_valid_block_is_accepted_at_offset_zero_and_with_a_length_of_one():
    """O01: the bounds are inclusive at zero for the offset and one for the length."""
    assert P.provider_blocks(distribution(a=[[0, 1], (5, 2)]), "a") == [(0, 1), (5, 2)]


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
