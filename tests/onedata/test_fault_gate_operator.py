"""O35: a hold a failed heal leaves says so, naming the stuck pairs, and a wait that is no failed heal's does not.

A heal that leaves a pair failing keeps the gate closed for an operator: none runs again until the gate's own link check
passes (`GateHeals.latched`). The checks after it answer a wait (the recovery of a container restart the heal left open
owes a drain and a settle) and then close the gate, charging the run that finds it, and a wait past the campaign's
`max_defer_s` is a blocking hold. Every one of those answers leads with the reason `a failed heal: operator action
needed (stuck pairs: ...)`, ahead of the rest of its words, because the core keeps only the first 200 characters of a
detail and a status line shows 160 of them: the reason is what a reader of the status or the run log sees first.
"""

import threading
import time

from gate_heal_support import (
    EVERY,
    GONE_PROCESS,
    READER,
    UNCHARGED_WAIT,
    Federation,
    core_gate,
    gate_of,
    heal_lines,
    healed,
    next_process,
)

from expcore.campaign.deferral import DEFERRAL_META
from expcore.campaign.export import export_run_state
from expcore.campaign.healthgate import META, alarms, fact
from expcore_onedata import fault_gate_heal
from expcore_onedata.admission import OUTAGE_RUN, FederationLock
from expcore_onedata.fault_gate_heal import GateHeals
from expcore_onedata.fault_journal import InjectionJournal

REASON = "a failed heal: operator action needed (stuck pairs: providers[2] reading from providers[0])"


def failed_heal(tmp_path, *, restart_left_open: bool):
    """A gate whose heal has ended with the pair still stuck: no link restart mends it, and with `restart_left_open`
    the reader's container restart is not verified (it does not serve in time), so the heal leaves it open for the
    next check's recovery, which owes a session drain and a settle; without, the fallback is off and the heal ends
    having restarted links alone. The container serves again by the next check. With the restart left open the drain
    record's settle still closes the federation (the next checks wait it out); without it, the settle has passed."""
    federation = Federation()
    federation.link_fixes = dict.fromkeys(EVERY, ())
    options = {"heal": "all_links"} if restart_left_open else {"heal": "all_links", "heal_container_fallback": False}
    gate, federation, check, settings = gate_of(tmp_path, federation, **options)
    federation.knobs["never_serves"] = restart_left_open
    first = gate()
    assert (first.ok, first.deferred, first.charge) == UNCHARGED_WAIT, first.detail
    assert gate.wait_for_heal(10)
    federation.knobs["never_serves"] = False
    if not restart_left_open:
        federation.clock.sleep(600)  # past the drain record each link restart's settle wrote
    return gate, federation, check, settings


def plain(answer):
    """The answer names no failed heal and no operator: a wait or a failure that reads as it always did."""
    assert "failed heal" not in answer.detail, answer.detail
    assert "operator" not in answer.detail, answer.detail


def answers_until_closed(gate, federation, limit=12):
    """Every answer the gate gives, a minute apart, up to and including the first that is no wait."""
    seen = []
    for _ in range(limit):
        seen.append(gate())
        if not seen[-1].deferred:
            return seen
        federation.clock.sleep(100)
    raise AssertionError(f"the gate still defers after {limit} answers: {seen[-1].detail}")


def test_o35_a_gate_closed_by_a_failed_heal_leads_with_the_reason_and_the_stuck_pairs(tmp_path):
    """O35: the next check finds the pair still stuck with the latch set, closes the gate and charges the run that
    found it, as for any stuck link, and says first that a failed heal needs an operator and which pairs are stuck;
    the link check's own words follow."""
    gate, _, _, _ = failed_heal(tmp_path, restart_left_open=False)
    closing = gate()
    assert (closing.ok, closing.deferred, closing.charge) == (False, False, True)
    assert closing.detail.startswith(f"{REASON}; ")
    assert "stuck: providers[0]->providers[2]" in closing.detail
    assert "until a link check passes" in closing.detail


def test_o35_every_wait_the_recovery_of_a_failed_heals_open_restart_owes_leads_with_the_reason(tmp_path):
    """O35: the heal's container restart did not serve in time and was left open; the next checks recover it, which
    owes a session drain and holds a settle before the gate and the pairs are read. Each of those uncharged waits, and
    the closing after them, leads with the reason, so a wait that outlasts `max_defer_s` is not read as a long deferral
    of a heal that will end by itself."""
    gate, federation, _, settings = failed_heal(tmp_path, restart_left_open=True)
    (left,) = InjectionJournal(settings.journal).open_entries()
    assert (left["target"], left["clear_failed"], left["heal"]) == (READER, "heal_restart_failed", True)
    *waits, closing = answers_until_closed(gate, federation)
    assert len(waits) >= 2, [answer.detail for answer in waits]  # the drain it owes, then the settle it holds
    for wait in waits:
        assert (wait.ok, wait.deferred, wait.charge) == UNCHARGED_WAIT, wait.detail
        assert wait.detail.startswith(f"{REASON}; "), wait.detail
    assert (closing.deferred, closing.charge) == (False, True)
    assert closing.detail.startswith(f"{REASON}; ")


def test_o35_the_reason_names_at_most_two_stuck_pairs_and_counts_the_rest(tmp_path):
    """O35: a heal that leaves every pair of a crashed provider stuck names the first two in label order and says how
    many more there are, so the reason fits where a status shows it; no provider name or id is ever in it."""
    federation = Federation()
    federation.stuck = {(READER, "providers[0]"), ("providers[3]", "providers[0]"), ("providers[1]", "providers[0]")}
    federation.link_fixes = dict.fromkeys(EVERY, ())
    gate, federation, _, _ = gate_of(tmp_path, federation, heal="all_links", heal_container_fallback=False)
    assert gate().deferred is True
    assert gate.wait_for_heal(10)
    federation.clock.sleep(600)
    closing = gate()
    reason = closing.detail.split("; ")[0]
    assert reason == (
        "a failed heal: operator action needed (stuck pairs: providers[1] reading from providers[0], "
        "providers[2] reading from providers[0] and 1 more)"
    )
    assert len(reason) <= 160  # what a status line shows of a detail


def test_o35_the_record_of_a_failed_heal_names_the_pairs_the_reason_names(tmp_path):
    """O35: the reason reads the durable record, the heal's `ended` line (its `failing` pairs by label), so another
    process's check names the same pairs after this one is gone."""
    _, federation, _, settings = failed_heal(tmp_path, restart_left_open=False)
    (ended,) = [line for line in heal_lines(settings) if line["event"] == "ended"]
    assert ended["failing"] == [{"reader": READER, "source": "providers[0]"}]
    other, _ = next_process(settings, federation)
    assert other().detail.startswith(f"{REASON}; ")


def test_o35_a_heal_in_progress_says_nothing_of_a_failed_heal(tmp_path):
    """O35: while a heal runs every check answers the uncharged wait of its own, and the check that began it names no
    failed heal: none has ended."""
    federation = Federation()
    federation.restart_s = 0.3
    gate, federation, _, _ = gate_of(tmp_path, federation, heal="all_links")
    first = gate()
    meanwhile = gate()
    for answer in (first, meanwhile):
        assert (answer.ok, answer.deferred, answer.charge) == UNCHARGED_WAIT, answer.detail
        plain(answer)
    assert gate.wait_for_heal(10)


def test_o35_a_heal_that_mends_its_pairs_says_nothing_of_a_failed_heal(tmp_path):
    """O35: a heal that ends healed opens the gate, and no answer on the way names an operator."""
    gate, federation, _, _ = gate_of(tmp_path, heal="all_links")
    reading = healed(gate, federation)
    assert reading.ok is True, reading.detail
    assert "failed heal" not in reading.detail


def test_o35_a_plain_deferral_for_an_outage_run_keeps_its_wording_with_a_failed_heal_on_record(tmp_path):
    """O35: an outage run holds the federation: the wait is that run's, however the record stands, and reads as it
    always did. Nothing in it says an operator is needed, even with the failed heal's `ended` line on record, since
    the heal is not what the check waits for."""
    gate, _, _, settings = failed_heal(tmp_path, restart_left_open=False)
    outage = FederationLock(settings).acquire(exclusive=True, purpose=OUTAGE_RUN)
    try:
        reading = gate()
    finally:
        outage.close()
    assert (reading.ok, reading.deferred, reading.charge) == UNCHARGED_WAIT, reading.detail
    assert "an outage run" in reading.detail
    assert "holds the federation" in reading.detail
    plain(reading)


def test_o35_a_plain_deferral_with_no_heal_on_record_keeps_its_wording(tmp_path):
    """O35: with no heal ever run, a wait for an outage run is exactly what it was: no reason leads it."""
    gate, _, _, settings = gate_of(tmp_path, heal="all_links")
    outage = FederationLock(settings).acquire(exclusive=True, purpose=OUTAGE_RUN)
    try:
        reading = gate()
    finally:
        outage.close()
    assert reading.deferred is True
    assert reading.detail.startswith("an outage run")
    plain(reading)
    assert GateHeals(settings.journal).lines() == []


def test_o35_a_heal_cut_short_is_no_failed_heal_and_its_settle_wait_keeps_its_wording(tmp_path, monkeypatch):
    """O35: a clean exit stopped the heal after a reader's container restart: no `ended` line, so no failed heal,
    and the next process's waits for its settle are the plain waits they were, even though the restart is left
    open."""
    real = fault_gate_heal.this_process
    monkeypatch.setattr(fault_gate_heal, "this_process", lambda: GONE_PROCESS)
    federation = Federation()
    federation.stuck = {(READER, "providers[0]"), ("providers[3]", "providers[0]")}
    federation.link_fixes = {READER: (), "providers[3]": ()}
    federation.container_fixes = {READER}
    gate, federation, _, settings = gate_of(tmp_path, federation, heal="link_then_container")
    stopping = threading.Thread(target=gate.stop_heal, args=(10,))

    def the_exit_comes_during_the_readers_start(label):
        if label == READER and not stopping.is_alive():
            stopping.start()
            time.sleep(0.1)

    federation.on_start = the_exit_comes_during_the_readers_start
    assert gate().deferred is True
    assert gate.wait_for_heal(10)
    stopping.join(10)
    monkeypatch.setattr(fault_gate_heal, "this_process", real)
    assert [line["event"] for line in heal_lines(settings)] == ["healing"]
    nxt, _ = next_process(settings, federation)
    meanwhile = nxt()
    assert (meanwhile.ok, meanwhile.deferred, meanwhile.charge) == UNCHARGED_WAIT, meanwhile.detail
    federation.clock.sleep(1200)
    settling = nxt()
    assert settling.deferred is True
    for answer in (meanwhile, settling):
        plain(answer)


def test_o35_a_record_that_cannot_be_read_leaves_the_answer_as_it_was(tmp_path):
    """O35: the reason is read from the heal's record, and a record that cannot be read is a plain answer: the latch
    already says the record cannot be read, and no reason is guessed."""
    gate, _, _, settings = failed_heal(tmp_path, restart_left_open=False)
    with GateHeals(settings.journal).path.open("a", encoding="utf-8") as record:
        record.write("not json\n")
    closing = gate()
    assert "failed heal" not in closing.detail
    assert "cannot be read" in closing.detail


def test_o35_a_failed_heals_wait_past_max_defer_s_is_a_blocking_hold_the_status_and_the_log_say_needs_an_operator(
    tmp_path,
):
    """O35: the checks after a failed heal defer (the drain and settle its open restart owes), the core counts them
    against `max_defer_s`, and past it the gate closes blocking, charging no run. The outage record the status reads,
    its alarm and the run log's event all lead with the reason, and the closed gate is not a deferral that reads as a
    heal still working."""
    gate, federation, _, _ = failed_heal(tmp_path, restart_left_open=True)
    core, store, key = core_gate(tmp_path, gate, timeout_s=5, max_defer_s=100)
    for _ in range(12):
        if core.before_run(key).verdict != "defer":
            break
        state = fact(store.get_meta(META), store.get_meta(DEFERRAL_META))
        assert REASON in "".join(alarms(state, 30)), alarms(state, 30)
        federation.clock.sleep(100)
    assert core.closed
    assert core.blocking
    state = fact(store.get_meta(META), store.get_meta(DEFERRAL_META))
    assert state["open"]["blocking"] is True
    assert state["open"]["charged"] is False
    assert state["open"]["first_detail"].startswith(f"{REASON}; ")
    (alarm,) = alarms(state, 30)
    assert REASON in alarm
    (closed,) = [row["detail"] for row in store.q("SELECT detail FROM events WHERE kind='health_gate_closed'")]
    assert REASON in closed
    (hold,) = export_run_state(store)["operator_holds"]  # and the export, so the report says it too (C11)
    assert (hold["blocking"], hold["charged"], hold["end_utc"], hold["reason"]) == (True, False, None, REASON)
