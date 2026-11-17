"""What a kill took out of a store, counted back from the spend journal once, and charged by the budget (O62)."""

import json
import sqlite3

import pytest
from campaign_support import NOW, finished, make_schema, planned_store
from scheduler_support import build, models_for

from expcore.campaign.attempt_bounds import LOST_STATE, bound_usd
from expcore.campaign.export import export_run_state
from expcore.campaign.export_join import join_exports
from expcore.campaign.gate import Budget, BudgetGate
from expcore.campaign.lost_ledger import KEY, LostSpendError, read
from expcore.campaign.lost_spend import Pricing, costs, open_journal
from expcore.campaign.probe_spend import add as add_probe_charge
from expcore.campaign.reply_cap import is_unheld, mark_unheld, unheld
from expcore.campaign.selection_ledger import Pricing as SpendPricing
from expcore.campaign.selection_ledger import spend_of
from expcore.campaign.spend_journal import SpendJournal, SpendJournalError
from expcore.campaign.store import Store
from expcore.config.models import Limits
from expcore.config.prices import Price
from expcore.report import _table
from expcore.report_lost import HEADING, section

MODEL = "m1"
TABLE = models_for(MODEL, price=Price(3.0, 15.0), context_tokens=16_000)
SPEC = TABLE.resolve(MODEL)
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=300.0, token_budget=100_000, output_cap_chars=200, max_output_tokens=4096
)
PRICING = Pricing({MODEL: SPEC}, LIMITS)
BOUND = bound_usd(SPEC, LIMITS, reply_capped=False)  # 1.74: the cap and the window at 15 USD per million
MEASURED = (1000 * 3.0 + 200 * 15.0) / 1e6  # an attempt that reported 1,000 tokens in and 200 out
CAMPAIGN, LABEL = "c" * 64, "fed-a"


def start(store, tmp_path, name, *, campaign=CAMPAIGN, shard=LABEL):
    """A start of the store with a journal of its own: what it counted of the journals beside it."""
    (tmp_path / "journals").mkdir(exist_ok=True)
    return open_journal(store, tmp_path / "journals" / name, campaign, shard, NOW, PRICING)


def copy_of(store, tmp_path, name):
    """A consistent copy of the live store, opened as a store of its own: what a snapshot restores."""
    target = tmp_path / name
    target.mkdir()
    with sqlite3.connect(target / "campaign.db") as copy:
        store.db.backup(copy)
    return Store(target / "campaign.db", make_schema())


def killed_after_a_copy(tmp_path):
    """A first start that journals: attempt 1 begins before the copy; after it, attempt 1 takes a top-up, attempt 2
    of another run begins and ends, attempt 3 begins, and a probe is charged. Returns the live store, the copy and
    the run keys."""
    live, _, manifest = planned_store(tmp_path / "live")
    keys = [row.key.text for row in manifest.rows]
    start(live, tmp_path, "j1.jsonl")
    first = live.begin_attempt(keys[0], NOW, 4321, "try1")
    copy = copy_of(live, tmp_path, "copy")
    live.add_topup(first.id, 0.24, NOW, ("topup_charged", MODEL, "a resend"))
    second = live.begin_attempt(keys[1], NOW, 4321, "try1")
    live.finish_attempt(second.id, finished(input_tokens=1000, output_tokens=200), 3)
    live.begin_attempt(keys[2], NOW, 4321, "try1")
    add_probe_charge(live, MODEL, 0.24, NOW, ("probe_charged", MODEL, "the one-token completion"))
    return live, copy, keys


def test_o62_what_the_copy_lacks_of_its_own_journal_is_lost(tmp_path):
    """O62: of the store's own journal, an attempt that began after the copy is lost, at its measured cost when it
    ended and its bound when it did not; a top-up past the row's and a probe charge the copy lacks are lost too; the
    attempt the copy holds is the copy's."""
    _, copy, keys = killed_after_a_copy(tmp_path)
    counted = start(copy, tmp_path, "j2.jsonl")
    assert counted == {"journals": 1, "attempts": 2, "charges_usd": {MODEL: pytest.approx(0.48)}}
    lost = read(copy).counted["j1.jsonl"]
    assert [(a["run_key"], a["state"], a["usd"]) for a in lost["attempts"]] == [
        (keys[1], "done", pytest.approx(MEASURED)),
        (keys[2], "running", pytest.approx(BOUND)),
    ]
    assert [(c["kind"], c.get("run_key"), c["usd"]) for c in lost["charges"]] == [
        ("topup", keys[0], pytest.approx(0.24)),
        ("probe", None, pytest.approx(0.24)),
    ]
    assert read(copy).journal == "j2.jsonl"


def test_o62_the_budget_counts_what_was_lost_against_its_ceiling(tmp_path):
    """O62: the gate's spend holds the lost attempts and charges beside the store's own attempt (in flight: its
    reservation is the dispatcher's), and a ceiling they fill holds the next run."""
    _, copy, _ = killed_after_a_copy(tmp_path)
    start(copy, tmp_path, "j2.jsonl")
    gate = BudgetGate(copy, {MODEL: SPEC}, Budget(max_usd=4.0), LIMITS)
    state = gate.assess([], NOW, live=set())  # the copy's running attempt has no worker: charged its bound
    assert state.spent_usd == pytest.approx(BOUND + MEASURED + BOUND + 0.48)
    assert "over the ceiling" in (state.reason(MODEL) or "")
    lost = [c for c in costs(read(copy), {MODEL: SPEC}, LIMITS) if c.state == LOST_STATE]
    assert sum(c.usd for c in lost) == pytest.approx(MEASURED + BOUND + 0.48)


def test_o62_what_a_selection_spent_holds_what_was_lost(tmp_path):
    """O62: `spend_of`, the figure a merge of selections and a later selection read, counts it too."""
    _, copy, _ = killed_after_a_copy(tmp_path)
    start(copy, tmp_path, "j2.jsonl")
    spent = spend_of(copy.q, SpendPricing({MODEL: SPEC}, LIMITS))  # the copy's running attempt: in flight there
    assert spent.total == pytest.approx(MEASURED + BOUND + 0.48)
    assert spent.per_model[MODEL] == pytest.approx(MEASURED + BOUND + 0.48)


def test_o62_a_journal_is_counted_once_across_two_resumes(tmp_path):
    """O62: a second start of the resumed store counts nothing again; a start of the copy taken before the first
    resume counted counts the same once more, since that copy lacks it, and never twice."""
    _, copy, _ = killed_after_a_copy(tmp_path)
    before = copy_of(copy, tmp_path, "before")  # taken before the resume counted anything
    start(copy, tmp_path, "j2.jsonl")
    after = copy_of(copy, tmp_path, "after")  # taken after: it holds the count
    assert start(after, tmp_path, "j3.jsonl") == {"journals": 1, "attempts": 0, "charges_usd": {}}
    assert sorted(read(after).counted) == ["j1.jsonl", "j2.jsonl"]
    again = start(before, tmp_path, "j4.jsonl")
    assert (again["attempts"], again["charges_usd"]) == (2, {MODEL: pytest.approx(0.48)})
    for store in (after, before):
        lost = list(costs(read(store), {MODEL: SPEC}, LIMITS))
        assert sum(c.usd for c in lost) == pytest.approx(MEASURED + BOUND + 0.48)


def test_o62_a_journal_whose_store_never_reached_the_copy_is_lost_whole(tmp_path):
    """O62: a start that resumed an older copy and died before its store was copied left a journal the copy restored
    now has not counted: every attempt of it is lost, matched to nothing, even one whose id a row of the copy has."""
    _, copy, keys = killed_after_a_copy(tmp_path)
    old = copy_of(copy, tmp_path, "old")  # an older snapshot
    start(copy, tmp_path, "j2.jsonl")
    held = copy.begin_attempt(keys[3], NOW, 4321, "try1")
    snapshot = copy_of(copy, tmp_path, "snapshot")  # the newest snapshot: its own journal is j2
    start(old, tmp_path, "j3.jsonl")  # a start from the older snapshot, killed before any copy of it
    doomed = old.begin_attempt(keys[3], NOW, 999, "try1")
    assert doomed.id == held.id
    start(snapshot, tmp_path, "j4.jsonl")
    counted = read(snapshot).counted
    assert sorted(counted) == ["j1.jsonl", "j2.jsonl", "j3.jsonl"]
    assert counted["j2.jsonl"]["attempts"] == []  # the snapshot's own row
    assert [(a["id"], a["state"]) for a in counted["j3.jsonl"]["attempts"]] == [(doomed.id, "running")]


def test_o62_a_rolled_back_attempt_is_never_counted_as_lost(tmp_path):
    """O62: a begin refused in its transaction leaves no line, so a resume counts nothing for it."""
    live, _, manifest = planned_store(tmp_path / "live")
    key = manifest.rows[0].key.text
    start(live, tmp_path, "j1.jsonl")
    copy = copy_of(live, tmp_path, "copy")
    live.begin_attempt(key, NOW, 4321, "try1")
    copy.begin_attempt(key, NOW, 4321, "try1")  # the copy holds it too, under the same id
    with pytest.raises(Exception, match="already has a running attempt"):
        live.begin_attempt(key, NOW, 4321, "try2")
    assert start(copy, tmp_path, "j2.jsonl")["attempts"] == 0


@pytest.mark.parametrize(
    ("campaign", "shard", "named"), [("d" * 64, LABEL, "another campaign"), (CAMPAIGN, "fed-b", "shard")]
)
def test_o62_a_journal_of_another_campaign_or_shard_is_refused_and_changes_nothing(tmp_path, campaign, shard, named):
    """O62: a start whose journal directory holds a journal of another campaign or shard is refused, naming it, and
    neither its journal nor its ledger is left changed."""
    _, copy, _ = killed_after_a_copy(tmp_path)
    SpendJournal(tmp_path / "journals" / "foreign.jsonl", campaign, shard, NOW).close()
    with pytest.raises(SpendJournalError, match=f"foreign.jsonl: .*{named}"):
        start(copy, tmp_path, "j2.jsonl")
    assert read(copy).journal == "j1.jsonl"
    assert read(copy).counted == {}
    assert not (tmp_path / "journals" / "j2.jsonl").exists()


def test_o62_a_start_of_another_shard_on_this_store_is_refused(tmp_path):
    """O62: the store's own journal is of shard fed-a: a start as fed-b reads it as another shard's and stops."""
    _, copy, _ = killed_after_a_copy(tmp_path)
    with pytest.raises(SpendJournalError, match=r"j1\.jsonl: line 1 is of shard 'fed-a', not 'fed-b'"):
        start(copy, tmp_path, "j2.jsonl", shard="fed-b")
    assert read(copy).journal == "j1.jsonl"


def test_o62_the_same_label_in_another_partition_is_another_shard(tmp_path):
    """O62: a journal is of a shard's whole spec (rule, index, count and label), so the same label in a partition of
    another count is refused, and its spend never charged to the new partition's share."""
    one_of_two = '{"count":2,"index":1,"label":"fed-a","rule":"expcore-shard/2"}'
    one_of_three = '{"count":3,"index":1,"label":"fed-a","rule":"expcore-shard/2"}'
    old, _, manifest = planned_store(tmp_path / "old")
    start(old, tmp_path, "j1.jsonl", shard=one_of_two)
    old.begin_attempt(manifest.rows[0].key.text, NOW, 4321, "try1")
    new, _, _ = planned_store(tmp_path / "new")
    with pytest.raises(SpendJournalError, match=r"j1\.jsonl: line 1 is of shard"):
        start(new, tmp_path, "j2.jsonl", shard=one_of_three)


def test_o62_a_model_a_lost_start_took_off_the_reply_maximum_is_off_it_in_the_copy(tmp_path):
    """O62: a model taken off the reply maximum after the copy raises the bound its attempts are charged; the copy
    put back takes the mark from the journal, so the attempt it holds and the one it lost are charged by the window."""
    live, _, manifest = planned_store(tmp_path / "live")
    keys = [row.key.text for row in manifest.rows]
    start(live, tmp_path, "j1.jsonl")
    live.begin_attempt(keys[0], NOW, 4321, "try1", reserved_capped=True)
    copy = copy_of(live, tmp_path, "copy")
    mark_unheld(live, MODEL, NOW, "reply_over_maximum", "a reply of 5000 output tokens")
    live.begin_attempt(keys[1], NOW, 4321, "try1", reserved_capped=True)
    start(copy, tmp_path, "j2.jsonl")
    assert is_unheld(unheld(copy), MODEL)
    (lost,) = read(copy).counted["j1.jsonl"]["attempts"]
    window, capped = (bound_usd(SPEC, LIMITS, reply_capped=held) for held in (False, True))
    assert window > capped
    assert lost["usd"] == pytest.approx(window)
    spent = BudgetGate(copy, {MODEL: SPEC}, Budget(max_usd=10.0), LIMITS).assess([], NOW, live=set()).spent_usd
    assert spent == pytest.approx(2 * window)


def test_o62_a_plan_that_grew_reads_its_earlier_journals_as_its_own(tmp_path):
    """O62: a grown plan records a new fingerprint (C06); the journals keep the campaign of the first start."""
    store, _, manifest = planned_store(tmp_path / "store")
    start(store, tmp_path, "j1.jsonl")
    store.begin_attempt(manifest.rows[0].key.text, NOW, 4321, "try1")
    store.spend_journal.close()
    start(store, tmp_path, "j2.jsonl", campaign="e" * 64)  # the fingerprint the grown plan recorded
    assert read(store).campaign == CAMPAIGN
    header = json.loads((tmp_path / "journals" / "j2.jsonl").read_text().splitlines()[0])
    assert header["campaign"] == CAMPAIGN


def test_o62_a_store_whose_own_journal_is_gone_is_refused(tmp_path):
    """O62: what the store's last start spent after the copy cannot be known without its journal."""
    _, copy, _ = killed_after_a_copy(tmp_path)
    (tmp_path / "journals" / "j1.jsonl").unlink()
    with pytest.raises(LostSpendError, match=r"own spend journal j1\.jsonl is not in the journal directory"):
        start(copy, tmp_path, "j2.jsonl")


def test_o62_a_ledger_that_cannot_be_read_is_refused_and_holds_every_model(tmp_path):
    """O62: a damaged record is never overwritten, and the gate takes the total spent as unknown."""
    store, _, _ = planned_store(tmp_path / "store")
    store.put_meta_many({KEY: "not json"}, NOW, [])
    with pytest.raises(LostSpendError, match="cannot be read"):
        start(store, tmp_path, "j1.jsonl")
    state = BudgetGate(store, {MODEL: SPEC}, Budget(max_usd=4.0), LIMITS).assess([], NOW)
    assert state.spent_usd is None
    assert KEY in (state.reason(MODEL) or "")


def test_o62_the_export_and_the_report_show_what_was_lost(tmp_path):
    """O62: the export lists each lost attempt and charge; a merged export names each one's shard; the report has a
    section of them. An export of a store that lost nothing has neither."""
    _, copy, keys = killed_after_a_copy(tmp_path)
    clean = copy_of(copy, tmp_path, "clean")
    assert KEY not in export_run_state(clean)
    start(copy, tmp_path, "j2.jsonl")
    entries = export_run_state(copy)[KEY]
    assert [(e["kind"], e.get("run_key"), e.get("ended")) for e in entries] == [
        ("attempt", keys[1], True),
        ("attempt", keys[2], False),
        ("topup", keys[0], None),
        ("probe", None, None),
    ]
    assert entries[0]["usd"] == pytest.approx(MEASURED)
    assert entries[0]["journal"] == "j1.jsonl"
    lines = section({KEY: entries}, _table)
    assert lines[0] == HEADING
    assert any(f"{MEASURED:.6f}" in line for line in lines)
    assert section({}, _table) == []


def test_o62_a_merged_export_names_the_shard_of_each_loss(tmp_path):
    """O62: the join keeps each shard's entries, in shard order, with the shard's index."""
    base = {"format": "campaign-export-1", "factors": [], "planned_runs": 0, "exhausted_runs": [],
            "unfinished_runs": [], "analysed_attempts": [], "discarded_usage": [], "retries_by_kind": [],
            "infrastructure_by_condition": [], "budget_holds": [], "kinds": []}  # fmt: skip
    entry = {"journal": "j1.jsonl", "kind": "probe", "model": MODEL, "usd": 0.24}
    joined = join_exports({1: base, 2: {**base, KEY: [entry]}}, {}, [])
    assert joined[KEY] == [{**entry, "shard": 2}]
    assert list(joined).index(KEY) == list(joined).index("kinds") + 1


def test_o62_a_dispatcher_journals_its_spend_and_its_next_start_counts_none_it_holds(tmp_path):
    """O62: a campaign started with a journal writes each attempt's begin and end; a later start of the same store,
    which holds all of it, counts nothing lost and records its own journal."""
    (tmp_path / "journals").mkdir()
    with build(tmp_path / "out", spend_journal=tmp_path / "journals" / "j1.jsonl") as scheduler:
        scheduler.run()
        attempts = scheduler.store.q("SELECT id FROM attempts")
    entries = [json.loads(line) for line in (tmp_path / "journals" / "j1.jsonl").read_text().splitlines()]
    states = {}
    for entry in entries[1:]:
        states.setdefault(entry["id"], []).append(entry["state"])
    assert sorted(states) == sorted(row["id"] for row in attempts)
    assert attempts
    assert all(seen[0] == "running" and seen[-1] != "running" for seen in states.values())
    assert {entry["shard"] for entry in entries} == {None}
    with build(tmp_path / "out", spend_journal=tmp_path / "journals" / "j2.jsonl") as scheduler:
        assert scheduler.lost_counted == {"journals": 1, "attempts": 0, "charges_usd": {}}
        assert read(scheduler.store).journal == "j2.jsonl"


def test_o62_the_ledger_is_one_json_record(tmp_path):
    """O62: what a start counted stands in the store's meta, with the journal it writes."""
    _, copy, _ = killed_after_a_copy(tmp_path)
    start(copy, tmp_path, "j2.jsonl")
    record = json.loads(copy.get_meta(KEY))
    assert set(record) == {"campaign", "journal", "counted"}
    assert (record["campaign"], record["journal"]) == (CAMPAIGN, "j2.jsonl")
    assert [kind for (kind,) in copy.q("SELECT kind FROM events WHERE kind='lost_in_kill'")] == ["lost_in_kill"]
