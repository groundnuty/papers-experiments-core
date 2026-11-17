# Provenance: new
"""The mount-guard instrument: whether the agent touched the mount's token or oneclient's process, from the host
watcher's log (namespace `mount_guard`).

`MountGuard(source, nonce)` is an `Instrument` a platform's executor builds for each run whose mount cannot hide the
token from the agent the way a separate container does (an HPC batch node, where oneclient and the agent's cell run as
one uid). `source.mount_watch_log()` returns the bytes of the watcher's log, or None when there is none; `nonce` is the
value the host gave the run's watcher, so a log of another run is never read as this one's. The executor closes the
window (`disarmed`) after the agent's cell has stopped and stops the watcher before `finish_instruments`: a watcher
still running has written no end line, and its log reads as unknown (`not_closed`).

The snapshot, under `record.instruments["mount_guard"]`, holds counts and reasons, never a pid, a path or a name:

    {"version": 1, "log": "intact" | "unknown", "unknown": [<reasons>],
     "isolation": "proven" | "unproven", "unproven": [<reasons>],
     "accesses": {"token_file": 0, "token_dir": 0}, "traced": 0, "proofs": 2, "ptrace_scope": 1}

`accesses` are the inotify events on the token file and on its directory inside the agent's window (named so that the
record's redaction, which takes a key `token` or `..._token` for a secret's, leaves the counts as they are), `traced`
the tracers attached to oneclient inside it, `proofs` the isolation proofs read in it, and `ptrace_scope` the
kernel's Yama setting (None without Yama). When the log is unknown the counts are what its readable part shows, a
lower bound. The reasons are `expcore.mountguard.log`'s. **A missing or damaged log is `"log": "unknown"`, never
intact with no access; a log without a proof, or with a failed one, is `"isolation": "unproven"`.**

**A run with no mount** (a REST run, a run with no data path) has nothing for the guard to watch: its exposure to the
mount's token is nil, so the guard does not apply to it. `mount_guard_for(sandbox)` builds the instrument of any run, a
`MountGuard` for a mount run and `NoMount` for the others, so a study may list it for every run of a campaign that
mixes data paths. `NoMount` records the statement `{"version": 1, "mount": false, "data_path": "rest" | "none"}`, and
`judge` reads it as `not_applicable`: the run stays in the mount guard's rerun. **The sandbox's own data path decides,
never a missing watcher or a missing snapshot:** a mount run whose helper keeps no watcher has no nonce and is
refused, a mount run whose record lacks the snapshot is `unknown`, and a statement that is not exactly the one above is
`unknown` too.
"""

from collections.abc import Mapping
from typing import Any, Protocol

from expcore.mountguard.log import Reading, read_log

NAMESPACE = "mount_guard"
VERSION = 1
INTACT, UNKNOWN = "intact", "unknown"
PROVEN, UNPROVEN = "proven", "unproven"
MOUNT_PATH = "mount"  # the data path that has a mount
NO_MOUNT_PATHS = ("rest", "none")  # the data paths that have none (`expcore_onedata.sandbox.api.DataPathMode`)
NOT_APPLICABLE_KEYS = {"version", "mount", "data_path"}


class MountWatchSource(Protocol):
    """Where the run's watcher log is: its bytes, or None when there is none."""

    def mount_watch_log(self) -> bytes | None: ...


def snapshot(reading: Reading) -> dict[str, Any]:
    """The instrument's snapshot of one log reading (see the module's text)."""
    return {
        "version": VERSION,
        "log": INTACT if reading.intact else UNKNOWN,
        "unknown": list(reading.problems),
        "isolation": UNPROVEN if reading.isolation_problems else PROVEN,
        "unproven": list(reading.isolation_problems),
        "accesses": dict(reading.accesses),
        "traced": reading.traced,
        "proofs": reading.proofs,
        "ptrace_scope": reading.ptrace_scope,
    }


class MountRunSource(MountWatchSource, Protocol):
    """A sandbox as `mount_guard_for` reads it: the data path it was started with, which only its own spec says, and
    the nonce of its mount's watcher (None without one)."""

    @property
    def data_path_kind(self) -> str: ...

    @property
    def mount_guard_nonce(self) -> str | None: ...


def not_applicable_snapshot(data_path: str) -> dict[str, Any]:
    """The statement of a run whose data path has no mount (see the module's text)."""
    return {"version": VERSION, "mount": False, "data_path": data_path}


class NoMount:
    """The `Instrument` of a run whose data path has no mount: it records that the guard does not apply, and reads
    nothing. Built through `mount_guard_for`, which takes the data path from the sandbox."""

    namespace = NAMESPACE

    def __init__(self, data_path: str) -> None:
        if data_path not in NO_MOUNT_PATHS:
            raise ValueError(f"a run with no mount has a data path of {NO_MOUNT_PATHS}, not {data_path!r}")
        self._data_path = data_path

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read."""

    def finish(self) -> dict[str, Any]:
        return not_applicable_snapshot(self._data_path)


def mount_guard_for(sandbox: MountRunSource) -> "MountGuard | NoMount":
    """The mount-guard instrument of one run, from its sandbox: a `MountGuard` over the watcher's log for a mount run
    (`ValueError` when its helper has no nonce: an unguarded mount is not a run the guard does not apply to), `NoMount`
    for a run whose data path is REST or none. The data path is the sandbox's, never inferred from a missing nonce or
    log."""
    kind = sandbox.data_path_kind
    if kind == MOUNT_PATH:
        return MountGuard(sandbox, sandbox.mount_guard_nonce)  # a missing nonce raises there
    if kind in NO_MOUNT_PATHS:
        return NoMount(kind)
    raise ValueError(f"the run's data path is {kind!r}, not one of {(MOUNT_PATH, *NO_MOUNT_PATHS)}")


class MountGuard:
    """The `Instrument` of one run's mount guard: one per run, holding the source of its watcher's log."""

    namespace = NAMESPACE

    def __init__(self, source: MountWatchSource, nonce: str | None) -> None:
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("the run's watcher nonce is a non-empty text")
        self._source, self._nonce = source, nonce

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read before the run: the watcher's log begins before the agent's cell."""

    def finish(self) -> dict[str, Any]:
        """The snapshot of the watcher's log, read once, after the watcher has stopped."""
        return snapshot(read_log(self._source.mount_watch_log(), self._nonce))
