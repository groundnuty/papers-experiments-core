# Provenance: new
"""Every reset's exclusive hold of the reset lock, and the shortcut that rests on it (O11, O51, O66).

**Why every reset holds it.** A reset evicts, cancels transfers and reads every file's block map; a reader beside it
(another run's agent, a tool outside a campaign, a health check) can place bytes it then misses, or have a file evicted
under it. Every shared admission (a campaign run's, a tool's outside a campaign) holds `federation.reset` shared for as
long as it holds the federation (`resetlock.py`). So every reset, full or shortcut, holds it exclusively for its whole
run: `SpaceReset.run` and `SpacesReset` take the hold themselves (`exclusive`), or use the live section of the caller's
they are run in. **At its bound it refuses, never resetting under another holder:** `ResetLockBusy`, a `FederationBusy`
(`federation_busy`, which costs the run no attempt), naming the holders by process, with nothing sent.

**What the shortcut adds.** A reset's occupancy and quiet stages read every regular file's block map at every
provider that can hold the space's bytes, twice after the eviction (O46): for the large space of the first study, about
106 s a pass. Given a planner's reading made before the eviction (`SpaceReset.plan_scan`: each file's bytes at each
provider), the stages may re-read only the files it showed held, the files a transfer touched since, and those a QoS
requirement applies to (`reset.py`), and carry the rest as empty. That is sound only if nothing placed bytes on any
other file between the reading and the last re-read; an on-demand read places bytes and leaves no transfer record, so
the reading must be made inside the same hold as the reset:

    with reset.exclusive() as grant:      # SpaceReset.exclusive or SpacesReset.exclusive
        holdings = reset.plan_scan()
        record = reset.run(holdings.plan(), planned=holdings, ...)

**How it is taken.** Under a shared admission, the run first gives up its own share of `federation.reset` (its agent
has not started) while keeping its share of `federation.lock` throughout, so no outage run can get in; then it waits
for `federation.reset` exclusively, polled, at most `[faults] admission_timeout_s` and until the admission's `cancel`
is set (the campaign ending), with no turnstile (a resetter that held `federation.intent` while it waited could deadlock
with one that took the turnstile before giving up its share). A stream of readers can keep a resetter out to its bound,
which then refuses, uncharged, and the campaign asks again. On leaving it lets the exclusive hold go and takes its share
back on a new descriptor, within the same bound; past that bound (another reset holds the lock) it raises
`FederationBusy`, and the run's infrastructure stays refused (`admission.require_admission`). A run that holds
`federation.lock` exclusively (`exclusive = true`, an outage run) shares the federation with no one and is granted at
once (`federation_exclusive`). A wait refused at its bound, or cancelled, takes the share back before it raises.

**No reset without an admission.** On a platform with `[faults]`, a reset in a context that holds no admission (a
script outside a campaign, a thread that did not inherit its run's context) is refused (`AdmissionRefused`) before
anything is sent: it could not tell its own run's share from another holder's, and an outage run's agent holds no
share of the reset lock to wait for. A tool outside a campaign takes `tool_admission.shared_admission` around it. One
whose admission's owner has left (a context that inherited it) is refused too: the last worker's release would let
the hold go during the reset. Without `[faults]` there is no lock, and nothing is held (`no_admission`).

**The shortcut's fallback, never a failure, and never without the hold.** The grant holds the lock and gives no
shortcut (`why`), and the reset reads every file, when: `[placement] reset_shortcut` is off (`off`); the record of
the holds shows a holder whose process is gone (`holder_gone`: the kernel let its lock go, but a read it sent may still
be placing bytes, and a tool server it started may still run, since the MCP client starts the server in a session of
its own; the dead holder's line is ended, and every section refuses the shortcut for `DEAD_HOLDER_QUIET_S` after that,
naming it); the record cannot be read (`holders_unread`); or a worker of the run holds a share of its admission
(`shares_retained`: a sandbox's start thread, a task reusing the admission through a tool's hold, either of which may
be reading; the admission is not sealed then, and such a worker of the run's own already at work goes on, as it did
beside a full reset before: only other holders are excluded, and waiting for the run's own workers could keep its
reset out for good). `shortcut_unavailable` (`UNAVAILABLE`), the old fallback of a wait that
ran out, is no longer given: such a wait refuses.

**The seal.** A section first seals the run's admission (`Admission.seal`, granted only while the owner's is its one
share), so no worker takes a share until it ends, and nothing of the run may act (`Admission.may_act`: the sandbox and
the tool server refuse to start, a tool refuses to reuse the admission, the health check in the run's context defers).
A section belongs to its federation (`ResetGrant.federation`): a nested section for another federation's configuration
is refused rather than given the outer grant, and a reading made in one federation's section gives the full reads to a
reset of another (`resetshortcut.OTHER_FEDERATION`).

**The hold lasts as long as its last user.** The grant keeps a share of the admission (`Admission.retain_for_reset`),
so an owner that leaves while its reset still runs in a thread (a cancelled attempt) lets neither the admission nor its
locks go until the reset ends; and a nested section (`ResetGrant.join`, a reset in a thread that inherited its
caller's section) keeps the hold after the outer one has ended, the last to leave ending it. **A hold that lapses**
all the same (its lock let go under it, or its grant ended while a reset still uses it) is `ResetGrant.lapsed`: the
stage it lapsed in fails, the last included, and no later one begins (`resetspaces.py`); the shortcut's narrowed reads
fail the stage that finds it too.
"""

import contextlib
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from expcore_onedata.admission import (
    Admission,
    AdmissionRefused,
    Federation,
    FederationBusy,
    current_admission,
    fault_settings_of,
    federation_of,
    require_same_federation,
)
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_settings import FaultSettings
from expcore_onedata.rangemap import PlacementSettings
from expcore_onedata.resetlock import CLOSED, RESET_HOLDS, ResetShare
from expcore_onedata.shared_holds import HOLDER_GONE as ENDED_GONE
from expcore_onedata.shared_holds import SharedHolds

EXCLUSIVE, FEDERATION_EXCLUSIVE = "exclusive", "federation_exclusive"  # what a grant holds
OFF, NO_ADMISSION = "off", "no_admission"  # why a grant gives no shortcut
UNAVAILABLE = "shortcut_unavailable"  # no longer given (a wait that runs out refuses); kept for older records
HOLDER_GONE, HOLDERS_UNREAD, SHARES_RETAINED = "holder_gone", "holders_unread", "shares_retained"
LIVE, UNKNOWN = "live", "unknown"  # a holder's status beside HOLDER_GONE
LOCK_FREE = "lock_free"  # how a line is ended whose live process let the lock go without recording it
GRANTED, TIMED_OUT, CANCELLED = "granted", "timed_out", "cancelled"  # how a wait for the exclusive hold ended
POLL_S = 0.05  # seconds between two tries for the exclusive hold
# How long after a holder was found dead its line still refuses the shortcut: what it left behind (a tool server in a
# session of its own, a read the provider still serves, an agent's cell in its sandbox) ends within its own bounds,
# which the core does not know, so the refusal outlasts every bound the platform declares (`admission_timeout_s`).
DEAD_HOLDER_QUIET_S = 3600.0
_GRANT: ContextVar["ResetGrant | None"] = ContextVar("expcore_onedata_reset_grant", default=None)


class ResetLockBusy(FederationBusy):
    """A reset's wait for the reset lock ran out at its bound (`[faults] admission_timeout_s`) while other shared
    holders kept it, or was cancelled: refused, nothing sent, the hold `federation_busy`, which costs the run no
    attempt. `wait_s` is the seconds waited, and `holders` the holders the record named then (`holder_of`), None when
    it could not be read."""

    def __init__(self, detail: str, wait_s: float, holders: list[dict[str, Any]] | None) -> None:
        super().__init__(detail)
        self.wait_s = wait_s
        self.holders = holders


@dataclass
class ResetGrant:
    """What a reset's exclusive hold holds: `mode` (`exclusive`, `federation_exclusive`, or None for nothing: no
    `[faults]`), `why` the shortcut is not given (None when it is), the seconds waited and the holders it names."""

    why: str | None
    mode: str | None = None
    wait_s: float = 0.0
    holders: list[dict[str, Any]] = field(default_factory=list)
    granted: bool = False  # the exclusive hold was had
    federation: Federation | None = None  # the federation whose reset lock it took
    _share: ResetShare | None = field(default=None, repr=False)
    _epoch: int | None = field(default=None, repr=False)
    _admission: Admission | None = field(default=None, repr=False)
    _sealed: bool = field(default=False, repr=False)  # it sealed the admission, and unseals it at its end
    _keep: Callable[[], None] | None = field(default=None, repr=False)  # gives up its share of the admission
    _users: int = field(default=1, repr=False)  # the sections using it: its own, and every nested one still running
    _guard: threading.Lock = field(default_factory=threading.Lock, repr=False)
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
    def lapsed(self) -> bool:
        """Whether the hold was had and is no longer in force, ended or let go: another holder may act now, so a reset
        still using it certifies nothing from here."""
        return self.granted and not self.held

    def join(self) -> bool:
        """Count one more section using this grant (a nested one, maybe in a thread that inherited the context), while
        its hold is in force; False, counting nothing, once it is not."""
        with self._guard:
            if self._ended or not self.held:
                return False
            self._users += 1
            return True

    def leave(self) -> None:
        """A section using this grant has ended; the last one ends the grant (`end`), so the hold is never let go
        while a reset still runs under it."""
        with self._guard:
            self._users -= 1
            last = self._users == 0
        if last:
            self.end()

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
        `FederationBusy` past the admission's bound), then unseal the admission it sealed (`Admission.seal`); once,
        and nothing for a grant that holds nothing."""
        if self._ended:
            return
        self._ended = True
        try:
            if self.mode == EXCLUSIVE and self._share is not None:
                self._share.end_exclusive()
                self._share.take_shared()
        finally:
            try:
                if self._admission is not None and self._sealed:
                    self._admission.unseal()
            finally:
                if self._keep is not None:  # the last share lets the admission, and its locks, go (`Admission.close`)
                    self._keep()


def current_grant() -> "ResetGrant | None":
    """The grant of the exclusive hold the running context is in, if any."""
    return _GRANT.get()


@contextlib.contextmanager
def exclusive(config: PlatformConfig, settings: PlacementSettings) -> Iterator[ResetGrant]:
    """The exclusive hold of the reset lock (the module docstring) for the body of a `with`: yield the grant, and on
    leaving let the hold go and take the run's share back, once the last section using it has left. A hold inside a
    live one of the same federation uses the outer grant (`ResetGrant.join`), so an outer section that ends while a
    nested one still runs in a thread that inherited it leaves the hold to that one. Raise `AdmissionRefused` on a
    platform with `[faults]` and no admission in the context, or one of another federation, or one whose share is not
    held or whose owner has left; `ResetLockBusy` when the wait ran out or was cancelled; and `FederationBusy` when the
    share cannot be taken back; when the body raised, its exception is the one raised, with the failure to take the
    share back noted on it (the share then stays given up, and the run's infrastructure refused)."""
    outer = _GRANT.get()
    if outer is not None and outer.is_for(federation_for(config)) and outer.join():
        with _using(outer):
            yield outer
        return
    grant = _take(config, settings)
    token = _GRANT.set(grant)
    try:
        with _using(grant):
            yield grant
    finally:
        _GRANT.reset(token)


@contextlib.contextmanager
def _using(grant: ResetGrant) -> Iterator[None]:
    """One section's use of a grant, left however its body ends (`ResetGrant.leave`); the body's failure stays the
    one raised, since it says why the reset failed, with a failure to end the grant noted on it."""
    try:
        yield
    except BaseException as error:
        try:
            grant.leave()
        except Exception as ending:  # the share is still given up, so the run's infrastructure stays refused
            error.add_note(f"and the share of the reset lock was not taken back: {type(ending).__name__}: {ending}")
        raise
    grant.leave()


def federation_for(config: PlatformConfig) -> Federation | None:
    """The federation a platform's `[faults]` describes, None without them: what a grant must be of to stand."""
    faults = fault_settings_of(config)
    return None if faults is None else federation_of(faults)


def _take(config: PlatformConfig, settings: PlacementSettings) -> ResetGrant:
    faults = fault_settings_of(config)
    if faults is None:  # no injector and no lock: nothing to hold
        return ResetGrant(OFF if not settings.reset_shortcut else NO_ADMISSION)
    admission = current_admission()
    if admission is None:
        raise AdmissionRefused(
            f"{config.file}: [faults]: a reset runs only under the federation's admission, which this context does not "
            "hold: a campaign run's (reset in the run's own context; a thread pool's run_in_executor does not carry "
            "it, asyncio.to_thread does), or, outside a campaign, shared_admission(config, reason=...) around it. "
            "Without one it could run beside another holder of the federation; refused before anything was sent"
        )
    federation = federation_of(faults)
    require_same_federation(admission, federation)
    if admission.owner_left:
        raise AdmissionRefused(
            "the run's admission has been given up by its owner, and a worker's last share would let the reset lock go "
            "during the reset: a reset runs in its run's context, before the run ends"
        )
    if admission.reset is not None and not admission.reset.shared:
        raise AdmissionRefused("a reset needs the run's share of the reset lock, held, to give up")
    sealed = admission.seal()  # refused while a worker holds a share: it may be reading, so no shortcut
    keep = admission.retain_for_reset()  # the admission, and its locks, stay held for as long as the hold does
    if keep is None:
        if sealed:
            admission.unseal()
        raise AdmissionRefused("the run's admission was let go before its reset could hold the reset lock")
    try:
        return _hold(admission, federation, faults, settings, sealed, keep)
    except BaseException:
        try:
            if sealed:
                admission.unseal()
        finally:
            keep()
        raise


def _hold(
    admission: Admission,
    federation: Federation,
    faults: FaultSettings,
    settings: PlacementSettings,
    sealed: bool,
    keep: Callable[[], None],
) -> ResetGrant:
    """`_take` once the admission is checked (and `sealed`, when no worker held a share) and a share of it kept for
    the hold (`keep`): the exclusive hold, or the refusal."""
    holds = SharedHolds(faults.journal, name=RESET_HOLDS)
    if admission.exclusive:  # no shared admission can hold the federation, nor so the reset lock
        why, gone = _settled(holds)
        why = _why(settings, sealed, why)
        return ResetGrant(
            why, FEDERATION_EXCLUSIVE, 0.0, gone, True, federation, _admission=admission, _sealed=sealed, _keep=keep
        )
    share = admission.reset
    assert share is not None  # a shared admission's, checked by `_take`
    began = time.monotonic()
    share.give_up()
    holders: list[dict[str, Any]] | None = None
    epoch, why, gone = None, None, []
    try:  # until the grant exists: the wait, and the record of the holders read (and settled, once held)
        ended = _wait(share, faults.admission_timeout_s, admission.cancel)
        waited = time.monotonic() - began
        if ended == GRANTED:
            epoch = share.exclusive_epoch()
            why, gone = _settled(holds)
        else:
            holders = _holders(holds)
    except BaseException:  # an interrupt: no exclusive hold outlives it, and the run must not be left without its share
        share.end_exclusive()
        share.take_shared()
        raise
    if ended != GRANTED:
        refused = _refusal(ended, waited, holders, faults.admission_timeout_s)
        try:
            share.take_shared()  # nothing was reset: the run's share comes back, and the refusal is raised
        except Exception as ending:
            refused.add_note(f"and the share of the reset lock was not taken back: {type(ending).__name__}: {ending}")
        raise refused
    why = _why(settings, sealed, why)
    return ResetGrant(
        why,
        EXCLUSIVE,
        waited,
        gone,
        True,
        federation,
        _share=share,
        _epoch=epoch,
        _admission=admission,
        _sealed=sealed,
        _keep=keep,
    )


def _why(settings: PlacementSettings, sealed: bool, settled: str | None) -> str | None:
    """Why a grant that holds the lock gives no shortcut: the setting off first, then a worker's share (the admission
    could not be sealed), then what the record of the holds showed (`_settled`); None for the shortcut."""
    if not settings.reset_shortcut:
        return OFF
    return settled if sealed else SHARES_RETAINED


def _wait(share: ResetShare, bound_s: float, cancel: threading.Event | None) -> str:
    """Poll for the exclusive hold until `bound_s` has passed (one try for 0) or `cancel` is set; how it ended. A share
    the admission let go meanwhile (`CLOSED`) is a cancelled wait: the run is ending."""
    deadline = time.monotonic() + bound_s
    while not share.try_exclusive():
        if share.state == CLOSED or (cancel is not None and cancel.is_set()):
            return CANCELLED
        if time.monotonic() >= deadline:
            return TIMED_OUT
        if cancel is not None:
            cancel.wait(POLL_S)
        else:
            time.sleep(POLL_S)
    return GRANTED


def _refusal(ended: str, waited: float, holders: list[dict[str, Any]] | None, bound_s: float) -> ResetLockBusy:
    """The refusal of a wait that did not get the hold, naming the holders by process (pid and status), never by their
    reasons (O12)."""
    if holders is None:
        named = "the record of its holders could not be read"
    elif holders:
        named = "held by " + ", ".join(f"pid {holder['pid']} ({holder['status']})" for holder in holders)
    else:
        named = "the record names no open holder"
    if ended == CANCELLED:
        what = f"the reset's wait for the federation's reset lock was cancelled after {waited:.1f} s: the run is ending"
    else:
        what = (
            f"the reset waited {waited:.1f} s, the bound of [faults] admission_timeout_s ({bound_s:g} s), for the "
            "federation's reset lock, which other shared holders kept"
        )
    return ResetLockBusy(
        f"{what}; {named}: refused rather than reset under another holder, and nothing was sent", waited, holders
    )


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
    "CANCELLED",
    "DEAD_HOLDER_QUIET_S",
    "EXCLUSIVE",
    "FEDERATION_EXCLUSIVE",
    "HOLDERS_UNREAD",
    "HOLDER_GONE",
    "NO_ADMISSION",
    "OFF",
    "SHARES_RETAINED",
    "TIMED_OUT",
    "UNAVAILABLE",
    "ResetGrant",
    "ResetLockBusy",
    "current_grant",
    "exclusive",
    "federation_for",
    "holder_of",
]
