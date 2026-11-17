# Provenance: new
"""A run's window on the federation's services, as the sandbox's launch reads and records it (O86).

`SandboxPlatform(..., federation_watch=watch)` makes every run of the platform carry one window, in the sandbox's
`metadata()["infrastructure_facts"]["federation_window"]`, with nothing for a study's executor to call. `launch` reads
the federation (`FederationWatch.read`) after its refusals and before anything of the run exists, which is before any
injection the run's body makes; and, once the body has ended, reads it again for the window since (`window(start)`),
before it stops the sandbox. A fault injector whose `running` is inside the launch (the documented order, see
`faults.py`) has restored by then, so the run's own outage is in the window and explained by its own journal lines.

**The window is a fact, never a failure.** A read that cannot be made is `unknown`, with a reason that names the
exception's type and never its message, and the run goes on: whatever the watch raises (an `Exception`; a cancellation
is never held up) is recorded and nothing else happens. The study's post-check decides what a verdict means.

**What is recorded** is the watch's window, projected: the verdict, the backend, the window's two times, each service by
its label (`zone`, `providers[i]`) with its status, its state at each end and its restarts (`label` `outage`, `heal` or
`finding`, `explained`, `reason`), the seconds an HPC read waited at each end for Slurm's accounting to show a stopped
step (`accounting_wait_s_used`: `start` and `end`, None on Compose), the findings and the unknowns, and the journal's
reading. Never a service's name, a container or step id, an address, a path or a secret: the watch's own result holds
none (`fedwatch`), and nothing the backend's commands print is copied.

**Where it is written, and when.** Every run of a platform has the key from its preparation on, so a reader never meets
a silent absence: a platform given no watch records `unknown` with the reason "no federation watch configured"
(`unwatched`), and one given a watch records `unknown` "pending" (`pending`) until its launch closes the window.
`collect()` writes the sandbox's `metadata.json` before the injector's restore, so that file holds the placeholder, and
the window is written at the launch's exit as `federation-window.json` beside `metadata.json` in each directory
`collect` wrote its evidence into (`persist`), and in `sandbox.metadata()` (as for `teardown`). A directory that is gone
or cannot be written is logged and skipped, never a failure of the run; a run that was never collected has the window
in its metadata alone. The file is the facts as `metadata()` holds them, plain JSON.

**Whose injection.** `launch` takes the run's owner token (`liveness.owner()`) in its own context when it reads the
window's start, and passes it to `window()`: only an injection that names this attempt explains a restart or holds a
service down. Both executor orders are judged: with the injector's `running` inside the launch the restore is before the
window and the restart reads `restarted`, labelled `outage`; with the injection before the launch and the restore after
it (the pilot's order) the target is down at both ends and reads `held`, explained, every other service `stable`. The
restore of a held service comes after the window, so the facts point at the injector's `restore_verified`
(`restore`): a held service whose restore failed is no pass. Another attempt's injection, and a journal line that
records no owner, explain nothing: a finding.
"""

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Protocol

from expcore.campaign import liveness
from expcore.records.injections import FAULT_NAMESPACE

log = logging.getLogger(__name__)

KEY = "federation_window"  # the key of the run's `infrastructure_facts`
FILE = "federation-window.json"  # the window, beside `metadata.json` in the run's evidence
VERSION = 1
NO_WATCH = "no federation watch configured"
PENDING = f"the window closes after the run's evidence is collected: see {FILE} beside metadata.json"
CANCELLED = "the run was cancelled before its window was read"
INTERRUPTED = "the run was interrupted before its window was read"
RESTORE = {  # where the restore of a held service is recorded: the window closes before it comes
    "instrument": FAULT_NAMESPACE,
    "field": "restore_verified",
    "why": "a held service is restored after the window: it is no pass unless the run's restore was verified",
}
TEXT_CAP = 300  # a reason or a finding is a sentence: a longer one is cut, never copied whole


class Evidence(Protocol):
    """A sandbox as far as `persist` uses it: the directories `collect` wrote into."""

    def evidence_dirs(self) -> tuple[Path, ...]: ...


class Watch(Protocol):
    """A federation watch as far as the launch uses it (`expcore_onedata.fedwatch.FederationWatch`)."""

    def read(self) -> dict[str, Any]: ...

    def window(self, start: Mapping[str, Any] | None, *, owner: Mapping[str, Any] | None = None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Opened:
    """A window's start: the reading, or why there is none (`why` names an exception's type only), and the run's owner
    token as the injector writes it (`liveness.owner()`, taken in the run's own context): the window counts only the
    injections that name this attempt. Held in memory for the window's judge, never recorded."""

    reading: dict[str, Any] | None
    why: str | None
    owner: Mapping[str, Any]


def _text(value: Any) -> str:
    return str(value)[:TEXT_CAP]


def _failed(what: str, error: Exception) -> str:
    return f"{what} ({type(error).__name__})"


def unread(*reasons: str) -> dict[str, Any]:
    """The facts of a window that was never taken: unknown, with why (`reason` is the first), in the shape of a window
    with no service."""
    return {
        "version": VERSION,
        "verdict": "unknown",
        "reason": _text(reasons[0]) if reasons else None,
        "backend": None,
        "window": {"start": None, "end": None},
        "ownership": None,
        "services": {},
        "findings": [],
        "unknown": [_text(reason) for reason in reasons],
        "journal": None,
        "restore": None,
    }


def unwatched() -> dict[str, Any]:
    """What a run of a platform given no watch records, so that a post-check sees the gap and not an absence."""
    return unread(NO_WATCH)


def pending() -> dict[str, Any]:
    """What a run of a platform with a watch records until its window is closed: what `metadata.json` holds."""
    return unread(PENDING)


def persist(sandbox: Evidence, facts: Mapping[str, Any]) -> None:
    """Write the facts as `federation-window.json` beside the `metadata.json` of every directory the sandbox's `collect`
    wrote its evidence into, atomically. Never raises an `Exception`, whatever the file system does (a stale handle on
    a shared one included): it runs before the sandbox's stop, which nothing may skip. A directory that is gone is
    skipped, and one that cannot be written is logged (the error's type) and left without a partial file: the window is
    also in the sandbox's metadata, and the run goes on (O86)."""
    try:
        text, directories = json.dumps(facts, indent=1), sandbox.evidence_dirs()
    except Exception as error:
        log.error("the federation's window could not be written beside the run's evidence (%s)", type(error).__name__)
        return
    for directory in directories:
        temporary = directory / f".{FILE}.tmp"
        try:
            if not directory.is_dir():
                continue
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, directory / FILE)
        except Exception as error:
            log.error(
                "the federation's window could not be written beside the run's evidence (%s)", type(error).__name__
            )
            with contextlib.suppress(Exception):
                temporary.unlink(missing_ok=True)


def interrupted(error: BaseException) -> dict[str, Any]:
    """The facts of a window that a cancellation (or another `BaseException`) cut short: unknown, saying which."""
    return unread(CANCELLED if isinstance(error, asyncio.CancelledError) else INTERRUPTED)


def _waited(side: Mapping[str, Any] | None) -> float | None:
    """The seconds an HPC read waited for Slurm's accounting to show a stopped step, as the side recorded them: None for
    no reading, for Compose and for a reading that predates the wait."""
    value = None if side is None else side.get("accounting_wait_s_used")
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def project(window: Mapping[str, Any]) -> dict[str, Any]:
    """The facts of a watch's window: its verdict and its readings' summaries, and no service's own reading."""
    services = {
        label: {
            "status": _text(seen["status"]),
            "start": None if seen["start"] is None else _text(seen["start"]["state"]),
            "end": _text(seen["end"]["state"]),
            "accounting_wait_s_used": {"start": _waited(seen["start"]), "end": _waited(seen["end"])},
            "restarts": [
                {"label": _text(r["label"]), "explained": r["explained"] is True, "reason": _text(r["reason"])}
                for r in seen["restarts"]
            ],
            "held": None if seen["held"] is None else {k: seen["held"][k] for k in ("injections", "opened", "reason")},
        }
        for label, seen in window["services"].items()
    }
    held = any(seen["held"] is not None for seen in services.values())
    journal = window["journal"]
    return {
        "version": VERSION,
        "verdict": _text(window["verdict"]),
        "backend": _text(window["backend"]),
        "window": {key: window["window"][key] for key in ("start", "end")},
        "ownership": _text(window["ownership"]),
        "services": services,
        "findings": [_text(text) for text in window["findings"]],
        "unknown": [_text(text) for text in window["unknown"]],
        "journal": {
            "declared": journal["declared"] is True,
            "lines_start": journal["lines_start"],
            "lines_end": journal["lines_end"],
            "read": True if journal["read"] is True else _text(journal["read"]),
        },
        "restore": dict(RESTORE) if held else None,
    }


async def open_window(watch: Watch) -> Opened:
    """Read the federation, off the event loop (a read may run commands and wait on the platform's client). A read that
    raises is no start, with its reason."""
    owner = liveness.owner()  # in the run's own context: a worker thread would carry it too, but this reads it once
    try:
        return Opened(await asyncio.to_thread(watch.read), None, owner)
    except Exception as error:
        return Opened(None, _failed("the window's start could not be read", error), owner)


async def close_window(watch: Watch, opened: Opened) -> dict[str, Any]:
    """The window since `opened`, as facts. Never raises an `Exception`: what cannot be read is unknown with its
    reason, and a window whose start was not read is never stable, whatever its end shows."""
    try:
        facts = project(await asyncio.to_thread(partial(watch.window, opened.reading, owner=opened.owner)))
    except Exception as error:
        return unread(*([opened.why] if opened.why else []), _failed("the window could not be read", error))
    if opened.why is not None:
        facts["unknown"] = [opened.why, *facts["unknown"]]
        if facts["verdict"] in ("stable", "restarted"):
            facts["verdict"] = "unknown"
    return facts


__all__ = [
    "CANCELLED",
    "FILE",
    "INTERRUPTED",
    "KEY",
    "NO_WATCH",
    "PENDING",
    "VERSION",
    "Evidence",
    "Opened",
    "Watch",
    "close_window",
    "interrupted",
    "open_window",
    "pending",
    "persist",
    "project",
    "unread",
    "unwatched",
]
