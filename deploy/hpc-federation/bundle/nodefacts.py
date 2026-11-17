"""nodefacts.py: one node's facts for the HPC federation driver, in a control step (python3.11 -I, stdlib only).

Prints one JSON object on stdout: the node's FQDN as `hostname -f` gives it (the name every service's test web
certificate is made for, `lib.sh`'s node_fqdn), its IPv4 address on that name (the address the shim's peer lists
hold), the step's uid and gid (the posix storage's owner, finish.sh), and its `readings` (O87): the node's OS release
(ID and VERSION_ID of /etc/os-release), its glibc version and its kernel release. The readings are recorded and never
refused: one that cannot be made is "unknown", and `unknown` maps its name to why (a missing line, a value of no plain
shape, or the type of the exception, never its message or a path). The os-release file is read as text and never
executed. No secret, and nothing is written.

The readings are the sandbox's own (`expcore_onedata.sandbox.hostfacts`, which the tests hold to the same
answers) in a copy of their own: this step runs on a node's own Python with `-I` and nothing of the core imported.

Usage: nodefacts.py
"""

import json
import os
import re
import socket
import subprocess
import sys

FQDN = re.compile(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
UNKNOWN = "unknown"
OS_RELEASE = "/etc/os-release"
READ_BYTES = 64 * 1024
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]{0,63}")
GLIBC = re.compile(r"glibc [0-9][A-Za-z0-9._+~-]{0,31}")
KEYS = ("os.id", "os.version_id", "glibc", "kernel")


def unreadable(reason):
    """The readings of a node that could not be read at all: each unknown for `reason`."""
    return {
        "os": {"id": UNKNOWN, "version_id": UNKNOWN},
        "glibc": UNKNOWN,
        "kernel": UNKNOWN,
        "unknown": dict.fromkeys(KEYS, reason),
    }


def assignments(text):
    """`KEY=value` lines of an os-release text, the last assignment of a key winning; never evaluated."""
    found = {}
    for line in text.splitlines():
        key, sign, value = line.strip().partition("=")
        if sign and key and not key.startswith("#"):
            found[key] = value.strip()
    return found


def cut_line(head):
    """`head`, the first READ_BYTES of a file that is longer, without the line the bound cut, and that line's text."""
    end = head.rfind(b"\n") + 1
    return head[:end], head[end:].decode("utf-8", "replace").strip()


def may_be(fragment, key):
    """Whether the line the bound cut (`fragment`) may have been an assignment of `key`: it names the key before an `=`,
    or ends inside the key's name, so the value that followed, or the one that was to, is not known."""
    name, sign, _ = fragment.partition("=")
    return bool(fragment) and (name.strip() == key if sign else key.startswith(name.strip()))


def plain(value):
    """The value without one pair of quotes, or None when it is no plain value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value if WORD.fullmatch(value) else None


def read_os(path, unknown):
    """The OS release's ID and VERSION_ID; what is not there is unknown, with its reason in `unknown`."""
    out = {"id": UNKNOWN, "version_id": UNKNOWN}
    try:
        with open(path, "rb") as stream:
            # one byte more than the bound tells a longer file from one that ends at it
            raw = stream.read(READ_BYTES + 1)
        cut = ""
        if len(raw) > READ_BYTES:
            raw, cut = cut_line(raw[:READ_BYTES])
        text = raw.decode("utf-8")
        if "\x00" in text:
            raise UnicodeDecodeError("utf-8", raw, 0, 1, "a NUL byte")
    except UnicodeDecodeError:
        unknown["os.id"] = unknown["os.version_id"] = "not text"
        return out
    except Exception as error:  # an OSError of any kind: the file is absent, a directory, unreadable
        unknown["os.id"] = unknown["os.version_id"] = type(error).__name__
        return out
    assigned = assignments(text)
    for field, key in (("id", "ID"), ("version_id", "VERSION_ID")):
        if may_be(cut, key):
            unknown["os." + field] = "too long"
        elif key not in assigned:
            unknown["os." + field] = "no " + key + " line"
        elif not assigned[key].strip("\"'"):
            unknown["os." + field] = "empty"
        else:
            value = plain(assigned[key])
            if value is None:
                unknown["os." + field] = "not a plain value"
            else:
                out[field] = value
    return out


def read_one(name, reader, shape, unknown):
    """One reading: a string of the shape, or unknown with why."""
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


def readings(os_release=OS_RELEASE, confstr=os.confstr, uname=os.uname):
    """This machine's readings, never raising. The three sources are arguments for a test to give another machine."""
    unknown = {}
    out = {
        "os": read_os(os_release, unknown),
        "glibc": read_one("glibc", lambda: confstr("CS_GNU_LIBC_VERSION"), GLIBC, unknown),
        "kernel": read_one("kernel", lambda: uname().release, WORD, unknown),
    }
    if unknown:
        out["unknown"] = dict(sorted(unknown.items()))
    return out


def readings_or_unknown():
    """The readings, or every one unknown for the type of what went wrong: the step's other facts are never lost."""
    try:
        return readings()
    except Exception as error:
        return unreadable(type(error).__name__)


def main() -> int:
    fqdn = subprocess.run(["hostname", "-f"], capture_output=True, text=True, check=True).stdout.strip()
    if not FQDN.fullmatch(fqdn):
        print("nodefacts: hostname -f gave no fully qualified name", file=sys.stderr)
        return 1
    found = sorted({info[4][0] for info in socket.getaddrinfo(fqdn, None, socket.AF_INET, socket.SOCK_STREAM)})
    if len(found) != 1:
        print(f"nodefacts: the node's name has {len(found)} IPv4 addresses, not one", file=sys.stderr)
        return 1
    facts = {
        "fqdn": fqdn,
        "address": found[0],
        "uid": os.getuid(),
        "gid": os.getgid(),
        "readings": readings_or_unknown(),
    }
    print(json.dumps(facts, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
