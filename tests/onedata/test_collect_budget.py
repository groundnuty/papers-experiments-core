"""The fallback collection's budget: when it is due, that it counts again from each collection, and that it is exact
under threads (O47, #259). How a client uses it is tested with the client, in `placement/test_http_bodies.py`."""

import gc
import threading

import pytest

from expcore_onedata.collectbudget import COLLECT_AFTER_BYTES, CollectBudget, check_budget


def counting(budget_bytes):
    runs = []
    return CollectBudget(budget_bytes, collect=lambda: runs.append(1)), runs


def test_o47_nothing_is_collected_until_the_budget_is_spent_and_then_only_before_the_next_exchange():
    """O47: the bodies a client moved are counted as they are spent, and a collection is owed from the moment the
    count reaches the budget but runs only when an exchange begins."""
    budget, runs = counting(10)
    budget.before_exchange()
    budget.spent(9)
    budget.before_exchange()
    assert runs == []
    budget.spent(1)
    assert runs == []  # spending the budget runs nothing: the exchange that spent it is not over
    budget.before_exchange()
    assert runs == [1]


def test_o47_the_count_begins_again_at_each_collection():
    """O47: a collection settles what was spent before it, so the next one is a whole budget away."""
    budget, runs = counting(10)
    budget.spent(25)
    budget.before_exchange()
    budget.before_exchange()
    assert runs == [1]
    budget.spent(10)
    budget.before_exchange()
    assert runs == [1, 1]


def test_o47_a_budget_of_none_never_collects():
    """O47: a caller that collects itself turns the fallback off."""
    budget, runs = counting(None)
    budget.spent(10**12)
    budget.before_exchange()
    assert runs == []


def test_o47_the_collection_is_gcs_unless_a_caller_counts_its_own(monkeypatch):
    """O47: by default the collection is `gc.collect`, a full one, looked up when the budget is built."""
    calls = []
    monkeypatch.setattr(gc, "collect", lambda *args: calls.append(args))
    budget = CollectBudget(1)
    budget.spent(1)
    budget.before_exchange()
    assert calls == [()]


def test_o47_threads_spending_together_are_counted_exactly():
    """O47: one client serves several threads, so eight threads that spend 1000 bytes each, a byte at a time, reach a
    budget of 8000 exactly, and the one collection that follows is the only one."""
    budget, runs = counting(8000)

    def spend():
        for _ in range(1000):
            budget.spent(1)

    threads = [threading.Thread(target=spend) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    budget.before_exchange()
    budget.before_exchange()
    assert runs == [1]


@pytest.mark.parametrize("bad", [0, -1, True, False, 1.5, "1"])
def test_o47_a_budget_that_is_no_positive_whole_number_is_refused(bad):
    """O47: a budget of nothing would collect before every exchange, and a bool or a float is a mistake."""
    with pytest.raises(ValueError, match="collect_after_bytes"):
        check_budget(bad)


def test_o47_the_default_budget_is_a_positive_whole_number_of_bytes():
    """O47: the default is checked as any other would be."""
    assert check_budget(COLLECT_AFTER_BYTES) == COLLECT_AFTER_BYTES == 128 * 1024 * 1024
    assert check_budget(None) is None
