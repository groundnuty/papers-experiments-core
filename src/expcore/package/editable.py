# Provenance: new
"""Where an editable install's code lives, so that a wheel supplied for it can be compared with it (K10).

An editable install's installed files are a pointer to a checkout, so each member of the supplied wheel is compared
with the checkout's file it was built from. Where the install imports from comes first, and the first file that
exists there is the counterpart:
1. the member's path below a directory the install's `.pth` puts on the path;
2. the member's path below where its top-level package or module imports from (`top_level_location`, by
   `importlib.util.find_spec` without importing it), which finds a package behind an import hook (a `.pth` that runs
   `import ...`, as a src layout's `project/src/backend/` behind members `backend/...`); only a location inside the
   checkout counts.
Only a member with no counterpart there is looked for further, so a stale copy elsewhere in the checkout never stands
in for what is imported: below the project directory (from `direct_url.json`) when the `.pth` names no directory, then
through the mapping the project's `pyproject.toml` declares for its wheel (hatch's `sources` and `force-include`, for
the wheel target or the whole build), which maps files of the checkout outside the package into it, as the core maps
its example study into `expcore/examples/`; when the project declares no mapping, the member's path with its
top-level package stripped, below the project directory. A member found nowhere has nothing to compare with: a
Python member is refused, and a data member goes in, listed. Each wrong guess fails closed: a file found at a guessed
path is either equal or refused.

The wheel must also hold every Python file the install imports, directory links followed: every one below the `.pth`
directories, and every one below where each of its top-level packages imports from.
"""

import json
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from expcore.package.spec import PackageError
from expcore.provenance.environment import pth_directories, python_files, top_level_location


def _project(dist: metadata.Distribution) -> Path | None:
    """The project directory an editable install was made from, from its `direct_url.json`; None when unknown."""
    try:
        url = str(json.loads(dist.read_text("direct_url.json") or "{}").get("url", ""))
    except (ValueError, AttributeError):
        return None
    if urlsplit(url).scheme != "file":
        return None
    project = Path(url2pathname(urlsplit(url).path))
    return project if project.is_dir() else None


def _declared_mapping(project: Path | None) -> list[tuple[str, str]] | None:
    """The (wheel path, checkout path) pairs the project's pyproject.toml declares, longest wheel path first; None
    when it declares none."""
    path = project / "pyproject.toml" if project is not None else None
    if path is None or not path.is_file():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PackageError(f"{path}: cannot be read ({type(error).__name__}); its wheel's layout is unknown") from None
    build = data.get("tool", {}).get("hatch", {}).get("build", {})
    pairs: list[tuple[str, str]] = []
    for table in (build.get("targets", {}).get("wheel", {}), build):
        sources = table.get("sources")
        if isinstance(sources, dict):
            pairs += [(str(dest), str(source)) for source, dest in sources.items()]
        elif isinstance(sources, list):
            pairs += [("", str(source)) for source in sources]
        forced = table.get("force-include")
        if isinstance(forced, dict):
            pairs += [(str(dest), str(source)) for source, dest in forced.items()]
    if not pairs:
        return None
    cleaned = [(dest.strip("/"), source.strip("/")) for dest, source in pairs]
    return sorted(cleaned, key=lambda pair: -len(pair[0]))


@dataclass(frozen=True)
class Checkout:
    """An editable install's checkout: where its members are looked for, as described above."""

    pth: list[Path]  # the directories the install's .pth puts on the path
    project: Path | None
    mapping: list[tuple[str, str]] | None

    def located(self, top: str) -> tuple[list[Path], Path | None]:
        """Where a top-level package imports from (its directories) or a module (its file), inside the checkout."""
        return top_level_location(top, [*self.pth, *([self.project] if self.project else [])])

    def counterpart(self, member: str) -> Path | None:
        """The checkout's file a member of the wheel was built from; None when there is none."""
        top, _, rest = member.partition("/")
        imported = [d / member for d in self.pth]
        if rest:
            imported += [d / rest for d in self.located(top)[0]]
        elif member.endswith(".py"):
            module = self.located(member.removesuffix(".py"))[1]
            imported += [module] if module is not None else []
        found = next((path for path in imported if path.is_file()), None)
        return found if found is not None else self._elsewhere(member)

    def _elsewhere(self, member: str) -> Path | None:
        """A member's file outside where the install imports from: at the project root, or mapped in by the build."""
        if self.project is None:
            return None
        rest = member.partition("/")[2]
        candidates = [] if self.pth else [self.project / member]
        if self.mapping is not None:
            for dest, source in self.mapping:
                if not dest or member == dest or member.startswith(f"{dest}/"):
                    candidates.append(self.project / source / member[len(dest) :].lstrip("/"))
        elif rest:
            candidates.append(self.project / rest)
        return next((path for path in candidates if path.is_file()), None)

    def expected(self, members: list[str]) -> list[str]:
        """The Python files the install imports, by their path in a wheel, which the wheel must hold."""
        wanted = {p.relative_to(d).as_posix() for d in self.pth for p in python_files(d)}
        tops = {m.split("/", 1)[0] for m in members if "/" in m}
        for top in tops | {m.removesuffix(".py") for m in members if "/" not in m and m.endswith(".py")}:
            dirs, module = self.located(top)
            wanted |= {f"{top}/{p.relative_to(d).as_posix()}" for d in dirs for p in python_files(d)}
            wanted |= {f"{top}.py"} if module is not None else set()
        return sorted(wanted)


def checkout_of(dist: metadata.Distribution) -> Checkout:
    """The checkout of an editable install."""
    project = _project(dist)
    return Checkout(pth_directories(dist), project, _declared_mapping(project))


__all__ = ["Checkout", "checkout_of"]
