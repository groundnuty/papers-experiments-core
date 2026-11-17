# Provenance: new
"""Which `[toolserver]` command and arguments name a place relative to the working directory (O84).

The tool server starts in a new, empty directory of its own for each start (`expcore.tools.workdir.private_workdir`),
so a path that the study's command or arguments give relative to the working directory, which once resolved against the
driver's, resolves to nothing there. `load_platform` refuses one by these functions, so the failure is at load, where
the study can change it, and not at the first launch.

The rule is lexical: nothing is looked up, since a platform file is loaded on hosts where the paths it names may not
exist. It refuses only what cannot be mistaken for something else, so no valid server is refused:

- a command with a directory part that does not begin at the root (`./bin/server`, `venv/bin/python`, `~/bin/x`); a bare
  name (`python3`, `uv`) is looked for on `PATH` and is never a place beside the working directory;
- an argument that is `.`, `..` or `~`, or begins `./`, `../` or `~/`, whole or as the value of an `--option=value`;
- the script of a Python server, its first argument when that is not an option, however it is spelt (`server.py`).

What it does not see, and a study must still name by an absolute path: a relative path with a directory part that does
not begin with `.` or `~` (`conf/server.toml` reads as well as `application/json`), a value attached to a short option
(`-C./x`), a path inside code given to `-c`, and the script of a Python started with an interpreter option first.
"""

import os
from collections.abc import Sequence

from expcore_onedata.addresses import is_python_interpreter

_HERE = (".", "..", "~")
_ANCHORS = ("./", "../", "~/")


def relative_command(command: str) -> bool:
    """Whether `command` is a path with a directory part that does not begin at the root."""
    return "/" in command and not os.path.isabs(command)


def relative_argument(command: str, args: Sequence[str]) -> int | None:
    """The position (from 0) of the first of `args` that names a place relative to the working directory, or None; a
    value is never returned or shown. `command` tells whether the first argument is a Python server's script."""
    script = 0 if args and is_python_interpreter(command) and not args[0].startswith("-") else None
    for position, argument in enumerate(args):
        if position == script:
            if not os.path.isabs(argument):
                return position
        elif _is_relative(_value(argument)):
            return position
    return None


def _value(argument: str) -> str:
    """The word of an argument that may name a place: the argument, or an option's value after its `=`."""
    return argument.partition("=")[2] if argument.startswith("-") else argument


def _is_relative(value: str) -> bool:
    return value in _HERE or value.startswith(_ANCHORS)


__all__ = ["relative_argument", "relative_command"]
