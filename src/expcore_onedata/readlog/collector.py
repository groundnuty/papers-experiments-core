# Provenance: new
"""collector.py: the receiving end of the read-log channel, run by the harness outside the agent's cell.

  python3 -I collector.py <socket dir> <log file> <nonce> <expected peer uid> [<max log bytes>]

A sibling of the egress collector (deploy/hpc-federation/bundle/egress-collector.py), and it imports nothing of it or
of this package: it is a single file that runs alone. Standard library only, Python 3.8 or later.

It binds `<socket dir>/sock`, a Unix datagram socket, in an empty directory of this uid, and writes every datagram it
receives to `<log file>`, a file it creates (0600, refused if it exists) outside every path the cell sees. It prints
`readlog collector ready` on stdout once it is listening. On SIGTERM or SIGINT it reads what is still queued, writes its
end line, removes the socket and exits 0. A datagram is one client line (`client.py`); the sender's pid and uid are
as the kernel attests them (SCM_CREDENTIALS). The reader of the log (`readlog.reader`) decides what an incomplete log
means, and the peer rule is the reader's: a record whose sender's uid is not `<expected peer uid>` (the driver's on
Apptainer, the declared agent uid on Docker) makes the log unknown.

The log is JSON lines, each with `seq`, `wall` and `mono`:
  {"event": "start", "format": "expcore-readlog/1", "nonce", "pid", "uid", "peer_uid": <expected>, "max_bytes"}
  {"event": "record", "peer": {"pid", "uid"}, "rec": {"v", "op", "item", ["range"], ["bytes"], ["sha256"]}}
  {"event": "malformed", "peer": {...}, "bytes": <length>, "sha256": <of the datagram>, "why"}
  {"event": "end", "records", "malformed", "unwritten", "cap_reached"}
Anything that is not a well-formed client line is a malformed line, kept only as its size and SHA-256: no byte of it
is written. Past the log's byte cap a datagram is only counted (`unwritten`), the end line says `cap_reached`, and
the log is not intact.
"""

import sys

FORMAT = "expcore-readlog/1"
SOCKET_NAME = "sock"
MAX_LINE_BYTES = 4096
MAX_ITEM_CHARS = 1024
MAX_LOG_BYTES = 48 * 1024 * 1024
END_RESERVE_BYTES = 4096


def main(argv):
    """Run the collector until SIGTERM or SIGINT; return the exit status (0, or 2 for a refused start)."""
    raise NotImplementedError


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
