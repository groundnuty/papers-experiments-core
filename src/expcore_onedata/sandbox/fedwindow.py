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
`finding`, `explained`, `reason`), the findings and the unknowns, and the journal's reading. Never a service's name, a
container or step id, an address, a path or a secret: the watch's own result holds none (`fedwatch`), and nothing the
backend's commands print is copied.

**Where it is written, and when.** `collect()` writes the sandbox's `metadata.json` before the injector's restore, so
that file holds no window; read `sandbox.metadata()` once the launch has ended (as for `teardown`). A platform given no
watch records nothing, and a Docker sandbox then has no `infrastructure_facts` at all.

**The limit.** The window is the run's only when the injection comes after the read and the restore before the window:
an executor whose `running` encloses the launch takes its target down before the read and restores after the window,
and its window shows the target not running at its end, never stable.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

KEY = "federation_window"  # the key of the run's `infrastructure_facts`
VERSION = 1
CANCELLED = "the run was cancelled before its window was read"
INTERRUPTED = "the run was interrupted before its window was read"
TEXT_CAP = 300  # a reason or a finding is a sentence: a longer one is cut, never copied whole


class Watch(Protocol):
    """A federation watch as far as the launch uses it (`expcore_onedata.fedwatch.FederationWatch`)."""

    def read(self) -> dict[str, Any]: ...

    def window(self, start: Mapping[str, Any] | None) -> dict[str, Any]: ...


@dataclass(frozen=True)
class Opened:
    """A window's start: the reading, or why there is none (`why` names an exception's type only)."""

    reading: dict[str, Any] | None
    why: str | None


def _text(value: Any) -> str:
    return str(value)[:TEXT_CAP]


def _failed(what: str, error: Exception) -> str:
    return f"{what} ({type(error).__name__})"


def unread(*reasons: str) -> dict[str, Any]:
    """The facts of a window that was never taken: unknown, with why, in the shape of a window with no service."""
    return {
        "version": VERSION,
        "verdict": "unknown",
        "backend": None,
        "window": {"start": None, "end": None},
        "services": {},
        "findings": [],
        "unknown": [_text(reason) for reason in reasons],
        "journal": None,
    }


def interrupted(error: BaseException) -> dict[str, Any]:
    """The facts of a window that a cancellation (or another `BaseException`) cut short: unknown, saying which."""
    return unread(CANCELLED if isinstance(error, asyncio.CancelledError) else INTERRUPTED)


def project(window: Mapping[str, Any]) -> dict[str, Any]:
    """The facts of a watch's window: its verdict and its readings' summaries, and no service's own reading."""
    services = {
        label: {
            "status": _text(seen["status"]),
            "start": None if seen["start"] is None else _text(seen["start"]["state"]),
            "end": _text(seen["end"]["state"]),
            "restarts": [
                {"label": _text(r["label"]), "explained": r["explained"] is True, "reason": _text(r["reason"])}
                for r in seen["restarts"]
            ],
        }
        for label, seen in window["services"].items()
    }
    journal = window["journal"]
    return {
        "version": VERSION,
        "verdict": _text(window["verdict"]),
        "backend": _text(window["backend"]),
        "window": {key: window["window"][key] for key in ("start", "end")},
        "services": services,
        "findings": [_text(text) for text in window["findings"]],
        "unknown": [_text(text) for text in window["unknown"]],
        "journal": {
            "declared": journal["declared"] is True,
            "lines_start": journal["lines_start"],
            "lines_end": journal["lines_end"],
            "read": True if journal["read"] is True else _text(journal["read"]),
        },
    }


async def open_window(watch: Watch) -> Opened:
    """Read the federation, off the event loop (a read may run commands and wait on the platform's client). A read that
    raises is no start, with its reason."""
    try:
        return Opened(await asyncio.to_thread(watch.read), None)
    except Exception as error:
        return Opened(None, _failed("the window's start could not be read", error))


async def close_window(watch: Watch, opened: Opened) -> dict[str, Any]:
    """The window since `opened`, as facts. Never raises an `Exception`: what cannot be read is unknown with its
    reason, and a window whose start was not read is never stable, whatever its end shows."""
    try:
        facts = project(await asyncio.to_thread(watch.window, opened.reading))
    except Exception as error:
        return unread(*([opened.why] if opened.why else []), _failed("the window could not be read", error))
    if opened.why is not None:
        facts["unknown"] = [opened.why, *facts["unknown"]]
        if facts["verdict"] in ("stable", "restarted"):
            facts["verdict"] = "unknown"
    return facts


__all__ = [
    "CANCELLED",
    "INTERRUPTED",
    "KEY",
    "VERSION",
    "Opened",
    "Watch",
    "close_window",
    "interrupted",
    "open_window",
    "project",
    "unread",
]
