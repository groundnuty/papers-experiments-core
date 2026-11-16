"""The spend journal: a line for every change of what was spent, written after its commit and before the spend (O62)."""

import json

import pytest
from campaign_support import NOW, finished, planned_store

from expcore.campaign.attempts import AttemptRefused
from expcore.campaign.probe_spend import add as add_probe_charge
from expcore.campaign.spend_journal import SpendJournal, SpendJournalError, read_journal

CAMPAIGN, LABEL = "c" * 64, "fed-a"
MODEL = "m1"


def journaled(tmp_path, name="j1.jsonl"):
    """A store of a small plan with a journal attached, the journal and the first run's key."""
    store, _, manifest = planned_store(tmp_path / "store")
    (tmp_path / "journals").mkdir(exist_ok=True)
    journal = SpendJournal(tmp_path / "journals" / name, CAMPAIGN, LABEL, NOW)
    store.spend_journal = journal
    return store, journal, manifest.rows[0].key.text


def lines(journal):
    return [json.loads(line) for line in journal.path.read_text(encoding="utf-8").splitlines()]


def test_o62_each_change_of_an_attempt_is_a_line_after_its_commit(tmp_path):
    """O62: the begin, a top-up and the end of an attempt are each one line, with the campaign and the shard."""
    store, journal, key = journaled(tmp_path)
    begun = store.begin_attempt(key, NOW, 4321, str(tmp_path / "try1"), reserved_capped=True)
    store.add_topup(begun.id, 0.25, NOW, ("topup_charged", MODEL, "a resend"))
    store.finish_attempt(begun.id, finished(input_tokens=100, output_tokens=20), 3)
    header, began, topped, ended = lines(journal)
    assert header["kind"] == "header"
    assert header["format"] == "expcore-spend-journal/1"
    assert {entry["campaign"] for entry in (header, began, topped, ended)} == {CAMPAIGN}
    assert {entry["shard"] for entry in (header, began, topped, ended)} == {LABEL}
    assert (began["id"], began["run_key"], began["model"], began["pid"]) == (begun.id, key, MODEL, 4321)
    assert (began["state"], began["reserved_capped"], began["topups"]) == ("running", 1, 0)
    assert (topped["topups"], topped["topup_usd"]) == (1, 0.25)
    assert (ended["state"], ended["input_tokens"], ended["output_tokens"]) == ("done", 100, 20)


def test_o62_a_transaction_that_rolls_back_writes_no_line(tmp_path):
    """O62: a begin the store refuses, and a finish whose own step fails, leave nothing in the journal."""
    store, journal, key = journaled(tmp_path)
    begun = store.begin_attempt(key, NOW, 4321, str(tmp_path / "try1"))
    with pytest.raises(AttemptRefused):  # the run already has a running attempt: the insert rolls back
        store.begin_attempt(key, NOW, 4321, str(tmp_path / "try2"))

    def fails():
        raise RuntimeError("the unit's accounting failed")

    with pytest.raises(RuntimeError):
        store.finish_attempt(begun.id, finished(), 3, also=fails)
    kinds = [(entry["kind"], entry.get("state")) for entry in lines(journal)]
    assert kinds == [("header", None), ("attempt", "running")]
    assert store.q("SELECT state FROM attempts")[0]["state"] == "running"


def test_o62_a_journal_that_cannot_be_written_stops_the_spend_and_every_later_one(tmp_path):
    """O62: a write that fails raises after the commit, before the caller can start the run, and so does every later
    write, even once the file could be written again."""
    store, journal, key = journaled(tmp_path)
    saved, journal._fd = journal._fd, 1 << 20  # the shared filesystem is gone: every write fails (EBADF)
    with pytest.raises(SpendJournalError, match="cannot be written"):
        store.begin_attempt(key, NOW, 4321, str(tmp_path / "try1"))
    journal._fd = saved  # back again: the journal stays broken
    begun_id = store.q("SELECT id FROM attempts")[0]["id"]
    with pytest.raises(SpendJournalError, match="an earlier write failed"):
        store.add_topup(begun_id, 0.25, NOW, ("topup_charged", MODEL, "a resend"))
    assert [entry["kind"] for entry in lines(journal)] == ["header"]


def test_o62_a_probe_charge_names_the_event_the_store_wrote_with_it(tmp_path):
    """O62: a probe's charge and its settlement are lines naming their events."""
    store, journal, _ = journaled(tmp_path)
    add_probe_charge(store, MODEL, 0.32, NOW, ("probe_charged", MODEL, "the one-token completion"))
    add_probe_charge(store, MODEL, -0.3, NOW, ("probe_settled", MODEL, "the one-token completion"))
    events = {r["id"]: r["kind"] for r in store.q("SELECT id, kind FROM events")}
    probes = [entry for entry in lines(journal) if entry["kind"] == "probe"]
    assert [(events[p["event_id"]], p["model"], p["usd"]) for p in probes] == [
        ("probe_charged", MODEL, 0.32),
        ("probe_settled", MODEL, -0.3),
    ]


def test_o62_a_store_without_a_journal_writes_none(tmp_path):
    """O62: no journal, no line and nothing else changes."""
    store, _, manifest = planned_store(tmp_path)
    store.begin_attempt(manifest.rows[0].key.text, NOW, 4321, str(tmp_path / "try1"))
    assert store.spend_journal is None
    assert not list(tmp_path.glob("*.jsonl"))


def test_o62_a_journal_is_created_new(tmp_path):
    """O62: each start writes its own journal: an existing file is never appended to."""
    SpendJournal(tmp_path / "j1.jsonl", CAMPAIGN, LABEL, NOW).close()
    with pytest.raises(SpendJournalError, match="exists"):
        SpendJournal(tmp_path / "j1.jsonl", CAMPAIGN, LABEL, NOW)


def test_o62_a_line_cut_short_by_a_kill_is_read_as_not_written(tmp_path):
    """O62: the last line, cut short, allowed no spend; the lines before it are read."""
    store, journal, key = journaled(tmp_path)
    store.begin_attempt(key, NOW, 4321, str(tmp_path / "try1"))
    with open(journal.path, "ab") as handle:
        handle.write(b'{"kind": "attempt", "id": 2, "run')
    read = read_journal(journal.path, CAMPAIGN, LABEL)
    assert list(read.attempts) == [1]


def test_o62_a_journal_whose_header_was_cut_short_holds_nothing(tmp_path):
    """O62: a start killed while it created its journal spent nothing."""
    (tmp_path / "j1.jsonl").write_bytes(b'{"kind": "hea')
    read = read_journal(tmp_path / "j1.jsonl", CAMPAIGN, LABEL)
    assert (dict(read.attempts), tuple(read.probes)) == ({}, ())


@pytest.mark.parametrize(
    ("campaign", "shard", "named"),
    [("d" * 64, LABEL, "another campaign"), (CAMPAIGN, "fed-b", "of shard 'fed-b'")],
)
def test_o62_a_journal_of_another_campaign_or_shard_is_refused(tmp_path, campaign, shard, named):
    """O62: reading for one campaign and shard, a journal of another is refused, naming which."""
    SpendJournal(tmp_path / "j1.jsonl", campaign, shard, NOW).close()
    with pytest.raises(SpendJournalError, match=named):
        read_journal(tmp_path / "j1.jsonl", CAMPAIGN, LABEL)


def test_o62_a_damaged_line_before_the_last_is_refused(tmp_path):
    """O62: only the last line can be cut short; a bad line before it is never skipped."""
    store, journal, key = journaled(tmp_path)
    with open(journal.path, "ab") as handle:
        handle.write(b"not json\n")
    store.begin_attempt(key, NOW, 4321, str(tmp_path / "try1"))
    with pytest.raises(SpendJournalError, match="line 2 is not a journal line"):
        read_journal(journal.path, CAMPAIGN, LABEL)
