"""C31: an instrument may read the state at the agent's first turn and may refuse the attempt, at the first turn or
after the run, with a declared kind no condition can have as its treatment; an instrument with neither method, and a
run with none, are exactly what they were."""

import json

import pytest
from loop_support import FakeClock
from record_support import hand_made, make_config

from expcore.errors import InfraError, StudyKind, study_kinds
from expcore.plugin import FIRST_TURN_LABEL, Instrument
from expcore.records.instruments import (
    InstrumentRefusalError,
    finish_instruments,
    first_turn_instruments,
    start_instruments,
)
from expcore.records.record import RunRecord, decode
from expcore.records.schema import RecordError, validate

REFUSED = "tool_plane_integrity"  # a core kind no condition may declare as its treatment


class Plain:
    """An instrument as before: start and finish, no hook and no check."""

    def __init__(self, namespace="plain"):
        self.namespace = namespace

    def start(self, identity):
        pass

    def finish(self):
        return {"seen": True}


class Watcher:
    """An instrument with both hooks: it reads at the first turn, labels the reading with the clock, and checks."""

    def __init__(self, namespace="watcher", *, hook=None, check=None, log=None):
        self.namespace = namespace
        self._hook, self._check = hook, check
        self.log = log if log is not None else []
        self.moments: list[dict] = []

    def start(self, identity):
        self.log.append(f"{self.namespace}.start")

    def at_first_turn(self, moment):
        self.log.append(f"{self.namespace}.at_first_turn")
        self.moments.append(dict(moment))
        if self._hook is not None:
            raise self._hook

    def finish(self):
        self.log.append(f"{self.namespace}.finish")
        return {"first_turn_clock_s": self.moments[0]["clock_s"] if self.moments else None}

    def check(self):
        self.log.append(f"{self.namespace}.check")
        if self._check is not None:
            raise self._check


def timer_on(clock):
    return make_config().timer(clock)


def test_c31_an_instrument_with_the_hooks_is_still_an_instrument_and_one_without_them_needs_no_change():
    """C31: the hooks are optional methods, not members of the protocol, so an instrument written before them still
    satisfies it and is never asked for them."""
    assert isinstance(Plain(), Instrument)
    assert isinstance(Watcher(), Instrument)
    timer = timer_on(FakeClock())
    first_turn_instruments([Plain()], timer)
    assert timer.readings == []


def test_c31_the_first_turn_hook_gets_one_reading_of_the_runs_clock_which_the_record_logs_and_runs_in_order():
    """C31: the helper takes one reading of the run's clock, logged under a label of its own, and gives each hooked
    instrument that reading, in order, so what it records is labelled with the clock the record holds."""
    clock, log = FakeClock(500.0), []
    timer = timer_on(clock)
    first, second = Watcher("first", log=log), Watcher("second", log=log)
    first_turn_instruments([first, Plain(), second], timer)
    assert timer.readings == [(FIRST_TURN_LABEL, 500.0)]
    assert first.moments == [{"clock_s": 500.0}] == second.moments
    assert log == ["first.at_first_turn", "second.at_first_turn"]
    assert not timer.started  # the loop starts the agent's clock; the hook runs before it, outside the agent's time


def test_c31_the_first_turn_hook_stays_in_list_order_while_the_finishes_run_in_reverse():
    """C31: the first turn is one moment, not a nest, so the hooks run in list order, while the finishes of the same
    instruments run in the reverse of it."""
    log = []
    watchers = [Watcher(name, log=log) for name in ("first", "second", "third")]
    start_instruments(watchers, {"run": "r1"})
    first_turn_instruments(watchers, timer_on(FakeClock()))
    finish_instruments(hand_made(), watchers)
    assert [call for call in log if call.endswith((".at_first_turn", ".finish"))] == [
        "first.at_first_turn",
        "second.at_first_turn",
        "third.at_first_turn",
        "third.finish",
        "second.finish",
        "first.finish",
    ]


def test_c31_no_reading_is_taken_when_no_instrument_has_the_hook_so_a_run_without_one_is_unchanged():
    """C31: the clock gets no new reading from a study that declares no hook; the record of such a run is the one it
    was before the hook existed."""
    timer = timer_on(FakeClock())
    first_turn_instruments([], timer)
    first_turn_instruments([Plain("a"), Plain("b")], timer)
    assert timer.readings == []


def test_c31_the_hook_refuses_with_a_kind_no_condition_can_declare_and_the_error_names_the_instrument():
    """C31: an `InfraError` of an untreatable kind leaves the helper as infrastructure of that kind, with the
    instrument's namespace in the detail, for the executor's caller to set the attempt aside."""
    watcher = Watcher(hook=InfraError(REFUSED, "the files were not in their start state"))
    with pytest.raises(InfraError) as caught:
        first_turn_instruments([watcher], timer_on(FakeClock()))
    assert caught.value.kind == REFUSED
    assert "watcher" in caught.value.detail
    assert "the files were not in their start state" in caught.value.detail


def test_c31_a_kind_a_study_declared_untreatable_is_a_kind_an_instrument_may_refuse_with():
    """C31: "declared" is the registry's word: a study's own kind, declared not treatable, is as good as a core one."""
    with study_kinds({"manipulation_unrealised": StudyKind("the manipulation did not hold", treatable=False)}):
        watcher = Watcher(hook=InfraError("manipulation_unrealised", "start state wrong"))
        with pytest.raises(InfraError) as caught:
            first_turn_instruments([watcher], timer_on(FakeClock()))
        assert caught.value.kind == "manipulation_unrealised"


@pytest.mark.parametrize("kind", ["endpoint", "knowledge", "sandbox"])
def test_c31_a_hook_that_raises_a_treatable_kind_is_a_defect_of_the_instrument_not_an_infrastructure_failure(kind):
    """C31: a kind a condition may declare as its treatment would let a refused attempt be scored, so the instrument
    that raises one is wrong; the executor's caller sees a defect, not infrastructure of that kind."""
    watcher = Watcher(hook=InfraError(kind, "down"))
    with pytest.raises(InstrumentRefusalError, match=kind) as caught:
        first_turn_instruments([watcher], timer_on(FakeClock()))
    assert not isinstance(caught.value, InfraError)
    assert "watcher" in str(caught.value)


def test_c31_any_other_exception_of_the_hook_is_a_defect_and_reaches_the_caller_as_it_is():
    """C31: a hook that crashes is a defect of the instrument, like a `start` that crashes."""
    with pytest.raises(KeyError):
        first_turn_instruments([Watcher(hook=KeyError("state"))], timer_on(FakeClock()))


def test_c31_the_hook_runs_before_the_loop_starts_the_timer_and_not_after():
    """C31: after the agent's clock has started there is no first turn left to read at."""
    timer = timer_on(FakeClock())
    timer.start()
    with pytest.raises(RuntimeError, match="before the loop"):
        first_turn_instruments([Watcher()], timer)


def test_c31_a_check_runs_after_every_finish_and_a_run_whose_checks_pass_leaves_the_record_as_it_was():
    """C31: all snapshots are taken first, the last listed instrument's first; then each check runs in list order; a
    pass adds nothing to the record."""
    log, record = [], hand_made()
    first, second = Watcher("first", log=log), Watcher("second", log=log)
    start_instruments([first, second], {"run": "r1"})
    first_turn_instruments([first, second], timer_on(FakeClock()))
    out = finish_instruments(record, [first, Plain(), second])
    assert log == [
        "first.start",
        "second.start",
        "first.at_first_turn",
        "second.at_first_turn",
        "second.finish",
        "first.finish",
        "first.check",
        "second.check",
    ]
    assert out.instrument_refusal is None
    assert set(out.instruments) == {"first", "plain", "second"}
    assert "instrument_refusal" not in json.loads(out.dumps())


def test_c31_a_run_with_instruments_that_have_no_check_is_recorded_byte_for_byte_as_before():
    """C31: finish_instruments on instruments without a check is the function it was: the same snapshots and the same
    bytes as a record that went through none of the new code."""
    record = hand_made()
    out = finish_instruments(record, [Plain("a"), Plain("b")])
    expected = record.with_instrument("a", {"seen": True}).with_instrument("b", {"seen": True})
    assert out.dumps() == expected.dumps()
    assert out.instrument_refusal is None


def test_c31_a_check_that_refuses_puts_the_verdict_in_the_record_and_keeps_every_snapshot():
    """C31: the refusal is recorded with the instrument, the kind and the detail; the snapshots, the failing
    instrument's included, stay for diagnosis; and the checks after the refusing one are not run."""
    log = []
    refusing = Watcher("refusing", check=InfraError(REFUSED, "the plan failed mid-run"), log=log)
    later = Watcher("later", log=log)
    record = hand_made()
    out = finish_instruments(record, [Plain("first"), refusing, later])
    assert out.instrument_refusal == {
        "namespace": "refusing",
        "infra_kind": REFUSED,
        "detail": "the plan failed mid-run",
    }
    assert set(out.instruments) == {"first", "refusing", "later"}
    assert log == ["later.finish", "refusing.finish", "refusing.check"]
    assert record.instrument_refusal is None  # the argument is not mutated


def test_c31_a_check_runs_even_when_the_instruments_finish_failed_so_unknown_can_be_refused():
    """C31: an instrument whose reading failed is recorded as failed and its check still runs: it may refuse, since
    unknown is not a pass."""

    class Broken(Watcher):
        def finish(self):
            raise ConnectionResetError("x")

        def check(self):
            raise InfraError(REFUSED, "no reading to check")

    out = finish_instruments(hand_made(), [Broken("broken")])
    assert out.instruments["broken"] == {"ok": False, "error": "ConnectionResetError"}
    assert out.instrument_refusal["namespace"] == "broken"


def test_c31_a_check_that_raises_a_treatable_kind_or_crashes_is_a_defect_and_no_record_is_returned():
    """C31: only a declared untreatable kind refuses. A check that raises a kind a condition may declare, or anything
    else, raises out of `finish_instruments` to the executor's caller, a defect, as a `start` that raises does."""
    with pytest.raises(InstrumentRefusalError, match="endpoint"):
        finish_instruments(hand_made(), [Watcher(check=InfraError("endpoint", "down"))])
    with pytest.raises(KeyError):
        finish_instruments(hand_made(), [Watcher(check=KeyError("state"))])


def test_c31_a_refusal_is_detail_bounded_and_a_set_aside_record_for_the_rescoring():
    """C31: a record an instrument refused is set aside like one the loop ended as infrastructure, so re-scoring skips
    it; the detail is cut where an outcome's is."""
    out = finish_instruments(hand_made(), [Watcher(check=InfraError(REFUSED, "x" * 900))])
    assert len(out.instrument_refusal["detail"]) == 500
    assert out.set_aside is True
    assert hand_made().set_aside is False


def test_c31_the_refusal_is_written_only_when_there_is_one_and_reads_back_byte_for_byte():
    """C31: an absent refusal stays absent in the file (not null), a present one validates and loads, and the record
    writes back to the bytes it was read from."""
    plain = hand_made()
    assert "instrument_refusal" not in json.loads(plain.dumps())
    refused = plain.with_instrument_refusal({"namespace": "watcher", "infra_kind": REFUSED, "detail": "wrong start"})
    text = refused.dumps()
    data = decode(text)
    assert list(data)[-1] == "instrument_refusal"  # after the fields version 3 had, in the order the schema lists them
    assert data["instrument_refusal"] == {"namespace": "watcher", "infra_kind": REFUSED, "detail": "wrong start"}
    assert RunRecord.from_json(data).dumps() == text


@pytest.mark.parametrize(
    ("refusal", "message"),
    [
        ({"namespace": "w", "infra_kind": REFUSED}, "missing"),
        ({"namespace": "w", "infra_kind": REFUSED, "detail": "d", "extra": 1}, "unknown"),
        ({"namespace": "W", "infra_kind": REFUSED, "detail": "d"}, "namespace"),
        ({"namespace": "w", "infra_kind": "not_a_kind", "detail": "d"}, "infrastructure kind"),
        ({"namespace": "w", "infra_kind": "endpoint", "detail": "d"}, "treatable"),
        ({"namespace": "w", "infra_kind": REFUSED, "detail": 3}, "detail"),
        ("refused", "dict"),
    ],
)
def test_c31_a_malformed_refusal_is_refused_when_a_record_is_loaded(refusal, message):
    """C31: the field is checked like every other: its keys, its namespace, a kind in the registry that no condition
    may declare as its treatment, and the type of its detail."""
    data = decode(hand_made().with_instrument_refusal({"namespace": "w", "infra_kind": REFUSED, "detail": "d"}).dumps())
    data["instrument_refusal"] = refusal
    with pytest.raises(RecordError, match=message):
        validate(data)


@pytest.mark.parametrize("version", ["expcore-record/1", "expcore-record/2"])
def test_c31_a_record_of_an_earlier_version_cannot_hold_a_refusal(version):
    """C31: version 3 adds the field; a record of version 1 or 2 that holds it is refused, and one that does not is
    never given it."""
    data = decode(hand_made().dumps())
    data["schema_version"] = version
    data["instrument_refusal"] = {"namespace": "w", "infra_kind": REFUSED, "detail": "d"}
    with pytest.raises(RecordError, match="instrument_refusal"):
        validate(data)
    del data["instrument_refusal"]
    assert validate(data) == version
