"""A fake `/proc` and `/sys/fs/cgroup` tree of a cgroup v1 node, built from a real reading (O56).

`data/sandbox/cgroup_v1_step.json` is one Slurm step on a node with cgroup v1 only, as a process of the step saw it:
its `/proc/self/cgroup`, and the memory controller's files at the process's own cgroup (`OWN`, no limit), at its step
(limited) and at its job (limited), before a throwaway child of the step went over the step's limit and after the
kernel killed it. The numbers in the files are the node's; the job, user and step numbers in the paths are placeholders.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from expcore_onedata.sandbox.apptainer import node as N

READING = json.loads(
    (Path(__file__).resolve().parents[1] / "data" / "sandbox" / "cgroup_v1_step.json").read_text(encoding="utf-8")
)
OWN = "/slurm/uid_UID/job_JOB/step_STEP/task_0"  # the memory cgroup of the step's process: no limit of its own
STEP = "/slurm/uid_UID/job_JOB/step_STEP"  # the nearest level with a limit: one level above `OWN`
JOB = "/slurm/uid_UID/job_JOB"
NO_LIMIT = "9223372036854771712\n"  # what v1 shows for a cgroup with no limit


def level(path: str) -> dict:
    """The reading's level at `path`."""
    return next(each for each in READING["levels"] if each["path"] == path)


def build(
    root: Path,
    *,
    proc_cgroup: str | None = None,
    after: Mapping[str, bool] | None = None,
    files: Mapping[str, Mapping[str, str | None]] | None = None,
) -> tuple[Path, Path]:
    """Write the reading under `root`: `<root>/proc/self/cgroup` and `<root>/cgroup/memory/<path>/<file>` for each level
    of the reading. `after`: levels (by path) shown as after the OOM kill; `files`: per level, files to replace (text)
    or leave out (None). Returns the fake `/proc` and the fake `/sys/fs/cgroup`."""
    proc, sys_cgroup = root / "proc", root / "cgroup"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "cgroup").write_text(READING["proc_self_cgroup"] if proc_cgroup is None else proc_cgroup)
    for each in READING["levels"]:
        shown = dict(each["files"])
        if (after or {}).get(each["path"]):
            shown.update(each["after_oom"])
        shown.update((files or {}).get(each["path"], {}))
        directory = sys_cgroup / "memory" / each["path"].lstrip("/")
        directory.mkdir(parents=True, exist_ok=True)
        for name, text in shown.items():
            if text is not None:
                (directory / name).write_text(text)
    return proc, sys_cgroup


def v1_host(root: Path, monkeypatch: pytest.MonkeyPatch, **kwargs) -> Path:
    """Build the tree and point the node's `/proc` and cgroup root at it; returns the fake `/sys/fs/cgroup`."""
    proc, sys_cgroup = build(root, **kwargs)
    monkeypatch.setattr(N, "PROC", proc)
    monkeypatch.setattr(N, "CGROUP_ROOT", sys_cgroup)
    return sys_cgroup
