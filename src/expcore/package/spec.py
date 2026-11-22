# Provenance: new
"""The inclusion list of a replication package, read from `package.toml` in the study directory.

    [package]
    name = "tiny-study-package"
    include = ["study.toml", "plugin.py", "out/runs", "out/*.json", "out/report.md"]

Only the files the patterns select go in; anything not listed is left out. A pattern is a path relative to the study
directory with `/` between its parts: `*` and `?` match within one part, `**` matches any number of parts, and a
pattern that names a directory takes every file below it. A pattern that selects nothing is refused, because a list
that names nothing would pass for any package (K01), and so is a pattern that is absolute or climbs out with `..`.

Never selected, whatever the patterns say: version-control and cache directories, compiled Python, the live
campaign under out/ (the store, its lock and its leases), since the package's analysis reads the snapshot of the store
that the replay check keeps in the batch, the campaign's local logs (`out/logs`, the harness defects' tracebacks),
which stay on the author's machine, and the private evidence of an HPC campaign (`PRIVATE_EVIDENCE_SUFFIXES`,
`PRIVATE_EVIDENCE_NAMES`): a federation's root archive `<label>.root.tar` and its manifest, which hold node names,
site paths and the account's details, and the shard's `pass-env.json`, which holds a site path. They are private to the
operator, whatever directory they were pulled into, and no anonymisation list covers a site's details.

A batch merged by run (C45: its out/merge.json is `expcore-merge/2`, read with `campaign.merged_by_run`'s own
parser) keeps each counted lease's snapshot at `out/leases/<lease id>/campaign-snapshot.sqlite`, which the analysis
reads: exactly those paths of out/leases, the ones its merge.json lists as counted, are selectable (`MergedByRun`);
every other path under out/leases stays never selected, and so does all of out/leases in a batch that is not merged by
run. The merge's evidence (`out/evidence/<lease id>/campaign-snapshot.sqlite`, the stores of the leases that did not
count, never analysed) stays listed in merge.json by its SHA-256 and is never selected but for what
`packaged_evidence` lets in. The rule is the batch's own: nothing but its merge.json decides it.

A selected symbolic link is refused, since it could point out of the study.
The names the build writes at the package root (`GENERATED`) cannot be selected, nor can the other names GNU Make
reads there (`GNUmakefile`, `makefile`), which would take the place of the generated Makefile; the generated
instructions run `make -f Makefile check` as well. Nor can anything under `wheels/`, where the build puts the wheels
the package's check installs (`package.wheels`).

The package's checks read only what they can read in full (text, SQLite databases and archives of them; see `views`)
and refuse everything else. A file they cannot read, a PDF figure for instance, goes in only when the author attests
it, having inspected it by hand:

    [[opaque]]
    path = "figures/flow.pdf"   # one file the patterns select
    sha256 = "<its 64 hexadecimal digits>"
    reason = "a figure drawn by hand; read page by page, no name in it"

The build then includes that exact file unscanned, refuses it if its bytes no longer have that hash, and lists the
attestation in PACKAGE.json. A path under `wheels/` names a member of a vendored wheel instead,
`wheels/<wheel file>/<member>`, which the build checks once the wheels are made (`wheelscan`).
"""

import os
import re
import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore.campaign.by_run_layout import EVIDENCE_DIR, LEASES_DIR, is_by_run, snapshot_rel

SPEC_FILE = "package.toml"
GENERATED = ("Makefile", "SHA256SUMS", "PACKAGE.json", ".code-stamp.json")
RESERVED = (*GENERATED, "GNUmakefile", "makefile")  # GNU Make reads GNUmakefile, then makefile, then Makefile
SKIPPED_PARTS = frozenset({".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
SKIPPED_SUFFIXES = frozenset({".pyc", ".pyo"})
LIVE_CAMPAIGN = ("out/state.sqlite", "out/campaign.lock", "out/leases")
LOCAL_ONLY = ("out/logs",)  # the campaign's local logs (`campaign.defect_log`): never committed, never packaged
LOCAL_LOG_NAME = "harness-errors.log"  # that log by its name, wherever it lies: a shard's out/ unpacked under the study
# An HPC campaign's private evidence (`deploy/hpc-federation/shards.py collect`): by what a file is called, wherever it
# lies. The root archive's files are not named here, since they are `facts.json` and the like: the archive is.
PRIVATE_EVIDENCE_SUFFIXES = (".root.tar", ".root.json", ".root.tar.part", ".root.json.part")  # and a killed draft
PRIVATE_EVIDENCE_NAMES = ("pass-env.json",)
WHEELS_DIR = "wheels"  # the build fills it with the wheels of the environment (`package.wheels`)
MERGE_PATH = "out/merge.json"  # a merged batch's merge.json (`campaign.shard.MERGE_FILE` under out/)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class PackageError(RuntimeError):
    """A package cannot be built or verified; the message says why and never shows a value it must not.

    A path that must not be shown is named by its place in the package and the start of its hash (`views.hidden_name`).
    `private` maps each such name the message gives to the path it stands for, relative to the study: the command line
    shows it to the author on a terminal, never in a log or a file.
    """

    def __init__(self, message: str, *, private: Mapping[str, str] | None = None) -> None:
        super().__init__(message)
        self.private = dict(private or {})


@dataclass(frozen=True)
class Opaque:
    """A file the author attests by hand: the package includes exactly these bytes unscanned."""

    path: str
    sha256: str
    reason: str


@dataclass(frozen=True)
class PackageSpec:
    """What a package holds: its name (the archive's top directory), the patterns that select its files, and the
    files the author attests by hash because the checks cannot read them."""

    source: Path
    name: str
    include: tuple[str, ...]
    opaque: Mapping[str, Opaque] = field(default_factory=dict)


@dataclass(frozen=True)
class MergedByRun:
    """What a package holds of a batch merged by run (C45) beside its records, by path relative to the study, as its
    merge.json (`document`, read and validated) names it: merge.json itself, the ledger snapshot, each counted lease's
    snapshot in plan order, and the evidence snapshots `packaged_evidence` lets in."""

    document: Mapping[str, Any]
    ledger: str
    counted: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def files(self) -> tuple[str, ...]:
        """Every file of the batch the package's analysis reads, merge.json first."""
        return (MERGE_PATH, self.ledger, *self.counted, *self.evidence)

    def snapshots(self) -> list[tuple[str, str]]:
        """Each lease store's snapshot the package holds with the SHA-256 merge.json names for it: each counted
        lease's in plan order, then each packaged evidence lease's."""
        hashes = [entry["snapshot_sha256"] for entry in self.document["leases"]]
        hashes += [entry["snapshot_sha256"] for entry in packaged_evidence(self.document)]
        return list(zip((*self.counted, *self.evidence), hashes, strict=True))

    def named(self) -> list[tuple[str, str]]:
        """Every snapshot the package holds with the SHA-256 merge.json names for it: the ledger's, then the stores'."""
        return [(self.ledger, self.document["ledger_sha256"]), *self.snapshots()]


def packaged_evidence(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The evidence entries of a merge by run's merge.json whose snapshots a package holds: none. Every evidence lease
    stays listed in merge.json with its snapshot's SHA-256, and this is the one place that decides whether its snapshot
    goes in too: selection, the files the build requires, the hashes it verifies and the anonymised rewrite all ask
    it. An entry with no snapshot (a lease that left no store) can never be packaged."""
    return []


def merged_by_run(root: Path) -> MergedByRun | None:
    """The batch merged by run of the study at `root`, as its out/merge.json names it; None when out/merge.json is
    absent, a symbolic link (refused when selected), or of another schema (a merge of shards, read as before)."""
    path = Path(root) / MERGE_PATH
    if path.is_symlink() or not path.is_file():
        return None
    raw = path.read_bytes()
    return by_run_files(raw) if is_by_run(raw) else None


def by_run_files(raw: bytes) -> MergedByRun:
    """The files of a batch merged by run, from its merge.json's bytes; refuse a merge.json its own parser refuses.

    The merge by run's modules load the elastic ledger's, so they are imported here, on its branch only (C42)."""
    from expcore.campaign.ledger_read import LEDGER_SNAPSHOT
    from expcore.campaign.merge import MergeRefused
    from expcore.campaign.merged_by_run import read_by_run

    try:
        document = read_by_run(raw, MERGE_PATH)
    except MergeRefused as error:
        raise PackageError(str(error)) from None
    counted = tuple(f"out/{snapshot_rel(LEASES_DIR, entry['lease_id'])}" for entry in document["leases"])
    evidence = packaged_evidence(document)
    if any(entry.get("snapshot_sha256") is None for entry in evidence):
        raise PackageError(f"{MERGE_PATH}: an evidence lease to package left no snapshot")
    held = tuple(f"out/{snapshot_rel(EVIDENCE_DIR, entry['lease_id'])}" for entry in evidence)
    return MergedByRun(document, f"out/{LEDGER_SNAPSHOT}", counted, held)


def _check_pattern(source: Path, pattern: object) -> str:
    """Return a pattern that names a place inside the study; refuse anything else, naming the file and the key."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise PackageError(f"{source}: [package] include holds {pattern!r}; each entry is a non-empty path or glob")
    parts = pattern.split("/")
    if pattern.startswith("/") or "\\" in pattern or any(part in ("", ".", "..") for part in parts):
        raise PackageError(
            f"{source}: [package] include {pattern!r} must be a relative path with '/' between its parts, "
            "and no empty, '.' or '..' part"
        )
    return pattern


def load_spec(study_root: Path) -> PackageSpec:
    """Read and validate `package.toml` in the study directory."""
    source = Path(study_root) / SPEC_FILE
    try:
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise PackageError(f"{source}: not found; a package is built only from an explicit inclusion list") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PackageError(f"{source}: cannot be read ({type(error).__name__}: {error})") from None
    table = raw.get("package")
    unknown = sorted(set(raw) - {"package", "opaque"}) + sorted(set(table or {}) - {"name", "include"})
    if not isinstance(table, dict) or unknown:
        raise PackageError(f"{source}: needs a [package] table with `name` and `include` only (unknown: {unknown})")
    name = table.get("name")
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise PackageError(f"{source}: [package] name {name!r} must be letters, digits, '.', '_' or '-'")
    include = table.get("include")
    if not isinstance(include, list) or not include:
        raise PackageError(f"{source}: [package] include must be a non-empty list; nothing it does not list goes in")
    patterns = tuple(_check_pattern(source, pattern) for pattern in include)
    if len(set(patterns)) != len(patterns):
        raise PackageError(f"{source}: [package] include lists a pattern twice")
    return PackageSpec(source, name, patterns, _load_opaque(source, raw.get("opaque", [])))


def _load_opaque(source: Path, entries: object) -> dict[str, Opaque]:
    """The [[opaque]] attestations: each one included file, its full SHA-256 and a reason, and no path twice."""
    if not isinstance(entries, list):
        raise PackageError(f"{source}: [[opaque]] must be an array of tables")
    attested: dict[str, Opaque] = {}
    for number, entry in enumerate(entries, start=1):
        where = f"{source}: [[opaque]] entry {number}"
        if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "reason"}:
            raise PackageError(f"{where} needs exactly `path`, `sha256` and `reason`")
        path, digest, reason = entry["path"], entry["sha256"], entry["reason"]
        _check_pattern(source, path)
        if any(c in path for c in "*?"):
            raise PackageError(f"{where}: `path` must name one file, not a pattern")
        if not isinstance(digest, str) or len(digest) != 64 or set(digest) - set(string.hexdigits.lower()):
            raise PackageError(f"{where}: `sha256` must be the file's 64 lowercase hexadecimal digits")
        if not isinstance(reason, str) or not reason.strip():
            raise PackageError(f"{where}: `reason` must say why the file goes in unscanned")
        if path in attested:
            raise PackageError(f"{where}: attests {path} twice")
        attested[path] = Opaque(path, digest, reason)
    return attested


def pattern_regex(pattern: str) -> re.Pattern[str]:
    """Compile a pattern: `*` and `?` within one part, `**` across parts, and a directory takes what is below it."""
    parts = pattern.split("/")
    pieces = []
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if part == "**":
            pieces.append(".*" if last else "(?:[^/]+/)*")
            continue
        piece = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in part)
        pieces.append(piece if last else piece + "/")
    return re.compile("".join(pieces) + "(?:/.+)?")


def never_selected(rel: str, merged: MergedByRun | None = None) -> bool:
    """Tell whether a path is left out whatever the patterns say: caches, version control, the live campaign, the
    local logs, an HPC campaign's private evidence, and, in a batch merged by run (`merged`), the merge's evidence but
    what `packaged_evidence` lets in. A counted lease's snapshot that `merged` lists is not the live campaign's."""
    parts = rel.split("/")
    if SKIPPED_PARTS.intersection(parts) or os.path.splitext(rel)[1] in SKIPPED_SUFFIXES:
        return True
    if any(rel == local or rel.startswith(local + "/") for local in LOCAL_ONLY) or parts[-1] == LOCAL_LOG_NAME:
        return True
    if parts[-1].endswith(PRIVATE_EVIDENCE_SUFFIXES) or parts[-1] in PRIVATE_EVIDENCE_NAMES:
        return True
    if merged is not None and rel.startswith(f"out/{EVIDENCE_DIR}/"):
        return rel not in merged.evidence
    if merged is not None and rel in merged.counted:
        return False
    return any(rel == live or rel.startswith(live + "-") or rel.startswith(live + "/") for live in LIVE_CAMPAIGN)


def _candidates(root: Path) -> list[tuple[str, Path]]:
    """Every file and symbolic link below the root as (relative POSIX path, path); a linked directory is not entered."""
    found = []
    for directory, dirs, files in os.walk(root):
        here = Path(directory)
        dirs[:] = sorted(d for d in dirs if d not in SKIPPED_PARTS)
        names = files + [d for d in dirs if (here / d).is_symlink()]
        found += [((here / name).relative_to(root).as_posix(), here / name) for name in names]
    return sorted(found)


def select(root: Path, spec: PackageSpec) -> list[str]:
    """Return the sorted relative paths of the files the spec selects; refuse a pattern that selects no file."""
    compiled = [(pattern, pattern_regex(pattern)) for pattern in spec.include]
    hits = dict.fromkeys(spec.include, 0)
    merged = merged_by_run(Path(root))
    chosen = []
    for rel, path in _candidates(Path(root)):
        matched = [pattern for pattern, regex in compiled if regex.fullmatch(rel)]
        if not matched or never_selected(rel, merged):
            continue
        if path.is_symlink():
            raise PackageError(f"{spec.source}: {rel} is a symbolic link; a package holds regular files only")
        if rel in RESERVED or rel.startswith(f"{WHEELS_DIR}/"):
            raise PackageError(
                f"{spec.source}: {rel} is a name the build generates or make reads at the package root; leave it out"
            )
        for pattern in matched:
            hits[pattern] += 1
        chosen.append(rel)
    empty = [pattern for pattern, count in hits.items() if not count]
    if empty:
        raise PackageError(f"{spec.source}: [package] include {empty[0]!r} selects no file")
    stray = sorted(path for path in set(spec.opaque) - set(chosen) if not path.startswith(f"{WHEELS_DIR}/"))
    if stray:
        raise PackageError(f"{spec.source}: [[opaque]] attests {stray[0]}, which the package does not include")
    return chosen
