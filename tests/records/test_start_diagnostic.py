"""C40: a failed instrument start says which instrument failed, and may say what it checked and what it saw.

An instrument's `start` that raises an `InfraError` is infrastructure of its kind, as it always was. The error may now
carry a `diagnostic`, a JSON object the instrument built (what was checked, what was seen), and `start_instruments`
names the instrument and the phase on the error it lets through. The error is the same object and the same class: how
the attempt is classified and charged does not change.
"""

import pytest

from expcore.errors import InfraError
from expcore.records.instruments import start_instruments

IDENTITY = {"run_key": "r", "factors": {}, "attempt": 1}


class Inducer:
    """An instrument whose start verifies a state and refuses when it is not the one asked for."""

    namespace = "state_inducer"

    def __init__(self, error: BaseException | None = None):
        self.error = error

    def start(self, identity):
        if self.error is not None:
            raise self.error

    def finish(self):
        return {"ok": True}


class Quiet:
    namespace = "quiet"

    def __init__(self):
        self.started = False

    def start(self, identity):
        self.started = True

    def finish(self):
        return {}


def test_c40_an_infra_error_may_carry_a_diagnostic_and_has_none_by_default():
    """C40: the diagnostic is optional and kept as given, so an existing raise is unchanged."""
    assert InfraError("sandbox", "not induced").diagnostic is None
    error = InfraError("sandbox", "not induced", diagnostic={"checked": "a", "seen": "b"})
    assert (error.kind, error.detail, error.diagnostic) == ("sandbox", "not induced", {"checked": "a", "seen": "b"})
    assert str(error) == "sandbox: not induced"  # the diagnostic is not part of the message


def test_c40_start_instruments_names_the_instrument_whose_start_raised_and_lets_the_same_error_through():
    """C40: the same error object, of the same kind, with the instrument and the phase on it."""
    failure = InfraError("sandbox", "the start state was not reached", diagnostic={"checked": "a", "seen": "b"})
    with pytest.raises(InfraError) as caught:
        start_instruments([Quiet(), Inducer(failure)], IDENTITY)
    assert caught.value is failure
    assert caught.value.origin == {"phase": "start", "instrument": "state_inducer"}


def test_c40_a_failed_start_without_a_diagnostic_is_still_named():
    """C40: an instrument that gives no diagnostic still has its failed start counted by instrument."""
    failure = InfraError("sandbox", "not induced")
    with pytest.raises(InfraError) as caught:
        start_instruments([Inducer(failure)], IDENTITY)
    assert (caught.value.origin, caught.value.diagnostic) == ({"phase": "start", "instrument": "state_inducer"}, None)


def test_c40_an_error_that_is_not_infrastructure_passes_through_untouched():
    """C40: a defect of an instrument is still a defect: nothing is added to it, and later instruments never start."""
    crash, later = ValueError("a bug"), Quiet()
    with pytest.raises(ValueError, match="a bug") as caught:
        start_instruments([Inducer(crash), later], IDENTITY)
    assert caught.value is crash
    assert not hasattr(crash, "origin")
    assert later.started is False


def test_c40_a_start_that_succeeds_changes_nothing():
    quiet = Quiet()
    start_instruments([quiet], IDENTITY)
    assert quiet.started
