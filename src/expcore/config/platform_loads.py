# Provenance: new
"""What the core's own platform loaders read while a study was built (C11, O35).

A study's plugin can hide what its objects declare (a wrapper that leaves out `max_deferral_s` or `declared_deferral_s`)
and leave the platform's module out of `[study] platforms`, so that nothing visible names the platform. It cannot build
a platform without the platform's loader running, and the loader is the core's: `expcore_onedata.config.load_platform`
and `fault_settings.load_faults` record, through `record_platform_load`, the module a study lists in `[study] platforms`
for the platform and the configuration file they read. The pipeline collects those records around the study's build
(`watching_platform_loads`) and hands them to the scheduler, which refuses a record of a module the study does not list
(`check_loads_listed`), naming the module.

- **Scoped to the build.** A record is kept only while a watch is open, and each watch starts empty, so a second study
  in one process (or a test) inherits nothing. A platform's own tooling (a day-0 tool, a federation script) loads the
  same files with no study build open and leaves nothing: it is no study start and is never refused.
- **The module and the path only.** A record holds the module's name and the file's path: no value of the file, no
  secret. A refusal names the file's own name, never its directory.
- **The process's, one build at a time.** A plugin that builds its platform in a worker thread of its own is seen: every
  loader that runs while a build is open records into it. Builds in two threads of one process run one after the other
  (the second waits for the first's end), so a build that loads nothing is never refused for another's platform. Watches
  of one thread may nest, each keeping its own record, and a file loaded again in one watch is one record.
- **A record changes nothing.** No loader returns anything different, and no record, fingerprint or state file holds it.

A platform layer other than the Onedata one calls `record_platform_load(module, path)` from its own loader, with the
module name its study lists in `[study] platforms` for it, the one whose `declared_deferral_from_config` the core calls.

The record covers the study's build: the plugin's import and its `build`, and what the pipeline derives from the
plugin before the scheduler starts. Outside it are a platform loaded after that (a health check that loads its
configuration at its first call, say), a `PlatformConfig` made by hand without the loader, and one that a module outside
the study's own code (an installed helper, which stays imported between builds) loaded in an earlier build of the same
process and retains: the later build runs no loader, so it records nothing.
"""

import os
import threading
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NamedTuple

from expcore.config.loading import ConfigError


class PlatformLoad(NamedTuple):
    """One run of a platform's loader: the module a study lists for the platform, and the file the loader read."""

    module: str
    config: Path


_LOCK = threading.Lock()
_BUILD = threading.RLock()  # held by the thread whose study build is open: one build at a time in a process
_WATCHES: dict[object, tuple[list[PlatformLoad], set[PlatformLoad]]] = {}  # each open watch: its record, and its set


def record_platform_load(module: str, config: str | os.PathLike[str]) -> None:
    """Record, in every watch now open, that a loader ran for `module` on the file `config`; do nothing when no study
    build is open. A platform's loader calls it at its entry, so a loader that then refuses its file still counts as
    run. Raise ValueError, naming the argument, for a module that is not a non-empty name or a file that is not a
    path."""
    if not isinstance(module, str) or not module:
        raise ValueError(f"record_platform_load: module must be the non-empty name a study lists, not {module!r}")
    if not isinstance(config, str | os.PathLike):
        raise ValueError(f"record_platform_load: config must be a path, not a {type(config).__name__}")
    load = PlatformLoad(module, Path(config))
    with _LOCK:
        for loads, seen in _WATCHES.values():
            if load not in seen:
                seen.add(load)
                loads.append(load)


@contextmanager
def watching_platform_loads() -> Iterator[list[PlatformLoad]]:
    """Collect, in order, the loads of the platform loaders that run in the block: the study's build. Each watch starts
    empty and keeps its own record, a list that the block's loaders append to as they run.

    One thread's build is open at a time: another thread's waits here until it ends, so every loader that runs while a
    build is open, a worker thread of the plugin's included, belongs to that build, and a build that loads nothing is
    never refused for another's platform. The same thread may open a watch inside its own (the core's reading of a
    listed platform inside the build), and both then see what runs inside the inner one."""
    token, loads = object(), []
    with _BUILD:
        with _LOCK:
            _WATCHES[token] = (loads, set())
        try:
            yield loads
        finally:
            with _LOCK:
                del _WATCHES[token]


def check_loads_listed(loads: Collection[PlatformLoad], listed: Collection[str]) -> None:
    """Refuse, with a ConfigError that names each module once, a study whose build ran a loader for a module `listed`
    (what `[study] platforms` lists, and what each listed module speaks for) does not hold. The refusal says to list the
    module, so the core reads the platform's own configuration for its declared deferral, and names the first file the
    loader read, by its name."""
    unlisted: dict[str, PlatformLoad] = {}
    for load in loads:
        if load.module not in listed:
            unlisted.setdefault(load.module, load)
    if not unlisted:
        return
    names = ", ".join(repr(module) for module in unlisted)
    first = next(iter(unlisted.values()))
    several = len(unlisted) > 1
    raise ConfigError(
        f"the study's build loaded the platform configuration {first.config.name!r} through the platform "
        f"module{'s' if several else ''} {names}, which [study] platforms does not list: "
        f"list {'them' if several else 'it'}, so the core reads the platform's own configuration for its declared "
        "deferral and holds the health check and max_defer_s to it, whatever the study's plugin wraps"
    )
