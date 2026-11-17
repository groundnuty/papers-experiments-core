# Provenance: new
"""A federation's own services, observed over a window, the same on Compose and on HPC (O86).

A study's post-check asks one thing of the federation: did its services (the zone and each provider) run, and answer,
from the window's start to its end, and if one was restarted, was that the injector's doing? `FederationWatch.read()`
is one reading, taken at the window's start and kept (it is plain JSON); `FederationWatch.window(start)` reads again
now and returns the window. Every call only reads: nothing here stops, starts, restarts or writes anything.

**What is observed, per service, at each end.** Its state as the backend shows it: Compose, the container (`docker
compose ps`, `docker inspect`: its id, `StartedAt`, `RestartCount`, health, exit); HPC, the service's Slurm step by its
EXACT id (`deploy/hpc-federation/hpcobserve.py`: `squeue -s` and `sacct`, never the state file's word), run through the
injected `command` where the Slurm client commands answer. And the service's own liveness: the platform client's
`GET /configuration`, the zone's and each provider's, with the client the caller built (it goes where platform.toml
says; an address map for declared names is the caller's client's concern, not read here). A service that is not running
is not asked.

**A stop Slurm's accounting has not caught up with.** The driver confirms a stop once `squeue -s` no longer lists the
step, and `sacct` follows seconds later, so a read made at once, as the first study's order does (the target is stopped
just before the window's start reading), finds the step neither listed nor recorded as ended. When the driver's state
says the step was stopped and Slurm's commands do not yet agree, `hpcobserve.py` waits for them, polling every 2 s, up
to `accounting_wait_s` (an argument of `watch_for`: 60 s unless declared, from 0 to 300), and each side records the
seconds it waited (`accounting_wait_s_used`). At the bound the service is `unknown` with the reason `accounting_lag`:
never running, never stopped by assumption. It never waits for a service the state does not say was stopped, and the
cross-check below is unchanged: the state's stops and launches must still agree with what is read.

**Unknown is never healthy.** A state or an answer that cannot be read (a command that fails or does not finish, a
container or step missing, Slurm and accounting disagreeing, a liveness read that fails, no reading at the window's
start) makes the service `unknown`, with the reason, and the window `unknown` unless a finding outranks it.

**A restart** is an end reading whose container id (Compose) or step id (HPC) differs from the start's, whose start time
differs, or whose counter rose (`RestartCount`; the state's `launch`). It is explained only by the fault injector's own
journal, read as it was at the window's start: the journal is append-only and its lines carry no time, so the window is
the lines written after the start reading's count. An injection of the restarted service, with a line in the window,
whose stop was asked for (`stopping`, `injected` or `stop_unknown`) and which was not cleared `never_down`, explains a
restart: labelled `heal` when its line carries `heal` (a heal's container restart), else `outage`. On HPC the count is
checked: `launch` says how many starts there were, each restart of a running service owes a stop the state records
(`stops`) after the start reading, and an injection explains at most one launch. A restart nothing explains is labelled
`finding` and listed under `findings`: never silent. A journal that cannot be read, is shorter than at the start, or is
not declared explains nothing. `RestartCount` rising is Docker's restart policy, not the injector's stop and start,
so it is a finding whatever the journal holds. On Compose only a change is seen, so a service restarted twice reads as
one restart (the reason says "at least"); the injections of the window still have to account for it.

**The result** keys services by `zone` and `providers[i]` (the provider's position in platform.toml), never by a
name, and holds no host, address, path or service name. `verdict` is `finding` (any finding), else `unknown`, else
`held` (a service not running at the window's end that an open injection holds down, explained), else `restarted`
(a restart, each explained), else `stable`.

**Whose an injection is.** `window(start, owner=...)` is a run's window: only the injections whose `owner` (written by
the injector since #371) names that run's attempt explain a restart or hold a service down. Another attempt's
leftover, a line written before owners were recorded and an injection outside any attempt explain nothing, so they
leave a finding; a run that names no attempt owns none. With no `owner` (a batch's window) any injection counts, and
the result says so (`ownership`). A `held` service is explained by the run's own open injection that fits the start
reading: the service was down then too, or the stop was asked inside the window (one the service ran through is
another outage). The window comes before its restore, so `held` is read with the run's restore outcome.
"""

import json
import math
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from expcore_onedata.client import PlatformError
from expcore_onedata.compose import CommandRunner, ComposeDriver, DriverError, run_command
from expcore_onedata.fault_journal import JOURNAL
from expcore_onedata.fault_owner import attempt_of
from expcore_onedata.fault_settings import FaultSettings, compose_driver
from expcore_onedata.readretry import ONE_ATTEMPT

if TYPE_CHECKING:
    from expcore_onedata.config import PlatformConfig

FORMAT = "expcore-fedwatch/1"
HPC_FORMAT = "expcore-hpc-observe/1"  # what `hpcobserve.py` prints
# `hpcobserve.py` waits this long, at most, for Slurm's accounting to show a step the state says was stopped; it keeps
# its own copy of the three (a deploy script imports nothing of the package), and a test holds them equal.
DEFAULT_ACCOUNTING_WAIT_S = 60.0
MAX_ACCOUNTING_WAIT_S = 300.0
ACCOUNTING_LAG = "accounting_lag"  # the reason it gives when the wait ended and accounting still had not caught up
PROBE_TIMEOUT_S = 10.0
STOP_ASKED = ("stopping", "injected", "stop_unknown")  # a journal line says the service's stop was asked for
NOT_RUNNING = "the service is not running"


@dataclass(frozen=True)
class Member:
    """One service of the federation: its label (`zone`, `providers[i]`), the name its backend knows it by, and, for a
    provider, the declared provider's name the platform client reads it through (None for the zone)."""

    label: str
    service: str
    provider: str | None


def members_of(config: "PlatformConfig", *, zone: str, providers: Mapping[str, str]) -> tuple[Member, ...]:
    """The zone, then the providers of `providers` (provider name to its backend service, `[faults.services]`) in the
    configuration's order. The zone's service is not in `[faults.services]`, so the caller names it."""
    undeclared = set(providers) - set(config.providers)
    if undeclared:
        raise ValueError(f"{len(undeclared)} provider(s) named are not declared in the configuration")
    chosen = [
        Member(config.provider_label(name), providers[name], name) for name in config.providers if name in providers
    ]
    return (Member("zone", zone, None), *chosen)


class ServiceSource(Protocol):
    """Where a backend's state of each member is read: `backend`, and `read(members)` answering, by label, the fields
    `_side` knows (the rest are left None) and `unknown`, the reasons the state could not be read."""

    backend: str

    def read(self, members: Sequence[Member]) -> Mapping[str, dict[str, Any]]: ...


class Client(Protocol):
    """The platform client, as far as the liveness read uses it."""

    def get(self, path: str, *, provider: str | None = ..., timeout_s: float | None = ..., retry: Any = ...) -> Any: ...


def _utc(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def _reason(error: BaseException) -> str:
    """Why a read failed: a platform or driver error's own message (none holds a URL, name or output), else the type."""
    return (
        str(error)[:200]
        if isinstance(error, PlatformError | DriverError)
        else f"the read raised {type(error).__name__}"
    )


class ComposeSource:
    """Compose's containers, through `ComposeDriver.inspect_state` (one `ps` and one `inspect` per service)."""

    backend = "compose"

    def __init__(self, driver: ComposeDriver) -> None:
        self._driver = driver

    def read(self, members: Sequence[Member]) -> dict[str, dict[str, Any]]:
        return {member.label: self._one(member.service) for member in members}

    def _one(self, service: str) -> dict[str, Any]:
        try:
            seen = self._driver.inspect_state(service)
        except Exception as error:  # a read that cannot be made is unknown, whatever stopped it
            return {"unknown": [_reason(error)]}
        if seen is None:  # removed or never made: not a state to read, and nothing an injection's stop leaves
            return {"unknown": ["the service has no container"]}
        running = seen["running"] and not seen["restarting"] and not seen["paused"]
        never = "0001-01-01T00:00:00Z"
        return {
            "running": running,
            "container": seen["id"],
            "started_at": seen["started_at"],
            "ended_at": None if running or seen["finished_at"] in (None, never) else seen["finished_at"],
            "exit_code": None if running else seen["exit_code"],
            "health": seen["health"],
            "restart_count": seen["restart_count"],
            "notes": [f"the container is {state}" for state in ("restarting", "paused") if seen[state]],
        }


class HpcSource:
    """The HPC federation's steps, from the one JSON line `hpcobserve.py` prints, run as `command` (an argv, no shell,
    where the Slurm client commands answer: the login node) through `runner`."""

    backend = "hpc"

    def __init__(
        self, command: Sequence[str], *, runner: CommandRunner = run_command, timeout_s: float = 120.0
    ) -> None:
        if not command or not all(isinstance(word, str) and word for word in command):
            raise ValueError("the read's command is a non-empty argv")
        self._command, self._runner, self._timeout_s = list(command), runner, timeout_s

    def read(self, members: Sequence[Member]) -> dict[str, dict[str, Any]]:
        try:
            done = self._runner(self._command, timeout_s=self._timeout_s)
        except subprocess.TimeoutExpired:
            return self._all(members, f"the HPC read did not finish within {self._timeout_s:g} s")
        except OSError as error:
            return self._all(members, f"the HPC read could not run ({type(error).__name__})")
        if done.returncode != 0:
            return self._all(members, f"the HPC read failed with exit code {done.returncode}")
        try:
            doc = json.loads(done.stdout)
        except ValueError:
            doc = None
        services = doc.get("services") if isinstance(doc, dict) and doc.get("format") == HPC_FORMAT else None
        if not isinstance(services, dict):
            return self._all(members, "the HPC read answered something unreadable")
        return {m.label: self._one(services.get(m.service)) for m in members}

    @staticmethod
    def _all(members: Sequence[Member], reason: str) -> dict[str, dict[str, Any]]:
        return {member.label: {"unknown": [reason]} for member in members}

    @staticmethod
    def _one(record: Any) -> dict[str, Any]:
        if not isinstance(record, dict):
            return {"unknown": ["the HPC read lists no such service"]}
        running, stops, launch = record.get("running"), record.get("stops"), record.get("launch")
        waited = record.get("accounting_wait_s_used")  # absent from a document that predates the wait
        texts = [record.get(key) for key in ("step", "state", "started_at", "ended_at")]
        sound = (
            (running is None or isinstance(running, bool))
            and all(text is None or isinstance(text, str) for text in texts)
            and isinstance(stops, list)
            and (launch is None or (isinstance(launch, int) and not isinstance(launch, bool)))
            and (record.get("exit_code") is None or isinstance(record["exit_code"], int))
            and (waited is None or HpcSource._seconds(waited))
        )
        if not sound:
            return {"unknown": ["the HPC read's answer for the service is not readable"]}
        why = record.get("unknown")
        unknown = [str(why)[:200]] if why else ([] if running is not None else ["the step could not be told"])
        notes = ["the state file names another step than the one squeue lists"] if record.get("state_differs") else []
        if why == ACCOUNTING_LAG:
            took = "its bound" if waited is None else f"{waited:g} s"
            notes.append(f"the state records the step stopped, and Slurm's accounting did not catch up within {took}")
        elif waited:
            notes.append(f"the read waited {waited:g} s for Slurm's accounting to show the stopped step")
        return {
            "running": running,
            "step": record["step"],
            "started_at": record["started_at"],
            "ended_at": record["ended_at"],
            "exit_code": record["exit_code"],
            "launch": launch,
            "stops": len(stops),
            "unrecorded": record.get("state_differs") is True,
            "accounting_wait_s_used": None if waited is None else float(waited),
            "notes": notes,
            "unknown": unknown,
        }

    @staticmethod
    def _seconds(value: Any) -> bool:
        """Whether a figure of the document is a number of seconds: finite, never negative, never a bool."""
        return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _side(raw: Mapping[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """One service's reading at one end, with every key present whichever backend made it."""
    side: dict[str, Any] = {
        "state": "unknown", "running": None, "identity": None, "container": None, "step": None, "health": None,
        "started_at": None, "ended_at": None, "exit_code": None, "restart_count": None, "launch": None, "stops": None,
        "unrecorded": False, "accounting_wait_s_used": None, "notes": [], "live": live, "unknown": [],
    }  # fmt: skip
    side.update(raw)
    if side["unknown"] or side["running"] is None:
        side["running"], side["state"] = None, "unknown"
        side["unknown"] = side["unknown"] or ["the state could not be told"]
    else:
        side["state"] = "running" if side["running"] else "not_running"
    instance = side["container"] or side["step"]
    side["identity"] = None if instance is None else f"{instance}@{side['started_at'] or ''}"
    if side["state"] == "running" and live["answered"] is None:
        side["unknown"] = [*side["unknown"], f"its liveness could not be read: {live['reason']}"]
    return side


# --- the journal ------------------------------------------------------------------------------------------------------


class _Unreadable(Exception):
    """The journal cannot be used to explain anything; the message says why."""


def _journal_lines(directory: Path | None) -> list[str] | None:
    """The journal's complete lines, read without a lock and without a write (a line is appended whole and fsynced, and
    a torn last one is left out); [] when nothing was ever journalled; None when no journal is declared. Raise
    `_Unreadable` when the file cannot be read."""
    if directory is None:
        return None
    try:
        data = (directory / JOURNAL).read_bytes()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise _Unreadable(f"the journal could not be read ({type(error).__name__})") from None
    try:
        return data[: data.rfind(b"\n") + 1].decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise _Unreadable("the journal is not text") from None


def _entries(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Every injection the journal holds, whole file (`open_entries` drops the cleared ones): its service, whether it is
    a heal's, its events, the numbers of its lines, and how it was cleared. Raise `_Unreadable` for a line that is not a
    JSON object, or an `injecting` line that names no service."""
    found: dict[str, dict[str, Any]] = {}
    for number, text in enumerate(lines):
        try:
            line = json.loads(text)
        except ValueError:
            raise _Unreadable(f"line {number + 1} of the journal is not JSON") from None
        if not isinstance(line, dict):
            raise _Unreadable(f"line {number + 1} of the journal is not an injection event")
        event, key = line.get("event"), line.get("id")
        if event == "injecting":
            if not isinstance(line.get("service"), str) or not isinstance(key, str):
                raise _Unreadable(f"line {number + 1} of the journal names no service")
            found[key] = {"service": line["service"], "heal": line.get("heal") is True, "events": {event},
                          "lines": [number], "stop_line": -1, "cleared": None, "owner": line.get("owner")}  # fmt: skip
        elif key in found:
            entry = found[key]
            entry["events"].add(event)
            entry["lines"].append(number)
            if event in STOP_ASKED:
                entry["stop_line"] = number
            if event == "cleared":
                entry["cleared"] = str(line.get("how"))
    return list(found.values())


def _explaining(
    entries: list[dict[str, Any]],
    service: str,
    first: int,
    was_running: bool,
    owned: Callable[[Mapping[str, Any]], bool],
) -> list[dict[str, Any]]:
    """The injections of `service` that can explain a restart inside the window that began at line `first`, in the
    order they began: the stop was asked for, and the injection did not end `never_down`; and either the stop was asked
    inside the window, or the service was down at the window's start and the injection has a line in it (its restore is
    the restart). An injection whose stop and restore came before the window, with only its `cleared` line written
    after the start reading, explains nothing: the restart it made is not in the window. And only an injection that the
    window's `owned` accepts: with a run named, the run's own (see `_ownership`)."""
    return [
        entry
        for entry in entries
        if entry["service"] == service
        and owned(entry)
        and entry["stop_line"] >= 0
        and entry["cleared"] != "never_down"
        and (entry["stop_line"] >= first or (not was_running and entry["lines"][-1] >= first))
    ]


def _kind(entries: Sequence[dict[str, Any]]) -> str:
    kinds = {"heal" if entry["heal"] else "outage" for entry in entries}
    return kinds.pop() if len(kinds) == 1 else "mixed"


# --- the window -------------------------------------------------------------------------------------------------------

NO_JOURNAL = "no journal is declared, so nothing can explain a restart"


def _anyone(entry: Mapping[str, Any]) -> bool:
    return True


def _ownership(owner: Mapping[str, Any] | None) -> tuple[Callable[[Mapping[str, Any]], bool], str]:
    """Which injections of the journal may explain this window, and the word for how that was decided.

    With no run named (`owner` None: a batch's window), any: `not_checked`. With a run named, only the injections whose
    `owner` names the same attempt (`fault_owner.attempt_of`: the process, the campaign and the attempt's id): `run`.
    A run that names no attempt (a process alone) cannot be told from another, so nothing is its own: `unattributed`.
    An injection of another attempt, and one that records no owner (written before #371), explain nothing: an owner is
    never guessed."""
    if owner is None:
        return _anyone, "not_checked"
    judged = attempt_of(owner)
    if judged is None:
        return (lambda entry: False), "unattributed"
    return (lambda entry: attempt_of(entry.get("owner")) == judged), "run"


@dataclass(frozen=True)
class _JournalWindow:
    """What the journal says about one window: its injections (None when it cannot be used, and `why`), the number of
    the first line written after the start reading, and which injections count as the run's (`owned`, `ownership`)."""

    entries: list[dict[str, Any]] | None
    first: int
    why: str | None
    owned: Callable[[Mapping[str, Any]], bool] = _anyone
    ownership: str = "not_checked"

    @classmethod
    def of(
        cls,
        lines: list[str] | None,
        why: str | None,
        start: Mapping[str, Any] | None,
        owner: Mapping[str, Any] | None = None,
    ) -> "_JournalWindow":
        owned, ownership = _ownership(owner)
        if lines is None:
            return cls(None, 0, why or NO_JOURNAL, owned, ownership)
        before = None if start is None else start["journal"].get("lines")
        if not isinstance(before, int):
            return cls(None, 0, "the journal could not be read at the window's start", owned, ownership)
        if len(lines) < before:
            return cls(None, 0, "the journal is shorter than it was at the window's start", owned, ownership)
        try:
            return cls(_entries(lines), before, None, owned, ownership)
        except _Unreadable as error:
            return cls(None, 0, str(error), owned, ownership)

    def held(self, service: str, was_running: bool) -> list[dict[str, Any]]:
        """The injections that can explain a restart of the service in the window (`was_running` at its start), the
        run's own when a run is named."""
        return [] if self.entries is None else _explaining(self.entries, service, self.first, was_running, self.owned)

    def foreign(self, service: str, was_running: bool) -> int:
        """How many injections would have explained a restart of the service but are not the run's own."""
        if self.entries is None or self.ownership == "not_checked":
            return 0
        every = _explaining(self.entries, service, self.first, was_running, _anyone)
        return len(every) - len(_explaining(self.entries, service, self.first, was_running, self.owned))

    def holding(self, service: str, was_running: bool) -> list[dict[str, Any]]:
        """The open injections that hold the service down at the window's end, the run's own when a run is named: its
        stop was asked, it did not end `never_down`, nothing has cleared it, and it is consistent with what the start
        reading showed: the service was down then (the injection came before the window and held it down across it), or
        its stop was asked inside the window. An injection stopped before the window that the service was running
        despite at the start (an uncleared one, its restore unverified) holds nothing down: a service that is down at
        the end after that is another outage, and explained by none."""
        if self.entries is None:
            return []
        return [e for e in self.entries if self._holds(e, service) and self._fits(e, was_running) and self.owned(e)]

    def strangers(self, service: str, was_running: bool) -> int:
        """How many open injections that would hold the service down are not the run's own."""
        if self.entries is None or self.ownership == "not_checked":
            return 0
        return sum(
            1 for e in self.entries if self._holds(e, service) and self._fits(e, was_running) and not self.owned(e)
        )

    def _fits(self, entry: Mapping[str, Any], was_running: bool) -> bool:
        return not was_running or entry["stop_line"] >= self.first

    @staticmethod
    def _holds(entry: Mapping[str, Any], service: str) -> bool:
        return (
            entry["service"] == service
            and entry["stop_line"] >= 0
            and entry["cleared"] is None  # `never_down` is a clearing: such an entry never held it
        )


@dataclass(frozen=True)
class _Change:
    """What differs between a service's two readings: restarts seen (`exact` when counted, by the launches; else at
    least), how many of them the journal could explain (`eligible`: on HPC the launches the driver recorded, never a
    step the state does not name), how many Docker's restart policy made, and what does not add up."""

    count: int
    eligible: int
    exact: bool
    policy: int
    odd: list[str]
    cannot: str


def _change(before: Mapping[str, Any], after: Mapping[str, Any]) -> _Change:
    odd: list[str] = []
    now, was = after["container"] or after["step"], before["container"] or before["step"]
    moved = now is not None and now != was
    started = (
        now is not None
        and None not in (before["started_at"], after["started_at"])
        and before["started_at"] != after["started_at"]
    )
    policy = 0
    if isinstance(before["restart_count"], int) and isinstance(after["restart_count"], int):
        policy = max(0, after["restart_count"] - before["restart_count"])
    launches, backwards = None, False
    if isinstance(before["launch"], int) and isinstance(after["launch"], int):
        launches = after["launch"] - before["launch"]
        if launches < 0:
            odd.append("the launch counter went backwards between the readings")
            launches, backwards = None, True
    if launches and not (moved or started):
        odd.append("the launch counter rose while the step did not change: a start may be in flight")
    # a step squeue lists that the state does not name at the end, and which is not the one listed at the start (or that
    # moved since): a restart the driver did not make, whatever the driver launched
    outside = int(bool(after["unrecorded"]) and bool(moved or started or not before["unrecorded"]))
    named = "the state file names another step than the one squeue lists: a restart the driver did not make"
    if launches is None:  # Compose, or no usable counter: one change seen, at least one restart
        count = max(int(bool(moved or started or policy)), policy, outside)
        if backwards:
            return _Change(
                count, 0, False, policy, odd, "the launch counter went backwards: restarts cannot be counted"
            )
        return _Change(count, count - outside, False, policy, odd, named)
    # HPC: the driver's launches are the restarts it made; a step it does not name is one it did not make
    unrecorded = int(bool(outside) or (bool(moved or started) and launches == 0))
    return _Change(launches + unrecorded, launches, True, 0, odd, named)


def _restarts(
    member: Member, before: Mapping[str, Any], after: Mapping[str, Any], change: _Change, window: _JournalWindow
) -> list[dict[str, Any]]:
    """A service's restarts, each labelled `outage` or `heal` when the journal (and, on HPC, the state's recorded stops)
    explains it, else `finding`. The explained ones come first; no restart is explained twice."""
    if change.count == 0:
        return []
    said = f"{change.count} restart(s)" if change.exact else f"at least {change.count} restart(s)"

    def finding(why: str) -> dict[str, Any]:
        return {"label": "finding", "explained": False, "reason": f"{said}: {why}"}

    if window.entries is None:
        return [finding(str(window.why)) for _ in range(change.count)]
    if change.policy:
        why = f"RestartCount rose by {change.policy}: Docker's restart policy, which the injector never uses"
        return [finding(why) for _ in range(change.count)]
    held = window.held(member.service, bool(before["running"]))
    capacity, short = min(len(held), change.eligible), "no journalled injection of it is left"
    foreign = window.foreign(member.service, bool(before["running"]))
    if foreign and not held:
        short = f"{foreign} injection(s) of it in the window are not this run's (another attempt's, or none named)"
    if change.eligible < change.count:
        short = change.cannot
    if isinstance(before["stops"], int) and isinstance(after["stops"], int):  # HPC: a running service owes a stop
        allowed = max(after["stops"] - before["stops"], 0) + (0 if before["running"] else 1)
        if allowed < capacity:
            capacity, short = allowed, "the state records no stop for it after the start reading"
    restarts = []
    for index in range(change.count):
        if index < capacity:
            label = _kind(held[index : index + 1]) if change.exact else _kind(held)
            whose = ", this run's own" if window.ownership == "run" else ""
            reason = f"{said}; the journal holds {len(held)} injection(s) of the service in the window{whose}"
            restarts.append({"label": label, "explained": True, "reason": reason})
        else:
            restarts.append(finding(f"{min(change.count, capacity)} explained, the rest not: {short}"))
    return restarts


def _judge(
    member: Member, before: Mapping[str, Any] | None, after: dict[str, Any], window: _JournalWindow
) -> dict[str, Any]:
    """One service over the window: both readings, its restarts, and the reasons it is unknown or a finding."""
    unknown = ["no reading at the window's start, so a restart cannot be ruled out"] if before is None else []
    unknown += [f"at the start, {why}" for why in (before or {}).get("unknown", [])]
    unknown += [f"at the end, {why}" for why in after["unknown"]]
    restarts: list[dict[str, Any]] = []
    if before is not None and before["state"] != "unknown" and after["state"] != "unknown":
        change = _change(before, after)
        unknown += change.odd
        restarts = _restarts(member, before, after, change, window)
    findings = [f"restart not explained, {r['reason']}" for r in restarts if not r["explained"]]
    down = after["state"] == "not_running"
    was_running = bool(before and before["running"])
    holding = window.holding(member.service, was_running)
    if down and not holding:
        findings.append(_down_finding(window, member.service, was_running))
    if after["state"] == "running" and after["health"] not in (None, "healthy"):
        findings.append(f"its container's health check reports {after['health']} at the window's end")
    status = (
        "unknown"
        if unknown
        else "held"
        if down and holding
        else "down"
        if down
        else "restarted"
        if restarts
        else "stable"
    )
    return {
        "status": status,
        "start": before,
        "end": after,
        "restarts": restarts,
        "held": _held(window, holding) if status == "held" else None,
        "unknown": unknown,
        "findings": findings,
    }


def _down_finding(window: _JournalWindow, service: str, was_running: bool) -> str:
    """Why a service not running at the window's end is a finding: nothing holds it down that the window may count."""
    strangers = window.strangers(service, was_running)
    if window.ownership == "unattributed":
        return "not running at the window's end, and the run names no attempt, so no open injection of it is its own"
    if strangers:
        return (
            f"not running at the window's end, held down only by {strangers} open injection(s) that are not this run's "
            "(another attempt's, or one that names no attempt)"
        )
    mine = " that this run owns" if window.ownership == "run" else ""
    return f"not running at the window's end, and no open injection of it{mine} holds it down"


def _held(window: _JournalWindow, holding: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """What holds a service down at the window's end: how many open injections, which the run owns, and where they
    began. Not a pass by itself: the restore comes after the window, so the run's restore outcome is read with it."""
    before = any(entry["lines"][0] < window.first for entry in holding)
    inside = any(entry["lines"][0] >= window.first for entry in holding)
    where = (
        "before the window and inside it"
        if before and inside
        else "before the window"
        if before
        else "inside the window"
    )
    whose = "this run owns" if window.ownership == "run" else "(whose they are is not checked: no run was named)"
    return {
        "injections": len(holding),
        "opened": where,
        "reason": f"not running at the window's end, held down by {len(holding)} open injection(s) of it that {whose}, "
        f"opened {where}; its restore comes after the window, so the run's own restore outcome is read with this",
    }


def _is_start(start: Mapping[str, Any] | None, backend: str) -> bool:
    return (
        isinstance(start, Mapping)
        and start.get("format") == FORMAT
        and start.get("backend") == backend
        and isinstance(start.get("services"), Mapping)
        and isinstance(start.get("journal"), Mapping)
    )


class FederationWatch:
    """One federation's services, read now (`read`) and over a window (`window`); see the module docstring."""

    def __init__(
        self,
        members: Sequence[Member],
        source: ServiceSource,
        client: Client,
        *,
        journal: Path | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`journal` is the injection journal's directory (`[faults] journal`); None declares none, and then nothing can
        explain a restart."""
        labels = [member.label for member in members]
        if not members or len(set(labels)) != len(labels):
            raise ValueError("a watch has members, each with a label of its own")
        self._members, self._source, self._client = tuple(members), source, client
        self._journal, self._clock = None if journal is None else Path(journal), clock

    def _live(self, member: Member, running: Any) -> dict[str, Any]:
        """The service's own answer to `GET /configuration`: `answered` True, or None with the reason (never False: a
        service that does not answer is unread, not known to be down)."""
        if running is False:
            return {"answered": None, "reason": NOT_RUNNING}
        try:
            self._client.get("/configuration", provider=member.provider, timeout_s=PROBE_TIMEOUT_S, retry=ONE_ATTEMPT)
        except Exception as error:
            return {"answered": None, "reason": _reason(error)}
        return {"answered": True, "reason": None}

    def _lines(self) -> tuple[list[str] | None, str | None]:
        try:
            return _journal_lines(self._journal), None
        except _Unreadable as error:
            return None, str(error)

    def _reading(self, lines: list[str] | None, why: str | None) -> dict[str, Any]:
        raw = self._source.read(self._members)
        services = {}
        for member in self._members:
            seen = raw.get(member.label) or {"unknown": ["the backend's read answered nothing for the service"]}
            services[member.label] = _side(seen, self._live(member, seen.get("running")))
        journal = {"declared": self._journal is not None, "lines": None if lines is None else len(lines)}
        journal["read"] = True if lines is not None else (why or NO_JOURNAL)
        return {"format": FORMAT, "backend": self._source.backend, "at": _utc(self._clock()), "journal": journal,
                "services": services}  # fmt: skip

    def read(self) -> dict[str, Any]:
        """One reading of every service now, to be kept as a window's start: plain JSON."""
        return self._reading(*self._lines())

    def window(self, start: Mapping[str, Any] | None, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Read now and return the window since `start` (a reading `read` made); a missing or foreign `start` leaves
        every service unknown. `owner` names the run the window is of, as the injector writes it into each `injecting`
        line (`liveness.owner()`): only the injections that name the same attempt explain a restart or hold a service
        down, so another attempt's leftover, and a line written before owners were recorded, explain nothing. With no
        `owner` (a batch's window) any injection counts, which the result says (`ownership`: `not_checked`)."""
        lines, why = self._lines()
        end = self._reading(lines, why)
        first = start if _is_start(start, end["backend"]) else None
        window = _JournalWindow.of(lines, why, first, owner)
        services: dict[str, Any] = {}
        findings: list[str] = []
        unknown: list[str] = []
        for member in self._members:
            before = None if first is None else first["services"].get(member.label)
            seen = _judge(member, before, end["services"][member.label], window)
            findings += [f"{member.label}: {reason}" for reason in seen.pop("findings")]
            unknown += [f"{member.label}: {reason}" for reason in seen["unknown"]]
            services[member.label] = seen
        held = any(s["status"] == "held" for s in services.values())
        busy = any(s["restarts"] for s in services.values())
        verdict = (
            "finding" if findings else "unknown" if unknown else "held" if held else "restarted" if busy else "stable"
        )
        journal = {
            "declared": end["journal"]["declared"],
            "lines_start": None if first is None else first["journal"].get("lines"),
            "lines_end": end["journal"]["lines"],
            "read": True if window.entries is not None else window.why,
        }
        return {
            "format": FORMAT,
            "backend": end["backend"],
            "window": {"start": None if first is None else first["at"], "end": end["at"]},
            "verdict": verdict,
            "ownership": window.ownership,
            "services": services,
            "findings": findings,
            "unknown": unknown,
            "journal": journal,
        }


def _accounting_wait(value: Any) -> float:
    """`watch_for`'s `accounting_wait_s`: a number of seconds from 0 to `MAX_ACCOUNTING_WAIT_S`, never a bool."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= MAX_ACCOUNTING_WAIT_S:
        raise ValueError(
            f"accounting_wait_s must be a number of seconds from 0 to {MAX_ACCOUNTING_WAIT_S:g}, not {value!r}"
        )
    return float(value)


def watch_for(
    config: "PlatformConfig",
    settings: FaultSettings,
    client: Client,
    *,
    zone: str,
    hpc_command: Sequence[str] | None = None,
    runner: CommandRunner = run_command,
    clock: Callable[[], float] = time.time,
    accounting_wait_s: float = DEFAULT_ACCOUNTING_WAIT_S,
) -> FederationWatch:
    """The watch `[faults]` of platform.toml describes: the zone (`zone`, its backend's name for the service) and each
    provider of `[faults.services]`, read through Compose's driver or, for `backend = "hpc"`, through `hpc_command` (an
    argv that runs `hpcobserve.py --root <the federation's root>` where Slurm's client commands answer), with the
    injection journal `[faults] journal` names. `runner` runs the commands, a test's fake or the subprocess one.

    `accounting_wait_s` is the HPC read's: the longest, from 0 to 300 s (60 unless declared), that `hpcobserve.py` waits
    for Slurm's accounting to show a step the driver's state says was stopped (see the module docstring). The watch
    appends it to `hpc_command` as `--accounting-wait-s` and gives the command that long on top of
    `command_timeout_s`. A Compose watch has no such wait; the bound is still checked."""
    wait = _accounting_wait(accounting_wait_s)
    members = members_of(config, zone=zone, providers=settings.services)
    if settings.backend == "hpc":
        if hpc_command is None:
            raise ValueError("an HPC federation is read through hpcobserve.py: name the command that runs it")
        command = [*hpc_command, "--accounting-wait-s", f"{wait:g}"]
        source: ServiceSource = HpcSource(command, runner=runner, timeout_s=settings.command_timeout_s + wait)
    else:
        source = ComposeSource(compose_driver(settings, runner, probe=_never_asked))
    return FederationWatch(members, source, client, journal=settings.journal, clock=clock)


def _never_asked(service: str) -> bool:
    """The driver's required probe, which a read never uses: a call is a defect, not an answer."""
    raise RuntimeError("a read of the federation never asks a provider to serve")
