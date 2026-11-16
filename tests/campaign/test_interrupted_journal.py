"""The call log in the spend journal: an attempt a kill interrupted on a host whose store was put back from a snapshot,
or lost with it, is charged what the journal recorded of its requests and replies (C05, O62)."""

import json
import sqlite3

import pytest
from campaign_support import NOW, make_schema, planned_store
from scheduler_support import models_for

from expcore.campaign.attempt_bounds import LOST_STATE, attempt_costs, bound_usd, request_bound_usd
from expcore.campaign.export import export_run_state
from expcore.campaign.export_join import join_exports
from expcore.campaign.interrupted_spend import BOUND, EXPORT_KEY, INFLIGHT, MEASURED
from expcore.campaign.lost_ledger import KEY, read
from expcore.campaign.lost_spend import Pricing, costs, open_journal
from expcore.campaign.recovery import recover
from expcore.campaign.snapshot import rebase_run_dirs
from expcore.campaign.spend_journal import SpendJournalError
from expcore.campaign.store import Store
from expcore.config.models import Limits
from expcore.config.prices import Price
from expcore.report import _table
from expcore.report_interrupted import HEADING, section
from expcore.report_lost import section as lost_section

MODEL = "m1"
SPEC = models_for(MODEL, price=Price(3.0, 15.0), context_tokens=16_000).resolve(MODEL)
LIMITS = Limits(
    max_tool_rounds=10, time_cap_s=300.0, token_budget=100_000, output_cap_chars=200, max_output_tokens=4096
)
PRICING = Pricing({MODEL: SPEC}, LIMITS)
BOUND_USD = bound_usd(SPEC, LIMITS)  # 1.74: the cap and the window at 15 USD per million
REQUEST_USD = request_bound_usd(SPEC, LIMITS)  # 0.24: the window at 15
REPLY = (1_000, 200, 0, 0)
REPLY_USD = (1_000 * 3.0 + 200 * 15.0) / 1e6
CAMPAIGN, LABEL = "c" * 64, "fed-a"


def start(store, tmp_path, name):
    (tmp_path / "journals").mkdir(exist_ok=True)
    return open_journal(store, tmp_path / "journals" / name, CAMPAIGN, LABEL, NOW, PRICING)


def copy_of(store, tmp_path, name):
    target = tmp_path / name
    target.mkdir()
    with sqlite3.connect(target / "campaign.db") as copy:
        store.db.backup(copy)
    return Store(target / "campaign.db", make_schema())


def calls(store, attempt_id, *steps):
    for step in steps:
        if step == "sent":
            store.note_call_sent(attempt_id)
        else:
            store.note_call_answered(attempt_id, step)


def interrupted_cost(store):
    [cost] = [c for c in attempt_costs(store, {MODEL: SPEC}, LIMITS) if c.state == "interrupted"]
    return cost


def killed_mid_run_after_a_snapshot(tmp_path):
    """A journaled start: attempt 1 sends a request and has it answered before the snapshot, then two more requests
    answered and a fourth in flight when the host dies. Returns the live store, the snapshot and the attempt."""
    live, _, manifest = planned_store(tmp_path / "live")
    start(live, tmp_path, "j1.jsonl")
    begun = live.begin_attempt(manifest.rows[0].key.text, NOW, 4321, "try1", calls_logged=True)
    calls(live, begun.id, "sent", REPLY)
    snapshot = copy_of(live, tmp_path, "snapshot")
    calls(live, begun.id, "sent", REPLY, "sent", REPLY, "sent")
    return live, snapshot, begun


def journal_lines(tmp_path, name="j1.jsonl"):
    return [json.loads(line) for line in (tmp_path / "journals" / name).read_text().splitlines()[1:]]


def test_c05_the_journal_has_a_line_per_request_before_it_goes_out_and_per_reply_with_its_counts(tmp_path):
    """C05, O62: each request is a `call` line, fsynced after its commit and before the send it allows, and each
    reply a `call` line with its counts and cache parts; each carries the attempt's id and run, and the campaign and
    shard as every line does. The attempt line holds no log: it names how many call lines it follows."""
    _, _, begun = killed_mid_run_after_a_snapshot(tmp_path)
    mine = [line for line in journal_lines(tmp_path) if line["id"] == begun.id]
    assert [(line["kind"], line.get("phase"), line.get("entry")) for line in mine] == [
        ("attempt", None, None),
        ("call", "sent", None), ("call", "closed", list(REPLY)),
        ("call", "sent", None), ("call", "closed", list(REPLY)),
        ("call", "sent", None), ("call", "closed", list(REPLY)),
        ("call", "sent", None),
    ]  # fmt: skip
    assert all((line["campaign"], line["shard"]) == (CAMPAIGN, LABEL) for line in mine)
    assert all(line["run_key"] == mine[0]["run_key"] for line in mine)
    assert (mine[0]["calls_sent"], mine[0]["calls_closed"]) == (0, 0)
    assert "calls_usage" not in mine[0]


def test_c05_the_journal_grows_linearly_in_the_calls(tmp_path):
    """C05: each request and each reply adds one line of a size that does not grow with the calls before it, so an
    attempt of N requests adds about N times what one request adds, never N squared."""
    store, _, manifest = planned_store(tmp_path / "live")
    start(store, tmp_path, "j1.jsonl")
    begun = store.begin_attempt(manifest.rows[0].key.text, NOW, 4321, "try1", calls_logged=True)
    path = tmp_path / "journals" / "j1.jsonl"
    sizes = []
    for done in range(1, 121):
        calls(store, begun.id, "sent", (100_000 + done, 4_000, 1_000, 2_000))
        if done in (30, 60, 120):
            sizes.append(path.stat().st_size)
    first, second = sizes[1] - sizes[0], sizes[2] - sizes[1]  # 30 calls, then 60 calls
    assert second == pytest.approx(2 * first, rel=0.05)
    call_lines = [line for line in path.read_text().splitlines() if '"kind": "call"' in line]
    assert len(call_lines) == 2 * 120
    assert max(len(line) for line in call_lines) < 250


def test_c05_a_journal_line_of_a_call_whose_attempt_the_journal_never_began_or_of_another_run_is_refused(tmp_path):
    """C05, O62: a call line belongs to the attempt line of its id before it in the same journal, and to its run: one
    that names an attempt the journal never began, or another run, is a damaged journal, and the start is refused."""
    snapshot = None
    for case in ("unbegun", "another run"):
        root = tmp_path / case.replace(" ", "-")
        _, snapshot, begun = killed_mid_run_after_a_snapshot(root)
        path = root / "journals" / "j1.jsonl"
        lines = path.read_text().splitlines()
        forged = json.loads(lines[-1])
        forged.update(id=begun.id + 7) if case == "unbegun" else forged.update(run_key="another")
        path.write_text("\n".join([*lines[:-1], json.dumps(forged, sort_keys=True), lines[-1]]) + "\n")
        with pytest.raises(SpendJournalError, match="call line"):
            start(snapshot, root, "j2.jsonl")
    assert snapshot is not None


def test_c05_an_attempt_line_that_disagrees_with_its_call_lines_charges_the_bound(tmp_path):
    """C05: the attempt line names how many requests and replies the journal holds of it; a journal whose call lines
    say otherwise (a line the store committed and the journal lacks) is not trusted: the bound."""
    live, _, manifest = planned_store(tmp_path / "live")
    keys = [row.key.text for row in manifest.rows]
    start(live, tmp_path, "j1.jsonl")
    snapshot = copy_of(live, tmp_path, "snapshot")
    begun = live.begin_attempt(keys[1], NOW, 4321, "try1", calls_logged=True)
    calls(live, begun.id, "sent")
    journal, live.spend_journal = live.spend_journal, None  # a reply the store commits and the journal never gets
    calls(live, begun.id, REPLY)
    live.spend_journal = journal
    live.add_topup(begun.id, 0.24, NOW, ("topup", MODEL, "a request after a reply that left a count out"))
    start(snapshot, tmp_path, "j2.jsonl")
    [lost] = read(snapshot).counted["j1.jsonl"]["attempts"]
    assert (lost["basis"], lost["usd"]) == (BOUND, pytest.approx(BOUND_USD + 0.24))


@pytest.mark.parametrize("cut", [False, True])
def test_c05_a_failed_last_sends_top_up_and_close_are_one_journal_line_so_it_is_charged_once(tmp_path, cut):
    """C05, C26: the top-up of a request's last failed send and the close of that request are one `attempt` line
    (`closes`), so a write cut short loses both or neither: whole, the request costs its top-up; cut, it is in flight
    and costs one request's bound; once either way, never twice and never nothing."""
    live, _, manifest = planned_store(tmp_path / "live")
    start(live, tmp_path, "j1.jsonl")
    snapshot = copy_of(live, tmp_path, "snapshot")
    begun = live.begin_attempt(manifest.rows[0].key.text, NOW, 4321, "try1", calls_logged=True)
    calls(live, begun.id, "sent")
    live.add_topup(begun.id, REQUEST_USD, NOW, ("topup", MODEL, "the last failed send"), close_call=True)
    mine = [line for line in journal_lines(tmp_path) if line["id"] == begun.id]
    assert [(line["kind"], line.get("closes")) for line in mine[-2:]] == [("call", None), ("attempt", "failed")]
    assert (mine[-1]["topups"], mine[-1]["calls_sent"], mine[-1]["calls_closed"]) == (1, 1, 1)
    if cut:
        path = tmp_path / "journals" / "j1.jsonl"
        path.write_bytes(path.read_bytes()[:-40])
    start(snapshot, tmp_path, "j2.jsonl")
    [lost] = read(snapshot).counted["j1.jsonl"]["attempts"]
    assert (lost["basis"], lost["usd"], lost["topup_usd"]) == (
        INFLIGHT if cut else MEASURED,
        pytest.approx(REQUEST_USD),
        pytest.approx(0.0 if cut else REQUEST_USD),
    )


def test_c05_a_restored_snapshots_attempt_is_charged_by_its_own_journals_log_not_the_snapshots(tmp_path):
    """C05, O62: the snapshot's log stops at one reply, while the attempt went on to three and a fourth request in
    flight: recovery records the bound, and the start's count of the store's own journal gives it the journal's log,
    charged three replies and one request's bound, never the snapshot's one reply."""
    _, snapshot, begun = killed_mid_run_after_a_snapshot(tmp_path)
    recover(snapshot, NOW, 3)
    assert interrupted_cost(snapshot).basis == BOUND  # the ledger names j1: the snapshot's own log is not trusted
    start(snapshot, tmp_path, "j2.jsonl")
    [row] = snapshot.q("SELECT calls_sent, calls_usage, spend_basis FROM attempts WHERE id=?", (begun.id,))
    assert (row["calls_sent"], len(json.loads(row["calls_usage"])), row["spend_basis"]) == (4, 3, INFLIGHT)
    cost = interrupted_cost(snapshot)
    assert (cost.basis, cost.usd) == (INFLIGHT, pytest.approx(3 * REPLY_USD + REQUEST_USD))
    assert cost.usd >= 3 * REPLY_USD
    [event] = snapshot.q("SELECT detail FROM events WHERE kind='spend_basis_confirmed'")
    assert "measured_plus_inflight 1" in event["detail"]


def test_c05_a_restored_snapshot_resumed_without_its_journal_keeps_the_bound(tmp_path):
    """C05: a snapshot started without `--spend-journal` cannot confirm its log, which may be short: its bound."""
    _, snapshot, _ = killed_mid_run_after_a_snapshot(tmp_path)
    recover(snapshot, NOW, 3)
    cost = interrupted_cost(snapshot)
    assert (cost.basis, cost.usd) == (BOUND, pytest.approx(BOUND_USD))


def test_c05_a_store_copied_back_to_run_again_without_a_journal_does_not_trust_its_own_log(tmp_path):
    """C05: a store never journaled and copied while an attempt ran holds a log the attempt outgrew. Every copy put back
    to run again has its directories pointed at its new `runs/` (`rebase_run_dirs`, as a snapshot's restore and the
    shard driver's archive resume do), which marks it copied back: recovery then records the bound, not the copy's
    one reply."""
    live, _, manifest = planned_store(tmp_path / "live")
    begun = live.begin_attempt(manifest.rows[0].key.text, NOW, 4321, "try1", calls_logged=True)
    calls(live, begun.id, "sent", REPLY)
    (tmp_path / "copy").mkdir()
    with sqlite3.connect(tmp_path / "copy" / "campaign.db") as backup:
        live.db.backup(backup)
    calls(live, begun.id, "sent", REPLY)  # the attempt goes on after the copy
    rebase_run_dirs(tmp_path / "copy" / "campaign.db", tmp_path / "copy" / "runs")
    copy = Store(tmp_path / "copy" / "campaign.db", make_schema())
    recover(copy, NOW, 3)
    cost = interrupted_cost(copy)
    assert (cost.basis, cost.usd) == (BOUND, pytest.approx(BOUND_USD))
    recover(live, NOW, 3)  # the store that ran it, never copied, trusts its log
    assert interrupted_cost(live).basis == MEASURED


def test_c05_a_journal_that_disagrees_with_the_store_charges_the_bound(tmp_path):
    """C05: a log the journal holds that neither extends the store's nor is extended by it says nothing to trust."""
    _, snapshot, begun = killed_mid_run_after_a_snapshot(tmp_path)
    snapshot.db.execute("UPDATE attempts SET calls_usage='[[9, 9, 0, 0]]' WHERE id=?", (begun.id,))
    recover(snapshot, NOW, 3)
    start(snapshot, tmp_path, "j2.jsonl")
    cost = interrupted_cost(snapshot)
    assert (cost.basis, cost.usd) == (BOUND, pytest.approx(BOUND_USD))


def test_c05_a_live_store_ahead_of_its_journal_by_a_request_keeps_its_own_log(tmp_path):
    """C05: a store can lead its journal by the one line a failed write never journaled (nothing was sent after it):
    the store's longer log stands, the request it names counted in flight."""
    store, _, manifest = planned_store(tmp_path / "live")
    start(store, tmp_path, "j1.jsonl")
    begun = store.begin_attempt(manifest.rows[0].key.text, NOW, 4321, "try1", calls_logged=True)
    calls(store, begun.id, "sent", REPLY)
    store.spend_journal.close()
    store.spend_journal = None  # the journal write that failed: the store commits a request the journal lacks
    calls(store, begun.id, "sent")
    recover(store, NOW, 3)
    start(store, tmp_path, "j2.jsonl")
    cost = interrupted_cost(store)
    assert (cost.basis, cost.usd) == (INFLIGHT, pytest.approx(REPLY_USD + REQUEST_USD))


def test_c05_a_lost_attempt_is_charged_by_its_journaled_log_and_the_export_says_its_basis(tmp_path):
    """C05, O62: an attempt begun after the snapshot, which the snapshot lacks, is charged from the journal's log: two
    replies and a request in flight, never its whole bound; `lost_in_kill` says the basis, and so does the report."""
    live, _, manifest = planned_store(tmp_path / "live")
    keys = [row.key.text for row in manifest.rows]
    start(live, tmp_path, "j1.jsonl")
    snapshot = copy_of(live, tmp_path, "snapshot")
    begun = live.begin_attempt(keys[1], NOW, 4321, "try1", calls_logged=True)
    calls(live, begun.id, "sent", REPLY, "sent", REPLY, "sent")
    unlogged = live.begin_attempt(keys[2], NOW, 4321, "try1")  # no budget's guard: no log, its bound
    start(snapshot, tmp_path, "j2.jsonl")
    lost = read(snapshot).counted["j1.jsonl"]["attempts"]
    assert [(a["id"], a["basis"], a["usd"]) for a in lost] == [
        (begun.id, INFLIGHT, pytest.approx(2 * REPLY_USD + REQUEST_USD)),
        (unlogged.id, BOUND, pytest.approx(BOUND_USD)),
    ]
    charged = [c for c in costs(read(snapshot), {MODEL: SPEC}, LIMITS) if c.state == LOST_STATE]
    assert [(c.basis, c.usd) for c in charged] == [
        (INFLIGHT, pytest.approx(2 * REPLY_USD + REQUEST_USD)),
        (BOUND, pytest.approx(BOUND_USD)),
    ]
    entries = export_run_state(snapshot)[KEY]
    assert [e["basis"] for e in entries] == [INFLIGHT, BOUND]
    lines = lost_section({KEY: entries}, _table)
    assert any("Basis" in line for line in lines)
    assert any(INFLIGHT in line for line in lines)


def test_c05_the_export_and_the_report_show_each_interrupted_attempts_basis(tmp_path):
    """C05: an export of a store whose interrupted attempts kept a call log lists each with its basis and its log's
    size, after `lost_in_kill`; the report counts them per model; a store with none has neither."""
    store, _, manifest = planned_store(tmp_path / "store")
    keys = [row.key.text for row in manifest.rows]
    plain = store.begin_attempt(keys[0], NOW, 1, "")
    recover(store, NOW, 3)
    assert EXPORT_KEY not in export_run_state(store)  # no log kept: the export is the one it was
    measured = store.begin_attempt(keys[1], NOW, 1, "", calls_logged=True)
    calls(store, measured.id, "sent", REPLY)
    flying = store.begin_attempt(keys[2], NOW, 1, "", calls_logged=True)
    calls(store, flying.id, "sent")
    recover(store, NOW, 3)
    entries = export_run_state(store)[EXPORT_KEY]
    assert [(e["attempt_id"], e["basis"], e["calls_sent"], e["calls_answered"]) for e in entries] == [
        (plain.id, BOUND, None, None),
        (measured.id, MEASURED, 1, 1),
        (flying.id, INFLIGHT, 1, 0),
    ]
    lines = section({EXPORT_KEY: entries}, _table)
    assert lines[0] == HEADING
    assert [line for line in lines if line.startswith(f"| {MODEL}")] == [f"| {MODEL} | 1 | 1 | 1 |"]
    assert section({}, _table) == []


def test_c05_a_merged_export_names_the_shard_of_each_interrupted_attempt(tmp_path):
    """C05: the join keeps each shard's entries, in shard order, with the shard's index, after `lost_in_kill`."""
    base = {"format": "campaign-export-1", "factors": [], "planned_runs": 0, "exhausted_runs": [],
            "unfinished_runs": [], "analysed_attempts": [], "discarded_usage": [], "retries_by_kind": [],
            "infrastructure_by_condition": [], "budget_holds": [], "kinds": []}  # fmt: skip
    entry = {"attempt_id": 1, "run_key": "r", "model": MODEL, "basis": MEASURED, "calls_sent": 1, "calls_answered": 1}
    joined = join_exports({1: base, 2: {**base, EXPORT_KEY: [entry]}}, {}, [])
    assert joined[EXPORT_KEY] == [{**entry, "shard": 2}]
    assert list(joined).index(EXPORT_KEY) == list(joined).index("kinds") + 1
    lines = section(joined, _table)
    assert any(line.startswith("| 2 | m1 | 1 | 0 | 0 |") for line in lines)
