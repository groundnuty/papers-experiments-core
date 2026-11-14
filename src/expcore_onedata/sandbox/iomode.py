# Provenance: new
"""The mount's declared and effective I/O mode, per run, in the run's record (O48).

A mount's I/O mode is declared, `proxy` or `direct`, and never left to oneclient's own detection (O36). A study that
compares the two modes has to show, from each run's record, that every mount run ran in the mode it declares. The
declared mode is the sandbox's own setting; the effective one is what oneclient itself logs, read from its INFO log:

- the **start lines** (`logging.cc`) `Forced direct IO` and `Forced proxy IO`, each `0` or `1`: `1` and `0` is forced
  direct, `0` and `1` forced proxy, `0` and `0` is no forced mode, so oneclient's own detection (`auto`, the mode #227
  forbids), and `1` and `1` contradicts itself;
- the **lines of a read that did not follow the forced mode** (the names of `directio.MARKERS`, which `directio` reads
  at a direct mount's start and collection): after a forced direct start, oneclient's own detection, a direct read
  refused for permissions and made again through the provider, a switch to the provider's helper, a storage that is not
  direct on the provider, or a storage it could not reach itself. After a forced proxy start, only a detection that did
  not itself force proxy (`forced proxy io mode: 0`); a detection that forces it (`1`) is the declared mode.

`effective` is `proxy` or `direct` when the start lines force it and no such line follows, `auto` when none is forced,
`mixed` when the log contradicts itself (both forced, a start line logged again with another value, or a forced mode
followed by a line of a read that did not follow it), and `unknown` when the log could not be read or a start line is
missing or, anywhere in the log, is not `0` or `1`. Every logged occurrence of a start line counts, not the first alone.
**`agree` is true only when the declared mode and the effective one are the same word, `proxy` or `direct`: unknown
is never agreement.**

The sandbox reads the log once, when it collects (`DockerSandbox.mount_io_mode`, before the direct mount's own check
that may refuse the collection, so the reading is kept with that evidence), and keeps the reading in its metadata
(`mount.effective`) and for `MountIoMode`, the platform's fourth `Instrument` (namespace `mount_io_mode`), which an
executor lists beside the others and finishes after the collection. Its snapshot:

  {"version": 1, "mount": true, "declared": "direct", "effective": "direct", "agree": true,
   "forced_direct": "1", "forced_proxy": "0", "lines": [], "problems": []}

`forced_direct` and `forced_proxy` are the start lines' values, `0` or `1`, or None when not logged as one;
`lines` the names of the lines found that contradict the forced mode; `problems` the reasons, in words that name no id,
path or host, first the disagreement itself with both modes. Nothing of the log is kept. A run with no mount (a REST
run, a run with no data path) has no mode to confirm: the snapshot is `{"version": 1, "mount": false}`, so a study may
list the instrument for every run.

**Policy: a run whose modes disagree, or whose log cannot say, is refused.** `MountIoMode.check` raises `InfraError` of
the kind `io_mode_integrity`, which no condition can declare as its treatment (`sandbox.kinds`), whenever a mount run's
snapshot is not agreement, and when the snapshot was never taken. `finish_instruments` puts the refusal in the record
(`instrument_refusal`, naming both modes in its detail) and the scheduler sets the attempt aside as charged
infrastructure and runs it again: such a run is never scored as the declared mode, and keeps its record and its
snapshot. A study that wants the disagreement in the record without the refusal builds `MountIoMode(sandbox,
refuse=False)`; its scorer then confirms a run's mode with `agrees(snapshot)`, which recomputes the answer from the two
modes and reads anything that is not a matching mount snapshot as no.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any, Protocol

from expcore.errors import InfraError
from expcore_onedata.sandbox import directio
from expcore_onedata.sandbox import oneclient_start as start
from expcore_onedata.sandbox.kinds import IO_MODE_INTEGRITY
from expcore_onedata.sandbox.oneclient_args import DIRECT, IO_MODES
from expcore_onedata.sandbox.oneclient_log import DETECT_START

NAMESPACE = "mount_io_mode"
VERSION = 1
PROXY = "proxy"
AUTO, MIXED, UNKNOWN = "auto", "mixed", "unknown"  # the effective modes that are not a declared one
FLAGS = ("0", "1")  # what a start line of a force option says
UNREAD = "the sidecar's oneclient log could not be read"
NOT_COLLECTED = "the sandbox has not collected: oneclient's log is read by its collection"
NEVER_READ = "the mount's I/O mode was never read, so it is not shown to be the declared one"


def _logged(log_text: str, name: str) -> list[str]:
    """Every value the log's start lines give an option, in the order logged: not the first alone, which is all that
    `oneclient_start.start_settings` keeps, so a later line that says otherwise is never ignored."""
    return [
        parsed.group(2).strip()
        for line in log_text.splitlines()
        if (parsed := start.START_LINE.match(line)) and parsed.group(1) == name
    ]


def _contradicting(forced: str, log_text: str) -> list[str]:
    """The names of the lines of the log that show a read that did not follow the forced mode (module docstring)."""
    if forced == DIRECT:
        return directio.marker_lines(log_text)
    return ["automatic_detection"] if any(m.group(2) == "0" for m in DETECT_START.finditer(log_text)) else []


def _effective(log_text: str | None, why_unread: str) -> dict[str, Any]:
    """What the log shows: `effective`, the two start values, the lines that contradict the forced mode, and the
    reasons when it is not one of the two modes."""
    out: dict[str, Any] = {
        "effective": UNKNOWN,
        "forced_direct": None,
        "forced_proxy": None,
        "lines": [],
        "problems": [],
    }
    if log_text is None:
        out["problems"].append(why_unread)
        return out
    values: dict[str, set[str]] = {}
    for name in (start.FORCED_DIRECT, start.FORCED_PROXY):
        logged = _logged(log_text, name)
        if not logged:
            out["problems"].append(f"{name}: not logged")
        elif any(value not in FLAGS for value in logged):  # never the value itself: it is the log's text
            out["problems"].append(f"{name}: logged a value that is not '0' or '1'")
        else:
            values[name] = set(logged)
    if out["problems"]:
        return out
    again = [name for name, logged in values.items() if len(logged) > 1]
    if again:  # a later line says another mode than an earlier one: the log contradicts itself
        out["effective"] = MIXED
        out["problems"] += [f"{name}: logged more than once, with different values" for name in again]
        return out
    (direct,), (proxy,) = values[start.FORCED_DIRECT], values[start.FORCED_PROXY]
    out["forced_direct"], out["forced_proxy"] = direct, proxy
    if (direct, proxy) == ("0", "0"):
        out["effective"], out["lines"] = AUTO, directio.marker_lines(log_text)
        out["problems"].append("neither force option is set: oneclient's own detection decides each storage's mode")
    elif (direct, proxy) == ("1", "1"):
        out["effective"] = MIXED
        out["problems"].append("both force options are logged as set: the start lines contradict each other")
    else:
        forced = DIRECT if direct == "1" else PROXY
        out["lines"] = _contradicting(forced, log_text)
        out["effective"] = MIXED if out["lines"] else forced
        out["problems"] += [f"oneclient logged a line of {name}" for name in out["lines"]]
    return out


def judge(declared: object, log_text: str | None, *, why_unread: str = UNREAD) -> dict[str, Any]:
    """The reading of a mount run: `declared`, `effective`, `agree`, `forced_direct`, `forced_proxy`, `lines` and
    `problems` (module docstring). `log_text` is oneclient's INFO log, or None when it could not be read, which
    `why_unread` then explains. The first problem names both modes whenever they are not the same."""
    seen = _effective(log_text, why_unread)
    problems = list(seen["problems"])
    if declared not in IO_MODES:
        problems.insert(0, "the declared mode is neither 'proxy' nor 'direct'")
    elif seen["effective"] != declared:
        problems.insert(0, f"declared {declared!r}, oneclient's start lines show {seen['effective']!r}")
    agree = declared in IO_MODES and seen["effective"] == declared
    return {
        "declared": declared if declared in IO_MODES else None,
        "effective": seen["effective"],
        "agree": agree,
        "forced_direct": seen["forced_direct"],
        "forced_proxy": seen["forced_proxy"],
        "lines": seen["lines"],
        "problems": [] if agree else problems,
    }


def agrees(snapshot: object) -> bool:
    """Whether a snapshot of this instrument confirms the mount's declared mode: a mount snapshot of version 1 whose
    declared mode is `proxy` or `direct` and whose effective mode is the same word. The `agree` flag is read too but
    never alone, so a record whose flag and modes differ is not agreement. A run with no mount, an unknown or mixed
    reading and anything that is not such a snapshot is not agreement: there is nothing it confirms."""
    return (
        isinstance(snapshot, Mapping)
        and snapshot.get("version") == VERSION
        and snapshot.get("mount") is True
        and snapshot.get("agree") is True
        and snapshot.get("declared") in IO_MODES
        and snapshot.get("effective") == snapshot.get("declared")
    )


class ReadsIoMode(Protocol):
    """A sandbox that reads its mount's I/O mode (`DockerSandbox.mount_io_mode`)."""

    def mount_io_mode(self) -> dict[str, Any]: ...


class MountIoMode:
    """The `Instrument` of a run's mount I/O mode: one per run, holding its sandbox, finished after `sandbox.collect`.

    With `refuse` (the default) its `check` refuses a mount run whose snapshot is not agreement, as the module
    docstring says; with `refuse=False` it only records.
    """

    namespace = NAMESPACE

    def __init__(self, sandbox: ReadsIoMode, *, refuse: bool = True) -> None:
        self._sandbox = sandbox
        self._refuse = refuse
        self._snapshot: dict[str, Any] | None = None

    def start(self, identity: Mapping[str, Any]) -> None:
        """Nothing to read before the run: oneclient's log is read by the sandbox's collection."""

    def finish(self) -> dict[str, Any]:
        """The sandbox's reading of the mount's mode, with the snapshot's version."""
        self._snapshot = {"version": VERSION, **self._sandbox.mount_io_mode()}
        return copy.deepcopy(self._snapshot)  # the record's copy: a later change of one never changes the other

    def check(self) -> None:
        """Return when the run has no mount, or its mode was shown to be the declared one; otherwise raise `InfraError`
        of the kind `io_mode_integrity` naming both modes. A `finish` that never ran or failed refuses too: unknown is
        not a pass. Without `refuse` it returns whatever the snapshot says."""
        if not self._refuse:
            return
        snapshot = self._snapshot
        if snapshot is None:
            raise InfraError(IO_MODE_INTEGRITY, NEVER_READ)
        if snapshot.get("mount") is False or agrees(snapshot):
            return
        problems = snapshot.get("problems") or [
            f"declared {snapshot.get('declared')!r}, oneclient's start lines show {snapshot.get('effective')!r}"
        ]
        detail = "the mount's I/O mode is not shown to be the declared one: " + "; ".join(problems)
        raise InfraError(IO_MODE_INTEGRITY, detail)


__all__ = [
    "AUTO",
    "MIXED",
    "NAMESPACE",
    "NOT_COLLECTED",
    "UNKNOWN",
    "VERSION",
    "MountIoMode",
    "ReadsIoMode",
    "agrees",
    "judge",
]
