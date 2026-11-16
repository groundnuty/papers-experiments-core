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
     "accesses": {"token": 0, "dir": 0}, "traced": 0, "proofs": 2, "ptrace_scope": 1}

`accesses` are the inotify events on the token file and on its directory inside the agent's window, `traced` the
tracers attached to oneclient inside it, `proofs` the isolation proofs read in it, and `ptrace_scope` the kernel's Yama
setting (None without Yama). When the log is unknown the counts are what its readable part shows, a lower bound.
The reasons are `expcore.mountguard.log`'s. **A missing or damaged log is `"log": "unknown"`, never intact with no
access; a log without a passing start and end proof of one cell is `"isolation": "unproven"`.**
"""

from collections.abc import Mapping
from typing import Any, Protocol

from expcore.mountguard.log import Reading, read_log

NAMESPACE = "mount_guard"
VERSION = 1
INTACT, UNKNOWN = "intact", "unknown"
PROVEN, UNPROVEN = "proven", "unproven"


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


class MountGuard:
    """The `Instrument` of one run's mount guard: one per run, holding the source of its watcher's log."""

    namespace = NAMESPACE

    def __init__(self, source: MountWatchSource, nonce: str) -> None:
        if not isinstance(nonce, str) or not nonce:
            raise ValueError("the run's watcher nonce is a non-empty text")
        self._source, self._nonce = source, nonce

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read before the run: the watcher's log begins before the agent's cell."""

    def finish(self) -> dict[str, Any]:
        """The snapshot of the watcher's log, read once, after the watcher has stopped."""
        return snapshot(read_log(self._source.mount_watch_log(), self._nonce))
