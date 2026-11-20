# Provenance: new
"""collector.py: the receiving end of the read-log channel, run by the harness outside the agent's cell.

  python3 -I collector.py <socket dir> <log file> <nonce> <expected peer uid> [<max log bytes>]

A sibling of the egress collector (deploy/hpc-federation/bundle/egress-collector.py), and it imports nothing of it or
of this package: it is a single file that runs alone. Standard library only, Python 3.8 or later.

It binds `<socket dir>/sock`, a Unix datagram socket, in an empty directory of this uid, and writes every datagram it
receives to `<log file>`, a file it creates (0600, refused if it exists) outside every path the cell sees. It prints
`readlog collector ready` on stdout once it is listening. On SIGTERM or SIGINT it stops receiving (a send then fails,
so a call that succeeded was queued before), reads what is still queued, writes its end line, removes the socket and
exits 0. A datagram is one client line (`client.py`); the sender's pid and uid are as the kernel attests them
(SCM_CREDENTIALS). The reader of the log (`readlog.reader`) decides what an incomplete log means, and the peer rule
is the reader's: a record whose sender's uid is not `<expected peer uid>` (the driver's on Apptainer, the declared
agent uid on Docker) makes the log unknown. When that uid is this process's own, the socket directory is 0700 and the
socket owner-only; for another uid the directory is 0755 and the socket 0666, so that sender can connect.

The log is JSON lines, each with `seq`, `wall` and `mono` (this process's receive time):
  {"event": "start", "format": "expcore-readlog/1", "nonce", "pid", "uid", "peer_uid": <expected>, "max_bytes"}
  {"event": "record", "peer": {"pid", "uid"}, "rec": {"v", "op", "item", ["range"], ["bytes"], ["sha256"]}}
  {"event": "malformed", "peer": {...}, "bytes": <length>, "sha256": <of the datagram>, "why"}
  {"event": "end", "records", "malformed", "unwritten", "cap_reached"}
A record is the client's line parsed field by field; anything else, a forged or damaged datagram, is a malformed line,
kept only as its size and SHA-256: no byte of it is written. Past the log's byte cap a datagram is only counted
(`unwritten`), the end line says `cap_reached`, and the log is not intact.
"""

import hashlib
import json
import os
import re
import signal
import socket
import stat
import struct
import sys
import time

# The fixed names, repeated because this file runs alone; `readlog.paths` holds the originals and a test holds the two
# equal.
FORMAT = "expcore-readlog/1"
SOCKET_NAME = "sock"
LINE_VERSION = 1
MAX_LINE_BYTES = 4096
MAX_ITEM_CHARS = 1024
OPS = ("read", "canary")
MAX_LOG_BYTES = 256 * 1024 * 1024
END_RESERVE_BYTES = 4096

MIN_LOG_BYTES = 8192  # the least a cap may be: the start line, the end line and their reserve fit
READY = "readlog collector ready"
NONCE = re.compile(r"[0-9a-f]{16,128}")
NUMBER = re.compile(r"(0|[1-9][0-9]{0,18})")
SHA256 = re.compile(r"[0-9a-f]{64}")
UCRED = struct.Struct("iII")
REQUIRED = frozenset({"v", "op", "item"})
ALLOWED = REQUIRED | {"range", "bytes", "sha256"}
BIG = 2**63


def refuse(message):
    print("readlog-collector: ERROR: " + message, file=sys.stderr)
    sys.exit(2)


def whole(value, low, high):
    return not isinstance(value, bool) and isinstance(value, int) and low <= value <= high


def number(text, name, low, high):
    if not NUMBER.fullmatch(text) or not low <= int(text) <= high:
        refuse(f"{name} must be a whole number of {low} to {high}")
    return int(text)


def check_arguments(argv):
    """The validated arguments: (socket dir, log file, nonce, expected peer uid, max log bytes)."""
    if len(argv) not in (4, 5):
        refuse("usage: collector.py <socket dir> <log file> <nonce> <expected peer uid> [<max log bytes>]")
    sock_dir, log, nonce = argv[0], argv[1], argv[2]
    if not NONCE.fullmatch(nonce):
        refuse("the nonce must be 16 to 128 lower-case hex digits")
    peer_uid = number(argv[3], "the expected peer uid", 0, 2**32 - 1)
    max_bytes = number(argv[4], "the max log bytes", MIN_LOG_BYTES, MAX_LOG_BYTES) if len(argv) == 5 else MAX_LOG_BYTES
    try:
        st = os.lstat(sock_dir)
    except OSError:
        refuse("no socket directory at " + sock_dir)
    mode = 0o700 if peer_uid == os.getuid() else 0o755
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != mode:
        refuse(f"{sock_dir} must be a 0{mode:o} directory of this uid (the expected peer uid is {peer_uid})")
    if os.listdir(sock_dir):
        refuse(sock_dir + " must be empty")
    return sock_dir, log, nonce, peer_uid, max_bytes


def _pairs(pairs):
    keys = [key for key, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("a repeated key")
    return dict(pairs)


def _no_constant(name):
    raise ValueError("a JSON constant: " + name)


def _no_float(text):
    raise ValueError("a number that is no whole number: " + text)


def _valid(rec):
    """True when `rec` is a client line's object: the fields, their types and ranges."""
    if not isinstance(rec, dict) or not REQUIRED <= set(rec) <= ALLOWED:
        return False
    if not whole(rec["v"], LINE_VERSION, LINE_VERSION) or rec["op"] not in OPS:
        return False
    item = rec["item"]
    if not isinstance(item, str) or not 1 <= len(item) <= MAX_ITEM_CHARS:
        return False
    if "range" in rec:
        span = rec["range"]
        if not isinstance(span, list) or len(span) != 2 or not all(whole(x, 0, BIG) for x in span) or span[0] > span[1]:
            return False
    if "bytes" in rec and not whole(rec["bytes"], 0, BIG):
        return False
    return "sha256" not in rec or (isinstance(rec["sha256"], str) and SHA256.fullmatch(rec["sha256"]) is not None)


def parse(data, truncated):
    """(record, None) for a client line, or (None, why) for a datagram that is none; never raises on its content."""
    if truncated or len(data) > MAX_LINE_BYTES:
        return None, "too_long"
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return None, "not_ascii"
    try:
        rec = json.loads(text, object_pairs_hook=_pairs, parse_constant=_no_constant, parse_float=_no_float)
    except (ValueError, RecursionError):
        return None, "not_json"
    if not _valid(rec):
        return None, "not_a_record"
    return rec, None


class Log:
    """The log file: created here, appended to, every line numbered, and held under its byte cap."""

    def __init__(self, path, max_bytes):
        try:
            self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC, 0o600)
        except FileExistsError:
            refuse(path + " already exists: a log is only ever this collector's own")
        except OSError as error:
            refuse(f"cannot create the log ({error.strerror})")
        self.seq = 0
        self.size = 0
        self.max_bytes = max_bytes

    def encode(self, event, fields):
        line = dict(fields, event=event, seq=self.seq, wall=time.time(), mono=time.monotonic())
        return (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")

    def fits(self, data):
        """Whether a line may be written: the cap holds back END_RESERVE_BYTES for the end line."""
        return self.size + len(data) <= self.max_bytes - END_RESERVE_BYTES

    def write(self, event, **fields):
        """Write one line, numbered. Returns False, writing nothing, for a line that would pass the cap."""
        data = self.encode(event, fields)
        if event not in ("start", "end") and not self.fits(data):
            return False
        while data:
            written = os.write(self.fd, data)
            self.size += written
            data = data[written:]
        self.seq += 1
        return True

    def close(self):
        os.fsync(self.fd)
        os.close(self.fd)


STOPPING = []  # the signals asked to stop: the loop sees them between datagrams, never inside a write


def stop(signum, frame):
    STOPPING.append(signum)


def stop_receiving(sock):
    """End the receiving side: a send fails from now on (EPIPE), and the datagrams already queued stay readable.

    A send that succeeded was queued before this call and is drained after it, so no send can succeed and be lost
    between the last read and the close; the queue is then read without blocking."""
    sock.shutdown(socket.SHUT_RD)
    sock.setblocking(False)


def peer_of(ancillary):
    peer = {"pid": None, "uid": None}
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(value) >= UCRED.size:
            pid, uid, _ = UCRED.unpack(value[: UCRED.size])
            peer = {"pid": pid, "uid": uid}
    return peer


def receive(sock, log, counts):
    """Write one queued datagram; False when none is queued (non-blocking) or none came in time (with a timeout)."""
    try:
        data, ancillary, flags, _ = sock.recvmsg(MAX_LINE_BYTES + 1, socket.CMSG_SPACE(UCRED.size))
    except (BlockingIOError, socket.timeout):
        return False
    peer = peer_of(ancillary)
    rec, why = parse(data, bool(flags & socket.MSG_TRUNC))
    if rec is not None:
        written = log.write("record", peer=peer, rec=rec)
        counts["records" if written else "unwritten"] += 1
    else:
        written = log.write("malformed", peer=peer, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), why=why)
        counts["malformed" if written else "unwritten"] += 1
    if not written:
        counts["cap_reached"] = True
    return True


def open_socket(sock_dir, peer_uid):
    where = os.path.join(sock_dir, SOCKET_NAME)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
        sock.bind(where)
    except OSError as error:
        refuse(f"cannot bind the socket ({error})")
    if peer_uid != os.getuid():
        # another uid's sender must be able to connect: the directory, empty but for this socket, bounds who sees it
        os.chmod(where, 0o666)
    return sock, where


def main(argv):
    """Run the collector until SIGTERM or SIGINT; return the exit status (0, or 2 for a refused start)."""
    sock_dir, path, nonce, peer_uid, max_bytes = check_arguments(argv)
    os.umask(0o077)
    log = Log(path, max_bytes)
    sock, where = open_socket(sock_dir, peer_uid)
    counts = {"records": 0, "malformed": 0, "unwritten": 0, "cap_reached": False}
    log.write(
        "start", format=FORMAT, nonce=nonce, pid=os.getpid(), uid=os.getuid(), peer_uid=peer_uid, max_bytes=max_bytes
    )
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(READY, flush=True)
    sock.settimeout(0.2)
    while not STOPPING:
        receive(sock, log, counts)
    stop_receiving(sock)
    while receive(sock, log, counts):  # what was queued before the stop
        pass
    sock.close()
    os.unlink(where)
    log.write("end", **counts)
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
