"""Opening one new database from many connections at once (C20): the switch to WAL and what follows it."""

import multiprocessing
import sqlite3
import time

import pytest
from campaign_support import make_schema, open_in_rounds, open_store

from expcore.campaign.store import Store


def test_c20_sixteen_processes_opening_new_databases_together_all_get_wal_and_full_synchronous(tmp_path):
    """C20: adds the switch to WAL itself: it ignores the busy timeout, so openers that start together must retry it."""
    processes, rounds = 16, 20
    context = multiprocessing.get_context("spawn")  # a fresh interpreter each, as separate campaign processes are
    barrier, results = context.Barrier(processes), context.Queue()
    children = [
        context.Process(target=open_in_rounds, args=(str(tmp_path), rounds, barrier, results)) for _ in range(processes)
    ]
    try:
        for child in children:
            child.start()
        reports = [results.get(timeout=300) for _ in range(processes * rounds)]
        for child in children:
            child.join(timeout=60)
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
    assert [r for r in reports if r[1] != ("wal", 2, None)] == []
    assert sorted(r[0] for r in reports) == sorted(index for index in range(rounds) for _ in range(processes))
    assert [child.exitcode for child in children] == [0] * processes


def test_c20_a_write_lock_held_when_the_open_begins_is_waited_out_and_the_database_ends_in_wal(tmp_path, monkeypatch):
    """C20: adds that a concurrent opener's write lock, which SQLite does not make the open wait for, is retried."""
    path = tmp_path / "campaign.db"
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    pauses = []

    def release_on_first_pause(seconds):
        pauses.append(seconds)
        if holder.in_transaction:
            holder.execute("ROLLBACK")

    monkeypatch.setattr(time, "sleep", release_on_first_pause)  # the open's pause between two tries
    store = Store(path, make_schema())
    holder.close()
    assert pauses  # the first try met the lock and waited
    assert store.q("PRAGMA journal_mode")[0][0] == "wal"
    assert store.q("PRAGMA synchronous")[0][0] == 2  # FULL


def test_c20_an_open_that_cannot_switch_to_wal_in_time_fails_naming_the_database(tmp_path):
    """C20: adds that the open fails loudly, with the database's path, if another connection holds the lock too long.

    The wait is the open's own, bounded by the timeout it is given.
    """
    path = tmp_path / "campaign.db"
    holder = sqlite3.connect(str(path), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    with pytest.raises(sqlite3.OperationalError, match="could not switch the database to WAL") as raised:
        Store(path, make_schema(), lock_timeout_s=0.2)
    assert str(path) in str(raised.value)
    holder.execute("ROLLBACK")
    holder.close()
    open_store(tmp_path).close()  # once the lock is free, the same database opens
