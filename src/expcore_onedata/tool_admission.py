# Provenance: new
"""A tool outside a campaign holds the federation's shared admission (O48).

A campaign attempt holds the federation's lock shared, and an outage run holds it exclusively, so no attempt overlaps an
outage (`expcore_onedata.admission`). A tool that is no campaign attempt (a study's smoke driver, a warm-up, `verify`,
an ad hoc check) held none, so it could read and write through a provider that an outage is taking down or restoring.
`shared_admission(config, reason=...)` is the hold such a tool takes, around everything it does on the federation:

    with shared_admission(config, reason="smoke: warm-up of the first space"):
        ...  # start the sandbox and the tool server, read, write

It takes the same lock a campaign attempt takes, shared, so tools and attempts hold the federation together and an
outage run, which needs it exclusively, never overlaps one; and the same turnstile, so a stream of tools cannot keep an
outage run out for ever. What it does around that:

- **It waits for an outage run in progress, within a bound** (`timeout_s`, by default `[faults] admission_timeout_s`;
  `timeout_s=0` tries once), and past the bound is refused as `FederationBusy`, an `injector_failure`.
- **It is refused, under the lock, when the federation is not in its baseline state:** an injection the fault journal
  holds open (a provider may be down), a fault journal or a record of the tools' holds that cannot be read, and, for
  the `ingest_journals` it is given, a preallocated ingest's baseline that is not whole
  (`ingesthealth.baseline_health`): one marked unhealthy, orphaned by a run that is gone, or held open by a live run,
  which is being reset and would be read in flux (the campaign's own check defers there, a wait the tool has no clock
  for). The refusal is an `InfraError` of kind `injector_failure` and names why, the target or the space by its label or
  position, never by name.
- **It records who held it** (`expcore_onedata.shared_holds`): the reason, the process as the lease pool names a
  holder (boot id, pid and start time), the start and the end, in `shared-holds.jsonl` beside the fault journal that
  the exclusive admission reads. An exclusive wait that runs out says that tools hold the federation and since when,
  never their reasons.
- **It releases on exit and on an exception.** The lock goes with the process however it dies, and the record of a
  dead tool is ended by the next hold or the next exclusive admission as `holder_gone`.

The hold covers the body of the `with`, and the tool's whole use of the federation must be inside it: a thread, a task,
a forked child or a subprocess that goes on after the body has ended is not held back from an outage. A worker that
must outlive the body takes a share first (`expcore_onedata.admission.retain_admission`, which the sandbox's start
threads do) and gives it up when it ends; the lock, and the record's end, wait for it. A `with` inside a hold of the
same federation, shared (a warm-up that a smoke driver calls), uses that hold and records nothing more; inside an
exclusive one, or one of another federation, it is `AdmissionRefused`. A platform without `[faults]` has no injector
and no lock: the tool is admitted with no admission (`None`), its ingest journals still checked.

`shared_admission_async` is the form for a driver that runs in an event loop: the wait runs off the loop and stops on
cancellation, and `waits` (a timer or a call context) marks it a `lease_queue` wait as for a run.
"""

import contextlib
import math
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore_onedata.admission import Admission, FederationLock, current_admission, fault_settings_of
from expcore_onedata.config import PlatformConfig
from expcore_onedata.fault_recovery import failure
from expcore_onedata.fault_settings import FaultSettings
from expcore_onedata.ingesthealth import baseline_health
from expcore_onedata.shared_holds import SharedHolds

MAX_REASON = 200  # characters


@dataclass(frozen=True)
class _Plan:
    """What one entry into the hold needs, validated: the reason, the bound, the platform's `[faults]` (None without
    them) and the ingests' journals."""

    reason: str
    timeout_s: float | None
    settings: FaultSettings | None
    ingest_journals: tuple[Path, ...]


def _plan(config: PlatformConfig, reason: object, timeout_s: object, ingest_journals: Iterable[Path | str]) -> _Plan:
    """Validate the arguments of a hold, and read the platform's `[faults]`; `ValueError` names the argument."""
    if not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_REASON or not reason.isprintable():
        raise ValueError(f"reason must be one printable line of text of 1 to {MAX_REASON} characters")
    if timeout_s is not None and (
        isinstance(timeout_s, bool)
        or not isinstance(timeout_s, int | float)
        or not math.isfinite(timeout_s)
        or timeout_s < 0
    ):
        raise ValueError("timeout_s must be a finite number of seconds, 0 or more, or None for the platform's bound")
    settings = fault_settings_of(config)
    bound = float(timeout_s) if timeout_s is not None else settings.admission_timeout_s if settings else None
    return _Plan(reason, bound, settings, tuple(Path(journal) for journal in ingest_journals))


def _require_baselines(plan: _Plan) -> None:
    """Refuse while a preallocated ingest's baseline is not whole or is held open by a live run."""
    verdict = baseline_health(plan.ingest_journals)
    if not verdict.ok:
        raise failure(verdict.detail)


def _reusable(plan: _Plan, lock: FederationLock) -> Admission | None:
    """The shared admission of this federation the running context holds already, if it holds one."""
    held = current_admission()
    if held is not None and not held.exclusive and held.federation == lock.federation:
        _require_baselines(plan)
        return held
    return None


def _begin(plan: _Plan, settings: FaultSettings, admission: Admission) -> None:
    """Under the lock, the journals checked (`FederationLock` has checked the fault journal): record the hold, and have
    the admission end the record when the lock goes, the last share given up. A record that cannot be written or read
    refuses the tool: a hold nobody can read is not one."""
    _require_baselines(plan)
    holds = SharedHolds(settings.journal)
    try:
        holds.settle()  # a dead tool's record, ended by the next hold
        token = holds.begin(plan.reason)
    except ValueError:
        raise failure("the record of the tools' holds cannot be read") from None
    except OSError as error:  # its text holds the journal's path: only its type is told (O12)
        raise failure(f"the hold could not be recorded ({type(error).__name__})") from None
    admission.endings.append(lambda: holds.end(token))


@contextlib.contextmanager
def shared_admission(
    config: PlatformConfig,
    *,
    reason: str,
    timeout_s: float | None = None,
    ingest_journals: Iterable[Path | str] = (),
) -> Iterator[Admission | None]:
    """Hold the federation shared for the body of a `with`, as the running context's admission, recording who holds it
    (see the module docstring). `reason` is the tool's own words, one printable line of at most 200 characters, which
    goes into the record and nowhere else. Yields the admission, or None for a platform without `[faults]`.

    Raises `ValueError` for a reason or a bound that is not valid, `FederationBusy` when an outage run held the
    federation past the bound, `InfraError` of kind `injector_failure` when it is refused, `AdmissionRefused` inside
    an exclusive admission or one of another federation."""
    plan = _plan(config, reason, timeout_s, ingest_journals)
    settings = plan.settings
    if settings is None:
        _require_baselines(plan)
        yield None
        return
    lock = FederationLock(settings)
    reused = _reusable(plan, lock)
    if reused is not None:
        yield reused
        return
    with lock.held(exclusive=False, timeout_s=plan.timeout_s) as admission:
        _begin(plan, settings, admission)
        yield admission


@contextlib.asynccontextmanager
async def shared_admission_async(
    config: PlatformConfig,
    *,
    reason: str,
    timeout_s: float | None = None,
    ingest_journals: Iterable[Path | str] = (),
    waits: Any = None,
) -> AsyncIterator[Admission | None]:
    """`shared_admission` for an `async with`: the wait runs off the event loop and stops on cancellation, and with
    `waits` (a timer or a call context) it is inside `waits.infrastructure_wait("lease_queue")`."""
    plan = _plan(config, reason, timeout_s, ingest_journals)
    settings = plan.settings
    if settings is None:
        _require_baselines(plan)
        yield None
        return
    lock = FederationLock(settings)
    reused = _reusable(plan, lock)
    if reused is not None:
        yield reused
        return
    async with lock.admitted(exclusive=False, waits=waits, timeout_s=plan.timeout_s) as admission:
        _begin(plan, settings, admission)
        yield admission
