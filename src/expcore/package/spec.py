# Provenance: new
"""The inclusion list of a replication package, read from `package.toml` in the study directory.

    [package]
    name = "tiny-study-package"
    include = ["study.toml", "plugin.py", "out/runs", "out/*.json", "out/report.md"]

Only the files the patterns select go in; anything not listed is left out. A pattern is a path relative to the study
directory with `/` between its parts: `*` and `?` match within one part, `**` matches any number of parts, and a
pattern that names a directory takes every file below it. A pattern that selects nothing is refused, because a list
that names nothing would pass for any package (K01), and so is a pattern that is absolute or climbs out with `..`.

Never selected, whatever the patterns say: version-control and cache directories, compiled Python, and the live
campaign under out/ (the store, its lock and its leases), since the package's analysis reads the snapshot of the store
that the replay check keeps in the batch. A selected symbolic link is refused, since it could point out of the study.
The names the build writes at the package root (`GENERATED`) cannot be selected.
"""

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

SPEC_FILE = "package.toml"
GENERATED = ("Makefile", "SHA256SUMS", "PACKAGE.json", ".code-stamp.json")
SKIPPED_PARTS = frozenset({".git", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"})
SKIPPED_SUFFIXES = frozenset({".pyc", ".pyo"})
LIVE_CAMPAIGN = ("out/state.sqlite", "out/campaign.lock", "out/leases")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")


class PackageError(RuntimeError):
    """A package cannot be built or verified; the message says why and never shows a value it must not."""


@dataclass(frozen=True)
class PackageSpec:
    """What a package holds: its name (the archive's top directory) and the patterns that select its files."""

    source: Path
    name: str
    include: tuple[str, ...]


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
    unknown = sorted(set(raw) - {"package"}) + sorted(set(table or {}) - {"name", "include"})
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
    return PackageSpec(source, name, patterns)


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


def never_selected(rel: str) -> bool:
    """Tell whether a path is left out whatever the patterns say: caches, version control, the live campaign."""
    parts = rel.split("/")
    if SKIPPED_PARTS.intersection(parts) or os.path.splitext(rel)[1] in SKIPPED_SUFFIXES:
        return True
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
    chosen = []
    for rel, path in _candidates(Path(root)):
        matched = [pattern for pattern, regex in compiled if regex.fullmatch(rel)]
        if not matched or never_selected(rel):
            continue
        if path.is_symlink():
            raise PackageError(f"{spec.source}: {rel} is a symbolic link; a package holds regular files only")
        if rel in GENERATED:
            raise PackageError(f"{spec.source}: {rel} is a name the build generates; leave it out of the list")
        for pattern in matched:
            hits[pattern] += 1
        chosen.append(rel)
    empty = [pattern for pattern, count in hits.items() if not count]
    if empty:
        raise PackageError(f"{spec.source}: [package] include {empty[0]!r} selects no file")
    return chosen
