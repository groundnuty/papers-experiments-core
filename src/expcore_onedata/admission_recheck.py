# Provenance: new
"""The run admission's re-check of the fault gate's state: no run admitted on a pre-run check a heal outdated (O35).

A run's pre-run check can pass before a heal, or before a pair fails, and the run then waits for its lease and for the
federation's admission (`admission.run_admission`). A heal that runs in that window (the fault gate's,
`fault_gate_heal`, or an outage run's restore, `fault_restore`), or a link check that finds a pair failing, would
otherwise let the run in onto a stuck link once the heal lets the federation go, before the next link check. So:

- **The check notes the link epoch.** The fault gate (`fault_gate.FaultGate`) reads the federation's link epoch
  (`fault_epoch`) under the shared hold of a check that holds no run's admission, and notes it in the try's note
  (`expcore.campaign.prerun`). Only a run's pre-run check has a note: the dispatcher's own check does not, and the
  post-run check, under the run's admission, notes nothing.
- **The admission compares it** (`refusal`), holding the lock it took, so no heal can begin while it decides. It refuses
  the run as the hold `federation_busy`, which uses no attempt and is never scored, when:
  - the try noted no epoch: its check never reached the fault gate, so when it was made is unknown, and the run is
    checked again, never admitted blind;
  - the epoch moved since the note: a heal began, or the gate's link check found a pair failing, after the run's check
    read it;
  - the heal record holds a heal begun since the gate's link check last passed (`GateHeals.unpassed`), whether or not
    the epoch counted it;
  - either record cannot be read.

  The refusal's words say which, and the campaign puts them in the run's `run_unstarted` event. It then asks again,
  and the run's next pre-run check sees the federation as it is now.
- **A run with no note at all is admitted as before.** No pre-run check was asked (a campaign with no health check, or
  a caller that is no campaign), so no check vouched for anything an admission could find outdated.
"""

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from expcore_onedata.fault_epoch import GATE_NOTE, HEAL, LINKS_FAILED, LinkEpoch
from expcore_onedata.fault_gate_heal import GateHeals

_SAID = {HEAL: "a heal began", LINKS_FAILED: "the fault gate's link check found a pair failing"}
_STAMP = re.compile(r"[0-9T:+.-]{10,40}")
_AGAIN = "the run is checked again before it is admitted, and no attempt is used"


def refusal(directory: Path, note: Mapping[str, Any] | None) -> str | None:
    """Why a run whose try holds `note` (`prerun.current()`) may not be admitted now, or None when it may; call it with
    the federation's lock held. `directory` is the fault journal's (see the module docstring)."""
    if note is None:
        return None
    noted = note.get(GATE_NOTE)
    if isinstance(noted, bool) or not isinstance(noted, int):
        return (
            "the run's pre-run check noted no link epoch of the fault gate, so whether a heal ran or a pair failed "
            f"since it is unknown: {_AGAIN}"
        )
    try:
        record = LinkEpoch(directory).read()
        unpassed = GateHeals(directory).unpassed()
    except (OSError, ValueError) as error:  # its type only (O12)
        unread = "the federation's link epoch or the fault gate's heal record cannot be read"
        return f"{unread} ({type(error).__name__}): {_AGAIN}"
    epoch = record["epoch"]
    if epoch != noted:
        said = _SAID.get(str(record.get("event")), "the fault gate's state changed")
        utc = record.get("utc")
        at = f" at {utc}" if isinstance(utc, str) and _STAMP.fullmatch(utc) else ""
        return (
            f"since the run's pre-run check the federation's link epoch moved from {noted} to {epoch}, the latest "
            f"because {said}{at}: {_AGAIN}"
        )
    if unpassed:
        return f"a heal has begun since the fault gate's link check last passed: {_AGAIN}"
    return None
