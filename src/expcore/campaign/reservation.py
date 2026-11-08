# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/reservation.py, blob d056b1e22a59b0f72388c5d4e27953ff89514276
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Process-bound reservations: what a separate process on the dispatcher's host holds back from the campaign while it runs (#34, the lead, 3 Nov).

Fault calibration and the direct-path validation set (dataset_76/faults/calibrate.py, gate_validation.py) are processes of their own beside the campaign. While one is up:

- its sandboxes count in every cap of the campaign and in the local host's own cap (`sandboxes`), and
- its slot users (`slot_keys`) are out of the campaign's local pool, so no agent run starts on a user the process's attribution windows read (`score` knows only
  the process's own windows: an agent run on the same user inside the 2 s margin would put audit lines in it).

The reservation is a `flock` on a file of its own, `<reservation_dir>/<name>.lock`, taken by the process for as long as it runs. The kernel drops the lock when the
process dies, however it dies, so nothing is cleaned up and nothing stays reserved. The dispatcher looks at each lock without blocking (a shared lock: looks never
conflict with each other, only with the holder): held = the process is up. A lock that cannot be read (a missing directory, a permission error, any other error)
reads as HELD, never as released, and the caller alarms: a reservation is never dropped for want of an answer.

The launch JSON says what each holds (`reservation_dir`, `process_reservations`); `STUDY_VAR_84` is the same directory for a process that is not given the JSON.
No slot key is named here: the keys are the launch's.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

ENV_DIR = "STUDY_VAR_84"
ENV_KEYS = "STUDY_VAR_85"          # the campaign's own pool only: {name: [slot keys]}; a reserved process never has it (it would exclude its own keys)
TAKE_RETRY_S = 3.0                         # a look holds the lock for an instant; a holder starting at that instant tries again before it says another one is up
REPO = Path(__file__).resolve().parents[2]
HASHED = ("dataset_76", "config", "protocol", "tools")      # a setting made after the freeze must not live in the hashed tree

# the options each process leases from (a key of another option is a mistake in the launch JSON, found before any run starts)
PROCESS_OPTIONS: dict[str, tuple[str, ...]] = {
    "fault_calibration": ("T", "condition_a", "D", "M"),
    "validation_set": ("T", "D"),
}


class ReservationError(RuntimeError):
    pass


def resolve_dir(configured: str | None) -> Path | None:
    """The reservation directory: the launch JSON's `reservation_dir`, else `STUDY_VAR_84`; None if neither. Both set and different is refused, as is a
    relative path (it would mean another directory in another process) and one inside the hashed tree."""
    conf, env = (configured or "").strip(), os.environ.get(ENV_DIR, "").strip()
    if conf and env and os.path.abspath(conf) != os.path.abspath(env):
        raise ReservationError(f"reservation_dir {conf!r} in the launch JSON differs from {ENV_DIR}={env!r}: set one, or make them the same")
    d = conf or env
    if not d:
        return None
    p = Path(d)
    if not p.is_absolute():
        raise ReservationError(f"the reservation directory {d!r} is not an absolute path: another process would read another directory")
    real = Path(os.path.realpath(p))
    for h in HASHED:
        if real == REPO / h or (REPO / h) in real.parents:
            raise ReservationError(f"the reservation directory {d!r} is inside {h}/, which is hashed and compared by the hosts: it must live outside the tree")
    return p


def problems(specs: Any, slot_map: Mapping[str, str], local_keys: Iterable[str] | None) -> list[str]:
    """Everything wrong with `process_reservations`, all at once. `slot_map`: slot key -> option. `local_keys`: the local host's slot partition (None: every key,
    as on one host)."""
    if not specs:
        return []
    if not isinstance(specs, dict):
        return [f"process_reservations must be a mapping of name -> {{sandboxes, slot_keys}}, got {type(specs).__name__}"]
    local = None if local_keys is None else set(local_keys)
    probs: list[str] = []
    owner: dict[str, str] = {}
    for name, spec in sorted(specs.items()):
        if name not in PROCESS_OPTIONS:
            probs.append(f"process reservation {name!r}: not a process that reserves (known: {sorted(PROCESS_OPTIONS)})")
            continue
        if not isinstance(spec, dict) or set(spec) - {"sandboxes", "slot_keys"}:
            probs.append(f"process reservation {name!r}: must be {{sandboxes, slot_keys}} and nothing else, got {spec!r}")
            continue
        n = spec.get("sandboxes")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            probs.append(f"process reservation {name!r}: sandboxes must be a whole number of at least 1, got {n!r}")
        keys = spec.get("slot_keys")
        if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
            probs.append(f"process reservation {name!r}: slot_keys must be a list of slot keys, got {keys!r}")
            continue
        if len(set(keys)) != len(keys):
            probs.append(f"process reservation {name!r}: slot_keys repeats a key")
        for k in keys:
            if k not in slot_map:
                probs.append(f"process reservation {name!r}: slot key {k!r} is not in the slot map")
            elif local is not None and k not in local:
                probs.append(f"process reservation {name!r}: slot key {k!r} is not in the local host's partition: the dispatcher's pool cannot hold back what it never leases")
            elif slot_map[k] not in PROCESS_OPTIONS[name]:
                probs.append(f"process reservation {name!r}: slot key {k!r} is a {slot_map[k]} user, and this process leases only {list(PROCESS_OPTIONS[name])}")
            if k in owner and owner[k] != name:
                probs.append(f"slot key {k!r} is in two process reservations ({owner[k]!r}, {name!r})")
            owner[k] = name
    return probs


def lock_path(d: Path, name: str) -> Path:
    return Path(d) / f"{name}.lock"


def probe(d: Path, name: str) -> tuple[bool, str | None]:
    """(held, problem). held: the process is up, or its lock could not be read (then `problem` says why); not held: it is not up. No file means the process has
    not started (nothing in the directory is made by a look), but a missing directory is a failure to read."""
    path = lock_path(d, name)
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        try:
            os.stat(d)
        except OSError as e:
            return True, f"{type(e).__name__}: {e}"
        return False, None
    except OSError as e:
        return True, f"{type(e).__name__}: {e}"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True, None
        except OSError as e:
            return True, f"{type(e).__name__}: {e}"
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        return False, None
    finally:
        os.close(fd)


def look(d: Path | None, names: Iterable[str]) -> tuple[set[str], dict[str, str]]:
    """The names held now, and why any could not be read (those count as held)."""
    held: set[str] = set()
    bad: dict[str, str] = {}
    for n in names:
        if d is None:
            held.add(n)
            bad[n] = "no reservation directory"
            continue
        h, why = probe(Path(d), n)
        if h:
            held.add(n)
        if why:
            bad[n] = why
    return held, bad


def excluded_keys() -> set[str]:
    """For the campaign's own SlotPool: the slot keys of every reservation held now (or unreadable). Empty when the campaign set no reservations."""
    raw = os.environ.get(ENV_KEYS)
    if not raw:
        return set()
    try:
        specs = json.loads(raw)
        assert isinstance(specs, dict)
    except (ValueError, AssertionError):
        raise ReservationError(f"{ENV_KEYS} is not a JSON mapping of name -> slot keys: the pool cannot tell which keys are held back") from None
    d = os.environ.get(ENV_DIR, "").strip()
    held, _ = look(Path(d) if d else None, specs)
    return {k for n in held for k in specs[n]}


_TAKEN: list = []                    # a lock taken here is kept by the module, so a caller that drops its handle does not drop the reservation


class Held:
    """A reservation taken by this process; the lock lives as long as the process (or `release()`)."""

    def __init__(self, fh, name: str, path: Path, slot_keys: list[str], sandboxes: int):
        self._fh, self.name, self.path, self.slot_keys, self.sandboxes = fh, name, path, list(slot_keys), sandboxes

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if self in _TAKEN:
            _TAKEN.remove(self)
        if fh is not None:
            fh.close()


def hold(name: str, d: Path) -> Held:
    """Take the reservation of `name`, or raise if another process of that name holds it (two would use twice what is reserved)."""
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path(d, name), "a+")              # O_CLOEXEC: a sandbox or a child process never inherits it
    t0 = time.monotonic()
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() - t0 >= TAKE_RETRY_S:
                fh.close()
                raise ReservationError(f"reservation {name!r} is held by another process ({lock_path(d, name)}): it would use twice what the campaign counts") from None
            time.sleep(0.05)
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(json.dumps({"pid": os.getpid(), "since": time.time(), "name": name}))
        fh.flush()
    except OSError:
        pass                                          # the note is for people; the lock is the truth
    h = Held(fh, name, lock_path(d, name), [], 0)
    _TAKEN.append(h)
    return h


def claim(name: str, launch_config: str | os.PathLike | None, slot_map: Mapping[str, str], *, options: Iterable[str], concurrency: int) -> Held:
    """For a real run of a reserved process: read what the launch JSON says it holds, check it against the slot map and what the run needs, take the lock, and
    limit this process's own slot pool to its keys (`STUDY_VAR_95`, which every lease here reads). Refuses, taking nothing, on any problem: without its keys a
    run would lease from the shared pool, which is the thing this exists to prevent."""
    if not launch_config:
        raise ReservationError(f"{name}: a real run needs --launch-config FILE (the campaign's launch JSON, with process_reservations[{name!r}]): without its own slot keys it would "
                               "lease from the pool the agent runs share")
    try:
        raw = json.loads(Path(launch_config).read_text())
    except (OSError, ValueError) as e:
        raise ReservationError(f"{name}: the launch JSON {launch_config} could not be read ({type(e).__name__}: {e})") from e
    specs = raw.get("process_reservations") or {}
    spec = specs.get(name)
    if spec is None:
        raise ReservationError(f"{name}: the launch JSON has no process_reservations[{name!r}]")
    bad = problems({name: spec}, slot_map, None)
    if bad:
        raise ReservationError("; ".join(bad))
    keys = list(spec["slot_keys"])
    if not keys:
        raise ReservationError(f"{name}: process_reservations[{name!r}].slot_keys is empty: this process would have no slot user")
    d = resolve_dir(raw.get("reservation_dir"))
    if d is None:
        raise ReservationError(f"{name}: no reservation directory: set reservation_dir in the launch JSON (or {ENV_DIR}), the one the campaign reads")
    have = {slot_map[k] for k in keys}
    need = set(options)
    if need - have:
        raise ReservationError(f"{name}: this run needs a slot key of option(s) {sorted(need - have)} and process_reservations[{name!r}] has none")
    if concurrency > spec["sandboxes"]:
        raise ReservationError(f"{name}: --concurrency {concurrency} is more than the {spec['sandboxes']} sandbox(es) the launch JSON reserves for it")
    held = hold(name, d)
    held.slot_keys, held.sandboxes = keys, spec["sandboxes"]
    os.environ["STUDY_VAR_95"] = ",".join(sorted(keys))
    os.environ.pop(ENV_KEYS, None)                    # a leaked copy of the campaign's would hold back this process's own keys
    return held
