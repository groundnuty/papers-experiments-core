# Provenance: new
"""Where a record says which targets the run's fault injector took down: the one reading the treatment rule consults.

A condition whose manipulation is a fault on one target (a provider taken offline for the run) declares the kind with
that target. Its attempt is scored as treatment only if the failure names the target and the run's injector actually
held that target down during this attempt (`Outcome.is_treatment`, record version 3). The core imports no platform, so
the injector, whichever layer implements it, is an instrument writing under `FAULT_NAMESPACE`, and its snapshot holds
at least:

    {"run_key": "<the record's run key>", "attempt": <the record's attempt>,
     "injections": [{"target": "providers[1]", "injected": true, ...}, ...]}

`injected` is true only when the injector verified the target was down before the agent started. A snapshot of another
run or attempt, one that failed (`"ok": false` with no injections), an entry whose `injected` is anything but `true`,
and a record with no such snapshot give no injected target, so their failures are set aside. Replay serves the snapshot
from the record and so decides the same way, asking the platform nothing (R13).
"""

from collections.abc import Mapping
from typing import Any

from expcore.errors import TARGET_LABEL

FAULT_NAMESPACE = "fault_injector"


def injected_targets(instruments: Mapping[str, Any], run_key: str, attempt: int) -> frozenset[str]:
    """Return the targets the fault injector's snapshot records as injected in this run's attempt; empty otherwise."""
    reading = instruments.get(FAULT_NAMESPACE)
    if not isinstance(reading, Mapping) or reading.get("run_key") != run_key:
        return frozenset()
    held = reading.get("attempt")
    if isinstance(held, bool) or held != attempt:
        return frozenset()
    entries = reading.get("injections")
    if not isinstance(entries, list):
        return frozenset()
    return frozenset(
        entry["target"]
        for entry in entries
        if isinstance(entry, Mapping)
        and entry.get("injected") is True
        and isinstance(entry.get("target"), str)
        and TARGET_LABEL.fullmatch(entry["target"])
    )
