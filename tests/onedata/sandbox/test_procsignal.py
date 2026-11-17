"""O72: the harness half of a signal (`procsignal.signal_processes`) takes the root half's answer only when it is the
answer to its request, and what a process no signal reached costs.

A pid read from an answer as another one's (a float coerced to an integer, an entry out of order) could mark a process
done that no signal reached: a worker in D that can run again would then pass as quiet."""

import json

import pytest

from expcore_onedata.sandbox import procsignal
from expcore_onedata.sandbox.hostops import CmdResult

PROCS = [
    {"pid": 42, "start": "100", "cmd_sha256": "a" * 64},
    {"pid": 43, "start": "101", "cmd_sha256": "b" * 64},
]


class Answering:
    """A host whose privileged command gives this answer, and keeps what it was asked."""

    def __init__(self, stdout: str, returncode: int = 0, stderr: str = "") -> None:
        self.result, self.asked = CmdResult(returncode, stdout, stderr), []

    def run(self, argv, timeout_s, *, sudo=False, input=None):
        self.asked.append((list(argv), timeout_s, sudo, json.loads(input)))
        return self.result


def answer(*entries) -> str:
    return json.dumps({"results": list(entries)})


def entry(pid, outcome="signalled"):
    return {"pid": pid, "outcome": outcome}


def test_o72_a_well_formed_answer_is_read_by_pid_and_each_outcome_decides_whether_it_was_reached():
    host = Answering(answer(entry(42, "signalled"), entry(43, "changed")))
    sent = procsignal.signal_processes(host, PROCS, "KILL", 5.0)
    assert sent.failed is None
    assert [sent.reached(p) for p in PROCS] == [True, False]
    assert sent.declined(PROCS) == [PROCS[1]]
    argv, timeout_s, sudo, request = host.asked[0]
    assert (argv[-1], timeout_s, sudo) == ("KILL", 5.0, True)
    assert request == {
        "targets": [
            {"pid": 42, "start": "100", "cmd_sha256": "a" * 64},
            {"pid": 43, "start": "101", "cmd_sha256": "b" * 64},
        ]
    }


@pytest.mark.parametrize(("outcome", "reached"), [("signalled", True), ("gone", True), ("replaced", True),
                                                 ("changed", False), ("unreadable", False), ("refused", False),
                                                 ("failed", False), ("something new", False)])  # fmt: skip
def test_o72_only_a_signal_sent_a_process_found_gone_or_replaced_has_reached_it(outcome, reached):
    sent = procsignal.signal_processes(Answering(answer(entry(42, outcome))), PROCS[:1], "KILL", 5.0)
    assert sent.reached(PROCS[0]) is reached


@pytest.mark.parametrize(
    "stdout",
    [
        answer(entry(42.5, "gone"), entry(43, "gone")),  # a pid coerced from a float would read as 42
        answer(entry(True, "gone"), entry(43, "gone")),  # and a bool as 1
        answer(entry("42", "gone"), entry(43, "gone")),
        answer(entry(43, "gone"), entry(42, "gone")),  # out of order
        answer(entry(42, "gone")),  # a target missing
        answer(entry(42, "gone"), entry(43, "gone"), entry(44, "gone")),  # one more than asked
        answer(entry(42, 7), entry(43, "gone")),  # an outcome that is not text
        answer(1, 2),
        answer({"outcome": "gone"}, {"outcome": "gone"}),
        json.dumps({"results": "gone"}),
        json.dumps({"results": None}),
        json.dumps({"outcomes": []}),
        json.dumps([]),
        "not json",
        "",
    ],
    ids=lambda text: text[:48],
)
def test_o72_an_answer_that_is_not_the_answer_to_the_request_reaches_nobody(stdout):
    """O72: a process counts as reached only by an answer naming its own pid, in the order asked; nothing is coerced."""
    sent = procsignal.signal_processes(Answering(stdout), PROCS, "KILL", 5.0)
    assert sent.failed
    assert not any(sent.reached(p) for p in PROCS)
    assert sent.declined(PROCS) == PROCS


def test_o72_a_command_that_fails_as_a_whole_has_reached_nobody_and_says_why():
    sent = procsignal.signal_processes(Answering("", 1, "sudo: a password is required\n"), PROCS, "KILL", 5.0)
    assert sent.failed == "exit 1: sudo: a password is required"
    assert sent.declined(PROCS) == PROCS


def test_o72_nothing_is_asked_when_nothing_is_listed():
    host = Answering("")
    assert procsignal.signal_processes(host, [], "KILL", 5.0).outcomes == {}
    assert host.asked == []
