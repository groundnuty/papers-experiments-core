# Provenance: new
"""The plan of a progressive ingest: which files of a dataset arrive during a run, when, and from where (O27).

A study declares the plan in a TOML file of its own, one per condition that uses it, and loads it against its
platform.toml with `load_ingest_plan`:

```toml
[ingest]
space = "<label>"                # the [[spaces]] entry the files lie in; optional with one space
staging = "staged"               # a local directory, relative to this file, holding every source
visible_on = ["<provider>"]      # optional: the providers that must see each arrival; by default every provider of
                                 # the space

[[ingest.files]]
label = "day-13"                 # how records and messages name the file: never its path
path = "records/day-13.csv"      # where the agent finds it, relative to the space root
source = "day-13.csv"            # its content, relative to `staging`: hashed at load, never changed
at_s = 30                        # seconds after the run's start when it begins to arrive
baseline = "complete"            # what the dataset holds between runs: "complete" (the file, whole) or "absent"
parts = 4                        # optional: written in this many parts, so it is partial for a while (1: whole)
over_s = 60                      # with parts > 1: the last part is written this many seconds after the first
```

`baseline` is required, since it decides what the reset keeps: a "complete" file is part of the dataset that every
other condition reads, so the instrument removes it before the run and restores it after; an "absent" file is new
data, removed again after the run. Either way its staged source is the copy the study keeps.

A file of `parts` parts has its k-th part written at `at_s + over_s * (k - 1) / (parts - 1)`, ending at byte
`ceil(size * k / parts)`, so every part writes at least one byte. `IngestPlan.steps` lists the parts of every file in
the order they fall due, each with the SHA-256 of the prefix it leaves, read from the staged source at load.

Every refusal is a `ConfigError` naming the plan's file and the key, never a value. The plan is part of the condition
and of the campaign fingerprint (`fingerprint`): a resume after its schedule or a staged source changed is refused.
"""

import hashlib
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError, Section, load_toml
from expcore_onedata.config import PlatformConfig
from expcore_onedata.rangemap import safe_path
from expcore_onedata.spaces import LABEL

BASELINES = ("complete", "absent")
FILE_KEYS = ("label", "path", "source", "at_s", "baseline", "parts", "over_s")
_READ_BYTES = 1 << 20  # how much of a staged source is held at once while it is hashed


@dataclass(frozen=True)
class Step:
    """One write of the plan: a part of a file, when it falls due and the prefix it leaves (`stop` bytes, `sha256`)."""

    label: str
    part: int
    parts: int
    due_s: float
    stop: int
    sha256: str


@dataclass(frozen=True)
class PlannedFile:
    """One file of the plan. `path` and `source` are configuration and never reach a record: the label does."""

    label: str
    path: str
    source: Path
    size: int
    sha256: str
    at_s: float
    parts: int
    over_s: float
    baseline: str
    prefixes: tuple[tuple[int, str], ...]  # each part's end and the SHA-256 of the bytes before it

    def steps(self) -> tuple[Step, ...]:
        """The file's parts, each with its due time, its end and its prefix's hash."""
        last = max(self.parts - 1, 1)
        return tuple(
            Step(self.label, k, self.parts, self.at_s + self.over_s * (k - 1) / last, stop, digest)
            for k, (stop, digest) in enumerate(self.prefixes, start=1)
        )


@dataclass(frozen=True)
class IngestPlan:
    """A loaded plan: its space (by label), its files in the order declared, and the providers that must see each
    arrival (`None`: every provider of the space)."""

    space: str
    files: tuple[PlannedFile, ...]
    visible_on: tuple[str, ...] | None
    sha256: str

    def steps(self) -> tuple[Step, ...]:
        """Every part of every file, in the order they fall due (a tie goes by the files' order, then the part)."""
        order = {f.label: n for n, f in enumerate(self.files)}
        every = [step for planned in self.files for step in planned.steps()]
        return tuple(sorted(every, key=lambda s: (s.due_s, order[s.label], s.part)))

    def file(self, label: str) -> PlannedFile:
        """The planned file with this label."""
        return next(f for f in self.files if f.label == label)

    def summary(self) -> dict[str, Any]:
        """What a record keeps of the plan: its hash and each file by its label, never a path or a source."""
        return {
            "sha256": self.sha256,
            "files": [
                {
                    "label": f.label,
                    "bytes": f.size,
                    "sha256": f.sha256,
                    "at_s": f.at_s,
                    "parts": f.parts,
                    "over_s": f.over_s,
                    "baseline": f.baseline,
                }
                for f in self.files
            ],
        }


def fingerprint(plans: Mapping[str, IngestPlan]) -> dict[str, Any]:
    """The plans' part of the campaign fingerprint, by the name each is declared under (a condition): the plan's hash,
    which covers its space, its providers, and each file's path, source content, schedule and baseline. Add it to the
    platform half: `StudyPlugin(platform={**config.fingerprint(), **fingerprint(plans)})`."""
    return {"progressive_ingest": {str(name): plan.sha256 for name, plan in plans.items()}}


def _number(section: Section, key: str, default: float | None) -> float:
    value = section.data.get(key, default)
    if value is None:
        raise section.fail(key, "is required")
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise section.fail(key, "must be a number of seconds, zero or more")
    return float(value)


def _hash_prefixes(source: Path, stops: list[int]) -> tuple[str, list[str]]:
    """Read a staged source once, a block at a time, and return its SHA-256 and that of the prefix before each stop."""
    hasher, prefixes, offset = hashlib.sha256(), [], 0
    pending = list(stops)
    with open(source, "rb") as handle:
        while True:
            want = _READ_BYTES if not pending else min(_READ_BYTES, max(pending[0] - offset, 1))
            block = handle.read(want)
            while pending and pending[0] == offset:
                prefixes.append(hasher.copy().hexdigest())
                pending.pop(0)
            if not block:
                break
            hasher.update(block)
            offset += len(block)
    while pending and pending[0] == offset:
        prefixes.append(hasher.copy().hexdigest())
        pending.pop(0)
    return hasher.hexdigest(), prefixes


def _source(section: Section, staging: Path) -> Path:
    raw = section.string("source")
    found = (staging / raw).resolve()
    if not found.is_relative_to(staging) or not found.is_file():
        raise section.fail("source", "must name a regular file inside the staging directory")
    return found


def _planned(section: Section, staging: Path) -> PlannedFile:
    section.reject_unknown(FILE_KEYS)
    label = section.string("label")
    if not LABEL.fullmatch(label):
        raise section.fail("label", "must be letters, digits, '_' and '-', starting with a letter")
    path = section.string("path")
    try:
        safe_path(path)
    except ValueError as error:
        raise section.fail("path", str(error)) from None
    baseline = section.string("baseline")
    if baseline not in BASELINES:
        raise section.fail("baseline", f"must be one of {', '.join(BASELINES)}")
    source = _source(section, staging)
    size = source.stat().st_size
    parts = section.optional_integer("parts")
    parts = 1 if parts is None else parts
    if parts < 1 or parts > max(size, 1):
        raise section.fail("parts", "must be at least 1 and at most the source's size in bytes")
    over_s = _number(section, "over_s", 0.0)
    if (parts > 1) != (over_s > 0):
        raise section.fail("over_s", "must be more than 0 with parts > 1, and absent or 0 with one part")
    stops = [-(-size * k // parts) for k in range(1, parts + 1)]
    try:
        whole, prefixes = _hash_prefixes(source, stops)
    except OSError:
        raise section.fail("source", "could not be read") from None
    if len(prefixes) != parts:
        raise section.fail("source", "changed size while it was read")
    at_s = _number(section, "at_s", None)
    return PlannedFile(
        label, path, source, size, whole, at_s, parts, over_s, baseline, tuple(zip(stops, prefixes, strict=True))
    )


def _check_paths(files: list[PlannedFile], file: Path) -> None:
    """Refuse a label or a path given twice, and a path that lies under another planned path."""
    labels: set[str] = set()
    paths: dict[tuple[str, ...], int] = {}
    for number, planned in enumerate(files, start=1):
        where = Section({}, file, f"[[ingest.files]] row {number}")
        if planned.label in labels:
            raise where.fail("label", "names a file another row already names")
        labels.add(planned.label)
        parts = safe_path(planned.path)
        for other in paths:
            if parts[: len(other)] == other or other[: len(parts)] == parts:
                raise where.fail("path", "is, or lies under or above, the path of another row")
        paths[parts] = number


def _visible_on(table: Section, config: PlatformConfig, support: tuple[str, ...]) -> tuple[str, ...] | None:
    if "visible_on" not in table.data:
        return None
    names = table.strings("visible_on")
    if not names or len(set(names)) != len(names) or any(n not in support for n in names):
        raise table.fail("visible_on", "must name providers that support the plan's space, each once")
    return names


def load_ingest_plan(path: str | os.PathLike[str], config: PlatformConfig) -> IngestPlan:
    """Read a plan file against the loaded platform configuration; raise ConfigError naming the file and the key."""
    file = Path(path).resolve()
    root = Section(load_toml(file), file, "the ingest plan")
    root.reject_unknown(("ingest",))
    table = root.section("ingest")
    table.reject_unknown(("space", "staging", "visible_on", "files"))
    label = table.optional_string("space")
    try:
        space = config.space_named(label)
    except ConfigError:
        raise table.fail("space", "must be the label of a declared space; with several spaces it is required") from None
    staging = (file.parent / table.string("staging")).resolve()
    if not staging.is_dir():
        raise table.fail("staging", "must name a directory")
    visible_on = _visible_on(table, config, tuple(space.support.providers))
    rows = table.data.get("files", [])
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise table.fail("files", "must be an array of tables, [[ingest.files]]")
    files = [_planned(Section(row, file, f"[[ingest.files]] row {n}"), staging) for n, row in enumerate(rows, 1)]
    _check_paths(files, file)
    content = {
        "space": space.label,
        "visible_on": None if visible_on is None else list(visible_on),
        "files": [
            {k: getattr(f, k) for k in ("label", "path", "size", "sha256", "at_s", "parts", "over_s", "baseline")}
            for f in files
        ],
    }
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return IngestPlan(space.label, tuple(files), visible_on, digest)


__all__ = ["BASELINES", "IngestPlan", "PlannedFile", "Step", "fingerprint", "load_ingest_plan"]
