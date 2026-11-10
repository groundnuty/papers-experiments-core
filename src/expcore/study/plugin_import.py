# Provenance: new
"""How the core imports a study's plugin package and holds the code it runs to the study's code identity.

The plugin is imported as a package whose path is its own directory, named `expcore_study_plugin_` plus a hash of its
path (`study.plugin`), so a module beside it is imported relatively and named below that package. Three guarantees:

- **From the source.** Every module of the package is compiled from its file's bytes on each load, through
  `SourceOnlyLoader`, never from cached bytecode, which a same-size edit within one second leaves looking current. What
  runs is what the code identity hashed, and no bytecode is written into the study.
- **By location.** What counts is where code comes from, not its module name. While the plugin is imported and while
  its `build` runs, `watching` records every file whose code is executed (the interpreter's `exec` audit event), and
  `study_files` finds every module in `sys.modules` whose file lies in the study directory. Each such file must be one
  the code identity covers, and must be a module of the plugin's package: one imported through the module search path
  or loaded by path would be cached under a name any other study could be served, or run from stale bytecode.
- **Nothing left behind.** `forget` drops every module whose file lies in the study directory, whatever its name, and
  every module of the package, before an import and after a refused one.

Code that lives in the running environment (below `sys.prefix`, a virtual environment inside the study directory for
one) or below a directory the code identity skips is the environment's, not the study's. Only code is seen: a data
file the plugin reads by path is not, and is covered only if `[study] code` lists it. A module first imported while a
run is under way, after `build` returned, escapes the check.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from types import ModuleType
from typing import Any

PLUGIN_MODULE_PREFIX = "expcore_study_plugin_"  # the rest of a plugin's module name is a hash of its path


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
_RUN: list[set[str]] = []  # the files whose code ran, one set per watch in progress
_installed: list[bool] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    """Record the file of every code object executed while a watch is open."""
    if event != "exec" or not _RUN:
        return
    name = getattr(args[0], "co_filename", None) if args else None
    if isinstance(name, str) and os.path.isabs(name):
        with _LOCK:
            for files in _RUN:
                files.add(name)


def install() -> None:
    """Install the package finder and the audit hook, once for the process; neither acts outside a plugin's load."""
    with _LOCK:
        if _installed:
            return
        sys.meta_path.insert(0, _PackageFinder())
        sys.addaudithook(_audit)
        _installed.append(True)


@contextmanager
def watching() -> Iterator[set[str]]:
    """Collect the files whose code is executed in the block."""
    install()
    files: set[str] = set()
    with _LOCK:
        _RUN.append(files)
    try:
        yield files
    finally:
        with _LOCK:
            _RUN.remove(files)


@lru_cache(maxsize=4096)
def _real_dir(directory: str) -> str:
    return os.path.realpath(directory)


@lru_cache(maxsize=1)
def _environment() -> tuple[str, ...]:
    return tuple({os.path.realpath(p) for p in (sys.prefix, sys.base_prefix, sys.exec_prefix)})


def relative_in(file: str, root: str, skip_dirs: frozenset[str]) -> str | None:
    """The path of `file` relative to the study directory `root`, or None when the file is not the study's code: it
    lies outside, in the running environment, or below a directory the code identity skips."""
    absolute = os.path.abspath(file)
    resolved = os.path.join(_real_dir(os.path.dirname(absolute)), os.path.basename(absolute))
    if not resolved.startswith(root + os.sep):
        return None
    if any(resolved.startswith(prefix + os.sep) for prefix in _environment()):
        return None
    relative = os.path.relpath(resolved, root).replace(os.sep, "/")
    return None if set(relative.split("/")) & skip_dirs else relative


def study_files(root: str, skip_dirs: frozenset[str]) -> dict[str, str]:
    """Every module in `sys.modules` whose file is the study's code, by name, with that file relative to `root`."""
    found = {}
    for name, module in list(sys.modules.items()):
        file = getattr(module, "__file__", None)
        if isinstance(file, str):
            relative = relative_in(file, root, skip_dirs)
            if relative is not None:
                found[name] = relative
    return found


def forget(package: str, root: str, skip_dirs: frozenset[str]) -> None:
    """Drop the package's modules and every module whose file is the study's code, whatever it is called."""
    doomed = set(study_files(root, skip_dirs))
    doomed |= {name for name in list(sys.modules) if name == package or name.startswith(f"{package}.")}
    for name in doomed:
        sys.modules.pop(name, None)
