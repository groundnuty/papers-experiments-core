# Provenance: new
"""The run admission's re-check of the fault gate's state: no run admitted on a pre-run check a heal outdated (O35).

A run's pre-run check can pass before a heal, or before a pair fails, and the run then waits for its lease and for the
federation's admission (`admission.run_admission`). A heal that runs in that window (the fault gate's,
`fault_gate_heal`, or an outage run's restore, `fault_restore`), or a link check that finds a pair failing, would
otherwise let the run in onto a stuck link once the heal lets the federation go, before the next link check. So:

- **The check notes the link epoch.** The fault gate (`fault_gate.FaultGate`) reads the federation's link epoch
  (`fault_epoch`, made when there is none) under the shared hold of a check that holds no run's admission, and notes
  its `id` and count in the try's note (`expcore.campaign.prerun`). Only a run's pre-run check has a note: the
  dispatcher's own check does not, and the post-run check, under the run's admission, notes nothing.
- **The admission compares it** (`refusal`), holding the federation's lock it took, so no heal can begin while it
  decides, and holding `link-epoch.lock` exclusively (`LinkEpoch.settled`), so it waits for every link check of the
  gate under way to end and never reads between a check's finding a pair failing and its raise of the count. It
  refuses the run as the hold `federation_busy`, which uses no attempt and is never scored, when:
  - the try noted no epoch: its check never reached the fault gate, so when it was made is unknown, and the run is
    checked again, never admitted blind;
  - the epoch moved since the note: a heal began, or the gate's link check found a pair failing, after the run's check
    read it; or its record is missing, or was made anew (a new `id`): a raise could not be written;
  - the heal record holds a heal begun since the gate's link check last passed (`GateHeals.unpassed`), whether or not
    the epoch counted it;
  - either record cannot be read;
  - a link check under way outlasts the admission's bound (`admission_timeout_s`), or the campaign ends meanwhile.

  The refusal's words say which, and the campaign puts them in the run's `run_unstarted` event. It then asks again,
  and the run's next pre-run check sees the federation as it is now.
- **A run with no note at all is admitted.** It is a caller that is no campaign (a tool outside one, a test): no
  pre-run check was asked, so no check vouched for anything an admission could find outdated. It is never a campaign
  with `[faults]`, which never runs without a health check, two ways:
  - **At load.** The platform's run admission declares that it needs one (`HEALTH_REQUIRED`, through
    `expcore.campaign.health_required`), and the scheduler refuses, when it is built and before its lock or store, a
    campaign that has the admission and no health check, since nothing would ever write a note and every run would be
    admitted blind.
  - **At run time, for a declaration the refusal never saw** (an admission a study wrapped in a function of its own
    without `functools.wraps`, or a runner built by hand): a try of a campaign with no health check has a note that says
    so (`prerun.UNCHECKED`), and this admission, reached through the wrapper, refuses it as `AdmissionRefused`, a
    configuration error and not an `InfraError`, so the campaign stops loudly with no attempt made, charged or scored,
    never a run admitted and never one asked again for ever.

  A check that never reaches the fault gate still has a note, with no epoch, so its runs are refused above until it
  does.
"""

import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from expcore.campaign import prerun
from expcore_onedata.fault_epoch import GATE_NOTE, HEAL, LINKS_FAILED, LinkEpoch
from expcore_onedata.fault_gate_heal import GateHeals

# What the platform's run admission declares: why a campaign with `[faults]` needs a health check, and what to pass.
HEALTH_REQUIRED = (
    "an outage-capable federation needs the gate's health check, or runs would be admitted without a link verdict; "
    "pass the platform's health check (expcore_onedata.health.PlatformHealth, which runs the fault gate, "
    "expcore_onedata.fault_gate.FaultGate, around the whole check) to the campaign"
)
_SAID = {HEAL: "a heal began", LINKS_FAILED: "the fault gate's link check found a pair failing"}
_STAMP = re.compile(r"[0-9T:+.-]{10,40}")
_AGAIN = "the run is checked again before it is admitted, and no attempt is used"


def _noted(note: Mapping[str, Any]) -> tuple[str, int] | None:
    """The record's `id` and count the try's pre-run check noted, or None when it noted none that is whole."""
    noted = note.get(GATE_NOTE)
    if not isinstance(noted, Mapping):
        return None
    key, epoch = noted.get("id"), noted.get("epoch")
    if not isinstance(key, str) or isinstance(epoch, bool) or not isinstance(epoch, int):
        return None
    return key, epoch


def unchecked_refusal(note: Mapping[str, Any] | None) -> str | None:
    """Why a run whose try's `note` (`prerun.current()`) says its campaign has no health check may not be admitted, or
    None for a try with a check and for a caller that is no campaign (no note). The admission raises it as
    `AdmissionRefused`; see the module docstring."""
    if note is None or not note.get(prerun.UNCHECKED):
        return None
    return (
        f"the campaign has no health check, and the run admission needs one: {HEALTH_REQUIRED}. The scheduler refuses "
        "such a campaign when it is built, unless the declaration never reached it: an admission wrapped in a function "
        "of its own must keep its requires_health_check attribute (functools.wraps does)"
    )


def refusal(
    directory: Path, note: Mapping[str, Any] | None, timeout_s: float | None, cancel: threading.Event | None = None
) -> str | None:
    """Why a run whose try holds `note` (`prerun.current()`) may not be admitted now, or None when it may; call it with
    the federation's lock held. `directory` is the fault journal's; the wait for the link checks under way to end is
    bounded by `timeout_s` and stops at `cancel` (see the module docstring)."""
    if note is None:
        return None
    noted = _noted(note)
    if noted is None:
        return (
            "the run's pre-run check noted no link epoch of the fault gate, so whether a heal ran or a pair failed "
            f"since it is unknown: {_AGAIN}"
        )
    epochs = LinkEpoch(directory)
    with epochs.settled(timeout_s, cancel) as settled:  # no check's finding a pair failing is read before its raise
        if not settled:
            return f"a link check of the fault gate was still under way when the admission's wait ran out: {_AGAIN}"
        try:
            record = epochs.read()
            unpassed = GateHeals(directory).unpassed()
        except (OSError, ValueError) as error:  # its type only (O12)
            unread = "the federation's link epoch or the fault gate's heal record cannot be read"
            return f"{unread} ({type(error).__name__}): {_AGAIN}"
    return _outdated(noted, record, unpassed)


def _outdated(noted: tuple[str, int], record: Mapping[str, Any] | None, unpassed: bool) -> str | None:
    """Why the record as read outdates the run's check, or None when it does not."""
    if record is None:
        return f"the federation's link epoch is missing (a raise of it could not be written): {_AGAIN}"
    if record["id"] != noted[0]:
        return f"the federation's link epoch was made anew since the run's pre-run check (a raise failed): {_AGAIN}"
    epoch = record["epoch"]
    if epoch != noted[1]:
        said = _SAID.get(str(record.get("event")), "the fault gate's state changed")
        utc = record.get("utc")
        at = f" at {utc}" if isinstance(utc, str) and _STAMP.fullmatch(utc) else ""
        return (
            f"since the run's pre-run check the federation's link epoch moved from {noted[1]} to {epoch}, the latest "
            f"because {said}{at}: {_AGAIN}"
        )
    if unpassed:
        return f"a heal has begun since the fault gate's link check last passed: {_AGAIN}"
    return None
