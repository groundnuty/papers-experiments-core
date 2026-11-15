"""The egress instrument: counts per class, the outside destinations, and a log that is unknown when it is damaged."""

import pytest
from egress_logs import NONCE, encode, lines, log, rec

from expcore.egress import INTACT, NAMESPACE, UNKNOWN, EgressLog, declaration
from expcore.egress import instrument as egress_instrument
from expcore.records.instruments import finish_instruments

GATE_PORT = 41234
DECLARED = declaration([("127.0.0.1", GATE_PORT)], ("127.0.0.1", GATE_PORT))
RUN = [
    rec("connect", "127.0.0.1", GATE_PORT, fd=3),
    rec("result", "127.0.0.1", GATE_PORT, fd=3),
    rec("connect", "127.0.0.1", 8091, fd=4),
    rec("result", "127.0.0.1", 8091, fd=4, result=111),
    rec("connect", "192.0.2.1", 443, fd=5),
    rec("result", "192.0.2.1", 443, fd=5, result=115),
    rec("async", "-", 0, fd=5, result=110),
    rec("sendto", "192.0.2.53", 53, kind="udp", fd=6),
]


class Source:
    def __init__(self, data: bytes | None) -> None:
        self.data = data

    def egress_log(self) -> bytes | None:
        return self.data


def finish(data: bytes | None) -> dict:
    return EgressLog(Source(data), DECLARED, NONCE).finish()


def test_an_intact_log_gives_its_destinations_counted_by_class():
    """Attempts are counted, outcomes are not: a connect and a send to a new address each count once."""
    snap = finish(log(RUN))
    assert snap["log"] == INTACT
    assert snap["unknown"] == []
    assert snap["counts"] == {"gate": 1, "loopback": 1, "outside": 2}


def test_each_outside_destination_is_listed_with_its_attempts_and_results():
    """A result with no peer (a failed non-blocking connect) belongs to its socket's last connect."""
    snap = finish(log(RUN))
    assert snap["outside"] == [
        {"address": "192.0.2.1", "port": 443, "family": "inet", "type": "tcp", "attempts": 1,
         "results": {"110": 1, "115": 1}},
        {"address": "192.0.2.53", "port": 53, "family": "inet", "type": "udp", "attempts": 1, "results": {}},
    ]  # fmt: skip
    assert snap["outside_complete"] is True


def test_a_log_with_no_outside_destination_is_intact_with_zero_outside():
    snap = finish(log(RUN[:4]))
    assert (snap["log"], snap["counts"]["outside"], snap["outside"]) == (INTACT, 0, [])


@pytest.mark.parametrize("data", [None, b"", log(RUN, end=False), log(RUN)[:-9]])
def test_a_missing_or_damaged_log_is_unknown_with_no_counts(data):
    """Unknown is never zero: the counts are None, never a count of nothing."""
    snap = finish(data)
    assert snap["log"] == UNKNOWN
    assert snap["unknown"]
    assert snap["counts"] is None
    assert snap["outside_complete"] is False


def test_an_unknown_log_still_lists_the_outside_destinations_it_shows():
    snap = finish(log(RUN, end=False))
    assert [d["address"] for d in snap["outside"]] == ["192.0.2.1", "192.0.2.53"]


def test_more_outside_destinations_than_the_cap_are_counted_and_the_list_says_it_was_cut(monkeypatch):
    monkeypatch.setattr(egress_instrument, "MAX_OUTSIDE", 1)
    snap = finish(log(RUN))
    assert (len(snap["outside"]), snap["counts"]["outside"], snap["outside_cut"]) == (1, 2, True)
    assert snap["outside_complete"] is False


def test_the_snapshot_records_the_declaration_it_classified_with():
    assert finish(log(RUN))["declared"] == DECLARED.to_json()


def test_a_source_that_fails_is_recorded_as_a_failed_instrument_never_as_a_clean_log():
    """finish_instruments keeps the attempt's record and says the instrument failed (O08)."""

    class Broken:
        def egress_log(self):
            raise OSError("the log's directory is gone")

    class Record:
        def __init__(self) -> None:
            self.instruments: dict = {}

        def with_instrument(self, namespace, data):
            self.instruments = {**self.instruments, namespace: data}
            return self

    done = finish_instruments(Record(), [EgressLog(Broken(), DECLARED, NONCE)])
    assert done.instruments[NAMESPACE] == {"ok": False, "error": "OSError"}


def test_a_log_whose_end_line_is_followed_by_more_is_unknown():
    out = lines(RUN)
    out.append({**out[1], "seq": len(out), "mono": 999.0})
    assert finish(encode(out))["log"] == UNKNOWN


def test_a_log_that_opens_with_the_canary_and_holds_nothing_else_is_intact_with_zero_destinations():
    """The canary proves the shim was loaded; it is the launcher's, so it is no destination of the agent's."""
    snap = finish(log([]))
    assert (snap["log"], snap["canary"], snap["records"]) == (INTACT, True, 0)
    assert snap["counts"] == {"gate": 0, "loopback": 0, "outside": 0}


@pytest.mark.parametrize(
    "first",
    [
        [],
        [rec("connect", "127.0.0.1", GATE_PORT + 1)],
        [rec("sendto", "127.0.0.1", GATE_PORT, kind="udp")],
    ],
)
def test_a_log_that_does_not_open_with_the_canarys_connect_is_unknown(first):
    """With no canary, an empty log may be a shim that never loaded: unknown (`no_canary`), never zero."""
    snap = finish(log(first, canary=False))
    assert snap["log"] == UNKNOWN
    assert egress_instrument.NO_CANARY in snap["unknown"]
    assert (snap["canary"], snap["counts"]) == (False, None)


def test_a_canary_that_comes_after_another_record_is_no_canary():
    snap = finish(log([rec("connect", "192.0.2.1", 443), *RUN[:2]], canary=False))
    assert egress_instrument.NO_CANARY in snap["unknown"]
