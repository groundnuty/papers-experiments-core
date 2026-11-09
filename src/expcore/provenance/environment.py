# Provenance: new
"""The environment a campaign runs in: which distributions, at which versions, from which sources (#45, #60).

    {"python": "3.12",
     "distributions": {"numpy": "2.5.4", "papers-experiments-core": "0.1.0", ...},
     "sources": {"papers-experiments-core": {"kind": "vcs", "vcs": "git", "url": "https://...", "commit": "..."}}}

The distributions are the core's and those the study names in `[study] requires` (a platform package, a backend),
with everything they require, transitively: the closure of their `Requires-Dist` over what is installed, which is
what the study's code can import through its dependencies. A requirement behind an extra counts only when that extra
was asked for; any other marker is not evaluated, and a requirement that is not installed is left out, since the
installer decided it did not apply. Tools installed beside the study and not required by it (a test runner, a
linter, the installer itself) are not part of it, so a reader's fresh environment holds exactly these.

`sources` names, for each distribution installed from a direct URL rather than an index (PEP 610 `direct_url.json`),
where it came from: a version-control URL and commit, an archive URL and hash, or a local directory. A local path is
never kept (it names the machine), nor are credentials, a query or a fragment of a URL. A distribution installed from
an index has no entry. An anonymised replication package drops `sources` altogether.

The campaign fingerprint holds the environment, so a resume under another one is refused (C06). The replay check
records the environment it ran under, refused unless it is the campaign's, and the analysis refuses a check made
under another environment than the one in use (R04). A replication package compares names and versions only: it is
installed from the wheels it ships, which have no direct-URL source.
"""

import json
import re
import sys
from collections.abc import Iterable, Mapping
from importlib import metadata
from typing import Any
from urllib.parse import urlsplit, urlunsplit

CORE_DISTRIBUTION = "papers-experiments-core"
_REQUIREMENT = re.compile(r"\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)\s*(?:\[([^\]]*)\])?")
_ROOT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:\[[A-Za-z0-9._,\s-]*\])?")
_EXTRA = re.compile(r"""\bextra\s*==\s*['"]([^'"]+)['"]""")


class EnvironmentUnknown(RuntimeError):
    """A distribution the environment needs is not installed, or its metadata cannot be read."""


def canonical(name: str) -> str:
    """A distribution name as PEP 503 normalises it: lower case, runs of '-', '_' and '.' as one '-'."""
    return re.sub(r"[-_.]+", "-", name).lower()


def is_requirement_name(text: object) -> bool:
    """Whether a text names a distribution, with optional extras and no version: what `[study] requires` holds."""
    return isinstance(text, str) and _ROOT.fullmatch(text) is not None


def _parse(requirement: str) -> tuple[str, frozenset[str], frozenset[str]] | None:
    """A requirement's name, the extras it asks for, and the extras its marker is conditional on; None if unreadable."""
    head, _, marker = requirement.partition(";")
    found = _REQUIREMENT.match(head)
    if found is None:
        return None
    asked = frozenset(canonical(e) for e in (found.group(2) or "").split(",") if e.strip())
    return canonical(found.group(1)), asked, frozenset(canonical(e) for e in _EXTRA.findall(marker))


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
    return {"kind": "directory", "editable": editable}


def environment(requires: Iterable[str] = (), *, found: Mapping[str, metadata.Distribution] | None = None) -> dict:
    """The environment of the core and the distributions `requires` names, as described above.

    Raises EnvironmentUnknown when the core or a required root is not installed.
    """
    dists = installed() if found is None else found
    queue: list[tuple[str, frozenset[str]]] = []
    for root in (CORE_DISTRIBUTION, *requires):
        parsed = _parse(root)
        if parsed is None or parsed[0] not in dists:
            raise EnvironmentUnknown(
                f"{root!r} is not installed, so the environment it belongs to cannot be recorded; install it"
            )
        queue.append((parsed[0], parsed[1]))
    asked: dict[str, set[str]] = {}
    while queue:
        name, extras = queue.pop()
        if name in asked and extras <= asked[name]:
            continue
        asked.setdefault(name, set()).update(extras)
        for requirement in dists[name].requires or []:
            parsed = _parse(requirement)
            if parsed is None or parsed[0] not in dists:
                continue  # not installed: the installer judged that its marker did not apply
            if parsed[2] and not parsed[2] & asked[name]:
                continue  # behind an extra nobody asked for
            queue.append((parsed[0], parsed[1]))
    versions = {name: str(dists[name].version) for name in sorted(asked)}
    sources = {name: source for name in sorted(asked) if (source := source_of(dists[name])) is not None}
    python = f"{sys.version_info.major}.{sys.version_info.minor}"
    return {"python": python, "distributions": versions, "sources": sources}


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
