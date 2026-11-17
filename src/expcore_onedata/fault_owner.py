# Provenance: new
"""Whose an open injection is, for the health verdicts that name it (O80).

An injection that an attempt left open (its restore failed, or the attempt ended without clearing it) closes the
federation for every run that follows, and the attempt that left it was charged already by its own post-run check. A
run that merely meets it, in its pre-run check or in another attempt's post-run check, did nothing: charging it makes
an innocent run the one attempt of an outage it did not cause. So the injector writes the **owner** of each
`injecting` line, as the baseline marks do (`liveness.owner`: the process and, inside an attempt, its campaign and
the store's id of the attempt), and the verdicts (`FaultGate`, `SettledInjections`) charge by it:

- an entry owned by **the attempt this context runs** is charged to it, exactly as before: a run's own post-run check
  of its own failed restore is never spared;
- every entry owned by **another attempt**, and the check judging none (a pre-run check, the dispatcher's own), holds
  uncharged, and its detail says so (`LEFT_BY_ANOTHER`: short, since the gate keeps 200 characters of a detail);
- an entry that **names no attempt**, because it records no owner (written before owners were recorded, or one that
  cannot be read) or only a process (a heal's container restart, or a tool's injection outside any attempt), is
  charged as before and its detail says so (`NO_ATTEMPT`): an owner is never guessed.

With several entries open, the judged attempt's own wins over any other, and one that names no attempt keeps the
charge over those of other attempts, so nothing that cannot be told apart is ever spared. A journal that cannot be
read is no entry at all: its check fails closed and charges, as it did.
"""

from collections.abc import Iterable, Mapping
from typing import Any

from expcore.campaign import liveness

LEFT_BY_ANOTHER = "left by another attempt, so no run is charged for it"
NO_ATTEMPT = "it records no attempt as its owner: charged as before"  # short: the gate keeps 200 characters of a detail


def _attempt_of(owner: Any) -> tuple[Any, Any, Any] | None:
    """The (process, campaign, attempt id) an owner token names, or None when it names no attempt."""
    if not isinstance(owner, Mapping):
        return None
    attempt_id = owner.get("attempt_id")
    if isinstance(attempt_id, bool) or not isinstance(attempt_id, int):
        return None
    process, campaign = owner.get("process"), owner.get("campaign")
    if not isinstance(process, str) or not isinstance(campaign, str):
        return None
    return process, campaign, attempt_id


def charge_of(entries: Iterable[Mapping[str, Any]]) -> tuple[bool, str | None]:
    """Whether a verdict naming these open injections charges the run it fails, and the sentence its detail adds
    (None when it charges the judged attempt for its own, which adds nothing: the detail is what it was)."""
    here = liveness.current()
    judged = None if here is None else (here.get("process"), here.get("campaign"), here.get("attempt_id"))
    unattributed = False
    for entry in entries:
        owner = _attempt_of(entry.get("owner"))
        if owner is None:
            unattributed = True
        elif owner == judged:
            return True, None
    return (True, NO_ATTEMPT) if unattributed else (False, LEFT_BY_ANOTHER)


def with_note(detail: str, note: str | None) -> str:
    """A verdict's detail followed by what `charge_of` says of its charge."""
    return detail if note is None else f"{detail}; {note}"
