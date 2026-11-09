"""Instruments: a snapshot goes in the record under its namespace, and a failed one is recorded as failed."""

import pytest
from record_support import hand_made

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
