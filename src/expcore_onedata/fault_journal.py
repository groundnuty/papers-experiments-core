# Provenance: new
"""The fault injector's journal: every injection written down durably before it happens, and the targets' locks (O11).

A line is appended, flushed and fsynced, and its directory fsynced when the line created the file (and each directory
the append created), before the call returns, so neither a crash nor a power cut after `append` can lose it. The
injector appends `injecting` before it stops a service, `injected` once the service was seen down and `cleared` once it
is up again with a new identity, so an injection without its `cleared` line is open and the next start settles it.

Each target also has a lock file in the journal's directory. An injector holds the target's lock, exclusively, for as
long as it may have the target down; the kernel drops it when the holder dies, so recovery settles only the open
injections whose holder is gone and never brings back a provider another live run took down on purpose.
"""

import fcntl
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from expcore.errors import TARGET_LABEL

JOURNAL = "injections.jsonl"


def _fsync_directory(directory: Path) -> None:
    """Make a directory's entries durable: a file created in it survives a crash only once this has returned."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class InjectionJournal:
    """Append-only record of every injection and its clear: the target's label, its service and its identity's digest.

    Lines: `injecting` (before the stop command), `injected` (once the target was seen down) and `cleared` (once a
    restore was verified, or recovery found the target up). An `injecting` line no `cleared` line settles is open.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.path = self.directory / JOURNAL

    def append(self, line: Mapping[str, Any]) -> None:
        """Append one line durably: written, flushed and fsynced, with every directory this append created."""
        created: list[Path] = []
        missing = self.directory
        while not missing.exists():
            created.append(missing)
            missing = missing.parent
        new_file = not self.path.exists()
        self.directory.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(dict(line), sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if new_file:
            _fsync_directory(self.directory)
        for made in created:  # deepest first: each new directory's entry lives in its parent
            _fsync_directory(made.parent)

    def open_entries(self) -> list[dict[str, Any]]:
        """Return the open injections, each with `injected` set when it was seen down; raise ValueError when the journal
        cannot be read. Only a last line cut short by a crash is ignored: it was never acted on."""
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").split("\n")
        entries: dict[str, dict[str, Any]] = {}
        for number, text in enumerate(lines):
            if not text.strip():
                continue
            try:
                line = json.loads(text)
            except json.JSONDecodeError:
                if number == len(lines) - 1:  # no newline after it: the append that wrote it never finished
                    continue
                raise ValueError(f"line {number + 1} of the injection journal is not JSON") from None
            event, key = line.get("event"), line.get("id")
            if event == "injecting":
                if not TARGET_LABEL.fullmatch(str(line.get("target"))) or not isinstance(line.get("service"), str):
                    raise ValueError(f"line {number + 1} of the injection journal names no target or service")
                entries[key] = {**line, "injected": False}
            elif event == "injected" and key in entries:
                entries[key]["injected"] = True
            elif event == "cleared":
                entries.pop(key, None)
            else:
                raise ValueError(f"line {number + 1} of the injection journal is not an injection event")
        return list(entries.values())

    def try_lock(self, target: str) -> int | None:
        """Take the target's exclusive lock without waiting; return its descriptor, or None when another holds it."""
        self.directory.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^a-z0-9_]+", "-", target).strip("-")
        descriptor = os.open(self.directory / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return descriptor

    @staticmethod
    def release(descriptor: int) -> None:
        """Drop a lock taken with `try_lock`."""
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
