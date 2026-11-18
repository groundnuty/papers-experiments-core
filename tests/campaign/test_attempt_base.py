"""C41: a store that holds a lease numbers its run's attempts from the lease's attempt base, and counts the lease's
charged attempts against the attempt limit, so no attempt of a run has a number another lease's has, and the limit
counts every lease's charged attempts (step 2 of #462).

The lease carries two counts: `attempt_base`, what the run's earlier leases started (charged or not), from which the
lease numbers; and `charged_attempts`, the charged ones, which count against the limit. A store without a lease has
neither, and numbers its attempts from 1 as every store did before leases (the pins of
`tests/data/existing-forms-pins.json` hold the whole of that; the first test here holds the arithmetic).
"""

import pytest
from campaign_support import NOW, finished, infra, planned_store, run_one

from expcore.campaign.run_lease import META_ATTEMPT_BASE, META_CHARGED_ATTEMPTS


def one_key(store) -> str:
    return store.pending()[0]["run_key"]


def numbers(store, key: str) -> list[int]:
    return [row["attempt"] for row in store.q("SELECT attempt FROM attempts WHERE run_key=? ORDER BY id", (key,))]


def with_lease(store, base: int, charged: int) -> None:
    """Pin what a lease pins of its counts: the attempts its run's earlier leases started, and the charged ones."""
    store.put_meta(META_ATTEMPT_BASE, str(base), NOW)
    store.put_meta(META_CHARGED_ATTEMPTS, str(charged), NOW)


def test_c41_a_store_without_a_lease_numbers_its_attempts_from_one_as_before(tmp_path):
    """C41: no counts, no change: 1 then 2 after a charged infrastructure failure."""
    store, _, _ = planned_store(tmp_path)
    key = one_key(store)
    assert (store.charged_attempts(key), store.next_attempt_number(key)) == (0, 1)
    assert run_one(store, key, infra(), max_attempts=5) == "pending"
    assert run_one(store, key, finished(), max_attempts=5) == "done"
    assert numbers(store, key) == [1, 2]


def test_c41_a_store_with_a_lease_numbers_its_first_attempt_after_the_attempts_the_run_had_started(tmp_path):
    """C41: a lease that follows three started attempts, one of them charged, starts at attempt 4; one charged attempt
    counts against the limit."""
    store, _, _ = planned_store(tmp_path)
    with_lease(store, base=3, charged=1)
    key = one_key(store)
    assert (store.charged_attempts(key), store.next_attempt_number(key)) == (1, 4)
    run_one(store, key, finished(), max_attempts=5)
    assert numbers(store, key) == [4]


def test_c41_the_number_a_pre_run_check_is_told_is_the_number_the_attempt_gets(tmp_path):
    """C41: `AttemptRunner` tells the health check `next_attempt_number` before the row exists; with a lease it must be
    the row's own number, which is not the charged count plus one."""
    store, _, _ = planned_store(tmp_path)
    with_lease(store, base=4, charged=1)
    key = one_key(store)
    told = store.next_attempt_number(key)
    assert told != store.charged_attempts(key) + 1
    assert store.begin_attempt(key, NOW, 1, "").number == told == 5


def test_c41_an_uncharged_attempt_of_one_lease_is_numbered_past_by_the_next_lease(tmp_path):
    """C41: lease 1 starts an uncharged attempt (a key rejection) and a charged one; lease 2 is given what it started,
    2, and what was charged, 1, and numbers after the attempts of lease 1, uncharged one included: no number is in both
    stores, so the stores a merge keeps as evidence never collide by run and attempt."""
    first, _, _ = planned_store(tmp_path / "first")
    key = one_key(first)
    run_one(first, key, infra("key_rejected"), max_attempts=9)
    run_one(first, key, infra("endpoint"), max_attempts=9)
    started = len(first.q("SELECT 1 FROM attempts WHERE run_key=?", (key,)))
    second, _, _ = planned_store(tmp_path / "second")
    with_lease(second, base=started, charged=first.charged_attempts(key))
    run_one(second, key, infra("key_rejected"), max_attempts=9)
    run_one(second, key, finished(), max_attempts=9)
    assert (started, first.charged_attempts(key)) == (2, 1)
    assert not set(numbers(first, key)) & set(numbers(second, key))
    assert min(numbers(second, key)) > max(numbers(first, key))


def test_c41_two_charged_leases_of_one_run_never_reuse_a_number_either(tmp_path):
    """C41: lease 1 has two charged failures; lease 2 is given 2 started and 2 charged: 1, 2 and then 3."""
    first, _, _ = planned_store(tmp_path / "first")
    key = one_key(first)
    run_one(first, key, infra(), max_attempts=9)
    run_one(first, key, infra(), max_attempts=9)
    second, _, _ = planned_store(tmp_path / "second")
    with_lease(second, base=2, charged=2)
    run_one(second, key, finished(), max_attempts=9)
    assert numbers(first, key) + numbers(second, key) == [1, 2, 3]


@pytest.mark.parametrize(("charged", "left"), [(0, 2), (1, 1), (2, 0)])
def test_c41_the_attempt_limit_counts_the_charged_attempts_of_every_lease_and_not_the_started_ones(
    tmp_path, charged, left
):
    """C41: with a limit of 3, a lease after `charged` charged attempts has `left` more before the run is exhausted,
    whatever number of attempts the earlier leases started (5 here): the numbers go on from 5, the limit from 3."""
    store, _, _ = planned_store(tmp_path)
    with_lease(store, base=5, charged=charged)
    key = one_key(store)
    statuses = [run_one(store, key, infra(), max_attempts=3) for _ in range(left + 1)]
    assert statuses == ["pending"] * left + ["infra_exhausted"]
    assert numbers(store, key) == list(range(6, 6 + left + 1))
    assert store.charged_attempts(key) == 3


@pytest.mark.parametrize(
    ("key", "read"),
    [
        (META_ATTEMPT_BASE, lambda store, run: store.next_attempt_number(run)),
        (META_CHARGED_ATTEMPTS, lambda store, run: store.charged_attempts(run)),
    ],
)
def test_c41_a_count_that_is_not_a_whole_number_is_not_read_as_zero(tmp_path, key, read):
    """C41: a damaged row never lets a lease number from 1 again or lift its attempt limit: the store refuses."""
    store, _, _ = planned_store(tmp_path)
    with_lease(store, base=1, charged=1)
    store.put_meta(key, "two", NOW)
    with pytest.raises(ValueError, match="not a whole number"):
        read(store, one_key(store))
