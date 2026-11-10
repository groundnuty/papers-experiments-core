# Provenance: new
"""Where an editable install's code lives, so that a wheel supplied for it can be compared with it (K10).

An editable install's installed files are a pointer to a checkout, so each member of the supplied wheel is compared
with the checkout's file it was built from, the first of these that exists:
1. the member's path below a directory the install's `.pth` puts on the path (or below its project directory, from
   `direct_url.json`, when the `.pth` names none);
2. the member's path below where its top-level package or module imports from (`importlib.util.find_spec`, without
   importing it), which finds a package behind an import hook (a `.pth` that runs `import ...`, as a src layout's
   `project/src/backend/` behind members `backend/...`); only a location inside the checkout counts;
3. the member's path through the mapping the project's `pyproject.toml` declares for its wheel (hatch's `sources` and
   `force-include`, for the wheel target or the whole build), which maps files of the checkout outside the package
   into it, as the core maps its example study into `expcore/examples/`; when the project declares no mapping, the
   member's path with its top-level package stripped, below the project directory.
A member found nowhere has nothing to compare with: a Python member is refused, and a data member goes in, listed.
Each wrong guess fails closed: a file found at a guessed path is either equal or refused.

The wheel must also hold every Python file the install imports: every one below the `.pth` directories (or, below a
project directory, under a package the wheel holds), and every one below where each of its top-level packages imports
from.
"""

import importlib.util
import json
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

from expcore.package.spec import PackageError
from expcore.provenance.environment import editable_roots, pth_directories, python_files


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


def _inside(path: Path, places: list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == place or place in resolved.parents for place in places)


@dataclass(frozen=True)
class Checkout:
    """An editable install's checkout: where its members are looked for, as described above."""

    roots: list[Path]
    whole: bool  # the roots are what the .pth puts on the path, not a project directory
    project: Path | None
    mapping: list[tuple[str, str]] | None

    def _places(self) -> list[Path]:
        return [p.resolve() for p in [*self.roots, *([self.project] if self.project else [])]]

    def located(self, top: str) -> tuple[list[Path], Path | None]:
        """Where a top-level package imports from (its directories) or a module (its file), inside the checkout."""
        if not top.isidentifier():
            return [], None
        try:
            spec = importlib.util.find_spec(top)
        except (ImportError, ValueError):
            return [], None  # not importable here: its members have nothing to be compared with
        if spec is None:
            return [], None
        places = self._places()
        if spec.submodule_search_locations is not None:
            return [Path(d) for d in spec.submodule_search_locations if _inside(Path(d), places)], None
        origin = Path(spec.origin) if spec.has_location and spec.origin else None
        return [], origin if origin is not None and origin.is_file() and _inside(origin, places) else None

    def counterpart(self, member: str) -> Path | None:
        """The checkout's file a member of the wheel was built from; None when there is none."""
        top, _, rest = member.partition("/")
        candidates = [root / member for root in self.roots]
        if rest:
            candidates += [d / rest for d in self.located(top)[0]]
        elif member.endswith(".py"):
            module = self.located(member.removesuffix(".py"))[1]
            candidates += [module] if module is not None else []
        if self.project is not None and self.mapping is not None:
            for dest, source in self.mapping:
                if not dest or member == dest or member.startswith(f"{dest}/"):
                    candidates.append(self.project / source / member[len(dest) :].lstrip("/"))
        elif self.project is not None and rest:
            candidates.append(self.project / rest)
        return next((path for path in candidates if path.is_file()), None)

    def expected(self, members: list[str]) -> list[str]:
        """The Python files the install imports, by their path in a wheel, which the wheel must hold."""
        tops = {m.split("/", 1)[0] for m in members if "/" in m}
        found = {p.relative_to(root).as_posix() for root in self.roots for p in python_files(root)}
        wanted = {path for path in found if self.whole or path.split("/", 1)[0] in tops}
        for top in tops | {m.removesuffix(".py") for m in members if "/" not in m and m.endswith(".py")}:
            dirs, module = self.located(top)
            wanted |= {f"{top}/{p.relative_to(d).as_posix()}" for d in dirs for p in python_files(d)}
            wanted |= {f"{top}.py"} if module is not None else set()
        return sorted(wanted)


def checkout_of(dist: metadata.Distribution) -> Checkout:
    """The checkout of an editable install."""
    project = _project(dist)
    return Checkout(editable_roots(dist), bool(pth_directories(dist)), project, _declared_mapping(project))


__all__ = ["Checkout", "checkout_of"]
