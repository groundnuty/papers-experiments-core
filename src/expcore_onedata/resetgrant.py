# Provenance: new
"""The exclusive section of a shortcut reset: the reset lock held exclusively around a planner's reading and the reset
that uses it (O11, O51).

**What the shortcut needs.** A reset's occupancy and quiet stages read every regular file's block map at every
provider that can hold the space's bytes, twice after the eviction (O46): for the large space of the first study, about
106 s a pass. Given a planner's reading made before the eviction (`SpaceReset.plan_scan`: each file's bytes at each
provider), the stages may re-read only the files it showed held, the files a transfer touched since, and those a QoS
requirement applies to (`reset.py`), and carry the rest as empty. That is sound only if nothing placed bytes on any
other file between the reading and the last re-read; an on-demand read places bytes and leaves no transfer record, so
the only way to know is to exclude every reader. Every shared admission (a campaign run's, a tool's outside a campaign)
holds `federation.reset` shared for as long as it holds the federation (`resetlock.py`); `exclusive` takes it
exclusively, and the reading and the reset must both be made inside:

    with reset.exclusive() as grant:      # SpaceReset.exclusive or SpacesReset.exclusive
        holdings = reset.plan_scan()
        record = reset.run(holdings.plan(), planned=holdings, ...)

**How it is taken.** Under a shared admission, the run first gives up its own share of `federation.reset` (its agent
has not started) while keeping its share of `federation.lock` throughout, so no outage run can get in; then it polls
for `federation.reset` exclusively, at most `[placement] reset_exclusive_wait_s`, with no turnstile (a resetter that
held `federation.intent` while it waited could deadlock with one that took the turnstile before giving up its share,
and fairness is not needed: a stream of readers starves a resetter only into the fallback). On leaving it lets the
exclusive hold go and takes its share back on a new descriptor, within `[faults] admission_timeout_s`; past that bound
(another reset holds the lock) it raises `FederationBusy`, an infrastructure failure, and the run's infrastructure stays
refused (`admission.require_admission`). A run that holds `federation.lock` exclusively (`exclusive = true`, an outage
run) shares the federation with no one and is granted at once (`federation_exclusive`).

**The fallback, never a failure.** The grant gives no shortcut (`why`), and the reset reads every file, when:
`[placement] reset_shortcut` is off (`off`); no admission is held, or the platform has no `[faults]` (`no_admission`);
the wait ran out (`shortcut_unavailable`, with the seconds waited and the holders from `reset-holds.jsonl`: each one's
pid, boot id and process start time, `live`, or `holder_gone` when its process is gone, never its reason, O12); the hold
was granted but the record shows a holder whose process is gone (`holder_gone`: the kernel let its lock go, but a read
it sent may still be placing bytes, and a tool server it started may still run, since the MCP client starts the server
in a session of its own; the exclusive hold is let go at once and the dead holder's line is ended, and every section
refuses the shortcut for `DEAD_HOLDER_QUIET_S` after that, naming it); the record cannot be read (`holders_unread`);
or a worker of the run holds a share of its admission (`shares_retained`: a sandbox's start thread, a task reusing the
admission through a tool's hold, either of which may be reading), in which case nothing is given up.

**The seal.** A section first seals the run's admission (`Admission.seal`, granted only while the owner's is its one
share), so no worker takes a share until it ends, and nothing of the run may act (`Admission.may_act`: the sandbox and
the tool server refuse to start, a tool refuses to reuse the admission, the health check in the run's context defers).
A section belongs to its federation (`ResetGrant.federation`): a nested section for another federation's configuration
is refused rather than given the outer grant, and a reading made in one federation's section gives the full reads to a
reset of another (`resetshortcut.OTHER_FEDERATION`).
"""

import contextlib
import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from expcore_onedata.admission import (
    Admission,
    AdmissionRefused,
    Federation,
    current_admission,
    fault_settings_of,
    federation_of,
    require_same_federation,
)
from expcore_onedata.config import PlatformConfig
from expcore_onedata.rangemap import PlacementSettings
from expcore_onedata.resetlock import CLOSED, RESET_HOLDS, ResetShare
from expcore_onedata.shared_holds import HOLDER_GONE as ENDED_GONE
from expcore_onedata.shared_holds import SharedHolds

EXCLUSIVE, FEDERATION_EXCLUSIVE = "exclusive", "federation_exclusive"  # what a grant holds
OFF, NO_ADMISSION, UNAVAILABLE = "off", "no_admission", "shortcut_unavailable"  # why a grant gives no shortcut
HOLDER_GONE, HOLDERS_UNREAD, SHARES_RETAINED = "holder_gone", "holders_unread", "shares_retained"
LIVE, UNKNOWN = "live", "unknown"  # a holder's status beside HOLDER_GONE
LOCK_FREE = "lock_free"  # how a line is ended whose live process let the lock go without recording it
POLL_S = 0.05  # seconds between two tries for the exclusive hold
# How long after a holder was found dead its line still refuses the shortcut: what it left behind (a tool server in a
# session of its own, a read the provider still serves, an agent's cell in its sandbox) ends within its own bounds,
# which the core does not know, so the refusal outlasts every bound the platform declares (`admission_timeout_s`).
DEAD_HOLDER_QUIET_S = 3600.0
_GRANT: ContextVar["ResetGrant | None"] = ContextVar("expcore_onedata_reset_grant", default=None)


@dataclass
class ResetGrant:
    """What a shortcut reset's exclusive section holds: `mode` (`exclusive`, `federation_exclusive`, or None for
    nothing), `why` the shortcut is not given (None when it is), the seconds waited and the holders it names."""

    why: str | None
    mode: str | None = None
    wait_s: float = 0.0
    holders: list[dict[str, Any]] = field(default_factory=list)
    granted: bool = False  # the exclusive hold was had, even if the shortcut was then refused
    federation: Federation | None = None  # the federation whose reset lock it took
    _share: ResetShare | None = field(default=None, repr=False)
    _epoch: int | None = field(default=None, repr=False)
    _admission: Admission | None = field(default=None, repr=False)
    _ended: bool = field(default=False, repr=False)

    def is_for(self, federation: Federation | None) -> bool:
        """Whether this grant's hold is of `federation`'s reset lock: a section of one federation stands for none of
        another's, however its lock files are named."""
        return federation is not None and self.federation == federation

    @property
    def held(self) -> bool:
        """Whether the exclusive hold this grant took is still in force, never one taken later."""
        if self.mode == EXCLUSIVE and self._share is not None:
            return not self._ended and self._share.exclusive_epoch() == self._epoch
        if self.mode == FEDERATION_EXCLUSIVE and self._admission is not None:
            return not self._ended and self._admission.held
        return False

    @property
    def shortcut(self) -> bool:
        """Whether a reading and a reset made now may take the shortcut."""
        return self.why is None and self.held

    def facts(self) -> dict[str, Any]:
        """The record: the mode, why there is no shortcut, the seconds waited, whether the hold was had, the holders."""
        return {
            "mode": self.mode,
            "why": self.why,
            "wait_s": round(self.wait_s, 3),
            "granted": self.granted,
            "holders": [dict(holder) for holder in self.holders],
        }

    def end(self) -> None:
        """Let the exclusive hold go and take the run's share back (`ResetShare.take_shared`, which raises
        `FederationBusy` past the admission's bound), then unseal the admission (`Admission.seal`); once, and nothing
        for a grant that holds nothing."""
        if self._ended:
            return
        self._ended = True
        try:
            if self.mode == EXCLUSIVE and self._share is not None:
                self._share.end_exclusive()
                self._share.take_shared()
        finally:
            if self._admission is not None:
                self._admission.unseal()


def current_grant() -> "ResetGrant | None":
    """The grant of the exclusive section the running context is in, if any."""
    return _GRANT.get()


@contextlib.contextmanager
def exclusive(config: PlatformConfig, settings: PlacementSettings) -> Iterator[ResetGrant]:
    """The exclusive section of a shortcut reset (the module docstring): yield the grant, and on leaving let the hold
    go and take the run's share back. A section inside a live one uses the outer grant. Raise `AdmissionRefused` for
    an admission of another federation or one whose share is not held, and `FederationBusy` when the share cannot be
    taken back; when the body raised, its exception is the one raised, with the failure to take the share back noted
    on it (the share then stays given up, and the run's infrastructure refused)."""
    outer = _GRANT.get()
    if settings.reset_shortcut and outer is not None and outer.held and outer.is_for(federation_for(config)):
        yield outer
        return
    grant = _take(config, settings) if settings.reset_shortcut else ResetGrant(OFF)
    token = _GRANT.set(grant)
    try:
        yield grant
    except BaseException as error:  # the body's failure stays the one raised: it says why the reset failed
        _GRANT.reset(token)
        try:
            grant.end()
        except Exception as ending:  # the share is still given up, so the run's infrastructure stays refused
            error.add_note(f"and the share of the reset lock was not taken back: {type(ending).__name__}: {ending}")
        raise
    _GRANT.reset(token)
    grant.end()


def federation_for(config: PlatformConfig) -> Federation | None:
    """The federation a platform's `[faults]` describes, None without them: what a grant must be of to stand."""
    faults = fault_settings_of(config)
    return None if faults is None else federation_of(faults)


def _take(config: PlatformConfig, settings: PlacementSettings) -> ResetGrant:
    faults = fault_settings_of(config)
    admission = current_admission()
    if faults is None or admission is None:
        return ResetGrant(NO_ADMISSION)
    federation = federation_of(faults)
    require_same_federation(admission, federation)
    if admission.reset is not None and not admission.reset.shared:
        raise AdmissionRefused("a shortcut reset needs the run's share of the reset lock, held, to give up")
    if not admission.seal():  # a worker holds a share, and may be reading: the section cannot rule it out
        return ResetGrant(SHARES_RETAINED, federation=federation)
    try:
        grant = _sealed(admission, federation, SharedHolds(faults.journal, name=RESET_HOLDS), settings)
    except BaseException:
        admission.unseal()
        raise
    if not grant.shortcut:
        admission.unseal()  # no shortcut: the run's share is back, and its workers may take theirs again
    return grant


def _sealed(
    admission: Admission, federation: Federation, holds: SharedHolds, settings: PlacementSettings
) -> ResetGrant:
    """`_take` with the admission sealed, so no worker of the run can take a share meanwhile."""
    if admission.exclusive:  # no shared admission can hold the federation, nor so the reset lock
        why, gone = _settled(holds)
        return ResetGrant(why, FEDERATION_EXCLUSIVE, 0.0, gone, True, federation, _admission=admission)
    share = admission.reset
    assert share is not None  # a shared admission's, checked by `_take`
    began = time.monotonic()
    share.give_up()
    try:
        granted = _wait(share, settings.reset_exclusive_wait_s)
    except BaseException:  # an interrupt: the run must not be left without its share
        share.end_exclusive()
        share.take_shared()
        raise
    waited = time.monotonic() - began
    if not granted:
        holders = _holders(holds)
        share.take_shared()
        why = UNAVAILABLE if holders is not None else HOLDERS_UNREAD
        return ResetGrant(why, None, waited, holders or [], federation=federation)
    epoch = share.exclusive_epoch()
    why, gone = _settled(holds)
    if why is not None:
        share.end_exclusive()
        share.take_shared()
        return ResetGrant(why, None, waited, gone, True, federation)
    return ResetGrant(None, EXCLUSIVE, waited, [], True, federation, _share=share, _epoch=epoch, _admission=admission)


def _wait(share: ResetShare, wait_s: float) -> bool:
    """Poll for the exclusive hold until `wait_s` has passed (one try for 0); whether it was had."""
    deadline = time.monotonic() + wait_s
    while not share.try_exclusive():
        if share.state == CLOSED or time.monotonic() >= deadline:
            return False
        time.sleep(POLL_S)
    return True


def holder_of(hold: Mapping[str, Any]) -> dict[str, Any]:
    """One holder as a record names it: pid, boot id and process start time, when it began, whether it is a campaign
    attempt, and its status: `live`, `holder_gone` when its process is gone, `unknown` when that cannot be told."""
    named = hold.get("holder") if isinstance(hold.get("holder"), Mapping) else {}
    owner = hold.get("owner") if isinstance(hold.get("owner"), Mapping) else {}
    parts = str(owner.get("process", "")).split(":")
    live = hold.get("live")
    return {
        "pid": named.get("pid"),
        "boot_id": named.get("boot_id"),
        "start_time": parts[2] if len(parts) == 3 else None,
        "since_utc": hold.get("since_utc"),
        "in_campaign": "attempt_id" in owner,
        "status": LIVE if live is True else HOLDER_GONE if live is False else UNKNOWN,
    }


def _holders(holds: SharedHolds) -> list[dict[str, Any]] | None:
    """The holders the record names for a wait that ran out; None when the record cannot be read."""
    try:
        return [holder_of(hold) for hold in holds.open_holds()]
    except (OSError, ValueError):
        return None


def _settled(holds: SharedHolds) -> tuple[str | None, list[dict[str, Any]]]:
    """Under the exclusive hold, which proves that no shared admission holds the reset lock: end every open line, as
    `holder_gone` when its process is gone or cannot be told, as `lock_free` when it runs (it let the lock go and its
    end was not recorded); and return why the shortcut is refused, with the holders that refuse it: a holder whose
    process is gone or cannot be told, found now or within `DEAD_HOLDER_QUIET_S` before (a line ended `holder_gone`
    since then), or a record that cannot be read or ended."""
    try:
        opened = holds.open_holds()
        gone = [holder_of(hold) for hold in opened if hold["live"] is not True]
        for hold in opened:
            holds.end(hold["id"], LOCK_FREE if hold["live"] is True else ENDED_GONE)
        recent = [holder_of({**hold, "live": False}) for hold in holds.holds() if _recently_gone(hold, opened)]
    except (OSError, ValueError):
        return HOLDERS_UNREAD, []
    refusing = gone + recent
    return (HOLDER_GONE if refusing else None), refusing


def _recently_gone(hold: Mapping[str, Any], opened: list[dict[str, Any]]) -> bool:
    """Whether a line was ended as `holder_gone` before this settle, less than `DEAD_HOLDER_QUIET_S` ago (or at a time
    that cannot be read): what its dead holder left behind may still place bytes."""
    if hold.get("how") != ENDED_GONE or any(hold["id"] == other["id"] for other in opened):
        return False
    try:
        until = datetime.fromisoformat(str(hold.get("until_utc")))
    except ValueError:
        return True
    if until.tzinfo is None:
        return True
    return (datetime.now(UTC) - until).total_seconds() < DEAD_HOLDER_QUIET_S


__all__ = [
    "DEAD_HOLDER_QUIET_S",
    "EXCLUSIVE",
    "FEDERATION_EXCLUSIVE",
    "HOLDERS_UNREAD",
    "HOLDER_GONE",
    "NO_ADMISSION",
    "OFF",
    "SHARES_RETAINED",
    "UNAVAILABLE",
    "ResetGrant",
    "current_grant",
    "exclusive",
    "federation_for",
    "holder_of",
]
