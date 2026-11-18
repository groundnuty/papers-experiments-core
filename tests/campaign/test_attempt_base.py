"""C41: a store that holds a lease numbers its run's attempts from the lease's attempt base, and counts the base against
the attempt limit, so attempts stay unique, and limited, across the leases of one run (step 2 of #462).

A store without a lease has no base, and numbers its attempts from 1 as every store did before leases (the pins of
`tests/data/existing-forms-pins.json` hold the whole of that; the first test here holds the arithmetic).
"""

import pytest
from campaign_support import NOW, finished, infra, planned_store, run_one

from expcore.campaign.run_lease import META_ATTEMPT_BASE


def one_key(store) -> str:
    return store.pending()[0]["run_key"]


def numbers(store, key: str) -> list[int]:
    return [row["attempt"] for row in store.q("SELECT attempt FROM attempts WHERE run_key=? ORDER BY id", (key,))]


def test_c41_a_store_without_a_base_numbers_its_attempts_from_one_as_before(tmp_path):
    """C41: no base row, no change: 1 then 2 after a charged infrastructure failure."""
    store, _, _ = planned_store(tmp_path)
    key = one_key(store)
    assert store.charged_attempts(key) == 0
    assert run_one(store, key, infra(), max_attempts=5) == "pending"
    assert run_one(store, key, finished(), max_attempts=5) == "done"
    assert numbers(store, key) == [1, 2]


def test_c41_a_store_with_a_base_numbers_its_first_attempt_after_the_base(tmp_path):
    """C41: a lease that follows two charged attempts of its run in earlier leases starts at attempt 3."""
    store, _, _ = planned_store(tmp_path)
    store.put_meta(META_ATTEMPT_BASE, "2", NOW)
    key = one_key(store)
    assert store.charged_attempts(key) == 2
    run_one(store, key, finished(), max_attempts=5)
    assert numbers(store, key) == [3]


def test_c41_the_number_a_pre_run_check_is_told_is_the_number_the_attempt_gets(tmp_path):
    """C41: `AttemptRunner` tells the health check `charged_attempts + 1` before the row exists; with a base, that
    must still be the row's own number."""
    store, _, _ = planned_store(tmp_path)
    store.put_meta(META_ATTEMPT_BASE, "4", NOW)
    key = one_key(store)
    told = store.charged_attempts(key) + 1
    begun = store.begin_attempt(key, NOW, 1, "")
    assert (told, begun.number) == (5, 5)


def test_c41_two_leases_of_one_run_never_reuse_an_attempt_number(tmp_path):
    """C41: lease 1 (base 0) has two charged failures; lease 2 starts from the base the ledger sums, 2, in a store of
    its own: the numbers 1, 2 and 3 are all different, in the order the run had them."""
    first, _, _ = planned_store(tmp_path / "first")
    key = one_key(first)
    run_one(first, key, infra(), max_attempts=9)
    run_one(first, key, infra(), max_attempts=9)
    second, _, _ = planned_store(tmp_path / "second")
    second.put_meta(META_ATTEMPT_BASE, str(first.charged_attempts(key)), NOW)
    run_one(second, key, finished(), max_attempts=9)
    assert numbers(first, key) + numbers(second, key) == [1, 2, 3]


@pytest.mark.parametrize(("base", "left"), [(0, 2), (1, 1), (2, 0)])
def test_c41_the_attempt_limit_counts_the_attempts_of_every_lease(tmp_path, base, left):
    """C41: with a limit of 3, a lease after `base` charged attempts has `left` more before the run is exhausted:
    base 2 leaves none after the one attempt it starts, which is the third."""
    store, _, _ = planned_store(tmp_path)
    store.put_meta(META_ATTEMPT_BASE, str(base), NOW)
    key = one_key(store)
    statuses = [run_one(store, key, infra(), max_attempts=3) for _ in range(left + 1)]
    assert statuses == ["pending"] * left + ["infra_exhausted"]
    assert numbers(store, key) == list(range(base + 1, base + left + 2))
    assert max(numbers(store, key)) == 3


def test_c41_a_base_that_is_not_a_whole_number_is_not_read_as_zero(tmp_path):
    """C41: a damaged row never lets a lease number from 1 again: the store refuses the attempt."""
    store, _, _ = planned_store(tmp_path)
    store.put_meta(META_ATTEMPT_BASE, "two", NOW)
    with pytest.raises(ValueError, match="attempt base"):
        store.begin_attempt(one_key(store), NOW, 1, "")
