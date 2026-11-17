"""The evidence a federation's root holds, and what `down` may not remove (O63; README, "The copy back").

`hpcfed.py down` removes the federation's root, and with it files the study's post-check and the facts read: the state,
`platform.toml`, `facts.json`, the link check, the render identity, the manifests the spaces' data identities are
hashes of, the step records, the fault journal and the fault relay's journal.
`shards.py collect`, run while the federation is up, archives exactly those files (`archive_root`) to
`<results>/<label>.root.tar` with a manifest of its own, and writes a marker into the root (`write_marker`); `down`
refuses a root that holds evidence a run left (`present_evidence`) and no current marker (`uncollected`).

**An allow-list, never a deny-list.** The archive holds the files named in `EVIDENCE_FILES` and the files of
`EVIDENCE_TREES` whose names match, and nothing else: not `secrets/`, not any file that holds a token, key or password
by whatever name, not the relay's heartbeat and lock, not the CA, the
shim or a provider's data. Every file is opened below a descriptor of the root with `O_NOFOLLOW` and read once, as
bytes; the archive is built from those bytes, so a link anywhere among the evidence is refused and never followed, and
what was checked is what is archived. As a last check no archived file may hold the content of a file of `secrets/`
(its whole stripped content, or the value of a `KEY=value` line of a `.env` file): such a file is refused, naming it
and never the value.

**Evidence present, not a step run.** `down` cannot see the results directory, so what gates it is what the root
holds. A root that only ran Day-0 measurements has no shard step, and holds evidence all the same when the platform
declares `[faults]` (the injector's journal and the relay's files are written there; a Day-0 run's own report and
the checks that only read go to its `--out` directory, never to the root), so the gate keys on the evidence a run
left (`present_evidence`), by who writes it: `UP_FILES` and `UP_TREES` are what `up` writes (the state, the link
check, the platform file, the facts, the render identity, the data manifests), present in every root `up` finished,
and count for nothing; `RUN_FILES` and `RUN_TREES` are what a run, a step or the operator leaves, and each one with
something in it counts: a file of the fault journal at any depth, a relay request, taken request, answer or
`late-steps.jsonl`, a shard claim, the pilot's `day0-wiring.toml`, and any step record (`shard-steps/<label>.json`,
which `shards.py step` leaves in the root when a step of that shard starts: the shard's spec and how many steps of
it started; no path, host or secret). An empty file does not (the injector's lock files, the claim's lock), nor does
an NFS silly-rename file (a removed file still held open, which a `down` run again after a removal cut short finds),
and a thing that cannot be looked at (a link, a special file, a tree nested past `MAX_DEPTH`) does, since it proves
no absence. `up` writes no line of the fault journal and takes no admission (a test pins that no module on its path
imports what does, directly), and the relay it starts makes only empty directories, `relay.lock` and a heartbeat,
which are in no set: so a root after `up` alone, or a failed `up`'s, holds none, and `down` removes it as it always
did. The marker names the archive's SHA-256, the digest of the step records (`steps_digest`) and the SHA-256 of each
archived file (`files`), all as they were when the evidence was read; `collect --roots-only` writes one from the
plan, whatever the root holds, so a Day-0-only root can be marked. `down` reads the root's evidence again
(`check_collected`) and compares it with the marker file by file.

**Two sets, compared differently.** `STRICT_FILES` and the trees (`STRICT_TREES`) hold what a
step, the study or `up` writes: the fault journal, the relay's journals (`late-steps.jsonl` and its request, taken and
answer files), the shard claims, the step records, the data manifests, and the files `up` writes once (`platform.toml`,
`facts.json`, `render-identity.json`) and the pilot's `day0-wiring.toml`. A change to any of them since the collect,
a line appended to a journal included, makes the marker stale and `down` refuses. `SELF_REWRITTEN_FILES` holds the two
files the running federation rewrites itself, which post-date every run once `collect` has run: the state
(`hpc-federation.json`, rewritten by every state update, such as a resumed step's node or a data verification) and the
link check (`link-check.json`, rewritten by each check: `up`'s, a restore's, a heal's and the health gate's). A change
to them is no reason to refuse and needs no second archive: the collect-time copy is what the post-check reads, and
`check_collected` names the ones that changed (names only) for `down` to record. A check a step runs also appends to
the fault journal's holds (`reset-holds.jsonl`), so a step still running after the collect refuses `down` through the
journal all the same: the tolerance is for a rewrite that journals nothing. A file in neither set compares strictly.
`pass-env.json` is a strict file of the results (`<label>/pass-env.json`, listed by hash in `archives.sha256.json` and
checked by `verify-copy`), not of the root, so `down` cannot see it.

This module uses the standard library alone: `down` imports it, `collect` loads it by its path, and `shards.py`, which
runs alone on a node, keeps its own copy of the two names of the step's record (a test compares them).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import secrets
import stat
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

STATE = "hpc-federation.json"
LINK_CHECK = "link-check.json"
# The two sets of the root's own files (see the docstring), by name. Present-only except `REQUIRED`: a federation that
# ran no faults, or has no platform template, lacks some. `day0-wiring.toml` is the pilot's, hand-copied into the root;
# `shard-claim.json` and `shard-claim.lock` are written by its pre-step guard (who held the root, which a resume dispute
# needs). One line each: a file the post-check reads is added to the set it belongs to, which is the question whether a
# process of the federation's own rewrites it while the federation is up.
SELF_REWRITTEN_FILES = (STATE, LINK_CHECK)
# The allow-list is also split by who writes a file, which decides whether `down` counts it as evidence a run left:
# `UP_FILES` and `UP_TREES` are what `up` writes (and the federation rewrites), present in every root `up` finished, so
# they say nothing of what ran; `RUN_FILES` and `RUN_TREES` (below) are what a run, a step or the operator leaves, and
# are the evidence `down` refuses to remove uncollected. Every file of the allow-list is in exactly one of the two.
UP_FILES = (STATE, LINK_CHECK, "platform.toml", "facts.json", "render-identity.json")
UP_TREES = ("data-manifests",)  # `hpcdata.data_manifests`: each non-imported space's manifest, written by `up`
STEPS_KIND = "step records"
JOURNAL_KIND = "the fault journal"
RELAY_KIND = "the relay's journals"
CLAIM_KIND = "a shard claim"
WIRING_KIND = "the day-0 wiring"
KINDS = (STEPS_KIND, JOURNAL_KIND, RELAY_KIND, CLAIM_KIND, WIRING_KIND)  # the order `down` names them in
RUN_FILES = {  # left by the pilot (the wiring a day-0 run reads) or by a step's pre-step guard (who held the root)
    "day0-wiring.toml": WIRING_KIND,
    "shard-claim.json": CLAIM_KIND,
    "shard-claim.lock": CLAIM_KIND,
}
RUN_TREES = {  # the injector's journal, the relay's journals and the step records: written by a run, never by `up`
    "fault-journal": JOURNAL_KIND,
    "fault-relay": RELAY_KIND,
    "fault-relay/requests": RELAY_KIND,
    "fault-relay/taken": RELAY_KIND,
    "fault-relay/answers": RELAY_KIND,
    "shard-steps": STEPS_KIND,
}
STRICT_FILES = (*(name for name in UP_FILES if name not in SELF_REWRITTEN_FILES), *RUN_FILES)
EVIDENCE_FILES = (*SELF_REWRITTEN_FILES, *STRICT_FILES)  # the allow-list: every file in exactly one of the two sets
STRICT = "strict"
SELF_REWRITTEN = "self-rewritten"
UP, RUN = "up", "run"  # `kind_of`
REQUIRED = (STATE, "facts.json")  # what `up` always writes: a root without one is not the evidence the post-check needs
_REQUEST_FILE = re.compile(r"[0-9a-f]{32}\.json")  # the relay's requests, the ones it took and its answers
_JSON_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json")
# A directory of the root and the names of the files directly in it that are evidence; every other name is left out.
EVIDENCE_TREES = (
    ("fault-relay", re.compile(r"late-steps\.jsonl")),  # not heartbeat.json, relay.lock
    ("fault-relay/requests", _REQUEST_FILE),
    ("fault-relay/taken", _REQUEST_FILE),
    ("fault-relay/answers", _REQUEST_FILE),
    # what `up` made each non-imported space's data identity from (`hpcdata.MANIFESTS`): `platform.toml` has its hash
    ("data-manifests", _JSON_FILE),
    ("shard-steps", _JSON_FILE),  # `STEPS_DIR`: which shards' steps ran against this root, and how many times
)
# A directory archived whole, every file at any depth: the fault journal (`injections.jsonl`, `shared-holds.jsonl`,
# `reset-holds.jsonl`, `link-epoch.json`, the holder and drain records and the lock files the pilot's heal flocks). The
# files are read as they are, never locked; a link or a name that is not a plain file name refuses the collect.
WHOLE_TREES = ("fault-journal",)
# Every directory of the allow-list is strict: no process of the federation's own rewrites a file in one.
STRICT_TREES = (*(directory for directory, _ in EVIDENCE_TREES), *WHOLE_TREES)
_PLAIN_NAME = re.compile(r"[A-Za-z0-9._-]{1,200}")
# An NFS client's silly-rename file: a file removed while a process still holds it open, which stays (as `.nfsXXXX`)
# until the last close. It is a removed file, so no evidence: a `down` run again after a removal cut short finds only
# these, once its state is gone. The same pattern as `hpcremove.SILLY`, which this module cannot import (it uses the
# standard library alone: `collect` loads it by its path); a test compares the two.
SILLY = re.compile(r"\.nfs[0-9A-Fa-f]+")
MAX_DEPTH = 4
SECRETS = "secrets"  # never archived; its files' contents are what no archived file may hold
MIN_SECRET = 8  # the shortest value taken for a secret when scanning (a shorter one would match by chance)
MAX_SECRET_FILE = 1 << 20
MAX_FILE = 256 << 20  # a journal larger than this is refused rather than held in memory
MAX_TOTAL = 1 << 30

ROOT_FORMAT = "expcore-hpc-root-evidence/1"  # `<label>.root.json`
STEPS_DIR = "shard-steps"  # <root>/shard-steps/<label>.json: a step of that shard started against this root
STEP_FORMAT = "expcore-hpc-shard-step-record/1"
COLLECTED = "evidence-collected.json"  # <root>/: written by `collect`, read by `down`; never archived
COLLECTED_FORMAT = "expcore-hpc-evidence-collected/1"
SHA256 = re.compile(r"[0-9a-f]{64}")
_NOFOLLOW = os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


class EvidenceError(Exception):
    """The root's evidence cannot be archived as it stands; the message names the file and never a value."""


class RootGone(EvidenceError):
    """The federation's root is not there."""


def set_of(name: str) -> str | None:
    """Which of the two sets the archived file `name` (relative to the root) is in: `SELF_REWRITTEN`, `STRICT`, or None
    for one that is in neither (which `compared_strictly` still takes as strict)."""
    if name in SELF_REWRITTEN_FILES:
        return SELF_REWRITTEN
    if name in STRICT_FILES or any(name.startswith(f"{tree}/") for tree in STRICT_TREES):
        return STRICT
    return None


def compared_strictly(name: str) -> bool:
    """Whether a change to `name` since the collect makes `down` refuse: every file but the federation's own."""
    return set_of(name) != SELF_REWRITTEN


def kind_of(name: str) -> str | None:
    """Who writes the archived file `name` (relative to the root): `UP` for one `up` writes or the federation rewrites,
    `RUN` for one a run, a step or the operator leaves (evidence `down` counts), None for one that is in neither."""
    if name in UP_FILES or any(name.startswith(f"{tree}/") for tree in UP_TREES):
        return UP
    if name in RUN_FILES or any(name.startswith(f"{tree}/") for tree in RUN_TREES):
        return RUN
    return None


@dataclass(frozen=True)
class RootArchive:
    """One federation's root evidence as bytes: the tar, its manifest (`<label>.root.json`) and the digest of the step
    records read before the files were (what a marker names). `reused` is an archive an earlier collect wrote, kept
    because the root is gone."""

    label: str
    tar: bytes
    manifest: bytes
    sha256: str
    steps_sha256: str
    names: tuple[str, ...]
    reused: bool = False
    up_id: str = ""  # a hash of the allocation's name: which `up` of the root's path made the federation
    hashes: Mapping[str, str] = field(default_factory=dict)  # each archived file's SHA-256, from the bytes archived


# ------------------------------------------------------------------------------------------------ reading below a root
def _open_dir(parent: int, name: str, shown: str) -> int:
    """`name` below the directory descriptor `parent`, a plain directory (never a link); EvidenceError otherwise."""
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=parent)
    except OSError as error:
        raise EvidenceError(f"{shown} is not a plain directory ({type(error).__name__})") from None


def _tree(root_fd: int, relative: str) -> int | None:
    """A directory of the allow-list below the root, opened part by part with no link followed; None when a part is
    not there; EvidenceError when one is a link or not a directory."""
    fd, shown = os.dup(root_fd), ""
    try:
        for part in relative.split("/"):
            shown = f"{shown}/{part}".lstrip("/")
            try:
                info = os.lstat(part, dir_fd=fd)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(info.st_mode):
                raise EvidenceError(f"{shown} is a link or not a directory, never followed")
            following = _open_dir(fd, part, shown)
            os.close(fd)
            fd = following
        result, fd = fd, -1
        return result
    finally:
        if fd >= 0:
            os.close(fd)


def _read_regular(directory_fd: int, name: str, shown: str) -> bytes:
    """One file below `directory_fd`, opened with no link followed and read once; EvidenceError when it is not a regular
    file or too large."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW, dir_fd=directory_fd)
    except OSError as error:
        raise EvidenceError(f"{shown} is a link or cannot be read ({type(error).__name__}): never followed") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise EvidenceError(f"{shown} is not a regular file")
        if info.st_size > MAX_FILE:
            raise EvidenceError(f"{shown} is larger than {MAX_FILE >> 20} MiB")
        data = handle.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise EvidenceError(f"{shown} is larger than {MAX_FILE >> 20} MiB")
    return data


def _candidates(root_fd: int) -> list[str]:
    """The names, relative to the root, of the files of the allow-list that are there, sorted; a link or a thing that is
    no regular file at one of them is EvidenceError."""
    found: list[str] = []
    for name in EVIDENCE_FILES:
        try:
            info = os.lstat(name, dir_fd=root_fd)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise EvidenceError(f"{name} is a link or not a regular file, never followed")
        found.append(name)
    for relative, pattern in EVIDENCE_TREES:
        directory = _tree(root_fd, relative)
        if directory is None:
            continue
        try:
            with os.scandir(directory) as listing:
                for entry in listing:
                    if pattern.fullmatch(entry.name):
                        if not stat.S_ISREG(entry.stat(follow_symlinks=False).st_mode):
                            raise EvidenceError(
                                f"{relative}/{entry.name} is a link or not a regular file, never followed"
                            )
                        found.append(f"{relative}/{entry.name}")
        finally:
            os.close(directory)
    for relative in WHOLE_TREES:
        directory = _tree(root_fd, relative)
        if directory is not None:
            try:
                _whole(directory, relative, 0, found)
            finally:
                os.close(directory)
    return sorted(found)


def _whole(directory: int, relative: str, depth: int, found: list[str]) -> None:
    """Every file below the directory `directory` (the descriptor of `relative`), at any depth up to `MAX_DEPTH`, added
    to `found`; EvidenceError for a link or another thing that is no file or directory, a name that is not plain, or a
    tree nested deeper."""
    if depth > MAX_DEPTH:
        raise EvidenceError(f"{relative} is nested more than {MAX_DEPTH} directories deep")
    with os.scandir(directory) as listing:
        entries = sorted((entry.name, entry.stat(follow_symlinks=False).st_mode) for entry in listing)
    for name, mode in entries:
        shown = f"{relative}/{name}"
        if not _PLAIN_NAME.fullmatch(name):
            raise EvidenceError(f"{relative}/ holds a name that is not a plain file name")
        if stat.S_ISREG(mode):
            found.append(shown)
        elif stat.S_ISDIR(mode):
            below = _open_dir(directory, name, shown)
            try:
                _whole(below, shown, depth + 1, found)
            finally:
                os.close(below)
        else:
            raise EvidenceError(f"{shown} is a link or not a regular file, never followed")


def _read_named(root_fd: int, relative: str) -> bytes:
    *parents, name = relative.split("/")
    directory = os.dup(root_fd)
    try:
        for part in parents:
            following = _open_dir(directory, part, relative)
            os.close(directory)
            directory = following
        return _read_regular(directory, name, relative)
    finally:
        os.close(directory)


def secret_values(root_fd: int) -> list[bytes]:
    """What a file of `secrets/` holds that no archived file may: its whole stripped content, and for a `.env` file the
    value of each `KEY=value` line, each at least `MIN_SECRET` bytes. Links are not followed and nothing is archived."""
    values: list[bytes] = []
    try:
        top = os.open(SECRETS, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=root_fd)
    except FileNotFoundError:
        return values
    except OSError as error:
        raise EvidenceError(
            f"{SECRETS}/ cannot be read ({type(error).__name__}): no check that no secret is in"
        ) from None
    stack = [(top, "")]
    while stack:
        directory, prefix = stack.pop()
        try:
            with os.scandir(directory) as listing:
                entries = [(entry.name, entry.stat(follow_symlinks=False).st_mode) for entry in listing]
            for name, mode in entries:
                if stat.S_ISDIR(mode):
                    stack.append((os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=directory),
                                  f"{prefix}{name}/"))  # fmt: skip
                elif stat.S_ISREG(mode):
                    values += _values_of(name, _read_secret(directory, name))
        except OSError as error:
            raise EvidenceError(
                f"{SECRETS}/ cannot be read ({type(error).__name__}): no check that no secret is in"
            ) from None
        finally:
            os.close(directory)
    return [value for value in values if len(value) >= MIN_SECRET]


def _read_secret(directory: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _NOFOLLOW, dir_fd=directory)
    with os.fdopen(fd, "rb") as handle:
        return handle.read(MAX_SECRET_FILE)


def _values_of(name: str, content: bytes) -> list[bytes]:
    values = [content.strip()]
    if name.endswith(".env"):
        values += [line.partition(b"=")[2].strip().strip(b"\"'") for line in content.splitlines() if b"=" in line]
    return values


# ------------------------------------------------------------------------------------------------ the step records
def _step_entries(root_fd: int) -> list[tuple[str, bytes | None]] | None:
    """The records of `shard-steps/` as (name, bytes), sorted; a thing that is no regular file has None for its bytes
    (so that it can never equal a record); the empty list when there is no such directory; None when it is a link or
    cannot be listed (nothing can be said of what ran)."""
    try:
        info = os.lstat(STEPS_DIR, dir_fd=root_fd)
    except FileNotFoundError:
        return []
    if not stat.S_ISDIR(info.st_mode):
        return None
    try:
        directory = os.open(STEPS_DIR, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=root_fd)
    except OSError:
        return None
    try:
        with os.scandir(directory) as listing:
            names = sorted((entry.name, entry.stat(follow_symlinks=False).st_mode) for entry in listing)
        entries: list[tuple[str, bytes | None]] = []
        for name, mode in names:
            try:
                entries.append((name, _read_regular(directory, name, name) if stat.S_ISREG(mode) else None))
            except EvidenceError:
                entries.append((name, None))
        return entries
    except OSError:
        return None
    finally:
        os.close(directory)


def _digest_of(entries: list[tuple[str, bytes | None]] | None) -> str:
    listed = ["unreadable"] if entries is None else [
        [name, hashlib.sha256(data).hexdigest() if data is not None else None] for name, data in entries
    ]  # fmt: skip
    return hashlib.sha256(json.dumps(listed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def steps_digest(root: Path) -> str:
    """The SHA-256 over the names and contents of the step records in `root`: the same for the same records, and not
    for one more, one changed, or records that cannot be read."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return _digest_of(_step_entries(fd))
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------------------------ evidence present
def _journal_has(directory: int, depth: int) -> bool:
    """Whether the fault journal below `directory` (a descriptor; `depth` 0 at its top) holds a file with something in
    it, or a thing no collect could archive (a link or another special file, a name that is not plain, a tree nested
    past `MAX_DEPTH`): the last proves no absence, so it counts. OSError and EvidenceError are for the caller."""
    if depth > MAX_DEPTH:
        return True
    with os.scandir(directory) as listing:
        entries = [(entry.name, entry.stat(follow_symlinks=False)) for entry in listing]
    for name, info in entries:
        if SILLY.fullmatch(name):  # a removed file still held open: the file is gone
            continue
        if not _PLAIN_NAME.fullmatch(name):
            return True
        if stat.S_ISREG(info.st_mode):
            if info.st_size > 0:  # a lock file the injector took and wrote nothing to is empty: no evidence
                return True
        elif stat.S_ISDIR(info.st_mode):
            below = _open_dir(directory, name, name)
            try:
                if _journal_has(below, depth + 1):
                    return True
            finally:
                os.close(below)
        else:
            return True
    return False


def _tree_has(root_fd: int, relative: str, pattern: re.Pattern[str] | None) -> bool:
    """Whether the allow-list's directory `relative` of the root holds evidence: with a `pattern`, a file of that name
    that is not empty (or not a regular file); without one, the fault journal's tree (`_journal_has`). A directory that
    is not there holds none; one that is a link or cannot be read proves no absence, so it counts."""
    try:
        directory = _tree(root_fd, relative)
        if directory is None:
            return False
        try:
            if pattern is None:
                return _journal_has(directory, 0)
            with os.scandir(directory) as listing:
                for entry in listing:
                    if pattern.fullmatch(entry.name):
                        info = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_size > 0:
                            return True
            return False
        finally:
            os.close(directory)
    except (EvidenceError, OSError):
        return True


def _present(root_fd: int, steps: list[tuple[str, bytes | None]] | None) -> tuple[str, ...]:
    """The kinds of evidence (`KINDS`) a run left in the root, by `RUN_FILES` and `RUN_TREES`: a step record of any
    kind (`steps`, which `_step_entries` read: a record counts whatever it holds, and records that cannot be read count
    too), a fault-journal file or a relay journal with something in it, a claim file or the pilot's wiring that is not
    empty. Nothing `up` writes counts (`UP_FILES`, `UP_TREES`): it writes no line of the journal and takes no
    admission, so a root after `up` alone holds none. Every read is below the root's descriptor with no link followed,
    and only sizes are read: a thing that cannot be looked at counts, never passes for nothing."""
    found: set[str] = set()
    if steps is None or any(not SILLY.fullmatch(name) for name, _ in steps):
        found.add(STEPS_KIND)
    for name, kind in RUN_FILES.items():
        try:
            info = os.lstat(name, dir_fd=root_fd)
        except FileNotFoundError:
            continue
        except OSError:
            found.add(kind)
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_size > 0:
            found.add(kind)
    for relative, pattern in EVIDENCE_TREES:
        kind = RUN_TREES.get(relative)
        if kind is not None and kind != STEPS_KIND and _tree_has(root_fd, relative, pattern):
            found.add(kind)
    for relative in WHOLE_TREES:
        if _tree_has(root_fd, relative, None):
            found.add(RUN_TREES[relative])
    return tuple(kind for kind in KINDS if kind in found)


def present_evidence(root: Path) -> tuple[str, ...]:
    """The kinds of evidence a run left in `root` (`_present`), in `KINDS`' order; empty for a root after `up` alone, a
    failed `up`'s, and one that is not there."""
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return ()
    try:
        return _present(fd, _step_entries(fd))
    finally:
        os.close(fd)


# ------------------------------------------------------------------------------------------------ the root archive
def _up_id(state: bytes) -> str:
    """A hash of the allocation's name in the state file (`hpcop-<project>-<random>`: a new one for each `up`, no path):
    two federations brought up one after the other at one root are told apart by it. Empty when the state names none."""
    try:
        name = json.loads(state)["allocation"]["name"]
    except (ValueError, KeyError, TypeError):
        return ""
    return hashlib.sha256(f"expcore-up-id:{name}".encode()).hexdigest() if isinstance(name, str) and name else ""


def _tar_of(files: dict[str, bytes]) -> bytes:
    """The files as a tar built from bytes: sorted, fixed mode, owner and time, so that the same files give the same
    bytes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime, info.uid, info.gid = len(files[name]), 0o600, 0, 0, 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(files[name]))
    return buffer.getvalue()


def _manifest_of(label: str, tar: bytes, files: dict[str, bytes], root_id: str, up_id: str) -> bytes:
    document = {
        "format": ROOT_FORMAT,
        "label": label,
        "root_id": root_id,
        "up_id": up_id,
        "sha256": hashlib.sha256(tar).hexdigest(),
        "bytes": len(tar),
        "files": [
            {"name": name, "bytes": len(files[name]), "sha256": hashlib.sha256(files[name]).hexdigest()}
            for name in sorted(files)
        ],
    }
    return (json.dumps(document, sort_keys=True, indent=2) + "\n").encode()


def archive_root(root: Path, label: str, root_id: str = "") -> RootArchive:
    """The evidence of the federation's root as bytes (the module's docstring): the digest of the step records first,
    then each file of the allow-list read once with no link followed, then the check that none holds a secret.
    `root_id` (a hash of the root's path, which the manifest records and no path) tells one federation's archive from
    another's under one shard label (a shard that moved). `RootGone` when the root is not there; EvidenceError naming
    the file when a required one is missing, one is a link or holds a secret, or the total is too large."""
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        raise RootGone("its root is gone") from None
    except OSError as error:
        raise EvidenceError(f"its root cannot be opened ({type(error).__name__})") from None
    try:
        steps = _digest_of(_step_entries(fd))
        files: dict[str, bytes] = {}
        for relative in _candidates(fd):
            files[relative] = _read_named(fd, relative)
            if sum(map(len, files.values())) > MAX_TOTAL:
                raise EvidenceError(f"the evidence is larger than {MAX_TOTAL >> 20} MiB")
        for name in REQUIRED:
            if name not in files:
                raise EvidenceError(f"{name} is missing: it is what `up` always writes, and the post-check needs it")
        secrets_found = secret_values(fd)
    finally:
        os.close(fd)
    for name, data in files.items():
        if any(value in data for value in secrets_found):
            raise EvidenceError(f"{name} holds the content of a file of {SECRETS}/: not archived")
    tar = _tar_of(files)
    up_id = _up_id(files[STATE])
    manifest = _manifest_of(label, tar, files, root_id, up_id)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    return RootArchive(
        label, tar, manifest, hashlib.sha256(tar).hexdigest(), steps, tuple(sorted(files)), up_id=up_id, hashes=hashes
    )


def _plain_file(path: Path) -> bytes:
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise EvidenceError(f"{path.name} is not a regular file")
        return path.read_bytes()
    except FileNotFoundError:
        raise EvidenceError(f"{path.name} is missing") from None
    except OSError as error:
        raise EvidenceError(f"{path.name} cannot be read ({type(error).__name__})") from None


def verified_pair(results: Path, stem: str, label: str, root_id: str | None = None) -> RootArchive:
    """The root archive `<stem>.root.tar` of `results` and its manifest `<stem>.root.json`, shown to be one
    another's: the manifest names the archive's SHA-256 and size and this shard's `label` (and, with `root_id`, this
    federation's root), and lists exactly the files the archive holds with their sizes and hashes, the required ones
    among them. EvidenceError otherwise, saying which."""
    tar = _plain_file(results / f"{stem}.root.tar")
    raw = _plain_file(results / f"{stem}.root.json")
    try:
        manifest = json.loads(raw)
        listed = {entry["name"]: (entry["bytes"], entry["sha256"]) for entry in manifest["files"]}
        valid = manifest["format"] == ROOT_FORMAT and manifest["label"] == label
        valid = valid and manifest["sha256"] == hashlib.sha256(tar).hexdigest() and manifest["bytes"] == len(tar)
        with tarfile.open(fileobj=io.BytesIO(tar)) as opened:
            held = {m.name: opened.extractfile(m).read() for m in opened.getmembers() if m.isfile()}
        valid = valid and set(held) == set(listed) and all(name in held for name in REQUIRED)
        valid = valid and all(listed[n] == (len(held[n]), hashlib.sha256(held[n]).hexdigest()) for n in held)
        other = root_id is not None and manifest.get("root_id") != root_id
    except (ValueError, KeyError, TypeError, AttributeError, tarfile.TarError, OSError):
        valid, other = False, False
    if not valid:
        raise EvidenceError(f"{stem}.root.tar is not the root archive its manifest {stem}.root.json names")
    if other:
        raise EvidenceError(
            f"{stem}.root.tar is the root archive of another federation (the shard moved), not this one's"
        )
    return RootArchive(label, tar, raw, hashlib.sha256(tar).hexdigest(), "", tuple(sorted(held)), reused=True)


def earlier_archive(results: Path, label: str, root_id: str | None = None) -> RootArchive:
    """The root archive an earlier collect wrote to `results` for this shard, for a root that is gone: `verified_pair`
    of `<label>`, which must be this federation's root (`root_id`)."""
    return verified_pair(results, label, label, root_id)


def stem_of(results: Path, label: str, root_id: str, up_id: str = "") -> str | None:
    """What `<label>.root.tar` of `results` is, when it is the archive of another federation than this one (a shard
    that moved to another root, or a federation brought up again at the same root: `root_id` or `up_id` differs): the
    first 12 hex digits of its SHA-256, the name it is set aside under (`<label>.<12 digits>`, which keeps the archive
    and no path); None when there is none, or it is this federation's."""
    tar, manifest = results / f"{label}.root.tar", results / f"{label}.root.json"
    if not (os.path.lexists(tar) or os.path.lexists(manifest)):
        return None
    try:
        found = json.loads(_plain_file(manifest))
        if found.get("root_id") == root_id and found.get("up_id", "") == up_id:
            return None
    except (EvidenceError, ValueError, AttributeError):
        pass
    try:
        return hashlib.sha256(_plain_file(tar)).hexdigest()[:12]
    except EvidenceError:
        return secrets.token_hex(6)


# ------------------------------------------------------------------------------------------------ the marker
def _write_private(path: Path, payload: bytes) -> None:
    """`path` written whole, mode 0600: a draft beside it, fsynced, renamed, the directory fsynced."""
    draft = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft, path)
    except BaseException:
        draft.unlink(missing_ok=True)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def write_marker(
    root: Path,
    label: str,
    root_archive_sha256: str,
    steps_sha256: str | None = None,
    files: Mapping[str, str] | None = None,
) -> None:
    """Write `<root>/evidence-collected.json`: the root archive's SHA-256, the digest of the step records the evidence
    was read against (`steps_sha256`; the digest of the records now when not given) and the SHA-256 of each archived
    file (`files`: `RootArchive.hashes`, the hashes of the bytes archived, which is how `down` tells a file a step wrote
    from one the federation rewrote; without it the marker compares the archive as a whole, as an earlier collect's
    did). No path, host, value or secret."""
    record = {
        "format": COLLECTED_FORMAT,
        "label": label,
        "root_archive_sha256": root_archive_sha256,
        "steps_sha256": steps_digest(root) if steps_sha256 is None else steps_sha256,
    }
    if files is not None:
        record["files"] = dict(sorted(files.items()))
    _write_private(Path(root) / COLLECTED, (json.dumps(record, sort_keys=True) + "\n").encode())


def _marked_files(value: Any) -> bool:
    """Whether `files` of a marker is a table of root-relative plain names to SHA-256 hashes."""
    if not isinstance(value, dict):
        return False
    return all(
        isinstance(name, str)
        and len(name) <= 1000
        and all(part not in ("", ".", "..") and _PLAIN_NAME.fullmatch(part) for part in name.split("/"))
        and isinstance(digest, str)
        and SHA256.fullmatch(digest)
        for name, digest in value.items()
    )


def _marker(root_fd: int) -> dict | None:
    """The marker as a well formed record, or None: missing, a link, not JSON, or not of this form."""
    try:
        record = json.loads(_read_regular(root_fd, COLLECTED, COLLECTED))
    except (EvidenceError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("format") != COLLECTED_FORMAT:
        return None
    if not isinstance(record.get("label"), str):
        return None
    if not all(
        isinstance(record.get(key), str) and SHA256.fullmatch(record[key])
        for key in ("root_archive_sha256", "steps_sha256")
    ):
        return None
    if "files" in record and not _marked_files(record["files"]):
        return None
    return record


@dataclass(frozen=True)
class Check:
    """What `down` finds when it reads a root that holds evidence a run left: the shards that have step records
    (`labels`, none for a root that only ran Day-0 measurements); whether a current marker exists (`marked`); whether
    the evidence can no longer be read or archived (`unreadable`); the names of the strict files that differ from the
    marker's (`changed`); the names of the self-rewritten files that do (`rewritten`: no reason to refuse, named for the
    record); and the kinds of evidence present (`present`, `KINDS`' order: what the output names when there is no
    shard to name)."""

    labels: tuple[str, ...]
    marked: bool = True
    unreadable: bool = False
    changed: tuple[str, ...] = ()
    rewritten: tuple[str, ...] = ()
    present: tuple[str, ...] = ()

    @property
    def named(self) -> str:
        """What the output calls the evidence: that of the shards that have step records, else the kinds present."""
        return f"evidence of {', '.join(self.labels)}" if self.labels else f"evidence ({', '.join(self.present)})"

    @property
    def refused(self) -> bool:
        """Whether `down` may not remove the root: no current marker, or a strict file changed since it."""
        return not self.marked or self.unreadable or bool(self.changed)

    @property
    def stale(self) -> bool:
        """Refused though a marker exists: the evidence was collected and has changed since."""
        return self.refused and self.marked


def _differing(before: Mapping[str, str], now: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """The names, sorted, that differ between the marker's file hashes and the files now (one that was added or removed
    differs): the strict ones, and the self-rewritten ones."""
    strict: list[str] = []
    rewritten: list[str] = []
    for name in sorted(set(before) | set(now)):
        if before.get(name) != now.get(name):
            (strict if compared_strictly(name) else rewritten).append(name)
    return strict, rewritten


def check_collected(root: Path) -> Check | None:
    """None when the root holds no evidence a run left (or there is no root): `down` has nothing to gate. That is a root
    with no step record and none of the files of `RUN_FILES` and `RUN_TREES` with anything in them (`_present`): one
    after `up` alone, or a failed `up`'s. Otherwise the `Check` of its evidence against the marker `collect` wrote: no
    current marker; a strict file (one a step, the study or `up` writes) that is not what the marker's hash names, or a
    root that no longer archives (a link, a file that holds a secret, a required file missing); and, of no consequence,
    the self-rewritten files that changed. Reads below a descriptor of the root, no link followed; a marker without
    per-file hashes compares the archive as a whole, so any change refuses."""
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except FileNotFoundError:
        return None  # no root to remove
    except OSError:
        return Check(("(its root cannot be read)",), marked=False)  # nothing can be said of what ran: refused
    try:
        entries = _step_entries(fd)
        present = _present(fd, entries)
        if not present:
            return None
        labels = tuple(
            ["(its records cannot be read)"]
            if entries is None
            else [
                name[: -len(".json")] if name.endswith(".json") else name
                for name, _ in entries
                if not SILLY.fullmatch(name)
            ]
        )
        marker = _marker(fd)
        steps_moved = marker is not None and marker["steps_sha256"] != _digest_of(entries)
    finally:
        os.close(fd)
    if marker is None:
        return Check(labels, marked=False, present=present)
    try:
        held = archive_root(root, "")
    except (EvidenceError, OSError):
        return Check(labels, unreadable=True, present=present)
    if "files" not in marker:
        whole = steps_moved or held.sha256 != marker["root_archive_sha256"]
        return Check(labels, changed=("the root archive as a whole",) if whole else (), present=present)
    strict, rewritten = _differing(marker["files"], held.hashes)
    if steps_moved and not any(name.startswith(f"{STEPS_DIR}/") for name in strict):
        strict.append(f"{STEPS_DIR}/")  # a record that cannot be told by file: unreadable, or not a plain file
    return Check(labels, changed=tuple(strict), rewritten=tuple(rewritten), present=present)


def uncollected(root: Path) -> tuple[list[str], bool] | None:
    """None when `down` may remove `root` for want of evidence: the root holds none a run left (no step record, no
    journal, relay file, claim or wiring with anything in it), or its evidence was collected and no strict file has
    changed since (`check_collected`). Otherwise (the shards that have records, and whether a marker exists but is
    stale): a root with evidence and no marker (or one that cannot be read) is `(labels, False)`; one whose marker
    names other step records than there are now, or a strict file that changed (a running step wrote since, or a file
    was edited), or whose evidence cannot be archived, is `(labels, True)`. The labels are empty for a root that only
    ran Day-0 measurements: it has no step record (`Check.present` names what it holds)."""
    found = check_collected(root)
    if found is None or not found.refused:
        return None
    return list(found.labels), found.stale
