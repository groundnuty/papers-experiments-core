# Provenance: new
"""One run's mount-guard verdict, from its record alone: `clean`, `flagged` or `unknown`.

`judge(record, condition)` reads `record.instruments["mount_guard"]`. A run gets each flag that holds:

- `token_access`: its log shows an event on the token file or its directory inside the agent's window;
- `process_access`: its log shows a tracer attached to oneclient inside the window;
- `log_unknown`: there is no snapshot (the instrument was not run), the instrument failed (`{"ok": false, ...}`), the
  snapshot is not one this code reads, or it says the log is unknown;
- `isolation_unproven`: the snapshot says the cell's isolation was not proven (no passing start and end proof of one
  cell: its own PID namespace, no host `/proc`, no protected directory reachable), or there is no readable snapshot.

The verdict is `flagged` when a run has an access flag, whatever else it has (a damaged log that still shows an access
says both); otherwise `unknown` when it has any flag; otherwise `clean`. **A missing or damaged log, or an unproven
isolation, is never clean.** Every run that is not clean is listed in the report and left out of the sensitivity
analysis, as the egress check leaves out a run whose log is unknown.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from expcore.mountguard.instrument import INTACT, NAMESPACE, PROVEN, UNKNOWN, UNPROVEN, VERSION
from expcore.mountguard.log import TARGETS

TOKEN_ACCESS, PROCESS_ACCESS = "token_access", "process_access"
LOG_UNKNOWN, ISOLATION_UNPROVEN = "log_unknown", "isolation_unproven"
FLAGS = (TOKEN_ACCESS, PROCESS_ACCESS, LOG_UNKNOWN, ISOLATION_UNPROVEN)
ACCESS_FLAGS = (TOKEN_ACCESS, PROCESS_ACCESS)
CLEAN, FLAGGED, UNKNOWN_VERDICT = "clean", "flagged", "unknown"
VERDICTS = (CLEAN, FLAGGED, UNKNOWN_VERDICT)
NOT_RECORDED, INSTRUMENT_FAILED, NOT_A_SNAPSHOT = "not_recorded", "instrument_failed", "not_a_snapshot"
SNAPSHOT_KEYS = {"version", "log", "unknown", "isolation", "unproven", "accesses", "traced", "proofs", "ptrace_scope"}


@dataclass(frozen=True)
class RunMountGuard:
    """One run's mount-guard verdict: its log's state and why it is unknown, its isolation and why it is unproven,
    the accesses and tracers its log shows, and its flags (empty for a clean run)."""

    run_key: str
    attempt: int
    condition: str
    log: str
    unknown: tuple[str, ...]
    isolation: str
    unproven: tuple[str, ...]
    accesses: Mapping[str, int]
    traced: int
    flags: tuple[str, ...]

    @property
    def verdict(self) -> str:
        if any(flag in ACCESS_FLAGS for flag in self.flags):
            return FLAGGED
        return UNKNOWN_VERDICT if self.flags else CLEAN

    @property
    def left_out(self) -> bool:
        """Whether the sensitivity analysis leaves the run out: every run that is not clean."""
        return bool(self.flags)


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _reasons(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(reason, str) and reason for reason in value)


def _readable(data: Any) -> tuple[Mapping[str, Any] | None, str | None]:
    """The snapshot when this code reads it; else why not."""
    if data is None:
        return None, NOT_RECORDED
    if isinstance(data, Mapping) and data.get("ok") is False:
        return None, INSTRUMENT_FAILED
    if not isinstance(data, Mapping) or set(data) != SNAPSHOT_KEYS or data["version"] != VERSION:
        return None, NOT_A_SNAPSHOT
    if data["log"] not in (INTACT, UNKNOWN) or data["isolation"] not in (PROVEN, UNPROVEN):
        return None, NOT_A_SNAPSHOT
    if not _reasons(data["unknown"]) or not _reasons(data["unproven"]):
        return None, NOT_A_SNAPSHOT
    # an intact log has no reason and an unknown one at least one; so has the isolation
    if (data["log"] == INTACT) == bool(data["unknown"]) or (data["isolation"] == PROVEN) == bool(data["unproven"]):
        return None, NOT_A_SNAPSHOT
    accesses, scope = data["accesses"], data["ptrace_scope"]
    if not isinstance(accesses, Mapping) or set(accesses) != set(TARGETS) or not all(map(_whole, accesses.values())):
        return None, NOT_A_SNAPSHOT
    if not _whole(data["traced"]) or not _whole(data["proofs"]) or not (scope is None or scope in (0, 1, 2, 3)):
        return None, NOT_A_SNAPSHOT
    if isinstance(scope, bool) or (data["isolation"] == PROVEN and data["proofs"] < 2):
        return None, NOT_A_SNAPSHOT  # a proven isolation has its start and its end proof at least
    return data, None


def judge(record: Any, condition: str) -> RunMountGuard:
    """The mount-guard verdict of one analysed run (see the module's text)."""
    data, why = _readable(record.instruments.get(NAMESPACE))
    if data is None:
        found = {LOG_UNKNOWN, ISOLATION_UNPROVEN}
        reasons = (str(why),)
        accesses: Mapping[str, int] = dict.fromkeys(TARGETS, 0)
        return RunMountGuard(
            record.run_key, record.attempt, condition, UNKNOWN, reasons, UNPROVEN, reasons, accesses, 0,
            tuple(flag for flag in FLAGS if flag in found),
        )  # fmt: skip
    found = set()
    if sum(data["accesses"].values()):
        found.add(TOKEN_ACCESS)
    if data["traced"]:
        found.add(PROCESS_ACCESS)
    if data["log"] != INTACT:
        found.add(LOG_UNKNOWN)
    if data["isolation"] != PROVEN:
        found.add(ISOLATION_UNPROVEN)
    return RunMountGuard(
        record.run_key, record.attempt, condition, data["log"], tuple(data["unknown"]), data["isolation"],
        tuple(data["unproven"]), dict(data["accesses"]), data["traced"], tuple(flag for flag in FLAGS if flag in found),
    )  # fmt: skip
