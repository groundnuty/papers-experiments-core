# Provenance: new
"""The read-log channel's fixed names, in one place. Every lane (Apptainer, Docker, the scan) imports them from here.

A study's canonical scripts run in the agent's code tool, inside the sandbox cell, and log every read they make to a
collector the harness runs outside the cell. The channel's targets are FIXED: there is no environment variable, so an
agent's own code cannot point a canonical script at a collector of its making. Both targets are bound READ-ONLY into
every cell, and a Unix datagram socket takes `sendto` on a read-only bind (the egress shim depends on the same).

    /expcore-readlog/sock                                 the collector's datagram socket
    /expcore-readlog-client/expcore_readlog_client.py     the client module (stdlib only), imported by explicit path

The client module and the collector program are single files that run without this package (the cell holds only the
file, the collector runs as `python3 -I collector.py`), so each repeats the few constants it needs; a test holds those
copies equal to the ones here.
"""

from typing import Final

# --- the cell's targets, harness-owned (added to the Apptainer settings' OWN_TARGETS and to `sandbox.inputs.TAKEN`)
SOCKET_DIR: Final = "/expcore-readlog"  # bound read-only; holds the socket
SOCKET_NAME: Final = "sock"
SOCKET: Final = f"{SOCKET_DIR}/{SOCKET_NAME}"  # a Unix datagram socket
CLIENT_DIR: Final = "/expcore-readlog-client"  # bound read-only; holds the client module
CLIENT_MODULE: Final = "expcore_readlog_client"  # the module's name, as a script imports it by explicit path
CLIENT_NAME: Final = f"{CLIENT_MODULE}.py"
CLIENT: Final = f"{CLIENT_DIR}/{CLIENT_NAME}"
TARGETS: Final = (SOCKET_DIR, CLIENT_DIR)  # what a study may neither bind nor name as an input target

# --- the log
FORMAT: Final = "expcore-readlog/1"  # the schema of a collector log line (`start.format`)
LINE_VERSION: Final = 1  # `v` of a client line
MAX_LINE_BYTES: Final = 4096  # a client line's most: ASCII, refused by the client before sending when longer
OPS: Final = ("read", "canary")  # `op` of a client line
CANARY_ITEM: Final = "canary"  # the `item` of the harness's canary record
MAX_ITEM_CHARS: Final = 1024  # the longest `item`
# The collector writes at most this many bytes of log, its end line included (the end line goes in the
# END_RESERVE_BYTES the cap holds back); past it a datagram is only counted (`unwritten`) and the end line says
# `cap_reached`. The reader's default limit is this, and the side-file cap (272 MiB) is above it, so a log the collector
# wrote is never refused for its size. It was 48 MiB.
MAX_LOG_BYTES: Final = 256 * 1024 * 1024
END_RESERVE_BYTES: Final = 4096  # room kept for the end line

# --- the run's record
NAMESPACE: Final = "read_log"  # the instrument's name: `record.instruments["read_log"]`
SIDE_FILE_NAME: Final = "read_log.jsonl"  # the conventional `pattern` of the side file a study declares for the log
SIDE_HASH_FIELD: Final = f"instruments.{NAMESPACE}.side_sha256"  # the declared `hash_field` of that side file
FACTS_OWNER: Final = "readlog"  # the owner of the run's read-log fact in `infrastructure_facts` (`infrafacts`, O89)
FACT_KEY: Final = "read_log"

__all__ = [
    "CANARY_ITEM",
    "CLIENT",
    "CLIENT_DIR",
    "CLIENT_MODULE",
    "CLIENT_NAME",
    "END_RESERVE_BYTES",
    "FACTS_OWNER",
    "FACT_KEY",
    "FORMAT",
    "LINE_VERSION",
    "MAX_ITEM_CHARS",
    "MAX_LINE_BYTES",
    "MAX_LOG_BYTES",
    "NAMESPACE",
    "OPS",
    "SIDE_FILE_NAME",
    "SIDE_HASH_FIELD",
    "SOCKET",
    "SOCKET_DIR",
    "SOCKET_NAME",
    "TARGETS",
]
