"""O35: a run's admission names a failed heal first when that is why it refuses the run.

A run whose pre-run check passed before a heal is refused at its admission, uncharged, as the hold `federation_busy`
(`admission_recheck`). When the heal ended with a pair still failing (its `ended` line in `gate-heals.jsonl`), the
refusal's detail leads with the reason every answer of the gate leads with after a failed heal, `a failed heal: operator
action needed (stuck pairs: ...)`, ahead of the epoch's own words, so a reader of the run's `run_unstarted` event can
tell it was a failed heal. The refusal is the same hold with the same charge: only its words change.
"""

import contextlib
import contextvars
import threading

import pytest
from campaign_support import make_schema
from gate_support import CRASHED, EVERY, READER, Federation, gate_of
from scheduler_support import FakeExecutor, attempts_of, build, key_of, settings_for

from expcore.campaign import prerun
from expcore.campaign.factors import FactorSchema
from expcore.errors import uncharged_kinds
from expcore_onedata.admission import FederationBusy, run_admission
from expcore_onedata.config import load_platform
from expcore_onedata.fault_gate_heal import GateHeals
from expcore_onedata.sandbox.kinds import FEDERATION_BUSY

REASON = f"a failed heal: operator action needed (stuck pairs: {READER} reading from {CRASHED})"
SMALL = {"tasks": ("task-a",), "seeds": (1,)}
SCHEMA = make_schema()
ONE = FactorSchema(SCHEMA.declared, SCHEMA.order, {"api-none": SCHEMA.conditions["api-none"]})  # a plan of one run
ONE_KEY = key_of(ONE, "api-none")
LONG_DEFERRAL_S = 10_000.0  # above the longest wait a declared heal can defer a check (`max_deferral_s`, #293)
HEAL_WAITS = (False, True, False)  # (ok, deferred, charge): the answer of the check that begins a heal


def admission_in(directory):
    """The campaign's run admission for the platform in `directory`."""
    return run_admission(load_platform(directory / "platform.toml", require_secrets=False))


def admitted(admit) -> str:
    """Enter and leave a baseline run's admission; what it held."""
    with admit((), {}, threading.Event(), False) as mode:
        return mode


def elsewhere(call):
    """`call` in a context of its own: another worker's check, never the try under test's."""
    return contextvars.Context().run(call)


def stuck_gate(tmp_path, *, container_fallback=False):
    """A gate whose pair (READER, CRASHED) is stuck and that no link restart mends: a heal ends `links_stuck`."""
    federation = Federation()
    federation.link_fixes = dict.fromkeys(EVERY, ())
    options = {"heal": "all_links", "heal_container_fallback": container_fallback}
    gate, federation, _, settings = gate_of(tmp_path, federation, **options)
    federation.stuck = set()
    return gate, federation, settings


def failing_heal_meanwhile(gate, federation):
    """Another run's check finds the pair stuck and begins the heal, which ends with the pair still failing; once it
    has ended, past its settle."""
    federation.stuck = {(READER, CRASHED)}
    began = elsewhere(gate)
    assert (began.ok, began.deferred, began.charge) == HEAL_WAITS, began.detail
    assert gate.wait_for_heal(10)
    assert gate.last_heal["outcome"] == "links_stuck"
    federation.clock.sleep(600)  # past the drain record each restart's settle wrote on the gate's clock


def test_o35_a_failed_heal_between_a_runs_check_and_its_admission_leads_the_refusal_with_it(tmp_path):
    """O35: the run's check passed; another check heals the stuck pair and the heal ends with it still failing. The
    run's admission is refused, uncharged, and its detail leads with the failed heal and the pair, then says what the
    epoch did."""
    gate, federation, _ = stuck_gate(tmp_path)
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        failing_heal_meanwhile(gate, federation)
        with pytest.raises(FederationBusy) as refused:
            admitted(admit)
    detail = refused.value.detail
    assert detail.startswith(f"{REASON}; since the run's pre-run check the federation's link epoch moved"), detail
    assert "because a heal began" in detail
    assert detail.endswith("the run is checked again before it is admitted, and no attempt is used")
    assert refused.value.kind == FEDERATION_BUSY
    assert FEDERATION_BUSY in uncharged_kinds()


def test_o35_a_failed_heals_pair_is_named_by_label_and_never_by_a_providers_name(tmp_path):
    """O35: the reason names the stuck pair by the labels of the link check's own words, as the gate's answers do
    (O12), so a refusal a run's event keeps holds no provider's name or id."""
    gate, federation, settings = stuck_gate(tmp_path)
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        failing_heal_meanwhile(gate, federation)
        with pytest.raises(FederationBusy) as refused:
            admitted(admit)
    (ended,) = [line for line in GateHeals(settings.journal).lines() if line["event"] == "ended"]
    assert ended["failing"] == [{"reader": READER, "source": CRASHED}]
    assert refused.value.detail.split("; ")[0] == REASON


def test_o35_a_failed_heal_leads_the_refusal_of_a_pair_a_later_check_found_failing_too(tmp_path):
    """O35: after the failed heal, another check finds the pair still failing and raises the epoch again. The epoch
    moved for that, and the refusal says so, but the heal still needs an operator, so the reason still leads."""
    gate, federation, _ = stuck_gate(tmp_path)
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        failing_heal_meanwhile(gate, federation)
        closing = elsewhere(gate)
        assert (closing.ok, closing.deferred) == (False, False), closing.detail
        with pytest.raises(FederationBusy) as refused:
            admitted(admit)
    detail = refused.value.detail
    assert detail.startswith(f"{REASON}; "), detail
    assert "found a pair failing" in detail


def test_o35_a_healed_pair_leaves_the_refusal_as_it_was(tmp_path):
    """O35: the heal mended the pair (no pair left failing): the run is refused for the heal as before, and nothing
    in the refusal says a failed heal or an operator."""
    gate, federation, _, _ = gate_of(tmp_path, heal="all_links")
    federation.stuck = set()
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        federation.stuck = {(READER, CRASHED)}
        assert elsewhere(gate).deferred is True
        assert gate.wait_for_heal(10)
        assert gate.last_heal["outcome"] == "healed"
        federation.clock.sleep(600)
        with pytest.raises(FederationBusy) as refused:
            admitted(admit)
    assert "a heal began" in refused.value.detail
    assert "failed heal" not in refused.value.detail
    assert "operator" not in refused.value.detail


def test_o35_a_heal_cut_short_leaves_the_refusal_as_it_was(tmp_path):
    """O35: a heal that began and has no `ended` line (a heal under way, or one cut short) holds nothing for an
    operator: the refusal for the heal on record says what it always said."""
    gate, _, settings = stuck_gate(tmp_path)
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        GateHeals(settings.journal).append({"event": "healing", "id": "another-process", "mode": "all_links"})
        with pytest.raises(FederationBusy) as refused:
            admitted(admit)
    assert refused.value.detail.startswith("a heal has begun since the fault gate's link check last passed")
    assert "operator" not in refused.value.detail


def test_o35_a_failed_heal_the_gates_link_check_has_since_seen_mended_leads_nothing(tmp_path):
    """O35: an operator mends the pair and the gate's own link check passes (the episode ends): the run checked
    before the heal is still refused for the epoch's move, and the refusal no longer leads with a failed heal."""
    gate, federation, _ = stuck_gate(tmp_path)
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        failing_heal_meanwhile(gate, federation)
        federation.stuck = set()  # the operator mends it
        assert elsewhere(gate).ok  # a link check that passes ends the stuck episode
        with pytest.raises(FederationBusy) as refused:
            admitted(admit)
    assert "a heal began" in refused.value.detail
    assert "failed heal" not in refused.value.detail


def test_o35_a_failed_heal_whose_pairs_are_named_by_no_label_leaves_the_refusal_as_it_was(tmp_path):
    """O35: the record's `ended` line names a pair by something that is no label (a provider's name, say): the reason
    is not made from it (O12), so the refusal says what it always said."""
    gate, _, settings = stuck_gate(tmp_path)
    admit = admission_in(tmp_path)
    with prerun.opened():
        assert gate().ok
        heals = GateHeals(settings.journal)
        heals.append({"event": "healing", "id": "x", "mode": "all_links"})
        heals.append({"event": "ended", "id": "x", "ok": False, "failing": [{"reader": "a-name", "source": "b-name"}]})
        with pytest.raises(FederationBusy) as refused:
            admitted(admit)
    assert refused.value.detail.startswith("a heal has begun since the fault gate's link check last passed")
    assert "a-name" not in refused.value.detail


def test_o35_a_campaign_run_refused_for_a_failed_heal_says_so_in_its_event_and_uses_no_attempt(tmp_path):
    """O35: in a campaign whose `max_attempts` is 1, a heal ends with the pair failing between the run's pre-run check
    and its admission. The run's `run_unstarted` event leads, after the key, with the failed heal; it uses no attempt,
    and runs once the operator has mended the pair and a fresh check has passed."""
    federation_dir = tmp_path / "federation"
    gate, federation, _ = stuck_gate(federation_dir)
    admit = admission_in(federation_dir)
    asked = []

    @contextlib.contextmanager
    def heal_first(kinds, targets, cancel, exclusive):
        asked.append(kinds)
        if len(asked) == 1:  # between this run's check and its admission, another check's heal fails
            failing_heal_meanwhile(gate, federation)
        try:
            with admit(kinds, targets, cancel, exclusive) as mode:
                yield mode
        except FederationBusy:
            federation.stuck = set()  # the operator mends the pair before the run is asked again
            raise

    settings = settings_for(max_attempts=1, max_defer_s=LONG_DEFERRAL_S)
    with build(
        tmp_path / "campaign",
        schema=ONE,
        executor=FakeExecutor(),
        plan=SMALL,
        settings=settings,
        health=gate,
        admission=heal_first,
    ) as scheduler:
        summary = scheduler.run()
        rows = attempts_of(scheduler.store, ONE_KEY)
        charged = scheduler.store.charged_attempts(ONE_KEY)
        unstarted = [r["detail"] for r in scheduler.store.q("SELECT detail FROM events WHERE kind='run_unstarted'")]
    assert summary.statuses == {"done": 1}
    assert [state for state, _, _ in rows] == ["done"]
    assert charged == 0
    assert len(unstarted) == 1
    leads = f"{ONE_KEY}: not admitted by the platform, no attempt used: {REASON}; "
    assert unstarted[0].startswith(leads), unstarted[0]
    assert "no attempt is used" in unstarted[0]
