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

**A restore that was not verified is never scored (O11).** Once the injector has taken a target down, its reading also
holds `restore_verified`: `true` when the restore was verified (the provider runs with an identity other than the one
before the injection, and serves), `false` with `how` (a word such as `not_serving`), `identity_changed` and `serving`
when it was not. The provider may then still be down or half restored, so whatever the run measured says nothing of
the manipulation. A record that carries `restore_verified` as anything but `true`, in any instrument's reading, is set
aside (`RunRecord.set_aside`) and the campaign sets its attempt aside as infrastructure of `INJECTOR_FAILURE`, a kind no
condition can declare as its treatment, whatever its outcome was and whatever the post-run health check decides
(`restore_refusal`). The field is optional and additive: a reading from before it has none, and is refused for nothing.
"""

import re
from collections.abc import Mapping
from typing import Any

from expcore.errors import TARGET_LABEL

FAULT_NAMESPACE = "fault_injector"
# The fault injector's own kind of failure: it could not inject, clear or verify its fault. The platform that writes
# the reading registers it (untreatable), and its name is here so that the core can set an attempt aside as that kind.
INJECTOR_FAILURE = "injector_failure"
RESTORE_VERIFIED = "restore_verified"
_WORD = re.compile(r"[A-Za-z_]{1,40}")  # how a restore ended, as a word: nothing else of a reading goes into a message


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


def restore_refusal(instruments: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the refusal a record's instruments make of the attempt when a reading says a restore was not verified, in
    the shape of an instrument's refusal (`namespace`, `infra_kind`, `detail`), or None when none does.

    Every instrument's entry is read, in the order of the namespaces, not only the injector's own namespace: a reading
    the executor filed by hand under another name does not hide the failure. An entry that holds `restore_verified` as
    anything but `true` is a restore that was not shown verified. The detail is built from the entry's position label,
    one plain word and two answers, never from its text, so a reading cannot put anything else into a message (O12).
    """
    for namespace in sorted(instruments):
        reading = instruments[namespace]
        if isinstance(reading, Mapping) and RESTORE_VERIFIED in reading and reading[RESTORE_VERIFIED] is not True:
            return {"namespace": namespace, "infra_kind": INJECTOR_FAILURE, "detail": _detail(reading)}
    return None


def _detail(reading: Mapping[str, Any]) -> str:
    """Say which provider, how its restore ended and what was checked, from the fields of a reading that holds them."""
    target, how = reading.get("target"), reading.get("how")
    label = target if isinstance(target, str) and TARGET_LABEL.fullmatch(target) else "the provider"
    word = how if isinstance(how, str) and _WORD.fullmatch(how) else "failed"
    changed = "changed" if reading.get("identity_changed") is True else "did not change to a running one"
    served = "served" if reading.get("serving") is True else "did not serve"
    return f"the restore of {label} is not verified ({word}): its identity {changed} and it {served}"
