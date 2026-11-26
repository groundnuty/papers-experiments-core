"""`recover-run` on a run whose old leases were settled from their dead stores: each holds `charged_added` above 0 and
the ledger holds a settlement row for it, so a recovery that wrote to either, to the money the leases hold, or to an
earlier row of the log would show. Its only writes are the run's state and charged count and one appended event."""

import pytest
from elastic_support import join, leased
from recover_support import (
    assert_only_the_run_and_one_event_changed,
    charged_added_of,
    event_detail,
    lost_and_settled,
    open_ledger,
)
from recover_support import recover_found as _recover
from reopen_support import dump, sha256_of

from expcore.campaign.elastic import LedgerReader
from expcore.campaign.ledger_read import read_ledger, take_ledger_snapshot


def _status(path):
    reader = LedgerReader(path)
    try:
        return reader.status()
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("state", "lost", "charged", "rule"),
    [
        ("infra_exhausted", 3, 0, "waiver"),
        ("infra_exhausted", 3, 2, "waiver"),
        ("harness_error", 1, 1, "declared"),
        ("harness_error", 1, 0, "waiver"),
    ],
)
def test_a_recovery_writes_the_runs_row_and_one_event_and_nothing_else_of_a_ledger_with_settled_leases(
    tmp_path, state, lost, charged, rule
):
    found = lost_and_settled(tmp_path, state=state)
    before, final, money, added = (
        dump(found.path),
        sha256_of(found.source),
        _status(found.path),
        charged_added_of(found.path),
    )
    assert len(before["settlements"]) == lost  # the old leases were settled ...
    assert [added[lease] for lease in found.leases[:lost]] == [1] * lost  # ... and each was counted for an attempt
    done = _recover(found, charged=charged)
    after = dump(found.path)
    assert (done.rule, done.leases) == (rule, found.leases)
    assert_only_the_run_and_one_event_changed(found, before, after)
    assert after["settlements"] == before["settlements"] != []
    assert charged_added_of(found.path) == added
    assert _status(found.path) == money  # what the leases hold and what is committed are as they were
    assert sha256_of(found.source) == final
    assert event_detail(found.path)["leases"] == list(found.leases)


@pytest.mark.parametrize("charged", [0, 2])
def test_a_run_recovered_after_its_leases_were_settled_is_leased_with_the_chosen_count_and_no_hold(tmp_path, charged):
    found = lost_and_settled(tmp_path)
    _recover(found, charged=charged)
    ledger = open_ledger(found)
    try:
        assert ledger.holds() == {}
        join(ledger, found.clock, "fed-next")
        again = leased(ledger, "fed-next")
        assert (again.run_key, again.attempt_base) == (found.run_key, charged)
    finally:
        ledger.close()


def test_the_merges_replay_accepts_a_recovered_ledger_whose_old_leases_were_settled(tmp_path):
    found = lost_and_settled(tmp_path)
    _recover(found, charged=0)
    snapshot = tmp_path / "ledger-snapshot.sqlite"
    take_ledger_snapshot(found.path, snapshot)
    facts = read_ledger(snapshot.read_bytes(), "0" * 64)
    assert facts.runs[found.run_key].state == "pending"
    assert sorted(facts.settlements) == list(found.leases)
