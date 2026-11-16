# Provenance: new
"""What `/proc` says of a process, parsed: pure functions over the files' text, so they are tested without a node (O56).

On an HPC node `/proc` is mounted `hidepid=invisible`: a process of another uid has no directory at all, and neither has
a setuid program's process of our own (Apptainer's starter), whose credentials are root's while it runs. A cell's own
processes are ours and visible: the starter execs them with our uid. Each `apptainer exec --containall` gives its
processes a PID namespace of their own, which `/proc/<pid>/ns/pid` names (`pid:[<inode>]`); that is how the harness
finds a cell's processes, whatever they call themselves and whatever environment they cleared.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

NAMESPACE = re.compile(r"pid:\[[0-9]{1,20}\]")
PAGE_KIB = 1024


@dataclass(frozen=True)
class Proc:
    """One process of ours, as the harness needs it.

    `start` is its start time in clock ticks after boot (field 22 of `stat`), which with the pid names it uniquely;
    `pidns` its PID namespace (`pid:[<inode>]`), None when the link could not be read; `rss` its resident anonymous and
    shared-memory bytes (`RssAnon` + `RssShmem`), None when `status` could not be read; `tags` which of the strings the
    caller asked for its environment or its command line holds."""

    pid: int
    start: str
    ppid: int
    state: str
    comm: str
    pidns: str | None
    rss: int | None
    tags: frozenset[str] = frozenset()

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


def cgroup_path(text: str) -> str | None:
    """The cgroup v2 path of `/proc/<pid>/cgroup` (its `0::<path>` line), None on a v1 or hybrid host."""
    for line in text.splitlines():
        if line.startswith("0::"):
            path = line[3:].strip()
            return path if path.startswith("/") else None
    return None


__all__ = ["Proc", "cgroup_path", "namespace", "parse_rss", "parse_stat", "parse_uid", "tags_in"]
