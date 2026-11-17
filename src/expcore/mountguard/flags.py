# Provenance: new
"""One run's mount-guard verdict, from its record alone: `clean`, `flagged`, `unknown` or `not_applicable`.

`judge(record, condition)` reads `record.instruments["mount_guard"]`. A run gets each flag that holds:

- `token_access`: its log shows an event on the token file or its directory inside the agent's window;
- `process_access`: its log shows a tracer attached to oneclient inside the window;
- `log_unknown`: there is no snapshot (the instrument was not run), the instrument failed (`{"ok": false, ...}`), the
  snapshot is not one this code reads, or it says the log is unknown;
- `isolation_unproven`: the snapshot says the cells' isolation was not proven (no proof, or a failed one: a cell
  without a PID namespace of its own, with the host's `/proc`, or reaching a protected directory), or there is no
  readable snapshot.

The verdict is `flagged` when a run has an access flag, whatever else it has (a damaged log that still shows an access
says both); otherwise `unknown` when it has any flag; otherwise `clean`. **A missing or damaged log, or an unproven
isolation, is never clean.** Every run that is not clean is listed in the report and left out of the sensitivity
analysis, as the egress check leaves out a run whose log is unknown.

A run whose record carries the statement that it had no mount (`expcore.mountguard.instrument.NoMount`: a REST run or a
run with no data path) is `not_applicable`: the guard has nothing to say of it, it has no flag, it is kept in the
sensitivity analysis (it had no mount exposure to leave it out for), and it is shown as such, never as clean. **Only
that exact statement makes a run not applicable**, or, for a record with no snapshot at all, the campaign's own record
that the run's data path had no mount (`expcore.mountguard.campaign`): its `data_path` level maps, in the campaign
fingerprint's `data_paths`, to `rest` or `none`. A record without the snapshot whose level maps to `mount`, to
anything else or to nothing is `unknown` (`not_recorded`), so a mount run that was not guarded is never not
applicable, and any other shape under the namespace is `unknown` (`not_a_snapshot`). A statement that names another
data path than the campaign records for the run's level is `unknown` (`data_path_mismatch`).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from expcore.mountguard.instrument import (
    INTACT,
    MOUNT_PATH,
    NAMESPACE,
    NO_MOUNT_PATHS,
    NOT_APPLICABLE_KEYS,
    PROVEN,
    UNKNOWN,
    UNPROVEN,
    VERSION,
)
from expcore.mountguard.log import ACCESS_KEYS

TOKEN_ACCESS, PROCESS_ACCESS = "token_access", "process_access"
LOG_UNKNOWN, ISOLATION_UNPROVEN = "log_unknown", "isolation_unproven"
FLAGS = (TOKEN_ACCESS, PROCESS_ACCESS, LOG_UNKNOWN, ISOLATION_UNPROVEN)
ACCESS_FLAGS = (TOKEN_ACCESS, PROCESS_ACCESS)
CLEAN, FLAGGED, UNKNOWN_VERDICT, NOT_APPLICABLE = "clean", "flagged", "unknown", "not_applicable"
VERDICTS = (CLEAN, FLAGGED, UNKNOWN_VERDICT)  # what a run with a mount can be; NOT_APPLICABLE is a run with none
NOT_RECORDED, INSTRUMENT_FAILED, NOT_A_SNAPSHOT = "not_recorded", "instrument_failed", "not_a_snapshot"
DATA_PATH_MISMATCH = "data_path_mismatch"  # a no-mount statement that names another data path than the campaign does
DATA_PATH_FACTOR = "data_path"  # the study factor whose levels the platform maps to a data path
FROM_RECORD, FROM_CAMPAIGN = "record", "campaign"  # where a run's not-applicable data path was read from
SNAPSHOT_KEYS = {"version", "log", "unknown", "isolation", "unproven", "accesses", "traced", "proofs", "ptrace_scope"}


@dataclass(frozen=True)
class RunMountGuard:
    """One run's mount-guard verdict: its log's state and why it is unknown, its isolation and why it is unproven,
    the accesses and tracers its log shows, and its flags (empty for a clean run). `not_applicable` is the data path a
    run's record says had no mount (`rest` or `none`), None for every other run; a run with it has no flags, and its log
    and isolation say `not_applicable`, never `intact` or `proven`; `data_path_from` says whether the run's own record
    (`record`) or the campaign's fingerprint (`campaign`) named it."""

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
    not_applicable: str | None = None
    data_path_from: str | None = None

    @property
    def verdict(self) -> str:
        if self.not_applicable is not None:
            return NOT_APPLICABLE
        if any(flag in ACCESS_FLAGS for flag in self.flags):
            return FLAGGED
        return UNKNOWN_VERDICT if self.flags else CLEAN

    @property
    def left_out(self) -> bool:
        """Whether the sensitivity analysis leaves the run out: every run that is not clean. A run the guard does not
        apply to has no flag and stays in."""
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
    keys = set(ACCESS_KEYS.values())
    if not isinstance(accesses, Mapping) or set(accesses) != keys or not all(map(_whole, accesses.values())):
        return None, NOT_A_SNAPSHOT
    if not _whole(data["traced"]) or not _whole(data["proofs"]) or not (scope is None or scope in (0, 1, 2, 3)):
        return None, NOT_A_SNAPSHOT
    if isinstance(scope, bool) or (data["isolation"] == PROVEN and data["proofs"] < 1):
        return None, NOT_A_SNAPSHOT  # a proven isolation has a proof at least
    return data, None


def _campaign_kind(record: Any, data_paths: Mapping[str, str] | None) -> str | None:
    """The data path the campaign records for the level of the run's `data_path` factor; None for a run without the
    factor, a level the campaign does not map, or a campaign that records none."""
    factors = getattr(record, "factors", None)
    level = factors.get(DATA_PATH_FACTOR) if isinstance(factors, Mapping) else None
    kind = data_paths.get(level) if data_paths and isinstance(level, str) else None
    return kind if kind in (MOUNT_PATH, *NO_MOUNT_PATHS) else None


def judge(record: Any, condition: str, data_paths: Mapping[str, str] | None = None) -> RunMountGuard:
    """The mount-guard verdict of one analysed run (see the module's text). `data_paths` is the kind (`mount`, `rest`
    or `none`) the campaign fingerprint records for each level of the `data_path` factor
    (`expcore.mountguard.campaign.recorded_data_paths`); without it a run is judged from its own record alone."""
    snapshot = record.instruments.get(NAMESPACE)
    campaign = _campaign_kind(record, data_paths)
    if snapshot is None and campaign in NO_MOUNT_PATHS:  # no snapshot, and the campaign says this level had no mount
        return RunMountGuard(record.run_key, record.attempt, condition, *_not_applicable(campaign, FROM_CAMPAIGN))
    stated = _no_mount(snapshot)
    if stated is not None and campaign is not None and stated != campaign:
        return RunMountGuard(record.run_key, record.attempt, condition, *_unreadable(DATA_PATH_MISMATCH))
    return RunMountGuard(record.run_key, record.attempt, condition, *_assessed(snapshot))


def verdict_of(snapshot: Any) -> str:
    """The verdict a snapshot gives a run (`clean`, `flagged` or `unknown`), for a sandbox's own metadata."""
    return RunMountGuard("", 0, "", *_assessed(snapshot)).verdict


def _no_mount(data: Any) -> str | None:
    """The data path a run's statement of no mount names, when `data` is exactly that statement
    (`instrument.not_applicable_snapshot`): its three keys and no other, this version, `mount` the boolean False, and
    a data path that has no mount. Anything else is None."""
    if not isinstance(data, Mapping) or set(data) != NOT_APPLICABLE_KEYS:
        return None
    version, mount, path = data["version"], data["mount"], data["data_path"]
    if isinstance(version, bool) or not isinstance(version, int) or version != VERSION or mount is not False:
        return None
    return path if isinstance(path, str) and path in NO_MOUNT_PATHS else None


def _not_applicable(data_path: str, source: str) -> tuple[Any, ...]:
    """What a run with no mount is judged as: no flag, its log and isolation `not_applicable`, and its data path."""
    zero = dict.fromkeys(ACCESS_KEYS.values(), 0)
    return NOT_APPLICABLE, (), NOT_APPLICABLE, (), zero, 0, (), data_path, source


def _unreadable(why: str) -> tuple[Any, ...]:
    """What a run with no usable snapshot is judged as: an unknown log and an unproven isolation, for `why`."""
    both = tuple(flag for flag in FLAGS if flag in (LOG_UNKNOWN, ISOLATION_UNPROVEN))
    return UNKNOWN, (why,), UNPROVEN, (why,), dict.fromkeys(ACCESS_KEYS.values(), 0), 0, both


def _assessed(snapshot: Any) -> tuple[Any, ...]:
    """A snapshot's log state and reasons, isolation and reasons, accesses, tracers, flags and, for a run with no
    mount, its data path and where it was read from."""
    data_path = _no_mount(snapshot)
    if data_path is not None:
        return _not_applicable(data_path, FROM_RECORD)
    data, why = _readable(snapshot)
    if data is None:
        return _unreadable(str(why))
    found = set()
    if sum(data["accesses"].values()):
        found.add(TOKEN_ACCESS)
    if data["traced"]:
        found.add(PROCESS_ACCESS)
    if data["log"] != INTACT:
        found.add(LOG_UNKNOWN)
    if data["isolation"] != PROVEN:
        found.add(ISOLATION_UNPROVEN)
    flags = tuple(flag for flag in FLAGS if flag in found)
    return (data["log"], tuple(data["unknown"]), data["isolation"], tuple(data["unproven"]), dict(data["accesses"]),
            data["traced"], flags)  # fmt: skip
