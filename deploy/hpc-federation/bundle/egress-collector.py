"""egress-collector.py: the receiving end of the agent's egress shim (egresslog.c), run outside the agent's cell.

  python3 -I egress-collector.py <socket dir> <log file> <nonce>

It binds `<socket dir>/sock`, a Unix datagram socket, in an empty 0700 directory of this uid that the launcher binds
read-only into the cell at /hpcop-egress, and writes every datagram it receives to `<log file>`, a file it creates
(0600, refused if it exists) outside every path the cell sees. It prints `egress collector ready` on stdout once it
is listening. On SIGTERM or SIGINT it reads what is still queued, writes its end line, removes the socket and exits 0.

The log is JSON lines, each with `seq` (0, 1, 2, ... with no gap), `wall` (time.time()) and `mono` (time.monotonic()):
  {"event": "start", "format": "hpcop-egress/1", "nonce": <the launcher's>, "pid", "uid": <the collector's>, ...}
  {"event": "record", "peer": {"pid", "uid"}, "rec": {"op", "pid", "fd", "time", "type", "family", "address", "port",
   "result", "dropped"}, ...}
  {"event": "malformed", "peer": {...}, "bytes": <length>, "sha256": <of the datagram>, ...}
  {"event": "end", "records": <record lines written>, "malformed": <malformed lines>, "unwritten": <datagrams past
   the cap, counted and not written>, ...}
`peer` is the sender's pid and uid as the kernel attests them (SCM_CREDENTIALS). A record is the shim's line parsed
field by field; anything else, a forged or damaged datagram, is a malformed line, kept only as its size and SHA-256:
no byte of it is written. The reader of the log (the core's `expcore.egress`) decides what an incomplete log means.
"""

import hashlib
import ipaddress
import json
import os
import re
import signal
import socket
import stat
import struct
import sys
import time

FORMAT = "hpcop-egress/1"
DATAGRAM_MAX = 512
MAX_LINES = 1000000  # records and malformed lines written; past it a datagram is only counted (`unwritten`)
OPS = {"connect", "result", "async", "sendto", "sendmsg", "sendmmsg"}
OUTCOMES = {"result", "async"}  # their result is a number; every other op is an attempt, "-"
TYPES = {"tcp", "udp", "raw", "other"}
FAMILIES = {"inet": 4, "inet6": 6}
TIME = re.compile(r"[0-9]{1,20}\.[0-9]{9}")
NUMBER = re.compile(r"(0|[1-9][0-9]{0,18})")
NONCE = re.compile(r"[0-9a-f]{16,128}")
UCRED = struct.Struct("iII")


def refuse(message):
    print("egress-collector: ERROR: " + message, file=sys.stderr)
    sys.exit(2)


def number(text, low, high):
    if not NUMBER.fullmatch(text) or not low <= int(text) <= high:
        raise ValueError(text)
    return int(text)


def parse(datagram):
    """The shim's record as a dict; ValueError for anything else."""
    text = datagram.decode("ascii")
    if not text.endswith("\n") or "\n" in text[:-1]:
        raise ValueError("not one line")
    fields = text[:-1].split(" ")
    if len(fields) != 11 or fields[0] != FORMAT:
        raise ValueError("not a record")
    _, op, pid, fd, when, kind, family, address, port, result, dropped = fields
    if op not in OPS or kind not in TYPES or family not in FAMILIES or not TIME.fullmatch(when):
        raise ValueError("a field out of its set")
    if address == "-":
        if op != "async":
            raise ValueError("only an async record may lack its address")
    elif ipaddress.ip_address(address).version != FAMILIES[family]:
        raise ValueError("an address of another family")
    if (result == "-") == (op in OUTCOMES):
        raise ValueError("an attempt's result is '-', an outcome's a number")
    return {
        "op": op,
        "pid": number(pid, 1, 2**31),
        "fd": number(fd, 0, 2**31),
        "time": when,
        "type": kind,
        "family": family,
        "address": address,
        "port": number(port, 0, 65535),
        "result": None if result == "-" else number(result, 0, 4095),
        "dropped": number(dropped, 0, 2**63),
    }


def check_arguments(argv):
    if len(argv) != 3:
        refuse("usage: egress-collector.py <socket dir> <log file> <nonce>")
    sock_dir, log, nonce = argv
    if not NONCE.fullmatch(nonce):
        refuse("the nonce must be 16 to 128 lower-case hex digits")
    try:
        st = os.lstat(sock_dir)
    except OSError:
        refuse("no socket directory at " + sock_dir)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        refuse(sock_dir + " must be a 0700 directory of this uid")
    if os.listdir(sock_dir):
        refuse(sock_dir + " must be empty")
    return sock_dir, log, nonce


class Log:
    """The log file: created here, appended to, every line numbered."""

    def __init__(self, path):
        try:
            self.fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | os.O_CLOEXEC, 0o600)
        except FileExistsError:
            refuse(path + " already exists: a log is only ever this collector's own")
        self.seq = 0

    def write(self, event, **fields):
        line = dict(fields, event=event, seq=self.seq, wall=time.time(), mono=time.monotonic())
        data = (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        while data:
            data = data[os.write(self.fd, data) :]
        self.seq += 1

    def close(self):
        os.fsync(self.fd)
        os.close(self.fd)


STOPPING = []  # the signals asked to stop: the loop sees them between datagrams, never inside a write


def stop(signum, frame):
    STOPPING.append(signum)


def receive(sock, log, counts):
    """Write one queued datagram; False when none is queued (non-blocking) or none came in time (with a timeout)."""
    try:
        data, ancillary, flags, _ = sock.recvmsg(DATAGRAM_MAX, socket.CMSG_SPACE(UCRED.size))
    except (BlockingIOError, socket.timeout):
        return False
    peer = {"pid": None, "uid": None}
    for level, kind, value in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_CREDENTIALS and len(value) >= UCRED.size:
            pid, uid, _ = UCRED.unpack(value[: UCRED.size])
            peer = {"pid": pid, "uid": uid}
    if counts["written"] >= MAX_LINES:
        counts["unwritten"] += 1
        return True
    counts["written"] += 1
    try:
        if flags & socket.MSG_TRUNC:
            raise ValueError("cut")
        log.write("record", peer=peer, rec=parse(data))
        counts["records"] += 1
    except (ValueError, UnicodeDecodeError):
        log.write("malformed", peer=peer, bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
        counts["malformed"] += 1
    return True


def main(argv):
    sock_dir, path, nonce = check_arguments(argv)
    os.umask(0o077)
    log = Log(path)
    where = os.path.join(sock_dir, "sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_PASSCRED, 1)
    sock.bind(where)
    counts = {"written": 0, "records": 0, "malformed": 0, "unwritten": 0}
    log.write("start", format=FORMAT, nonce=nonce, pid=os.getpid(), uid=os.getuid())
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("egress collector ready", flush=True)
    sock.settimeout(0.2)
    while not STOPPING:
        receive(sock, log, counts)
    sock.setblocking(False)
    while receive(sock, log, counts):  # what was queued before the stop
        pass
    sock.close()
    os.unlink(where)
    log.write("end", records=counts["records"], malformed=counts["malformed"], unwritten=counts["unwritten"])
    log.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
