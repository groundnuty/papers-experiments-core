# Provenance: new
"""The owner check of the data gate: serve a connection only if its peer's socket belongs to the gate's own uid.

On an HPC batch node there is no network namespace. The gate listens on the node's loopback, which every other user of
the node shares, so any of them could connect to it. `/proc/net/tcp` and `/proc/net/tcp6` list every socket of the
namespace with the uid of its owner (`hidepid` hides other users' processes, not their sockets), which is enough to
attribute a connection without looking at any process. The check reads the row of the peer's own socket, the one whose
local address and port are the peer's and whose remote address and port are the gate's end of the connection, and
serves the connection only when that row is this uid's. It is the gate's side of the node's shared-loopback rule; the
bundle's `portshim-owner.h` makes the same decision in C for the Onedata services, with the same matching.

Refused, never served: another uid, a socket the table shows no owner for (uid 0, or the overflow uid, which is what
kernels before 6.16 print for a socket with no open file, such as a peer that has already closed), a peer with no row (a
connection from another host, or one the tables do not list), and every connection when the tables cannot be read. A
check for uid 0 or the overflow uid is refused at its start (`OwnerCheck`): it could not tell its own sockets from
ownerless ones.

Rows move while the kernel prints a table a page at a time, so a row not found is looked up once more.

This file runs inside the gate's container or on the batch node beside `gate.py`, which loads it by its path when the
check is on: it imports the standard library only, and nothing of the platform layer.
"""

from __future__ import annotations

import os
import re
import socket
import sys
from dataclasses import dataclass

PROC_NET_TCP = "/proc/net/tcp"
PROC_NET_TCP6 = "/proc/net/tcp6"
OVERFLOW_UID_FILE = "/proc/sys/kernel/overflowuid"
DEFAULT_OVERFLOW_UID = 65534  # the kernel's default, used when its file cannot be read
TIME_WAIT = 0x06  # the state of a finished connection's leftover row, which never has an owner
LOOKUPS = 2  # a row not found is looked up once more: a table read a page at a time can miss a row that moves

Endpoint = tuple[bytes, int]  # an address as 16 bytes, an IPv4 address as ::ffff:a.b.c.d, and a port
_V4_PREFIX = b"\x00" * 10 + b"\xff\xff"

_HEX = "[0-9A-Fa-f]"
_ROW = re.compile(
    rf"^\s*\d+:\s+({_HEX}{{8}}|{_HEX}{{32}}):({_HEX}{{4}})\s+({_HEX}{{8}}|{_HEX}{{32}}):({_HEX}{{4}})\s+({_HEX}{{2}})"
    r"\s+\S+\s+\S+\s+\S+\s+(\d+)(?:\s|$)"  # tx_queue:rx_queue, tr:tm->when, retrnsmt, then the uid
)


def endpoint(address: str, port: int) -> Endpoint:
    """An address as text and a port, in the one form every comparison here uses: IPv4 maps into IPv6, so a v4-mapped
    peer of a dual-stack listener equals the IPv4 row of its own socket. A scope (`fe80::1%lo`) is dropped."""
    address = address.partition("%")[0]
    try:
        return _V4_PREFIX + socket.inet_pton(socket.AF_INET, address), port
    except (OSError, ValueError):
        return socket.inet_pton(socket.AF_INET6, address), port


@dataclass(frozen=True)
class Row:
    """One socket of a table: its two ends, its TCP state and the uid it shows."""

    local: Endpoint
    remote: Endpoint
    state: int
    uid: int


@dataclass(frozen=True)
class Verdict:
    """Whether to serve a connection, and why. `reason`: `ours` (served), `other_uid`, `no_owner_shown`, `not_found`,
    `table_unreadable`, `no_address`, or `check_error` (the gate's caller, for a failure of the check itself); the
    rest of them refuse. `peer_uid` is the uid the table showed, `peer` the peer's address as `host:port`."""

    served: bool
    reason: str
    peer_uid: int | None = None
    peer: str = ""


def _address(digits: str) -> bytes:
    """Each 8-digit group is a 32-bit word of the address as it sits in memory, printed as one native integer."""
    return b"".join(int(digits[i : i + 8], 16).to_bytes(4, sys.byteorder) for i in range(0, len(digits), 8))


def _end(digits: str, port: str, six: bool) -> Endpoint | None:
    if len(digits) != (32 if six else 8):
        return None
    packed = _address(digits)
    return (packed if six else _V4_PREFIX + packed), int(port, 16)


def parse_row(line: str, six: bool = False) -> Row | None:
    """A row of /proc/net/tcp (`six` false) or tcp6, or None for a header, a short line or any other line."""
    found = _ROW.match(line)
    if not found:
        return None
    local, remote = _end(found[1], found[2], six), _end(found[3], found[4], six)
    if local is None or remote is None:
        return None
    return Row(local, remote, int(found[5], 16), int(found[6]))


def overflow_uid() -> int:
    """The uid the kernel shows for an owner it cannot map into the reader's user namespace."""
    try:
        with open(OVERFLOW_UID_FILE, encoding="ascii") as handle:
            value = int(handle.read().strip())
    except (OSError, ValueError):
        return DEFAULT_OVERFLOW_UID
    return value if value > 0 else DEFAULT_OVERFLOW_UID


def _scan(path: str, six: bool, peer: Endpoint, gate: Endpoint) -> Row | None:
    """The live row of the socket at `peer` connected to `gate` in one table (IPv6's when `six`). OSError if it cannot
    be read."""
    needle = f"{peer[1]:04X}"
    with open(path, "rb") as handle:
        for raw in handle:
            line = raw.decode("ascii", "replace").upper()
            if needle not in line:  # most rows are other connections: skip them before the parse
                continue
            row = parse_row(line, six)
            if row and row.local == peer and row.remote == gate and row.state != TIME_WAIT:
                return row
    return None


class OwnerCheck:
    """Decides, for an accepted connection, whether its peer is a socket of `uid` (the process's own by default).

    `tables` overrides the two table paths (a test's own); by default the module's `PROC_NET_TCP` and
    `PROC_NET_TCP6` are read at each decision. Raises `ValueError` for a uid the tables also print for a socket
    that has no owner."""

    def __init__(self, uid: int | None = None, *, tables: tuple[str, str] | None = None) -> None:
        self.uid = os.getuid() if uid is None else uid
        self.no_owner = {0, overflow_uid()}
        if self.uid in self.no_owner:
            raise ValueError(
                f"uid {self.uid} is one the kernel's socket tables also show for a socket that has no owner, "
                "so the owner check could not tell its peers from ownerless ones"
            )
        self._tables = tables

    def _paths(self) -> tuple[str, str]:
        return self._tables or (PROC_NET_TCP, PROC_NET_TCP6)

    def require_readable(self) -> None:
        """Raise `ValueError` unless the IPv4 table can be read: without it every connection would be refused."""
        path = self._paths()[0]
        try:
            with open(path, "rb") as handle:
                handle.read(1)
        except OSError as e:
            raise ValueError(f"{path} cannot be read ({e.strerror or e}), so no peer could be attributed") from None

    def _lookup(self, peer: Endpoint, gate: Endpoint) -> tuple[Row | None, bool]:
        """The peer's row, and whether a table could not be read. The IPv6 table first for an IPv6 peer."""
        tcp, tcp6 = self._paths()
        tables = [(tcp, False), (tcp6, True)]
        unreadable = False
        for path, six in tables if peer[0].startswith(_V4_PREFIX) else tables[::-1]:
            try:
                row = _scan(path, six, peer, gate)
            except OSError:
                unreadable = True
                continue
            if row is not None:
                return row, unreadable
        return None, unreadable

    def decide(self, conn: socket.socket) -> Verdict:
        """Serve `conn`, just accepted and not yet read or written, only if its peer's socket is this uid's."""
        try:
            host, port = conn.getpeername()[:2]
            peer = endpoint(host, port)
            gate = endpoint(*conn.getsockname()[:2])
        except (OSError, ValueError, TypeError, IndexError):
            return Verdict(False, "no_address")
        label = f"{host}:{port}"
        row, unreadable = None, False
        for _ in range(LOOKUPS):
            row, was_unreadable = self._lookup(peer, gate)
            unreadable = unreadable or was_unreadable
            if row is not None:
                break
        if row is None:
            return Verdict(False, "table_unreadable" if unreadable else "not_found", None, label)
        if row.uid in self.no_owner:
            return Verdict(False, "no_owner_shown", row.uid, label)
        if row.uid != self.uid:
            return Verdict(False, "other_uid", row.uid, label)
        return Verdict(True, "ours", row.uid, label)
