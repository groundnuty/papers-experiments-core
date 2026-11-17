# Provenance: new
"""expcore_readlog_client: the study scripts' end of the read-log channel. Standard library only, Python 3.8 or later.

The harness binds this file read-only into every cell at `/expcore-readlog-client/expcore_readlog_client.py`; a
canonical script imports it by that explicit path (cells run `python -I`, which ignores PYTHONPATH):

    import importlib.util as u
    path = "/expcore-readlog-client/expcore_readlog_client.py"
    spec = u.spec_from_file_location("expcore_readlog_client", path)
    readlog = u.module_from_spec(spec)
    spec.loader.exec_module(readlog)

    readlog.log_read("file-7", range=[0, 4096], bytes=4096, sha256="<of what is returned>")   # BEFORE returning data
    return data

**The contract of a script.** Log BEFORE returning data, and return NO data when the call raises: the log is then an
upper bound on what the script handed out, and an item the log lacks was not read. `ReadLogError` is raised for every
failure, so a script never carries on unlogged: the socket is missing (ENOENT), nothing listens (ECONNREFUSED, the
collector is dead), the collector's queue stays full past `SEND_TIMEOUT_S` (a timeout), the collector has stopped
reading (EPIPE), a field is not one the collector accepts, or the line is longer than `MAX_LINE_BYTES` (refused
before anything is sent). A send that returned only means the datagram is queued at the collector: a later loss makes
the run's log unknown (the reader's `intact` rules), never a run with fewer reads.

The target is fixed (`SOCKET`); there is no environment variable and no argument that names another socket.
"""

import json
import re
import socket

# The fixed names, repeated here because this file runs alone in the cell; `readlog.paths` holds the originals and a
# test holds the two equal.
SOCKET = "/expcore-readlog/sock"
LINE_VERSION = 1
MAX_LINE_BYTES = 4096
MAX_ITEM_CHARS = 1024
OPS = ("read", "canary")
SEND_TIMEOUT_S = 2.0

_SHA256 = re.compile(r"[0-9a-f]{64}")
_BIG = 2**63


class ReadLogError(Exception):
    """The read could not be logged. The caller must return no data: the read is "unread"."""


def _whole(value, low, high):
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def _check(op, item, range, bytes, sha256):
    """Refuse, with a `ReadLogError`, any field the collector would refuse: it would otherwise become a malformed
    line, which makes the run's whole log unknown."""
    if op not in OPS:
        raise ReadLogError("op must be one of " + ", ".join(OPS))
    if not isinstance(item, str) or not 1 <= len(item) <= MAX_ITEM_CHARS:
        raise ReadLogError(f"item is the study's id of what was read: a text of 1 to {MAX_ITEM_CHARS} characters")
    if range is not None:
        ok = isinstance(range, (list, tuple)) and len(range) == 2 and all(_whole(x, 0, _BIG) for x in range)
        if not ok or range[0] > range[1]:
            raise ReadLogError("range is [start, stop], two whole numbers with start not past stop")
    if bytes is not None and not _whole(bytes, 0, _BIG):
        raise ReadLogError("bytes is a whole number of 0 or more")
    if sha256 is not None and not (isinstance(sha256, str) and _SHA256.fullmatch(sha256)):
        raise ReadLogError("sha256 is 64 lower-case hexadecimal digits")


def encode(op, item, range=None, bytes=None, sha256=None):
    """The datagram for one record: ASCII JSON of at most `MAX_LINE_BYTES`. Raises `ReadLogError` for a bad field or
    a line that is too long; nothing is sent here."""
    _check(op, item, range, bytes, sha256)
    record = {"v": LINE_VERSION, "op": op, "item": item}
    if range is not None:
        record["range"] = [range[0], range[1]]
    if bytes is not None:
        record["bytes"] = bytes
    if sha256 is not None:
        record["sha256"] = sha256
    line = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii")  # ensure_ascii: ASCII
    if len(line) > MAX_LINE_BYTES:
        raise ReadLogError(f"the line is {len(line)} bytes, longer than the {MAX_LINE_BYTES} a line may hold")
    return line


def send(op, item, range=None, bytes=None, sha256=None):
    """Send ONE datagram for a record to the collector's socket; return None, or raise `ReadLogError` on any error."""
    line = encode(op, item, range, bytes, sha256)  # the cap and the fields are checked before a socket is made
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    except OSError as error:
        raise ReadLogError(f"cannot open a socket ({error.__class__.__name__})") from error
    try:
        sock.settimeout(SEND_TIMEOUT_S)
        sock.connect(SOCKET)
        sent = sock.send(line)
    except Exception as error:  # every failure, of any kind, is "unread"
        raise ReadLogError(f"the read could not be logged ({error.__class__.__name__})") from error
    finally:
        sock.close()
    if sent != len(line):
        raise ReadLogError(f"the datagram was cut: {sent} of {len(line)} bytes")


def log_read(item, range=None, bytes=None, sha256=None):
    """Log one read (`op` "read"): `item` is the study's id of what is returned (never a host path), `range` the
    `[start, stop]` of it (stop excluded), `bytes` how many bytes are returned and `sha256` their hash. Raises
    `ReadLogError`; the script then returns no data."""
    send("read", item, range, bytes, sha256)


def log_canary(item="canary"):
    """Log the harness's canary (`op` "canary"): the log's first record, made before the agent runs."""
    send("canary", item)


__all__ = ["SOCKET", "ReadLogError", "encode", "log_canary", "log_read", "send"]
