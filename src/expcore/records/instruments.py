# Provenance: new
"""Run the instruments around a run and put what they measured into the record.

`start_instruments` is called before the run and `finish_instruments` after it, by the study's executor: nothing in the
loop calls an instrument for you. Both run outside the agent's time. The record gets one entry per instrument under its
namespace, `RunRecord.instruments[namespace]`, and a scorer reads that entry and nothing else (R13).

A finish that raises, or returns something that is not a JSON object, is recorded as `{"ok": False, "error": <class>}`
and never dropped or guessed (O08): the attempt keeps its record, and a scorer that needs the snapshot sees it failed.
Two instruments with one namespace, or a namespace that is not lower-case letters, digits and underscores, are
refused before anything runs.
"""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

from expcore.plugin import Instrument
from expcore.records.digest import canonical
from expcore.records.record import RunRecord

NAMESPACE = re.compile(r"[a-z][a-z0-9_]*")


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


def finish_instruments(record: RunRecord, instruments: Sequence[Instrument]) -> RunRecord:
    """Return the record with each instrument's snapshot under its namespace; a failed one is recorded as failed."""
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
    return record
