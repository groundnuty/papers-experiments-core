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
collector is dead), the collector's queue stays full past `SEND_TIMEOUT_S` (EAGAIN), or the line is longer than
`MAX_LINE_BYTES` (refused before anything is sent). A send that returned only means the datagram is queued at the
collector: a later loss makes the run's log unknown (the reader's `intact` rules), never a run with fewer reads.

The target is fixed (`SOCKET`); there is no environment variable and no argument that names another socket.
"""

# The fixed names, repeated here because this file runs alone in the cell; `readlog.paths` holds the originals and a
# test holds the two equal.
SOCKET = "/expcore-readlog/sock"
LINE_VERSION = 1
MAX_LINE_BYTES = 4096
MAX_ITEM_CHARS = 1024
OPS = ("read", "canary")
SEND_TIMEOUT_S = 2.0


class ReadLogError(Exception):
    """The read could not be logged. The caller must return no data: the read is "unread"."""


def encode(op, item, range=None, bytes=None, sha256=None):
    """The datagram for one record: ASCII JSON of at most `MAX_LINE_BYTES`. Raises `ReadLogError` for a bad field or
    a line that is too long; nothing is sent here."""
    raise NotImplementedError


def send(op, item, range=None, bytes=None, sha256=None):
    """Send ONE datagram for a record to the collector's socket; return None, or raise `ReadLogError` on any error."""
    raise NotImplementedError


def log_read(item, range=None, bytes=None, sha256=None):
    """Log one read (`op` "read"): `item` is the study's id of what is returned (never a host path), `range` the
    `[start, stop]` of it (stop excluded), `bytes` how many bytes are returned and `sha256` their hash. Raises
    `ReadLogError`; the script then returns no data."""
    raise NotImplementedError


def log_canary(item="canary"):
    """Log the harness's canary (`op` "canary"): the log's first record, made before the agent runs."""
    raise NotImplementedError


__all__ = ["SOCKET", "ReadLogError", "encode", "log_canary", "log_read", "send"]
