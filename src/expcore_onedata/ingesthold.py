# Provenance: new
"""The lock that says a live run holds a preallocated ingest's baseline open (O45).

A baseline mark names its owner (`ingesthealth`), but a reader in another process cannot ask that process's campaign
store whether the owner's attempt has ended, and a mark taken outside any attempt names only its process, which may
outlive the instrument. So the start also takes a **hold**: an exclusive `flock` on `<journal>.<space>.hold`, beside
the journal every reader of the marks already has, taken under the journal's admission lock before the start marks
the baseline open, and let go only after a restore marks it whole. It is let go too when the attempt that took it ends
(`expcore.campaign.liveness.on_end`), when the instrument holding it is collected, and by the kernel when its process
dies, however it dies. A reader asks whether someone holds it (`hold_taken`) by trying a shared lock without waiting,
which it gives back at once; a lock held in this process by another instrument conflicts as one held elsewhere does.

An `open` mark with no hold taken is no run in progress, whatever its owner says: its attempt ended, in this process
or another, or its instrument was dropped. A recovery that may run lets go of a hold this process still keeps on the
space (`release_here`), an instrument it never restored, so the start after it can take the hold again.
"""

import fcntl
import os
import threading
import time
import weakref
from pathlib import Path

from expcore.campaign import liveness
from expcore_onedata.fault_journal import make_directories

HOLD_WAIT_S = 5.0  # how long a start waits for a hold a restore that marked the baseline whole is letting go
_POLL_S = 0.01
_LOCK = threading.Lock()
_HOLDS: dict[str, "weakref.ref[BaselineHold]"] = {}  # hold file -> this process's hold on it


def hold_path(journal: Path, space: str) -> Path:
    """The hold file of a space's baseline, beside its journal."""
    return journal.with_name(f"{journal.name}.{space}.hold")


def _key(path: Path) -> str:
    return str(path.absolute())


class BaselineHold:
    """This process's hold on one space's baseline: taken by the start, let go by the restore that marks it whole, by
    the end of the attempt that took it, by the collection of its holder, or with the process."""

    def __init__(self, journal: Path, space: str) -> None:
        self.path = hold_path(journal, space)
        self._release: weakref.finalize | None = None

    @property
    def taken(self) -> bool:
        return self._release is not None and self._release.alive

    def take(self, wait_s: float = HOLD_WAIT_S) -> bool:
        """Take the hold, waiting at most `wait_s` for another to let it go; False when it was not let go."""
        if self.taken:
            return True
        make_directories(self.path.parent)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + wait_s
        try:
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        os.close(descriptor)
                        return False
                    time.sleep(_POLL_S)
        except BaseException:
            os.close(descriptor)
            raise
        self._release = weakref.finalize(self, os.close, descriptor)  # closing the descriptor lets the lock go
        liveness.on_end(self._release)  # inside an attempt, let go when it ends, whoever still holds this
        with _LOCK:
            _HOLDS[_key(self.path)] = weakref.ref(self)
        return True

    def release(self) -> None:
        """Let the hold go; harmless when it is not taken."""
        if self._release is not None:
            self._release()


def hold_taken(journal: Path, space: str) -> bool | None:
    """Whether a live holder, in this process or another, holds the space's baseline: True, False (no hold file, or
    none holds it), or None when the file cannot be opened or locked for another reason."""
    try:
        descriptor = os.open(hold_path(journal, space), os.O_RDONLY)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return None
    finally:
        os.close(descriptor)  # gives back the shared lock, if it was taken
    return False


def release_here(journal: Path, space: str) -> None:
    """Let go of the hold this process keeps on the space, if it keeps one: an instrument it never restored."""
    with _LOCK:
        ref = _HOLDS.get(_key(hold_path(journal, space)))
    hold = ref() if ref is not None else None
    if hold is not None:
        hold.release()


__all__ = ["HOLD_WAIT_S", "BaselineHold", "hold_path", "hold_taken", "release_here"]
