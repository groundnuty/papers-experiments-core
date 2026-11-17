"""Where "complete and no request" is decided: a readable, complete call log that holds no request, no top-up on the
attempt, and the backend's own count of its requests confirming none (C05)."""

import json

import pytest

from expcore.campaign.no_request import no_request_in


class Counting:
    """A backend that counts its requests."""

    def __init__(self, sent):
        self.requests_sent = sent


@pytest.mark.parametrize(
    ("sent", "usage", "topup", "backend", "expected"),
    [
        (0, "[]", 0.0, Counting(0), True),
        (0, "[]", None, Counting(0), False),  # a top-up total that is missing is not none
        (0, "[]", 0, Counting(0), True),
        (0, "[]", True, Counting(0), False),
        (0, "[]", "0", Counting(0), False),
        (0, "[]", 0.0, object(), False),  # a backend that keeps no count confirms nothing
        (0, "[]", 0.0, Counting(1), False),
        (0, "[]", 0.0, Counting(None), False),
        (0, "[]", 0.0, Counting(True), False),  # a count that is no whole number confirms nothing
        (0, "[]", 0.0, Counting("0"), False),
        (0, "[]", 0.25, Counting(0), False),
        (1, "[]", 0.0, Counting(0), False),
        (1, '["failed"]', 0.0, Counting(0), False),
        (0, "[null]", 0.0, Counting(0), False),
        (0, "not json", 0.0, Counting(0), False),
        (0, None, 0.0, Counting(0), False),
        (None, "[]", 0.0, Counting(0), False),
        (True, "[]", 0.0, Counting(0), False),
        (-1, "[]", 0.0, Counting(0), False),
        (0, json.dumps([[1, 1, 0, 0]]), 0.0, Counting(0), False),
    ],
)
def test_c05_no_request_is_decided_only_by_a_readable_complete_empty_log_no_top_up_and_a_count_of_none(
    sent, usage, topup, backend, expected
):
    """C05: the one place where "complete and no request" is decided."""
    assert no_request_in(sent, usage, topup, backend) is expected


def test_c05_a_backend_whose_count_cannot_be_read_confirms_nothing():
    """C05: a count that raises is no evidence of none: the attempt is charged its bound."""

    class Broken:
        @property
        def requests_sent(self):
            raise RuntimeError("the counter is gone")

    assert no_request_in(0, "[]", 0.0, Broken()) is False
