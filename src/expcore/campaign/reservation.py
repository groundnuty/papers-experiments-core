# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/reservation.py, blob d056b1e22a59b0f72388c5d4e27953ff89514276
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Process-bound reservations: what a separate process beside the dispatcher holds back from the campaign (C19).

A calibration run or a validation script is a process of its own on the dispatcher's host. While it is up:

- its sandboxes count in the campaign's concurrency caps (`count`), and
- its keys are out of the campaign's lease pool, so no run leases a resource whose audit trail that process reads.

A reservation is a `flock` on a file of its own, `<directory>/<name>.lock`, taken by the process for as long as it runs.
The kernel drops the lock when the process dies, however it dies, so nothing is cleaned up and nothing stays reserved.
The dispatcher looks at each lock without blocking, with a shared lock, so looks never conflict with each other, only
with the holder. **A lock that cannot be read reads as held**, never as released: a missing directory, a permission
error or any other error keeps the reservation in force, and the caller says so. A reservation is never dropped for
want of an answer.

The directory and the specs are parameters; nothing is read from the environment, and no name or key is spelled here.
"""

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TAKE_RETRY_S = (
    3.0  # a look holds the lock for an instant; a holder starting then tries again before it says another one is up
)


class ReservationError(RuntimeError):
    """A reservation is misdeclared, or another process already holds the one asked for."""


@dataclass(frozen=True)
class ReservationSpec:
    """What one process holds back while it is up: `count` sandboxes in the caps, and the lease `keys` it reads."""

    count: int
    keys: tuple[str, ...] = ()


def problems(specs: Mapping[str, ReservationSpec], known_keys: Collection[str] | None = None) -> list[str]:
    """Return everything wrong with the declared reservations, all at once.

    A count must be a whole number of at least one; a key must be one the pool knows (when `known_keys` is given), and
    a key belongs to one reservation only.
    """
    found: list[str] = []
    owner: dict[str, str] = {}
    for name, spec in sorted(specs.items()):
        if isinstance(spec.count, bool) or not isinstance(spec.count, int) or spec.count < 1:
            found.append(f"reservation {name!r}: count must be a whole number of at least 1, got {spec.count!r}")
        if len(set(spec.keys)) != len(spec.keys):
            found.append(f"reservation {name!r}: keys repeats a key")
        for key in spec.keys:
            if known_keys is not None and key not in known_keys:
                found.append(f"reservation {name!r}: key {key!r} is not a key of the lease pool")
            if key in owner and owner[key] != name:
                found.append(f"key {key!r} is in two reservations ({owner[key]!r}, {name!r})")
            owner[key] = name
    return found


def lock_path(directory: Path, name: str) -> Path:
    """Return the lock file of a reservation."""
    return Path(directory) / f"{name}.lock"


def probe(directory: Path, name: str) -> tuple[bool, str | None]:
    """Return (held, problem). Held: the process is up, or its lock could not be read (then `problem` says why).

    No file means the process has not started; a missing directory is a failure to read, not an absence.
    """
    path = lock_path(directory, name)
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        try:
            os.stat(directory)
        except OSError as error:
            return True, f"{type(error).__name__}: {error}"
        return False, None
    except OSError as error:
        return True, f"{type(error).__name__}: {error}"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True, None
        except OSError as error:
            return True, f"{type(error).__name__}: {error}"
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return False, None
    finally:
        os.close(fd)


def look(directory: Path | None, names: Iterable[str]) -> tuple[set[str], dict[str, str]]:
    """Return the names held now, and why any could not be read (those count as held). No directory reads all as held."""
    held: set[str] = set()
    unreadable: dict[str, str] = {}
    for name in names:
        if directory is None:
            held.add(name)
            unreadable[name] = "no reservation directory"
            continue
        is_held, why = probe(Path(directory), name)
        if is_held:
            held.add(name)
        if why:
            unreadable[name] = why
    return held, unreadable


class Reservations:
    """The reservations a campaign honours, looked at each time they are used so that a process that starts is honoured
    at the next look and a key is back as soon as its process is gone."""

    def __init__(self, directory: Path | None, specs: Mapping[str, ReservationSpec]):
        bad = problems(specs)
        if bad:
            raise ReservationError("; ".join(bad))
        self.directory, self.specs = directory, dict(specs)

    def held(self) -> tuple[set[str], dict[str, str]]:
        """Return the names held now and the ones whose lock could not be read."""
        return look(self.directory, self.specs)

    def excluded_keys(self) -> set[str]:
        """Return the keys of every reservation held now or unreadable: out of the lease pool."""
        held, _ = self.held()
        return {key for name in held for key in self.specs[name].keys}

    def reserved_count(self) -> int:
        """Return the sandboxes the held reservations count in the caps."""
        held, _ = self.held()
        return sum(self.specs[name].count for name in held)

    def without_reserved(self, resolve: Callable[[str], Sequence[str]]) -> Callable[[str], list[str]]:
        """Wrap a pool's `resolve` so that keys held back for a process beside the campaign are left out."""

        def resolved(group: str) -> list[str]:
            gone = self.excluded_keys()
            return [key for key in resolve(group) if key not in gone]

        return resolved


_TAKEN: list["Held"] = []  # a lock taken here is kept by the module, so a caller that drops its handle does not drop it


class Held:
    """A reservation taken by this process; the lock lives as long as the process (or `release()`)."""

    def __init__(self, handle: Any, name: str, path: Path):
        self._handle, self.name, self.path = handle, name, path

    def release(self) -> None:
        """Drop the reservation."""
        handle, self._handle = self._handle, None
        if self in _TAKEN:
            _TAKEN.remove(self)
        if handle is not None:
            handle.close()


def hold(name: str, directory: Path, *, retry_s: float = TAKE_RETRY_S) -> Held:
    """Take the reservation of `name`, or raise if another process of that name holds it (two would use twice what is
    reserved)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # close-on-exec: children never inherit it. The handle is kept for as long as the process runs, so no `with`.
    handle = open(lock_path(directory, name), "a+", encoding="utf-8")  # noqa: SIM115
    began = time.monotonic()
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() - began >= retry_s:
                handle.close()
                raise ReservationError(
                    f"reservation {name!r} is held by another process ({lock_path(directory, name)}): "
                    "it would use twice what the campaign counts"
                ) from None
            time.sleep(0.05)
    with contextlib.suppress(OSError):  # the note is for people; the lock is the truth
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({"pid": os.getpid(), "since": time.time(), "name": name}))
        handle.flush()
    held = Held(handle, name, lock_path(directory, name))
    _TAKEN.append(held)
    return held
