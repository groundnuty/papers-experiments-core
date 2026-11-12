# Provenance: new
"""Run the instruments around a run and put what they measured into the record.

`start_instruments` is called before the run and `finish_instruments` after it, by the study's executor: nothing in the
loop calls an instrument for you. Both run outside the agent's time. The record gets one entry per instrument under its
namespace, `RunRecord.instruments[namespace]`, and a scorer reads that entry and nothing else (R13).

A finish that raises, or returns something that is not a JSON object, is recorded as `{"ok": False, "error": <class>}`
and never dropped or guessed (O08): the attempt keeps its record, and a scorer that needs the snapshot sees it failed.
Two instruments with one namespace, or a namespace that is not lower-case letters, digits and underscores, are
refused before anything runs.

Two optional methods let an instrument refuse the attempt (C31). `first_turn_instruments` calls `at_first_turn` on the
instruments that have it, just before the loop starts the agent's clock and so before the first request, with one
reading of the run's clock that the record logs under `FIRST_TURN_LABEL`. `finish_instruments` calls `check` on the
instruments that have it once every `finish` has run. Either refuses with an `InfraError` of a kind no condition can
declare as its treatment (the registry's untreatable kinds, a study's own among them). From the first-turn hook that
reaches the executor's caller as infrastructure of that kind, and the attempt, which has not sent a request, is set
aside like any other infrastructure failure. From the check it is written into the record (`instrument_refusal`) and
the scheduler sets the attempt aside next to its other post-run checks, so the attempt keeps its record and its usage.
Anything else an instrument raises there, an `InfraError` of a kind a condition may declare included, is a defect of
the instrument and reaches the executor's caller as it is. A run whose instruments have neither method takes no
reading and writes nothing new.
"""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from expcore.errors import InfraError, is_treatable
from expcore.loop.outcomes import DETAIL_LIMIT
from expcore.loop.timer import AgentTimer
from expcore.plugin import FIRST_TURN_LABEL, Instrument
from expcore.records.digest import canonical
from expcore.records.record import RunRecord

NAMESPACE = re.compile(r"[a-z][a-z0-9_]*")


class InstrumentRefusalError(ValueError):
    """An instrument refused with an `InfraError` of a kind a condition may declare as its treatment.

    Such a refusal would let the attempt it set aside be scored as the manipulation, so it is the instrument that is
    wrong: this is a defect of the instrument, not infrastructure of that kind.
    """


def check_instruments(instruments: Sequence[Instrument]) -> None:
    """Refuse a namespace that is not valid, and one that two instruments share."""
    seen: set[str] = set()
    for instrument in instruments:
        name = getattr(instrument, "namespace", None)
        if not isinstance(name, str) or not NAMESPACE.fullmatch(name):
            raise ValueError(f"instrument namespace {name!r} must be lower-case letters, digits and underscores")
        if name in seen:
            raise ValueError(f"two instruments share the namespace {name!r}")
        seen.add(name)


def start_instruments(instruments: Sequence[Instrument], identity: Mapping[str, Any]) -> None:
    """Check the instruments, then let each take its first reading. An instrument that raises here is a defect."""
    check_instruments(instruments)
    for instrument in instruments:
        instrument.start(identity)


def _method(instrument: Instrument, name: str) -> Any:
    """The optional method of an instrument, or None when it has none."""
    method = getattr(instrument, name, None)
    return method if callable(method) else None


def _require_untreatable(instrument: Instrument, error: InfraError, when: str) -> None:
    """Raise `InstrumentRefusalError` when the kind of a refusal is one a condition may declare as its treatment: that
    is no refusal but a defect of the instrument, as the refused attempt could be scored as the manipulation."""
    if is_treatable(error.kind):
        raise InstrumentRefusalError(
            f"instrument {instrument.namespace!r} refused {when} with {error.kind!r}, a kind a condition may declare "
            "as its treatment: a refusal must be of a kind that none can, or the refused attempt could be scored"
        ) from error


def _refusal(instrument: Instrument, error: InfraError, when: str) -> InfraError:
    """The error to raise for an instrument's refusal: the same kind, naming the instrument."""
    _require_untreatable(instrument, error, when)
    return InfraError(error.kind, f"instrument {instrument.namespace!r} refused {when}: {error.detail}")


def first_turn_instruments(instruments: Sequence[Instrument], timer: AgentTimer) -> None:
    """Let each instrument that has `at_first_turn` read the state at the agent's first turn, in order.

    The executor calls this after its last step of its own and just before `record_run`, so before the loop starts the
    agent's clock and before the first request is sent; the timer is the request's, not yet started. One reading of
    the run's clock is taken, logged under `FIRST_TURN_LABEL`, and given to each hook as `{"clock_s": <reading>}`. With
    no instrument that has the hook no reading is taken, so a run without one is unchanged.

    An `InfraError` of a kind no condition can declare as its treatment is the instrument's refusal: it is raised again
    naming the instrument, and the executor lets it reach its caller, which sets the attempt aside as infrastructure of
    that kind (no request was sent, so no turn was completed and the kind is never treatment, whatever a condition
    declares). Any other exception is a defect of the instrument.
    """
    check_instruments(instruments)
    hooked = [(i, hook) for i in instruments if (hook := _method(i, "at_first_turn")) is not None]
    if not hooked:
        return
    if timer.started:
        raise RuntimeError("the first-turn hook runs before the loop starts the agent's clock, not after")
    moment = {"clock_s": timer.read(FIRST_TURN_LABEL)}
    for instrument, hook in hooked:
        try:
            hook(moment)
        except InfraError as error:
            raise _refusal(instrument, error, "at the first turn") from error


def _refused_by(instruments: Sequence[Instrument]) -> dict[str, Any] | None:
    """Run the checks in order and return the first refusal as the record keeps it, or None when every one passed."""
    for instrument in instruments:
        check = _method(instrument, "check")
        if check is None:
            continue
        try:
            check()
        except InfraError as error:
            _require_untreatable(instrument, error, "after the run")
            return {"namespace": instrument.namespace, "infra_kind": error.kind, "detail": error.detail[:DETAIL_LIMIT]}
    return None


def finish_instruments(record: RunRecord, instruments: Sequence[Instrument]) -> RunRecord:
    """Return the record with each instrument's snapshot under its namespace; a failed one is recorded as failed.

    Then each instrument that has `check` is asked, in order, once every snapshot is in; the first to refuse stops the
    rest, and its refusal is in the returned record (`RunRecord.instrument_refusal`), snapshots and all, so the attempt
    keeps its record. A check runs even when its instrument's `finish` failed: unknown is not a pass, and the check
    decides. The executor calls this after the sandbox's collection and before its teardown, so a check can still read
    what the run left. A check that raises a kind a condition may declare, or anything but a refusal, raises here.
    """
    check_instruments(instruments)
    for instrument in instruments:
        try:
            data = instrument.finish()
            if not isinstance(data, Mapping):
                raise TypeError("an instrument's snapshot must be a JSON object")
            snapshot: dict[str, Any] = json.loads(canonical(dict(data)))
        except Exception as error:  # a failing instrument must not cost the attempt its record
            snapshot = {"ok": False, "error": type(error).__name__}
        record = record.with_instrument(instrument.namespace, snapshot)
    refusal = _refused_by(instruments)
    return record if refusal is None else record.with_instrument_refusal(refusal)
