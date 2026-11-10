# Provenance: new
"""How the core imports a study's plugin package and holds the code it runs to the study's code identity.

The plugin is imported as a package whose path is its own directory, named `expcore_study_plugin_` plus a hash of its
path (`study.plugin`), so a module beside it is imported relatively and named below that package. Three guarantees:

- **From the source.** Every module of the package is compiled from its file's bytes on each load, through
  `SourceOnlyLoader`, never from cached bytecode, which a same-size edit within one second leaves looking current. What
  runs is what the code identity hashed, and no bytecode is written into the study.
- **By location.** What counts is where code comes from, not its module name. While the plugin is imported and while
  its `build` runs, `watching` records every file whose code is executed (the interpreter's `exec` audit event), and
  `Locator.modules` finds every module in `sys.modules` whose file is the study's own code. Each such file must be one
  the code identity covers, and must be a module of the plugin's package: one imported through the module search path
  or loaded by path would be cached under a name any other study could be served, or run from stale bytecode.
- **Nothing left behind.** `forget` drops every module of the study's own code, whatever its name, and every module of
  the package, before an import and after a refused one.

The study's own code is a file inside the study directory that the recorded environment does not cover: not the core's
own packages, not the running environment (below `sys.prefix`, a virtual environment inside the study directory for
one), not an installed distribution's code, editable or not (`environment.owns_file`, as the environment record finds
it), and not below a directory the code identity skips. So a study at the root of a repository that holds the core, or
one that vendors an editable dependency, neither refuses that code nor drops it, and this module, whose audit hook
cannot be removed, is never dropped and never installs a second one. Only code is seen: a data file the plugin reads by
path is not, and is covered only if `[study] code` lists it. A module first imported while a run is under way, after
`build` returned, escapes the check.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from importlib import metadata
from pathlib import Path
from types import ModuleType
from typing import Any

from expcore.provenance.environment import editable_roots, installed, is_editable, owns_file

PLUGIN_MODULE_PREFIX = "expcore_study_plugin_"  # the rest of a plugin's module name is a hash of its path
CORE_PACKAGES = ("expcore", "expcore_onedata")  # the core's own packages: the environment's code, never the study's


class SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    """Compile a module from its file's bytes every time: never read cached bytecode, never write it."""

    def get_code(self, fullname: str) -> Any:
        path = self.get_filename(fullname)
        return self.source_to_code(self.get_data(path), path)


class _PackageFinder(importlib.abc.MetaPathFinder):
    """Find the modules below a plugin package where the package's path says, and load each from its source."""

    def find_spec(self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None) -> Any:
        if not (fullname.startswith(PLUGIN_MODULE_PREFIX) and "." in fullname):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader) or spec.origin is None:
            return spec  # not found, a namespace directory, or a compiled extension: as Python finds it
        loader = SourceOnlyLoader(fullname, spec.origin)
        locations = spec.submodule_search_locations
        return importlib.util.spec_from_file_location(
            fullname, spec.origin, loader=loader, submodule_search_locations=locations
        )


_LOCK = threading.RLock()  # re-entrant: the audit hook may fire while it is held
_RUN: dict[object, set[str]] = {}  # the files whose code ran, by a token of each watch in progress
_installed: list[bool] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    """Record the file of every code object executed while a watch is open."""
    if event != "exec" or not _RUN:
        return
    name = getattr(args[0], "co_filename", None) if args else None
    if isinstance(name, str) and os.path.isabs(name):
        with _LOCK:
            for files in _RUN.values():
                files.add(name)


def install() -> None:
    """Install the package finder and the audit hook, once for the process; neither acts outside a plugin's load.

    An audit hook cannot be removed, so this module is never dropped from the process (`forget` keeps the core's
    modules), and a second hook is never added.
    """
    with _LOCK:
        if _installed:
            return
        sys.meta_path.insert(0, _PackageFinder())
        sys.addaudithook(_audit)
        _installed.append(True)


@contextmanager
def watching() -> Iterator[set[str]]:
    """Collect the files whose code is executed in the block; watches may overlap, each keeps its own record."""
    install()
    token, files = object(), set()
    with _LOCK:
        _RUN[token] = files
    try:
        yield files
    finally:
        with _LOCK:
            del _RUN[token]


@lru_cache(maxsize=4096)
def _real_dir(directory: str) -> str:
    return os.path.realpath(directory)


def resolved(file: str) -> str:
    """A file's path with its directory's links resolved."""
    absolute = os.path.abspath(file)
    return os.path.join(_real_dir(os.path.dirname(absolute)), os.path.basename(absolute))


def _inside(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + os.sep)


@lru_cache(maxsize=1)
def _environment() -> tuple[str, ...]:
    """The running environment's directories and the core's own packages: code the environment record covers."""
    found = {os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)}
    for name in CORE_PACKAGES:
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            continue
        if spec is not None:
            found |= {os.path.realpath(d) for d in spec.submodule_search_locations or []}
    return tuple(sorted(found))


class _Installed:
    """Whether a file of the study directory is an installed distribution's code, editable or not, so that the recorded
    environment covers it rather than `[study] code`; the distributions are read once, when first asked.

    A distribution installed normally owns exactly the files its RECORD lists. An editable one owns the files below one
    of its import roots (the directories its `.pth` puts on the path, else its project) only when that root lies
    strictly inside the study directory, as a dependency vendored in the study does. A root that is the study directory,
    or holds it, is the study installed as a project of its own: its files are the study's code, never an install's,
    whatever names or paths would suggest otherwise."""

    def __init__(self, root: str) -> None:
        self.root = root
        self._dists: list[tuple[metadata.Distribution, list[str]]] | None = None

    def _all(self) -> list[tuple[metadata.Distribution, list[str]]]:
        if self._dists is None:
            self._dists = []
            for dist in installed().values():
                if not is_editable(dist):
                    self._dists.append((dist, []))
                    continue
                roots = [os.path.realpath(r) for r in editable_roots(dist)]
                vendored = [r for r in roots if _inside(r, self.root) and r != self.root]
                self._dists.append((dist, vendored))
        return self._dists

    def owns(self, path: str) -> bool:
        file = Path(path)
        for dist, vendored in self._all():
            if not is_editable(dist):
                site = os.path.realpath(str(dist.locate_file("")))
                if _inside(path, site) and owns_file(dist, file, ""):
                    return True
            elif any(_inside(path, root) and path != root for root in vendored):
                return True
        return False


@dataclass(frozen=True)
class StudyCode:
    """Code of the study directory: its resolved path, and that path relative to the study directory."""

    path: str
    relative: str


class Locator:
    """Which files are the study's own code: inside the study directory, outside the running environment and the
    core, below no directory the code identity skips, and not an installed distribution's."""

    def __init__(self, root: str, skip_dirs: frozenset[str]) -> None:
        self.root, self.skip_dirs, self._installed = root, skip_dirs, _Installed(root)

    def locate(self, file: str, name: str | None = None, *, own: bool = False) -> StudyCode | None:
        """The study code a file is, or None. `own`: a module of the plugin's package, the study's by definition."""
        path = resolved(file)
        if not _inside(path, self.root) or path == self.root:
            return None
        if any(_inside(path, place) for place in _environment()):
            return None
        relative = os.path.relpath(path, self.root).replace(os.sep, "/")
        if set(relative.split("/")) & self.skip_dirs:
            return None
        if not own and self._installed.owns(path):
            return None
        return StudyCode(path, relative)

    def modules(self, package: str) -> dict[str, StudyCode]:
        """Every module in `sys.modules` whose file is the study's code, by name. The core's modules never are."""
        found = {}
        for name, module in list(sys.modules.items()):
            file = getattr(module, "__file__", None)
            if not isinstance(file, str) or name.split(".", 1)[0] in CORE_PACKAGES:
                continue
            code = self.locate(file, name, own=in_package(name, package))
            if code is not None:
                found[name] = code
        return found


def in_package(name: str, package: str) -> bool:
    """Whether a module name is the plugin's package or below it."""
    return name == package or name.startswith(f"{package}.")


def forget(package: str, root: str, skip_dirs: frozenset[str]) -> None:
    """Drop the package's modules and every module whose file is the study's code, whatever it is called; never a
    module of the core or of an installed distribution."""
    doomed = set(Locator(root, skip_dirs).modules(package))
    doomed |= {name for name in list(sys.modules) if in_package(name, package)}
    for name in doomed:
        sys.modules.pop(name, None)
