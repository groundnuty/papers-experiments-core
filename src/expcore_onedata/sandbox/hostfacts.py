# Provenance: new
"""What a run records of the machine it ran on: its OS release, glibc and kernel (O87).

A study's agent runs code from an image on whatever machine a batch job or a Docker engine lands on, and the C
library, the kernel and the distribution of that machine decide what a preloaded shim or a bundled binary can load (a
node whose glibc is newer than the image's refuses the shim). A reader of a run's record wants to know which machine it
was, so each run's `infrastructure_facts` carries `node`, on the Apptainer backend (the batch node) and on Docker (the
host that runs the agent's container, the driver's own machine, as `host.kernel` is), in one shape and under one name so
that a comparison of two shards reads one place; the Apptainer backend adds `tool_server_host` when the tool server's
host differs from the node:

    {"os": {"id": "rocky", "version_id": "9.4"}, "glibc": "glibc 2.34", "kernel": "5.14.0-427.el9.x86_64"}

with the OS release's `ID` and `VERSION_ID`, the C library's own version string (`os.confstr("CS_GNU_LIBC_VERSION")`)
and the kernel's release (`os.uname().release`). They are recorded and never refused: a reading that cannot be made
is the string `unknown`, and `unknown` maps its name (`os.id`, `os.version_id`, `glibc`, `kernel`) to why, which is a
file's missing line, a value of no plain shape, or the type of the exception, never its message and never a path. The
key is there only when something is unknown.

The os-release file is read as text and never executed: it is a shell fragment, and sourcing one would run what a
hostile line holds. Only its first 64 KiB are read, a line `KEY=value` is split by hand, a value loses one pair of
quotes and must then be plain characters, and the last assignment of a key wins, as it would in a shell. A file longer
than that is read to its last whole line: the line the bound cut is no line that was read, so a value cut inside is
`unknown` ("too long") and never a prefix of itself, while the lines before it count as they would in a short file.

`deploy/hpc-federation/bundle/nodefacts.py` makes the same readings on each node of an HPC federation for the driver's
state, in a file of its own because it runs on a node's own Python with nothing of the core imported (3.9, standard
library only). The two are the same code and the same shape, and `tests/deploy/test_hpc_nodefacts.py` holds them to
the same answers.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from typing import Any

UNKNOWN = "unknown"
OS_RELEASE = "/etc/os-release"
READ_BYTES = 64 * 1024
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]{0,63}")  # an ID, a version or a release: short, plain characters
_GLIBC = re.compile(r"glibc [0-9][A-Za-z0-9._+~-]{0,31}")  # what `confstr("CS_GNU_LIBC_VERSION")` answers on glibc
_REASON = re.compile(r"[A-Za-z0-9._ -]{1,80}")  # a reason: an exception's type or a short phrase of ours, nothing else
_KEYS = ("os.id", "os.version_id", "glibc", "kernel")


def unreadable(reason: str) -> dict[str, Any]:
    """The readings of a node that could not be read at all: each `unknown` for `reason`."""
    return {
        "os": {"id": UNKNOWN, "version_id": UNKNOWN},
        "glibc": UNKNOWN,
        "kernel": UNKNOWN,
        "unknown": dict.fromkeys(_KEYS, reason),
    }


def _assignments(text: str) -> dict[str, str]:
    """`KEY=value` lines of an os-release text, the last assignment of a key winning; never evaluated."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        key, sign, value = line.strip().partition("=")
        if sign and key and not key.startswith("#"):
            found[key] = value.strip()
    return found


def _cut_line(head: bytes) -> tuple[bytes, str]:
    """`head`, the first `READ_BYTES` of a file that is longer, without the line the bound cut, and that line's text."""
    end = head.rfind(b"\n") + 1
    return head[:end], head[end:].decode("utf-8", "replace").strip()


def _may_be(fragment: str, key: str) -> bool:
    """Whether the line the bound cut (`fragment`) may have been an assignment of `key`: it names the key before an `=`,
    or ends inside the key's name, so the value that followed, or the one that was to, is not known."""
    name, sign, _ = fragment.partition("=")
    return bool(fragment) and (name.strip() == key if sign else key.startswith(name.strip()))


def _plain(value: str) -> str | None:
    """The value without one pair of quotes, or None when it is no plain value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value if _WORD.fullmatch(value) else None


def _os(path: str | os.PathLike[str], unknown: dict[str, str]) -> dict[str, str]:
    """The OS release's `ID` and `VERSION_ID`; what is not there is `unknown`, with its reason in `unknown`."""
    out = {"id": UNKNOWN, "version_id": UNKNOWN}
    try:
        with open(path, "rb") as stream:
            # one byte more than the bound tells a longer file from one that ends at it
            raw = stream.read(READ_BYTES + 1)
        cut = ""
        if len(raw) > READ_BYTES:
            raw, cut = _cut_line(raw[:READ_BYTES])
        text = raw.decode("utf-8")
        if "\x00" in text:
            raise UnicodeDecodeError("utf-8", raw, 0, 1, "a NUL byte")
    except UnicodeDecodeError:
        unknown["os.id"] = unknown["os.version_id"] = "not text"
        return out
    except Exception as error:  # an OSError of any kind: the file is absent, a directory, unreadable
        unknown["os.id"] = unknown["os.version_id"] = type(error).__name__
        return out
    assigned = _assignments(text)
    for field, key in (("id", "ID"), ("version_id", "VERSION_ID")):
        if _may_be(cut, key):
            unknown[f"os.{field}"] = "too long"
        elif key not in assigned:
            unknown[f"os.{field}"] = f"no {key} line"
        elif not assigned[key].strip("\"'"):
            unknown[f"os.{field}"] = "empty"
        elif (value := _plain(assigned[key])) is None:
            unknown[f"os.{field}"] = "not a plain value"
        else:
            out[field] = value
    return out


def _read(name: str, reader: Callable[[], Any], shape: re.Pattern[str], unknown: dict[str, str]) -> str:
    """One reading: a string of the shape, or `unknown` with why."""
    try:
        value = reader()
    except Exception as error:  # confstr raises ValueError off glibc, uname OSError: any failure is a reason
        unknown[name] = type(error).__name__
        return UNKNOWN
    if not isinstance(value, str) or not value.strip():
        unknown[name] = "no answer"
        return UNKNOWN
    if shape.fullmatch(value) is None:
        unknown[name] = "not a plain value"
        return UNKNOWN
    return value


def readings(
    *,
    os_release: str | os.PathLike[str] = OS_RELEASE,
    confstr: Callable[[str], Any] = os.confstr,
    uname: Callable[[], Any] = os.uname,
) -> dict[str, Any]:
    """This machine's readings, never raising. The three sources are arguments for a test to give another machine."""
    unknown: dict[str, str] = {}
    out = {
        "os": _os(os_release, unknown),
        "glibc": _read("glibc", lambda: confstr("CS_GNU_LIBC_VERSION"), _GLIBC, unknown),
        "kernel": _read("kernel", lambda: uname().release, _WORD, unknown),
    }
    return {**out, "unknown": dict(sorted(unknown.items()))} if unknown else out


def checked(found: object, *, reason: str = "not the expected shape") -> dict[str, Any]:
    """A node's readings as a node gave them (a fake's, another host's, a step's output), rebuilt from the known keys
    alone, or `unreadable(reason)` when they are no mapping of the shape; the values are plain strings of the same
    pattern, so a record never holds what a node chose to say."""
    if not isinstance(found, Mapping):
        return unreadable("not a mapping")
    os_part = found.get("os")
    reasons = found.get("unknown", {})
    if not isinstance(os_part, Mapping) or not isinstance(reasons, Mapping):
        return unreadable(reason)
    values = (os_part.get("id"), os_part.get("version_id"), found.get("glibc"), found.get("kernel"))
    shapes = (_WORD, _WORD, _GLIBC, _WORD)
    if not all(
        isinstance(v, str) and (v == UNKNOWN or shape.fullmatch(v)) for v, shape in zip(values, shapes, strict=True)
    ):
        return unreadable(reason)
    why = {k: v for k, v in reasons.items() if k in _KEYS and isinstance(v, str) and _REASON.fullmatch(v)}
    if len(why) != len(reasons) or {k for k, v in zip(_KEYS, values, strict=True) if v == UNKNOWN} != set(why):
        return unreadable(reason)
    out = {"os": {"id": values[0], "version_id": values[1]}, "glibc": values[2], "kernel": values[3]}
    return {**out, "unknown": dict(sorted(why.items()))} if why else out


def of_node(reader: Callable[[], Any]) -> dict[str, Any]:
    """What a node answers to a request for its readings, checked: a node whose reading raises, or answers in no known
    shape, is `unknown` with the exception's type, never a refusal."""
    try:
        return checked(reader())
    except Exception as error:
        return unreadable(type(error).__name__)


__all__ = ["OS_RELEASE", "UNKNOWN", "checked", "of_node", "readings", "unreadable"]
