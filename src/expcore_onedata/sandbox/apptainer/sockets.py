# Provenance: new
"""Where a collector's Unix socket is, and how long the work root may be for it to fit (O56, O91).

A collector binds `<work_root>/<run id>/<socket dir>/sock`. A Unix socket's address holds 108 bytes, the last a NUL, so
the path is at most 107; a collector that cannot bind it ends before it says it is ready, with nothing said of why. The
bound is held in three places, each saying the length and the limit and never a path:

* at load, `work_root_problem`, ONLY where the platform pins the read-log collector (a platform that does not keeps
  loading as it did, whatever its work root): the work root may hold at most 107 minus a run id of its longest (57
  bytes) and the socket's own suffix, 31 bytes, so a plan or any other load of that platform fails at once;
* at a run's start, `socket_path_problem` with the run's own id: the backstop, and the read-log collector's refusal
  before anything exists (`layout.check_socket_path`);
* when the egress collector's start fails, `socket_path_problem` again, so the failure names the cause. The egress
  collector has no load-time bound: every platform has it, and its fake starts bind no socket.
"""

from __future__ import annotations

import os
from pathlib import Path

SOCKET_PATH_MAX_BYTES = 107  # a Unix socket's address holds 108 bytes, the last a NUL
SOCKET_NAME = "sock"  # both collectors bind this file in their directory (the egress collector's file has it fixed)
EGRESS_SOCK_DIR = "egress-sock"  # in the run's directory, 0700, empty, bound read-only in every cell
READLOG_SOCK_DIR = "readlog-sock"  # likewise, when the platform pins the read-log collector
RUN_KEY_CHARS = 48  # a run id starts with the lease key or the run key, made of ASCII and cut to this many
RUN_SUFFIX_CHARS = 8  # and ends with "-" and this many hex digits
RUN_ID_MAX_BYTES = RUN_KEY_CHARS + 1 + RUN_SUFFIX_CHARS  # 57


def socket_path_problem(path: Path, what: str) -> str | None:
    """What is wrong with a collector's socket path, or None when it fits a Unix socket's address: its length and the
    limit, never the path (`what` names the collector: "egress", "read-log")."""
    size = len(os.fsencode(path))
    if size <= SOCKET_PATH_MAX_BYTES:
        return None
    return (
        f"the {what} socket path is {size} bytes, over the {SOCKET_PATH_MAX_BYTES}-byte limit of a Unix socket: "
        "use a shorter [sandbox] work_root"
    )


def work_root_max_bytes(socket_dir: str) -> int:
    """The longest work root under which a run of the longest id has its socket in `socket_dir` within the limit."""
    return SOCKET_PATH_MAX_BYTES - 1 - RUN_ID_MAX_BYTES - len(f"/{socket_dir}/{SOCKET_NAME}")


def work_root_problem(size: int) -> str | None:
    """What is wrong with a work root of `size` bytes for a platform that pins the read-log collector, as the end of a
    sentence about the key, or None when the read-log socket fits under it at the worst case."""
    limit = work_root_max_bytes(READLOG_SOCK_DIR)
    if size <= limit:
        return None
    return (
        f"is {size} bytes, over the {limit} a run's read-log collector socket allows: "
        f"<work_root>/<run id of at most {RUN_ID_MAX_BYTES} bytes>/{READLOG_SOCK_DIR}/{SOCKET_NAME} must fit a Unix "
        f"socket's {SOCKET_PATH_MAX_BYTES} bytes, so use a shorter work_root"
    )


__all__ = [
    "EGRESS_SOCK_DIR",
    "READLOG_SOCK_DIR",
    "RUN_ID_MAX_BYTES",
    "RUN_KEY_CHARS",
    "RUN_SUFFIX_CHARS",
    "SOCKET_NAME",
    "SOCKET_PATH_MAX_BYTES",
    "socket_path_problem",
    "work_root_max_bytes",
    "work_root_problem",
]
