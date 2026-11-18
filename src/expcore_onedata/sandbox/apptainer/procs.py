# Provenance: new
"""What `/proc` says of a process, parsed: pure functions over the files' text, so they are tested without a node (O56).

On an HPC node `/proc` is mounted `hidepid=invisible`: a process of another uid has no directory at all, and neither has
a setuid program's process of our own (Apptainer's starter), whose credentials are root's while it runs. A cell's own
processes are ours and visible: the starter execs them with our uid. Each `apptainer exec --containall` gives its
processes a PID namespace of their own, which `/proc/<pid>/ns/pid` names (`pid:[<inode>]`); that is how the harness
finds a cell's processes, whatever they call themselves and whatever environment they cleared.

A process of our own uid that makes itself non-dumpable (`prctl(PR_SET_DUMPABLE, 0)`) is hidden by `hidepid=invisible`
too, and its memory with it. Two outside readings still show it: the cgroup its memory is charged to counts it
(`memory.stat`, `stat_value`), and its visible parent's `children` files list its pid while `/proc` has no entry for
it (`hidden_children`).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass

NAMESPACE = re.compile(r"pid:\[[0-9]{1,20}\]")
PAGE_KIB = 1024


@dataclass(frozen=True)
class Proc:
    """One process of ours, as the harness needs it.

    `start` is its start time in clock ticks after boot (field 22 of `stat`), which with the pid names it uniquely;
    `pidns` its PID namespace (`pid:[<inode>]`), None when the link could not be read; `rss` its resident anonymous and
    shared-memory bytes (`RssAnon` + `RssShmem`), None when `status` could not be read; `tags` which of the strings the
    caller asked for its environment or its command line holds; `anon` its `RssAnon` alone (what a cgroup's `anon`
    counts of it), None when there is none (a zombie); `cgroup` its memory cgroup's path (v2, or v1's memory
    controller's), None when it could not be read; `threads` its tasks (`Threads:` of `status`, which Docker's pids.max
    counts), None when the line is not a number."""

    pid: int
    start: str
    ppid: int
    state: str
    comm: str
    pidns: str | None
    rss: int | None
    tags: frozenset[str] = frozenset()
    anon: int | None = None
    cgroup: str | None = None
    threads: int | None = None

    def brief(self) -> dict[str, object]:
        """The process as a deadline's or a quiesce's record keeps it: pid, start, comm and state."""
        return {"pid": self.pid, "start": self.start, "comm": self.comm, "state": self.state}


def parse_stat(text: str) -> tuple[str, str, int, str] | None:
    """`(comm, state, ppid, start)` from `/proc/<pid>/stat`, or None when it is not whole. The command name sits in
    parentheses and may hold spaces and parentheses itself, so the fields after it are read from the last `)`."""
    open_at, close_at = text.find("("), text.rfind(")")
    if open_at < 0 or close_at < open_at:
        return None
    fields = text[close_at + 2 :].split()
    if len(fields) < 20 or not fields[1].lstrip("-").isdigit() or not fields[19].isdigit():
        return None
    return text[open_at + 1 : close_at], fields[0], int(fields[1]), fields[19]


def parse_rss(text: str) -> int | None:
    """`RssAnon` + `RssShmem` of `/proc/<pid>/status`, in bytes; None when either line is missing or not a number of
    kB. File-backed pages (`RssFile`) are left out: the image's own files are shared and reclaimable."""
    found: dict[str, int] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key in ("RssAnon", "RssShmem"):
            parts = value.split()
            if len(parts) != 2 or parts[1] != "kB" or not parts[0].isdigit():
                return None
            found[key] = int(parts[0]) * PAGE_KIB
    if set(found) != {"RssAnon", "RssShmem"}:
        return None
    return found["RssAnon"] + found["RssShmem"]


def parse_anon(text: str) -> int | None:
    """`RssAnon` of `/proc/<pid>/status` in bytes; None when the line is missing (a zombie has no memory) or not a
    number of kB."""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key == "RssAnon":
            parts = value.split()
            return int(parts[0]) * PAGE_KIB if len(parts) == 2 and parts[1] == "kB" and parts[0].isdigit() else None
    return None


def parse_threads(text: str) -> int | None:
    """`Threads:` of `/proc/<pid>/status`, the process's tasks; None when the line is missing or not a whole number
    (unknown, never 0)."""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key == "Threads":
            word = value.strip()
            return int(word) if word.isdigit() else None
    return None


MAX_CORE = 65535  # a cpu list names no core above this: a longer range is no affinity, and is not expanded


def parse_cpu_list(text: str) -> frozenset[int] | None:
    """A list of cores and ranges (`0-3,8`, as `Cpus_allowed_list` and `taskset -c` write them) as a set; None when it
    is not one, the empty list included: an affinity nothing can be read from is unknown, and the empty set would pass
    for a subset of any pin."""
    cores: set[int] = set()
    for part in text.strip().split(","):
        low, dash, high = part.partition("-")
        if not low.isdigit() or (dash and not high.isdigit()):
            return None
        first, last = int(low), int(high) if dash else int(low)
        if first > last or last > MAX_CORE:
            return None
        cores.update(range(first, last + 1))
    return frozenset(cores)


def parse_cpus_allowed(text: str) -> frozenset[int] | None:
    """`Cpus_allowed_list` of a task's `status`, as a set; None when the line is missing or not a list."""
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key == "Cpus_allowed_list":
            return parse_cpu_list(value)
    return None


def stat_value(text: str, key: str) -> int | None:
    """The value of `key` in a `memory.stat` or a v1 `memory.oom_control` (lines `<key> <number>`); None when it is
    absent or no number."""
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == key:
            return int(parts[1]) if parts[1].isdigit() else None
    return None


def in_cgroup(path: str | None, root: str) -> bool:
    """Whether a process's memory cgroup path is `root` or below it: a v2 `memory.stat` and a v1 `total_rss` count the
    descendants too. A path that could not be read is in none."""
    if path is None or not path.startswith("/"):
        return False
    if root == "/":
        return True
    return path == root or path.startswith(root.rstrip("/") + "/")


def parse_children(text: str) -> set[int] | None:
    """The pids of a `/proc/<pid>/task/<tid>/children` file (space-separated); None when it is not that."""
    words = text.split()
    return {int(word) for word in words} if all(word.isdigit() for word in words) else None


def hidden_children(
    children: Callable[[], set[int] | None], shown: Callable[[int], bool], exists: Callable[[int], bool | None]
) -> list[int] | None:
    """The children of one process that `/proc` does not show as this uid's while they exist: a non-dumpable process
    under `hidepid`. A pid is one when the parent's `children` list it, its `/proc` entry is missing or another uid's
    (`shown` is False), a process still holds the pid (`exists` is not False: None, unknown, is taken as one), and the
    parent lists it again after those reads, so a child that ended (and was reaped) between them is never one.
    `children()` reads the parent's children, None when they cannot be read; then so is the answer."""
    first = children()
    if first is None:
        return None
    candidates = [pid for pid in sorted(first) if not shown(pid) and exists(pid) is not False]
    if not candidates:
        return []
    again = children()
    if again is None:
        return None
    return [pid for pid in candidates if pid in again and not shown(pid) and exists(pid) is not False]


def parse_uid(text: str) -> int | None:
    """The real uid of `/proc/<pid>/status` (the first field of `Uid:`), None when it cannot be read."""
    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
    return None


def namespace(link: str) -> str | None:
    """A PID namespace link's text when it is one (`pid:[<inode>]`), else None."""
    return link if NAMESPACE.fullmatch(link) else None


def tags_in(cmdline: bytes, environ: bytes, tags: Collection[str]) -> frozenset[str]:
    """Which of `tags` the process's command line or environment holds, each as a whole NUL-separated entry's prefix
    (an environment entry `NAME=value...`) or within a command-line argument."""
    if not tags:
        return frozenset()
    entries = [e.decode("utf-8", "replace") for e in environ.split(b"\0") if e]
    args = [a.decode("utf-8", "replace") for a in cmdline.split(b"\0") if a]
    return frozenset(t for t in tags if any(e.startswith(t) for e in entries) or any(t in a for a in args))


def parse_uids(text: str) -> tuple[int, int] | None:
    """The real and the effective uid of `/proc/<pid>/status` (the first two fields of `Uid:`), None when the line is
    missing or they are not numbers."""
    for line in text.splitlines():
        if line.startswith("Uid:"):
            parts = line.split()
            ok = len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit()
            return (int(parts[1]), int(parts[2])) if ok else None
    return None


def cgroup_path(text: str, *, v1: bool = False) -> str | None:
    """The memory cgroup of `/proc/<pid>/cgroup`: the `0::<path>` line's (cgroup v2), or with `v1` the path of the line
    whose controller list names `memory` (`<n>:<controllers>:<path>`, cgroup v1). None when that line is not there or
    its path is not absolute."""
    for line in text.splitlines():
        if v1:
            _, _, rest = line.partition(":")
            controllers, _, path = rest.partition(":")
            if "memory" in controllers.split(","):
                return path.strip() if path.startswith("/") else None
        elif line.startswith("0::"):
            path = line[3:].strip()
            return path if path.startswith("/") else None
    return None


__all__ = [
    "Proc",
    "cgroup_path",
    "hidden_children",
    "in_cgroup",
    "namespace",
    "parse_anon",
    "parse_children",
    "parse_cpu_list",
    "parse_cpus_allowed",
    "parse_rss",
    "parse_stat",
    "parse_threads",
    "parse_uid",
    "parse_uids",
    "stat_value",
    "tags_in",
]
