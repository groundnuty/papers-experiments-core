# Provenance: new
"""The shape of a tool call's `cell_deadline` (O34, O97): what a code tool says of a cell whose process outlived its
kill at the cell's deadline, and what the record keeps of it.

It is the deadline's own record, cut to what is safe and useful to keep: `outcome` (how the escalation ended), `utc`
(the wall-clock time of the cap), `grace_s` and `margin_s`, `steps` (each step of the escalation with its seconds since
the cap), and, when the backend read them, `survivors` (each process left, by its name and state, its tasks' wait and
what each task was blocked on), `survivors_cut` (how many the reading left out), `connection` (whether the mount's
connection could be aborted without the kernel's own help), `attribution` (whose block the verdict made it, with the
reason) and `restore` (what became of the mount's restore). Nothing in it is file content, a command line, an
environment, a configured path, a node name or an address: the survivors name the process, never its arguments, and a
file only by its space and its path below the space, which the agent's own code already names in the transcript.

This module checks the nested shape (`problem`), which the flat sections of the schema cannot; the schema calls it for
every call that holds the field. Every list is capped, every text is bounded, every value is plain JSON, and the whole
is bounded in size, so a field a tool builds from `/proc` can never make a record that cannot be written or read back.
It reads no module of a platform layer: a verdict and a label are the closed forms the core already knows.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from typing import Any

from expcore.errors import TARGET_LABEL

STEPS_CAP = 32  # the escalation's own steps: the kill, the survivors, the abort, the end, the restore: well under it
SURVIVORS_CAP = 8  # processes of a cell that are kept; `survivors_cut` counts the rest
TASKS_CAP = 4  # non-zombie tasks of a survivor that are kept, and the fds of it that were on a FUSE mount
TEXT_CAP = 300  # characters of any one text: a path below a space is at most 256 of them
SIZE_CAP = 16384  # bytes of the whole, written as JSON
DEPTH_CAP = 8  # a survivor holds tasks that hold a blocking object that holds a list of objects: six deep
REQUIRED = ("outcome", "utc", "grace_s", "margin_s", "steps")
OPTIONAL = ("restore", "survivors", "survivors_cut", "connection", "attribution")
VERDICTS = ("agent", "infrastructure")
ATTRIBUTION_KEYS = ("verdict", "why", "target", "space_holder")
SURVIVOR_KEYS = ("comm", "state", "tasks", "tasks_cut", "waits_on", "cwd_mount")
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,39}")


def _plain(value: Any, where: str, depth: int = 0) -> str | None:
    """The first problem with a value that must be plain JSON, bounded: scalars with finite numbers, texts of at most
    `TEXT_CAP`, lists and objects of text keys, no deeper than `DEPTH_CAP`; None when it is."""
    if depth > DEPTH_CAP:
        return f"{where}: nested deeper than {DEPTH_CAP}"
    if value is None or isinstance(value, bool | int):
        return None
    if isinstance(value, float):
        return None if math.isfinite(value) else f"{where}: a number that is not finite"
    if isinstance(value, str):
        return None if len(value) <= TEXT_CAP else f"{where}: a text of more than {TEXT_CAP} characters"
    if isinstance(value, list):
        return next((p for i, v in enumerate(value) if (p := _plain(v, f"{where}[{i}]", depth + 1))), None)
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return f"{where}: a key that is not text"
            if problem := _plain(item, f"{where}.{key}", depth + 1):
                return problem
        return None
    return f"{where}: {type(value).__name__} is not plain JSON"


def _name(value: Any, where: str) -> str | None:
    return None if isinstance(value, str) and _NAME.fullmatch(value) else f"{where}: a plain name, not {value!r}"


def _number(value: Any, where: str) -> str | None:
    ok = isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
    return None if ok else f"{where}: a number of seconds, not {value!r}"


def _survivor(survivor: Any, where: str) -> str | None:
    if not isinstance(survivor, dict):
        return f"{where}: an object"
    stray = sorted(set(survivor) - set(SURVIVOR_KEYS))
    if stray:
        return f"{where}: unknown field(s) {stray}"
    for key in ("comm", "state", "cwd_mount"):
        if key in survivor and survivor[key] is not None and not isinstance(survivor[key], str):
            return f"{where}.{key}: text or null"
    if "tasks_cut" in survivor and not (isinstance(survivor["tasks_cut"], int) and survivor["tasks_cut"] >= 0):
        return f"{where}.tasks_cut: a count"
    for key in ("tasks", "waits_on"):
        if key in survivor:
            items = survivor[key]
            if not isinstance(items, list) or len(items) > TASKS_CAP or not all(isinstance(i, dict) for i in items):
                return f"{where}.{key}: a list of at most {TASKS_CAP} objects"
    return _plain(survivor, where)


def _attribution(attribution: Any, where: str) -> str | None:
    if not isinstance(attribution, dict):
        return f"{where}: an object"
    stray = sorted(set(attribution) - set(ATTRIBUTION_KEYS))
    if stray:
        return f"{where}: unknown field(s) {stray}"
    if attribution.get("verdict") not in VERDICTS:
        return f"{where}.verdict: one of {VERDICTS}, not {attribution.get('verdict')!r}"
    if "why" not in attribution or _name(attribution["why"], f"{where}.why"):
        return f"{where}.why: the reason, a plain name"
    for key in ("target", "space_holder"):  # a position label such as 'providers[1]', never a name
        value = attribution.get(key)
        if value is not None and not (isinstance(value, str) and TARGET_LABEL.fullmatch(value)):
            return f"{where}.{key}: a position label such as 'providers[1]' or null, never a name"
    return None


def problem(data: Any, where: str = "cell_deadline") -> str | None:
    """The first thing wrong with a call's `cell_deadline`, as a sentence that starts with where it is; None when it is
    as the record keeps it (the module's docstring)."""
    if not isinstance(data, Mapping):
        return f"{where}: an object"
    missing = [k for k in REQUIRED if k not in data]
    if missing:
        return f"{where}: missing field(s) {missing}"
    stray = sorted(set(data) - set(REQUIRED) - set(OPTIONAL))
    if stray:
        return f"{where}: unknown field(s) {stray}"
    for key in ("outcome", "restore"):
        if key in data and (bad := _name(data[key], f"{where}.{key}")):
            return bad
    if not isinstance(data["utc"], str) or len(data["utc"]) > 40:
        return f"{where}.utc: the wall-clock time of the cap, as text"
    for key in ("grace_s", "margin_s"):
        if bad := _number(data[key], f"{where}.{key}"):
            return bad
    steps = data["steps"]
    if not isinstance(steps, list) or len(steps) > STEPS_CAP:
        return f"{where}.steps: a list of at most {STEPS_CAP} steps"
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or _name(step.get("step"), f"{where}.steps[{i}].step"):
            return f"{where}.steps[{i}]: an object with a plain `step` name"
        if bad := _plain(step, f"{where}.steps[{i}]"):
            return bad
    if "survivors" in data:
        survivors = data["survivors"]
        if not isinstance(survivors, list) or len(survivors) > SURVIVORS_CAP:
            return f"{where}.survivors: a list of at most {SURVIVORS_CAP} processes"
        if bad := next((p for i, s in enumerate(survivors) if (p := _survivor(s, f"{where}.survivors[{i}]"))), None):
            return bad
    if "survivors_cut" in data and not (
        isinstance(data["survivors_cut"], int)
        and not isinstance(data["survivors_cut"], bool)
        and data["survivors_cut"] >= 0
    ):
        return f"{where}.survivors_cut: a count"
    if "connection" in data:
        connection = data["connection"]
        if not isinstance(connection, dict) or set(connection) - {"fusectl"}:
            return f"{where}.connection: an object with `fusectl` alone"
        if "fusectl" in connection and _name(connection["fusectl"], f"{where}.connection.fusectl"):
            return f"{where}.connection.fusectl: a plain name"
    if "attribution" in data and (bad := _attribution(data["attribution"], f"{where}.attribution")):
        return bad
    if len(json.dumps(data, sort_keys=True)) > SIZE_CAP:
        return f"{where}: more than {SIZE_CAP} bytes as JSON"
    return None


__all__ = ["OPTIONAL", "REQUIRED", "SIZE_CAP", "STEPS_CAP", "SURVIVORS_CAP", "TASKS_CAP", "TEXT_CAP", "problem"]
