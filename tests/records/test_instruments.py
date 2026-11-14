"""Instruments: a snapshot goes in the record under its namespace, and a failed one is recorded as failed. They start in
list order and finish in the reverse of it (C31), the nested order of setup and teardown."""

import pytest
from record_support import hand_made

from expcore.errors import InfraError
from expcore.plugin import Instrument
from expcore.records.instruments import check_instruments, finish_instruments, start_instruments


class Counter:
    """A test instrument: counts its calls."""

    def __init__(self, namespace="counter", snapshot=None, boom=None):
        self.namespace = namespace
        self.identity = None
        self.snapshot = {"calls": 1} if snapshot is None else snapshot
        self.boom = boom

    def start(self, identity):
        self.identity = dict(identity)

    def finish(self):
        if self.boom:
            raise self.boom
        return self.snapshot


def test_a_class_with_the_three_members_is_an_instrument_without_inheriting():
    assert isinstance(Counter(), Instrument)
    assert not isinstance(object(), Instrument)


def test_the_snapshot_goes_under_the_namespace_and_the_record_is_a_new_value():
    record = hand_made()
    instrument = Counter()
    start_instruments([instrument], {"run": "r1"})
    out = finish_instruments(record, [instrument])
    assert out.instruments["counter"] == {"calls": 1}
    assert "counter" not in record.instruments  # the argument is not mutated
    assert instrument.identity == {"run": "r1"}


def test_o08_an_instrument_that_raises_is_recorded_as_failed_and_not_dropped():
    out = finish_instruments(hand_made(), [Counter(boom=ConnectionResetError("secret detail"))])
    assert out.instruments["counter"] == {"ok": False, "error": "ConnectionResetError"}
    assert "secret detail" not in repr(out.instruments)


@pytest.mark.parametrize("snapshot", [["not", "an", "object"], {"x": float("nan")}, {"x": object()}])
def test_o08_a_snapshot_that_is_not_json_is_recorded_as_failed(snapshot):
    out = finish_instruments(hand_made(), [Counter(snapshot=snapshot)])
    assert out.instruments["counter"]["ok"] is False


def test_two_instruments_may_not_share_a_namespace_and_a_namespace_must_be_plain():
    with pytest.raises(ValueError, match="share the namespace"):
        check_instruments([Counter("a"), Counter("a")])
    for bad in ("A", "a-b", "1a", "", None):
        with pytest.raises(ValueError, match="namespace"):
            check_instruments([Counter(bad)])


class Logged:
    """A test instrument that writes each of its calls into a log shared with the others, so a test reads the order."""

    def __init__(self, namespace, log, *, boom=None, refuse=None):
        self.namespace, self.log, self.boom, self.refuse = namespace, log, boom, refuse

    def start(self, identity):
        self.log.append(f"{self.namespace}.start")

    def finish(self):
        self.log.append(f"{self.namespace}.finish")
        if self.boom:
            raise self.boom
        return {"namespace": self.namespace}

    def check(self):
        self.log.append(f"{self.namespace}.check")
        if self.refuse is not None:
            raise self.refuse


def run(instruments):
    """Start the instruments and finish them the way an executor does; return the record."""
    start_instruments(instruments, {"run": "r1"})
    return finish_instruments(hand_made(), instruments)


def test_c31_instruments_finish_in_the_reverse_of_the_order_they_start():
    """C31: setup and teardown nest. Instruments start in list order and finish in the reverse of it, so the first
    listed starts first and finishes last."""
    log = []
    run([Logged("a", log), Logged("b", log), Logged("c", log)])
    assert [call for call in log if call.endswith(".start")] == ["a.start", "b.start", "c.start"]
    assert [call for call in log if call.endswith(".finish")] == ["c.finish", "b.finish", "a.finish"]
    assert log.index("c.start") < log.index("c.finish")  # every start comes before the first finish


def test_c31_an_instrument_listed_after_another_starts_after_it_and_finishes_before_it():
    """C31: for every pair, the one listed later starts later and finishes earlier: the probes listed after an
    injector start once it has injected and finish before it restores."""
    log = []
    names = ["injector", "probes_a", "probes_b"]
    run([Logged(name, log) for name in names])
    for i, earlier in enumerate(names):
        for later in names[i + 1 :]:
            assert log.index(f"{earlier}.start") < log.index(f"{later}.start")
            assert log.index(f"{later}.finish") < log.index(f"{earlier}.finish")


def test_c31_the_record_keeps_the_instruments_in_the_order_they_are_listed_whatever_order_they_finish_in():
    """C31: only the calls are reversed. The snapshots are put into the record in list order, so for snapshots that do
    not depend on the order a record has the key order, and the bytes, that one whose instruments finished in list
    order had."""
    out = run([Logged("a", []), Logged("b", []), Logged("c", [])])
    assert list(out.instruments) == ["a", "b", "c"]
    expected = hand_made()
    for name in ("a", "b", "c"):
        expected = expected.with_instrument(name, {"namespace": name})
    assert out.dumps() == expected.dumps()


def test_c31_the_checks_run_in_list_order_once_every_finish_has_run_in_reverse():
    """C31: a check is a verdict on the manipulation, taken once every snapshot is in, not a release. The checks keep
    list order, as the first-turn hook does."""
    log = []
    run([Logged("a", log), Logged("b", log), Logged("c", log)])
    assert [call for call in log if not call.endswith(".start")] == [
        "c.finish",
        "b.finish",
        "a.finish",
        "a.check",
        "b.check",
        "c.check",
    ]


def test_c31_the_first_listed_instrument_to_refuse_is_the_refusal_the_record_keeps():
    """C31: the first refusal stops the rest, and list order decides which it is: the instrument listed first, the
    outermost, is the one recorded, never the probe listed after it whose reading it caused."""
    log, kind = [], "tool_plane_integrity"
    outer = Logged("outer", log, refuse=InfraError(kind, "the plan failed mid-run"))
    inner = Logged("inner", log, refuse=InfraError(kind, "a symptom"))
    out = run([outer, inner])
    assert out.instrument_refusal == {"namespace": "outer", "infra_kind": kind, "detail": "the plan failed mid-run"}
    assert "inner.check" not in log


@pytest.mark.parametrize("failing", ["a", "b", "c"])
def test_c31_a_finish_that_raises_does_not_stop_the_finishes_of_the_others(failing):
    """C31: a finish that raises is recorded as failed and the rest still finish, whichever position it is in the
    reversed run: the ones that finish after it are not skipped, and the ones before it are kept."""
    log = []
    instruments = [Logged(n, log, boom=ConnectionResetError("x") if n == failing else None) for n in "abc"]
    out = run(instruments)
    assert [call for call in log if call.endswith(".finish")] == ["c.finish", "b.finish", "a.finish"]
    assert out.instruments[failing] == {"ok": False, "error": "ConnectionResetError"}
    for name in "abc":
        if name != failing:
            assert out.instruments[name] == {"namespace": name}
