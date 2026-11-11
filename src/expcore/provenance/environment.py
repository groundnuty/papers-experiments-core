# Provenance: new
"""The environment a campaign runs in: which distributions, at which versions, from which sources (#45, #60).

    {"python": "3.12",
     "distributions": {"numpy": "2.5.4", "papers-experiments-core": "0.1.0", ...},
     "sources": {"papers-experiments-core": {"kind": "vcs", "vcs": "git", "url": "https://...", "commit": "..."}}}

The distributions are the core's and those the study names in `[study] requires` (a platform package, a backend),
with everything they require, transitively: the closure of their `Requires-Dist` over what is installed, which is
what the study's code can import through its dependencies. Each requirement's marker is evaluated for this
interpreter and platform (`packaging.markers`), once with no extra and once for each extra asked of the distribution
that requires it, so a requirement counts when any of those makes its marker true: `extra == "x" or python_version
>= "3.0"` counts with or without the extra, and a tool whose marker is false is left out even when it is installed. A
requirement whose marker holds but that is not installed is left out too, since nothing can import it. Tools
installed beside the study and not required by it (a test runner, a linter, the installer itself) are not part of
it, so a reader's fresh environment holds exactly these.

`sources` names, for each distribution installed from a direct URL rather than an index (PEP 610 `direct_url.json`),
where it came from: a version-control URL and commit, an archive URL and hash, or a local directory. A local path is
never kept (it names the machine), nor are credentials, a query or a fragment of a URL. A distribution installed from
an index has no entry. An editable install's version does not change when its checkout does, so its entry also holds
`python_sha256`, a digest of the Python source it imports from: every `*.py` below the directories its `.pth` files
put on the path (or below its project directory when it has none), by path relative to that directory and content,
never by where the checkout lies. Editing the checkout or swapping it for another changes the environment. An
anonymised replication package drops `sources` altogether.

The campaign fingerprint holds the environment, so a resume under another one is refused (C06). The replay check
records the environment it ran under, refused unless it is the campaign's, and the analysis refuses a check made
under another environment than the one in use (R04). A replication package compares names and versions only: it is
installed from the wheels it ships, which have no direct-URL source.
"""

import hashlib
import importlib.util
import json
import os
import re
import sys
from collections.abc import Iterable, Mapping
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.request import url2pathname

from packaging.requirements import InvalidRequirement, Requirement

CORE_DISTRIBUTION = "papers-experiments-core"
_ROOT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:\[[A-Za-z0-9._,\s-]*\])?")


SKIPPED_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", ".tox", "build", "dist", "node_modules"})


class EnvironmentUnknown(RuntimeError):
    """A distribution the environment needs is not installed, or its metadata cannot be read."""


def canonical(name: str) -> str:
    """A distribution name as PEP 503 normalises it: lower case, runs of '-', '_' and '.' as one '-'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def is_requirement_name(text: object) -> bool:
    """Whether a text names a distribution, with optional extras and no version: what `[study] requires` holds."""
    return isinstance(text, str) and _ROOT.fullmatch(text) is not None


def _parse(requirement: str, where: str) -> Requirement:
    """A requirement as `packaging` reads it; one that cannot be read leaves the environment unknown."""
    try:
        return Requirement(requirement)
    except InvalidRequirement:
        raise EnvironmentUnknown(f"{where}: the requirement {requirement!r} cannot be read") from None


def applies(requirement: Requirement, extras: Iterable[str]) -> bool:
    """Whether a requirement's marker holds here, with no extra or with any of the extras asked of its requirer."""
    if requirement.marker is None:
        return True
    return any(requirement.marker.evaluate({"extra": extra}) for extra in ("", *sorted(extras)))


def installed() -> dict[str, metadata.Distribution]:
    """Every installed distribution by canonical name; where a name is installed twice, the first on the path wins."""
    found: dict[str, metadata.Distribution] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"] if dist.metadata is not None else None
        if isinstance(name, str) and name and canonical(name) not in found:
            found[canonical(name)] = dist
    return found


def _sanitised_url(url: str) -> str | None:
    """A URL without credentials, query or fragment; None for a local file URL, which would name the machine."""
    parts = urlsplit(url)
    if parts.scheme in ("", "file") or not parts.hostname:
        return None
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def pth_directories(dist: metadata.Distribution) -> list[Path]:
    """The directories a distribution's `.pth` files put on the path, as an editable install's do."""
    roots: list[Path] = []
    for item in dist.files or []:
        if item.suffix != ".pth":
            continue
        try:
            lines = Path(str(dist.locate_file(item))).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        entries = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith(("#", "import"))]
        roots += [Path(entry) for entry in entries if Path(entry).is_dir()]
    return roots


def editable_roots(dist: metadata.Distribution) -> list[Path]:
    """The directories an editable install imports from: what its `.pth` files put on the path, else its project."""
    roots = pth_directories(dist)
    if roots:
        return roots
    try:
        url = str(json.loads(dist.read_text("direct_url.json") or "{}").get("url", ""))
    except (ValueError, AttributeError):
        return []
    project = Path(url2pathname(urlsplit(url).path)) if urlsplit(url).scheme == "file" else None
    return [project] if project is not None and project.is_dir() else []


def python_files(root: Path) -> list[Path]:
    """The Python source below a directory, in path order, without caches, environments or version control.

    Directory links are followed, since a package linked into another imports like one inside it; a directory
    reached again (a link back up the tree) is walked once, by its resolved path.
    """
    found: list[Path] = []
    walked: set[Path] = set()
    for here, dirs, files in os.walk(root, followlinks=True):
        real = Path(here).resolve()
        if real in walked:
            dirs[:] = []
            continue
        walked.add(real)
        dirs[:] = [name for name in dirs if name not in SKIPPED_DIRS]
        found += [Path(here) / name for name in files if name.endswith(".py") and (Path(here) / name).is_file()]
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def _inside(path: Path, places: Iterable[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == place.resolve() or place.resolve() in resolved.parents for place in places)


def top_level_location(top: str, places: Iterable[Path]) -> tuple[list[Path], Path | None]:
    """Where a top-level package imports from (its directories) or a module (its file), if inside `places`.

    Found with `importlib.util.find_spec`, which does not import it, so a package behind an import hook is found where
    the hook loads it from. A name that is not importable here, or found outside `places`, has no location.
    """
    places = list(places)
    if not top.isidentifier():
        return [], None
    try:
        spec = importlib.util.find_spec(top)
    except (ImportError, ValueError):
        return [], None  # not importable here, so nothing of it is imported
    if spec is None:
        return [], None
    if spec.submodule_search_locations is not None:
        return [Path(d) for d in spec.submodule_search_locations if _inside(Path(d), places)], None
    origin = Path(spec.origin) if spec.has_location and spec.origin else None
    return [], origin if origin is not None and origin.is_file() and _inside(origin, places) else None


def is_editable(dist: metadata.Distribution) -> bool:
    """Whether a distribution is an editable install, by its `direct_url.json` (PEP 660)."""
    try:
        data = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        return False
    info = data.get("dir_info") if isinstance(data, dict) else None
    return isinstance(info, dict) and info.get("editable") is True


def top_level_names(dist: metadata.Distribution) -> set[str]:
    """The top-level names a distribution declares in `top_level.txt`, which setuptools writes for an editable install
    too; none when it declares none."""
    return set((dist.read_text("top_level.txt") or "").split())


def owns_file(dist: metadata.Distribution, file: Path, top: str) -> bool:
    """Whether an installed distribution's code holds the file of a module under top-level name `top`: listed in its
    RECORD, or, for an editable install, inside one of its top-level packages where it imports from (directly below a
    directory its `.pth` puts on the path, else where an import hook loads it inside the project), never merely
    somewhere below the project."""
    if is_editable(dist):
        pth = pth_directories(dist)
        if pth:
            homes = [d / top for d in pth if (d / top).is_dir()]
            return any(h.resolve() in file.parents for h in homes) or any(
                (d / f"{top}.py").resolve() == file for d in pth
            )
        directories, module = top_level_location(top, editable_roots(dist))
        if any(d.resolve() in file.parents for d in directories):
            return True
        # A single top-level module counts only when the distribution names it among its top-level names, so a loose
        # module of the project on a path added by hand is not taken for the distribution's code.
        return module is not None and module.resolve() == file and top in top_level_names(dist)
    site = Path(str(dist.locate_file(""))).resolve()
    try:
        relative = PurePosixPath(file.relative_to(site).as_posix())
    except ValueError:
        return False
    return any(PurePosixPath(item.as_posix()) == relative for item in dist.files or [])


def python_digest(roots: list[Path]) -> str:
    """A digest of the Python source below the directories, by relative path and content; no path of the machine."""
    digest = hashlib.sha256()
    for number, root in enumerate(roots):
        for path in python_files(root):
            name = f"{number}/{path.relative_to(root).as_posix()}".encode()
            digest.update(name + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def source_of(dist: metadata.Distribution) -> dict[str, Any] | None:
    """Where a distribution was installed from, from its `direct_url.json`; None when it came from an index."""
    text = dist.read_text("direct_url.json")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return {"kind": "unreadable"}
    if not isinstance(data, dict):
        return {"kind": "unreadable"}
    url = _sanitised_url(str(data.get("url", "")))
    shown = {} if url is None else {"url": url}
    if isinstance(data.get("vcs_info"), dict):
        vcs = data["vcs_info"]
        return {"kind": "vcs", "vcs": str(vcs.get("vcs")), **shown, "commit": str(vcs.get("commit_id"))}
    if isinstance(data.get("archive_info"), dict):
        archive = data["archive_info"]
        hashes = archive.get("hashes") if isinstance(archive.get("hashes"), dict) else {}
        digest = hashes.get("sha256") or (str(archive.get("hash", "")).partition("=")[2] or None)
        return {"kind": "archive", **shown, **({"sha256": digest} if digest else {})}
    editable = isinstance(data.get("dir_info"), dict) and data["dir_info"].get("editable") is True
    if not editable:
        return {"kind": "directory", "editable": False}
    roots = editable_roots(dist)
    if not roots:
        name = dist.metadata["Name"] if dist.metadata is not None else "a distribution"
        raise EnvironmentUnknown(f"{name} is installed editable, but the checkout it imports from cannot be found")
    return {"kind": "directory", "editable": True, "python_sha256": python_digest(roots)}


def closure(requires: Iterable[str] = (), *, found: Mapping[str, metadata.Distribution] | None = None) -> set[str]:
    """The canonical names of the distributions the environment record covers: the core, those `requires` names, and
    what they require here, with extras and markers. Reads metadata only, never a source file.

    Raises EnvironmentUnknown when the core or a required root is not installed.
    """
    dists = installed() if found is None else found
    return set(_closure(requires, dists))


def environment(requires: Iterable[str] = (), *, found: Mapping[str, metadata.Distribution] | None = None) -> dict:
    """The environment of the core and the distributions `requires` names, as described above.

    Raises EnvironmentUnknown when the core or a required root is not installed.
    """
    dists = installed() if found is None else found
    asked = _closure(requires, dists)
    versions = {name: str(dists[name].version) for name in sorted(asked)}
    sources = {name: source for name in sorted(asked) if (source := source_of(dists[name])) is not None}
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    return {"python": python, "distributions": versions, "sources": sources}


def _closure(requires: Iterable[str], dists: Mapping[str, metadata.Distribution]) -> dict[str, set[str]]:
    """Each distribution of the closure with the extras asked of it; see `closure`."""
    queue: list[tuple[str, frozenset[str]]] = []
    for root in (CORE_DISTRIBUTION, *requires):
        parsed = _parse(root, "[study] requires")
        if canonical(parsed.name) not in dists:
            raise EnvironmentUnknown(
                f"{root!r} is not installed, so the environment it belongs to cannot be recorded; install it"
            )
        queue.append((canonical(parsed.name), frozenset(canonical(e) for e in parsed.extras)))
    asked: dict[str, set[str]] = {}
    while queue:
        name, extras = queue.pop()
        if name in asked and extras <= asked[name]:
            continue
        asked.setdefault(name, set()).update(extras)
        for text in dists[name].requires or []:
            parsed = _parse(text, name)
            if not applies(parsed, asked[name]) or canonical(parsed.name) not in dists:
                continue  # its marker is false here, or nothing installed answers it
            queue.append((canonical(parsed.name), frozenset(canonical(e) for e in parsed.extras)))
    return asked


def without_sources(env: Mapping[str, Any]) -> dict[str, Any]:
    """The environment with no source: what an anonymised package keeps, with no URL and no commit."""
    return {key: value for key, value in env.items() if key != "sources"}


def is_environment(value: object) -> bool:
    """Whether a value has the shape of an environment: a Python version and distributions with their versions."""
    if not isinstance(value, Mapping) or not isinstance(value.get("python"), str):
        return False
    dists = value.get("distributions")
    if not isinstance(dists, Mapping) or not all(isinstance(k, str) and isinstance(v, str) for k, v in dists.items()):
        return False
    return isinstance(value.get("sources", {}), Mapping)


def differences(recorded: object, current: Mapping[str, Any], *, sources: bool = True) -> list[str]:
    """Say how the current environment differs from a recorded one, one line per difference; empty when it does not.

    With `sources` false, only the Python version and the distributions' versions are compared, as in a replication
    package, whose environment is installed from its own wheels. A line never shows a URL or a commit.
    """
    if not is_environment(recorded):
        return ["no environment is recorded, or the record is not one"]
    assert isinstance(recorded, Mapping)
    lines = []
    if recorded["python"] != current["python"]:
        lines.append(f"Python {recorded['python']} recorded, Python {current['python']} in use")
    was, now = recorded["distributions"], current["distributions"]
    for name in sorted(set(was) | set(now)):
        if name not in now:
            lines.append(f"{name} {was[name]} recorded, not part of the environment in use")
        elif name not in was:
            lines.append(f"{name} {now[name]} in use, not recorded")
        elif was[name] != now[name]:
            lines.append(f"{name} {was[name]} recorded, {now[name]} in use")
    if sources:
        before, after = recorded.get("sources", {}), current.get("sources", {})
        lines += [
            f"{name} installed from another source than the one recorded"
            for name in sorted(set(before) | set(after))
            if before.get(name) != after.get(name)
        ]
    return lines
