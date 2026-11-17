"""An injection an earlier attempt left open charges no run that meets it: the injector's journal names the attempt
that made each injection, and the health verdicts charge only the attempt that did, never another (O80)."""

import json

import pytest
from fault_support import OUTAGE_TARGET, SERVICES, FakeServices, injector_for, write_faults

from expcore.campaign import liveness
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate import _unsettled
from expcore_onedata.fault_journal import JOURNAL, InjectionJournal
from expcore_onedata.faults import SettledInjections, load_faults

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
NONE_RECORDED = object()  # the entry has no `owner` key at all
SETTLED = f"an injection of {OUTAGE_TARGET} is unsettled: the federation is not in its baseline state"
FAILED = f"the restore of {OUTAGE_TARGET} (start_failed) failed: the federation is not in its baseline state"


@pytest.fixture
def campaign():
    """A campaign registered in this process: its store's attempt rows, by id, as a dict the test sets."""
    rows: dict[int, str] = {}
    key = liveness.register(rows.get)
    rows.update({5: "running", 6: "running"})
    yield key
    liveness.unregister(key)


def attempt(key, attempt_id):
    return {"process": liveness.this_process(), "campaign": key, "attempt_id": attempt_id}


def entry(owner=NONE_RECORDED, *, failed=None, target=OUTAGE_TARGET, id="inj-1"):
    """An open injection of the fault journal, as `InjectionJournal.open_entries` returns it."""
    named = {} if owner is NONE_RECORDED else {"owner": owner}
    return {
        "event": "injecting",
        "id": id,
        "target": target,
        "service": SERVICES["beta"],
        "injected": True,
        **named,
        **({} if failed is None else {"clear_failed": failed}),
    }


def journal_with(tmp_path, *entries):
    """A journal directory whose open injections are `entries`, written as the injector writes them."""
    platform = write_faults(tmp_path)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    journal = InjectionJournal(settings.journal)
    for line in entries:
        journal.append({k: v for k, v in line.items() if k not in ("injected", "clear_failed")})
        if "clear_failed" in line:
            journal.append({"event": "clear_failed", "id": line["id"], "how": line["clear_failed"]})
    return SettledInjections(settings)


def test_o80_an_injection_line_names_the_attempt_that_made_it_and_outside_one_only_the_process(tmp_path, campaign):
    """O80: the `injecting` line carries the owner as the baseline marks do (`liveness.owner`): the process, the
    campaign and the store's id of the attempt inside one, and the process alone outside any."""
    platform, services = write_faults(tmp_path), FakeServices()
    injector = injector_for(platform, services)
    with liveness.running(campaign, 5):
        injector.start(RUN)
    [line] = [json.loads(t) for t in (tmp_path / "fault-journal" / JOURNAL).read_text().splitlines()[:1]]
    assert (line["event"], line["owner"]) == ("injecting", attempt(campaign, 5))
    injector.finish()
    outside = injector_for(platform, services)
    outside.start({**RUN, "attempt": 2})
    last = [json.loads(t) for t in (tmp_path / "fault-journal" / JOURNAL).read_text().splitlines()]
    assert [x["owner"] for x in last if x["event"] == "injecting"][-1] == {"process": liveness.this_process()}
    outside.finish()


def test_o80_an_attempts_own_unsettled_injection_is_charged_to_it_and_to_no_other(tmp_path, campaign):
    """O80: attempt 5's injection left open: its own post-run check is charged exactly as before; attempt 6's check, and
    a pre-run check outside any attempt, meet another attempt's leftover and charge no run, saying why."""
    check = journal_with(tmp_path, entry(attempt(campaign, 5)))
    with liveness.running(campaign, 5):
        own = check()
    assert (own.ok, own.deferred, own.charge, own.detail) == (False, False, True, SETTLED)
    for judged in (6, None):
        if judged is None:
            other = check()
        else:
            with liveness.running(campaign, judged):
                other = check()
        assert (other.ok, other.deferred, other.charge) == (False, False, False)
        assert other.detail.startswith(SETTLED)
        assert "left by another attempt" in other.detail


@pytest.mark.parametrize("failed", ["start_failed", "stop_unsettled"])
def test_o80_a_failed_restore_charges_the_attempt_that_injected_and_holds_every_other_uncharged(campaign, failed):
    """O80: the verdict that names a failed restore (`clear_failed`) charges its injecting attempt and no other."""
    detail = f"the restore of {OUTAGE_TARGET} ({failed}) failed: the federation is not in its baseline state"
    entries = [entry(attempt(campaign, 5), failed=failed)]
    with liveness.running(campaign, 5):
        own = _unsettled(entries)
    assert (own.charge, own.detail) == (True, detail)
    with liveness.running(campaign, 6):
        other = _unsettled(entries)
    assert (other.ok, other.charge) == (False, False)
    assert other.detail.startswith(detail)
    assert "left by another attempt" in other.detail
    assert _unsettled(entries).charge is False  # a pre-run check: no attempt is judged


def test_o80_an_entry_that_records_no_owner_is_charged_as_before_and_says_so(tmp_path, campaign):
    """O80: an injection written by code that recorded no owner (or one that cannot be read) is never guessed at: the
    check charges as it did, and its detail says that no owner is recorded."""
    for owner in (NONE_RECORDED, None, "a string", {"process": 3}):
        health = _unsettled([entry(owner, failed="start_failed")])
        assert (health.ok, health.charge) == (False, True), owner
        assert health.detail.startswith(FAILED), owner
        assert "records no attempt as its owner" in health.detail, owner
    health = journal_with(tmp_path)()  # nothing open: healthy, with nothing to say about owners
    assert health.ok is True


def test_o80_an_injection_made_outside_any_attempt_names_no_attempt_and_is_charged_as_before(campaign):
    """O80: a heal's container restart or a tool's injection is no attempt's failure to spare: its owner is the process
    alone, so a run that finds it is charged as before, with its detail saying no attempt owns it."""
    health = _unsettled([entry({"process": liveness.this_process()})])
    assert health.charge is True
    assert health.detail.startswith(SETTLED)
    assert "records no attempt as its owner" in health.detail


def test_o80_the_judged_attempts_own_injection_is_never_spared_by_anothers_beside_it(campaign):
    """O80: with entries of two attempts open, the check of one of them is charged (its own failure is never spared,
    as a baseline mark's is not); a check outside both charges no one."""
    entries = [entry(attempt(campaign, 5), id="a"), entry(attempt(campaign, 6), id="b", target="providers[1]")]
    with liveness.running(campaign, 6):
        assert _unsettled(entries).charge is True
    with liveness.running(campaign, 5):
        assert _unsettled(entries).charge is True
    assert _unsettled(entries).charge is False


def test_o80_an_entry_of_another_attempt_beside_one_with_no_owner_keeps_the_charge(campaign):
    """O80: an entry with no owner keeps the charge, so an ownerless leftover is never hidden by another's."""
    entries = [entry(attempt(campaign, 5), id="a"), entry(id="b", target="providers[1]")]
    with liveness.running(campaign, 6):
        health = _unsettled(entries)
    assert health.charge is True
    assert "records no attempt as its owner" in health.detail


def test_o80_a_journal_that_cannot_be_read_is_charged_as_before(tmp_path):
    """O80: an unreadable journal says nothing about whose it is: the check fails closed and charges, as it did."""
    platform = write_faults(tmp_path)
    settings = load_faults(platform, load_platform(platform, require_secrets=False))
    (settings.journal).mkdir(parents=True, exist_ok=True)
    (settings.journal / JOURNAL).write_text('{"event": "stopped-by-nobody"}\n', encoding="utf-8")
    health = SettledInjections(settings)()
    assert (health.ok, health.charge) == (False, True)
    assert "cannot be read" in health.detail
