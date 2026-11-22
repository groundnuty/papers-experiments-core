"""O35: the pair matrix the link heal reads (`expcore_onedata.fault_matrix`): which failing pairs the heal acts on.

A pair whose fetch failed is stuck, a pair that could not be tested is untested, and the heal acts on the readers of
both; a pair read with bytes other than the probe's was tested and transferred, so it is neither. Seen live in a
running campaign: a post-run check failed a pair as untested, no heal followed, and the next start failed.
"""

from expcore_onedata.fault_matrix import LinkMatrix, PairState
from expcore_onedata.linkcheck import REMEDY, LinkReport, PairResult

FETCH_FAILED = PairResult("providers[0]", "providers[2]", False, 30.0, reason="no transfer", remedy=REMEDY)
UNTESTED = PairResult("providers[1]", "providers[2]", False, reason="was not tested", untested=True)
MISREAD = PairResult("providers[2]", "providers[0]", False, 0.2, reason="is not the probe's bytes")
PASSED = PairResult("providers[0]", "providers[1]", True, 0.1, 65536)


def matrix_of(*results: PairResult) -> LinkMatrix:
    return LinkMatrix.from_report(LinkReport(all(r.ok for r in results), "the check", results, 0.0, 1.0))


def test_o35_a_failed_fetch_is_stuck_and_an_untested_pair_untested_and_the_heal_acts_on_both():
    """The heal's readers are those of the stuck pairs and of the untested ones, in label order."""
    matrix = matrix_of(FETCH_FAILED, UNTESTED, MISREAD, PASSED)
    states = {(pair.reader, pair.source): pair for pair in matrix.pairs}
    assert [(p.stuck, p.untested, p.heals) for p in states.values()] == [
        (True, False, True),
        (False, True, True),
        (False, False, False),
        (False, False, False),
    ]
    assert matrix.stuck_readers() == ["providers[0]", "providers[1]"]


def test_o35_a_matrix_whose_only_failure_is_a_misread_pair_names_no_reader_to_heal():
    """A pair that transferred the wrong bytes is a failure the heal cannot mend: nothing to act on."""
    assert matrix_of(MISREAD, PASSED).stuck_readers() == []


def test_o35_a_passing_pair_is_never_healed_whatever_its_flags_say():
    """`heals` is only for a failing pair: a pair that passed is left alone even if a flag was set on it."""
    assert PairState("providers[0]", "providers[1]", True, 0.1, True, True).heals is False


def test_o35_the_record_says_untested_only_when_it_is_so_older_records_read_alike():
    """The key `untested` is written only when true; every other pair's record is as it was."""
    matrix = matrix_of(FETCH_FAILED, UNTESTED)
    stuck, untested = matrix.to_json()["pairs"]
    assert "untested" not in stuck
    assert untested["untested"] is True
    assert untested["stuck"] is False
