# Provenance: new
"""A controller that honours operator pauses says so, and the operator's `pause` reads it before writing (C55).

A core without `pauses` opens a ledger that holds a pause and leases the paused model as before, silently. So
`pause` must not write while the controller that holds the ledger is such a core. Nothing the controller leaves names
its core (`campaign.lock` is an empty file that only carries a `flock`, the controller writes no pid file or heartbeat
and no ledger event), so a controller that honours pauses declares itself: `Ledger` takes an exclusive `flock` on
`<ledger file>.pause-aware`, beside the ledger file, right after the dispatcher lock, and holds it until it closes.
The kernel drops the lock with its holder however it dies, so a declaration cannot outlive the controller, and a core
without this change never takes it. The file is named as the ledger's own are (as its `.creating`): no worker, lease
store or federation root reads or writes it.

`controller_of` reads the two locks without taking either for longer than the probe:
- `aware`: the dispatcher lock is held exclusively and so is the declaration: a live controller that honours pauses;
- `older`: the dispatcher lock is held and the declaration is not: a live controller that would ignore a pause (or one
  that is only now starting, between its two locks: a refusal then is safe, and the command is run again);
- `none`: no controller holds the ledger;
- `unknown`: the locks could not be read (a file system without `flock`, an unreadable file).
"""

import fcntl
import os
import time
import warnings
from pathlib import Path

from expcore.campaign.lock import CampaignLocked, LockMissing, LockUnavailable, ReaderLock

MARK_SUFFIX = ".pause-aware"  # `ledger.sqlite.pause-aware`: named as the ledger's own files are (`-wal`, `.creating`)
AWARE, OLDER, NONE, UNKNOWN = "aware", "older", "none", "unknown"
TAKE_S = 2.0  # how long a controller waits for a probe's momentary shared hold before it runs undeclared


def mark_path(ledger: Path) -> Path:
    """The declaration file of the ledger file at `ledger`."""
    return Path(ledger).with_name(Path(ledger).name + MARK_SUFFIX)


class PauseAwareMark:
    """A controller's declaration that it honours operator pauses: an exclusive `flock` held from `acquire` to
    `release`, by the process that holds the dispatcher lock of the ledger file at `ledger`."""

    def __init__(self, ledger: Path) -> None:
        self.path = mark_path(ledger)
        self._fd: int | None = None

    def acquire(self) -> "PauseAwareMark":
        """Take the declaration. A controller never fails to start for it: one that cannot take it (a file system
        without `flock`, a read-only directory, a probe holding it past `TAKE_S`) warns and runs undeclared, so a
        `pause` is refused while it runs, never written to a controller that did not say it would honour it."""
        if self._fd is not None:
            raise RuntimeError("this controller already holds its declaration")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as error:
            warnings.warn(f"{self.path}: cannot be made, so operator pauses are refused: {error}", stacklevel=2)
            return self
        deadline = time.monotonic() + TAKE_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    warnings.warn(f"{self.path}: held by another process, so operator pauses are refused", stacklevel=2)
                    return self
                time.sleep(0.02)
            except OSError as error:
                os.close(fd)
                warnings.warn(f"{self.path}: cannot be locked, so operator pauses are refused: {error}", stacklevel=2)
                return self
        self._fd = fd
        return self

    def release(self) -> None:
        """Let go of the declaration; releasing twice, or one never held, does nothing."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @property
    def held(self) -> bool:
        return self._fd is not None


def _declared(ledger: Path) -> str:
    """Whether the declaration is held (`aware`), is not (`older`), or could not be read (`unknown`)."""
    path = mark_path(ledger)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return OLDER
    except OSError:
        return UNKNOWN
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return AWARE
    except OSError:
        return UNKNOWN
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return OLDER
    finally:
        os.close(fd)


def controller_of(ledger: Path) -> str:
    """The state of the controller of the ledger file at `ledger`: `aware`, `older`, `none` or `unknown` (the module's
    docstring)."""
    try:
        with ReaderLock(Path(ledger).parent):  # a shared hold is refused only while a dispatcher holds it exclusively
            return NONE
    except CampaignLocked:
        return _declared(ledger)
    except LockMissing:
        return NONE
    except LockUnavailable:
        return UNKNOWN


__all__ = ["AWARE", "MARK_SUFFIX", "NONE", "OLDER", "UNKNOWN", "PauseAwareMark", "controller_of", "mark_path"]
