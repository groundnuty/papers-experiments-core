"""O61: the data gate's owner check serves only a peer whose socket belongs to the gate's own uid.

On an HPC batch node there is no network namespace, so the gate listens on a loopback every other user of the node
shares. `gatepeer` reads `/proc/net/tcp` and `/proc/net/tcp6` (which list every socket of the namespace with its uid,
whatever `hidepid` hides) and answers, for an accepted connection, whether the peer's socket is ours. The kernel's own
tables are read here for a real connection of this process; the other uids' cases are tables written from that
connection's address pair, because a test cannot become another user. Nothing leaves the machine.
"""

import contextlib
import os
import socket
import sys
from pathlib import Path

import pytest

from expcore_onedata.sandbox import gatepeer

OVERFLOW = (
    int(Path("/proc/sys/kernel/overflowuid").read_text()) if Path("/proc/sys/kernel/overflowuid").exists() else 65534
)
MINE = os.getuid()
needs_proc = pytest.mark.skipif(
    not (Path(gatepeer.PROC_NET_TCP).exists() and os.access(gatepeer.PROC_NET_TCP, os.R_OK)),
    reason="this host has no readable /proc/net/tcp",
)


def _ipv6_loopback() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as s:
            s.bind(("::1", 0))
    except OSError:
        return False
    return True


needs_ipv6 = pytest.mark.skipif(not _ipv6_loopback(), reason="this host has no usable ::1")


def other_uid() -> int:
    """A uid that is not ours and not one the kernel prints for a socket nobody owns."""
    return next(u for u in range(MINE + 1, MINE + 10) if u not in (0, OVERFLOW))


@contextlib.contextmanager
def connection(family=socket.AF_INET, listen="127.0.0.1", dial="127.0.0.1", dual=False):
    """A real loopback connection of this process: yields (accepted, client), the gate's end and the peer's."""
    with socket.socket(family) as server:
        if dual:
            server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        server.bind((listen, 0))
        server.listen(4)
        with socket.create_connection((dial, server.getsockname()[1]), timeout=10) as client:
            accepted, _ = server.accept()
            with accepted:
                yield accepted, client


def hex_address(ip: str, six: bool) -> str:
    """An address as the kernel prints it: each 32-bit word of its in-memory bytes as one native integer."""
    packed = socket.inet_pton(socket.AF_INET6 if six else socket.AF_INET, ip)
    return "".join(f"{int.from_bytes(packed[i : i + 4], sys.byteorder):08X}" for i in range(0, len(packed), 4))


def table_row(local, remote, uid, *, six=False, state=0x01, sl=0) -> str:
    """One row of /proc/net/tcp (or tcp6): local and remote are (ip, port)."""
    (la, lp), (ra, rp) = local, remote
    return (
        f"{sl:4d}: {hex_address(la, six)}:{lp:04X} {hex_address(ra, six)}:{rp:04X} {state:02X} "
        f"00000000:00000000 00:00000000 00000000 {uid:5d}        0 {40000 + sl} 1 0000000000000000 100 0 0 10 0"
    )


HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode"


def write_tables(tmp_path: Path, rows4=(), rows6=()) -> tuple[str, str]:
    tcp, tcp6 = tmp_path / "tcp", tmp_path / "tcp6"
    tcp.write_text("\n".join([HEADER, *rows4]) + "\n")
    tcp6.write_text("\n".join([HEADER, *rows6]) + "\n")
    return str(tcp), str(tcp6)


def pair_of(accepted: socket.socket) -> tuple[tuple[str, int], tuple[str, int]]:
    """(the peer's address, the gate's address) of the accepted socket, as the client's own row would show them."""
    return accepted.getpeername()[:2], accepted.getsockname()[:2]


# ---------------------------------------------------------------------------------------------------- real tables


@needs_proc
def test_a_peer_of_our_own_uid_is_served_by_the_kernels_tables():
    """O61: the rows the kernel prints for a real connection parse, in this host's byte order, to our own uid."""
    with connection() as (accepted, _client):
        verdict = gatepeer.OwnerCheck(MINE).decide(accepted)
    assert (verdict.served, verdict.reason, verdict.peer_uid) == (True, "ours", MINE)


@needs_proc
def test_the_uid_compared_is_the_peers_row_not_the_gates_own():
    """O61: the same real connection is refused by a check that wants another uid, and says whose socket it was."""
    with connection() as (accepted, _client):
        verdict = gatepeer.OwnerCheck(other_uid()).decide(accepted)
    assert (verdict.served, verdict.reason, verdict.peer_uid) == (False, "other_uid", MINE)


@needs_proc
@needs_ipv6
def test_an_ipv6_peer_of_our_own_uid_is_served():
    """O61: a ::1 peer is found in tcp6, and is ours."""
    with connection(socket.AF_INET6, "::1", "::1") as (accepted, _client):
        verdict = gatepeer.OwnerCheck(MINE).decide(accepted)
    assert (verdict.served, verdict.reason) == (True, "ours")


@needs_proc
@needs_ipv6
def test_an_ipv4_client_of_a_dual_stack_listener_is_found_in_the_ipv4_table():
    """O61: the gate sees ::ffff:127.0.0.1, the peer's own row is in tcp as 127.0.0.1: both normalise alike."""
    with connection(socket.AF_INET6, "::", "127.0.0.1", dual=True) as (accepted, _client):
        assert accepted.getpeername()[0] == "::ffff:127.0.0.1"
        verdict = gatepeer.OwnerCheck(MINE).decide(accepted)
    assert (verdict.served, verdict.reason, verdict.peer_uid) == (True, "ours", MINE)


# ----------------------------------------------------------------------------------------------- written tables


def test_a_peer_of_another_uid_is_refused(tmp_path):
    """O61: the row of the peer's socket names another user."""
    with connection() as (accepted, _client):
        peer, gate = pair_of(accepted)
        tables = write_tables(tmp_path, [table_row(peer, gate, other_uid())])
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason, verdict.peer_uid) == (False, "other_uid", other_uid())


@pytest.mark.parametrize("uid", [0, OVERFLOW])
def test_a_socket_the_kernel_shows_no_owner_for_is_refused(tmp_path, uid):
    """O61: uid 0 and the overflow uid are what the table prints for a socket with no open file: not ours."""
    with connection() as (accepted, _client):
        peer, gate = pair_of(accepted)
        tables = write_tables(tmp_path, [table_row(peer, gate, uid)])
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason, verdict.peer_uid) == (False, "no_owner_shown", uid)


def test_a_peer_with_no_row_is_refused_as_unattributable(tmp_path):
    """O61: a connection from another host, or one the tables do not list, cannot be attributed: refused."""
    with connection() as (accepted, _client):
        tables = write_tables(tmp_path)
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason, verdict.peer_uid) == (False, "not_found", None)


def test_unreadable_tables_refuse_everything(tmp_path):
    """O61: fail closed: when neither table can be read, nothing is served and the reason says why."""
    with connection() as (accepted, _client):
        tables = (str(tmp_path / "missing4"), str(tmp_path / "missing6"))
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason) == (False, "table_unreadable")


def test_a_row_of_the_gates_own_side_does_not_attribute_the_peer(tmp_path):
    """O61: the accepted socket's own row (the two ends the other way round) must not stand for the peer's."""
    with connection() as (accepted, _client):
        peer, gate = pair_of(accepted)
        tables = write_tables(tmp_path, [table_row(gate, peer, MINE)])
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason) == (False, "not_found")


@pytest.mark.parametrize("what", ["peer_port", "gate_port", "peer_address", "gate_address"])
def test_a_row_must_match_both_addresses_and_both_ports(tmp_path, what):
    """O61: the local and remote pair is matched whole: one differing port or address is another connection's row."""
    with connection() as (accepted, _client):
        (pa, pp), (ga, gp) = pair_of(accepted)
        peer = (("127.0.0.2" if what == "peer_address" else pa), pp + 1 if what == "peer_port" else pp)
        gate = (("127.0.0.3" if what == "gate_address" else ga), gp + 1 if what == "gate_port" else gp)
        tables = write_tables(tmp_path, [table_row(peer, gate, MINE)])
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason) == (False, "not_found")


def test_the_row_among_many_is_the_one_found(tmp_path):
    """O61: other users' rows before and after it, and junk lines, neither hide nor stand for the peer's row."""
    with connection() as (accepted, _client):
        peer, gate = pair_of(accepted)
        rows = [
            table_row(("127.0.0.1", 40000), ("127.0.0.1", 9), other_uid(), sl=0),
            "not a row at all",
            "   1: ZZZZZZZZ:0050 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1000 0 1",
            table_row(peer, gate, MINE, sl=2),
            table_row(("127.0.0.1", 40001), ("127.0.0.1", 9), other_uid(), sl=3),
        ]
        tables = write_tables(tmp_path, rows)
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason) == (True, "ours")


def test_a_stale_time_wait_row_does_not_hide_a_live_one(tmp_path):
    """O61: a TIME_WAIT row of an earlier connection with the same pair (uid 0) comes first; the live row decides."""
    with connection() as (accepted, _client):
        peer, gate = pair_of(accepted)
        rows = [table_row(peer, gate, 0, state=0x06, sl=0), table_row(peer, gate, MINE, sl=1)]
        tables = write_tables(tmp_path, rows)
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason) == (True, "ours")


def test_a_v6_row_for_a_mapped_peer_is_found(tmp_path):
    """O61: an IPv4 peer of a dual-stack gate may be in the IPv6 table, its addresses printed ::ffff:a.b.c.d."""
    with connection() as (accepted, _client):
        (pa, pp), (ga, gp) = pair_of(accepted)
        mapped = (f"::ffff:{pa}", pp), (f"::ffff:{ga}", gp)
        tables = write_tables(tmp_path, rows6=[table_row(*mapped, MINE, six=True)])
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason) == (True, "ours")


def test_a_v6_row_of_another_uid_for_a_mapped_peer_is_refused(tmp_path):
    with connection() as (accepted, _client):
        (pa, pp), (ga, gp) = pair_of(accepted)
        mapped = (f"::ffff:{pa}", pp), (f"::ffff:{ga}", gp)
        tables = write_tables(tmp_path, rows6=[table_row(*mapped, other_uid(), six=True)])
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason, verdict.peer_uid) == (False, "other_uid", other_uid())


@needs_ipv6
def test_an_ipv6_peer_of_another_uid_is_refused(tmp_path):
    with connection(socket.AF_INET6, "::1", "::1") as (accepted, _client):
        peer, gate = accepted.getpeername()[:2], accepted.getsockname()[:2]
        tables = write_tables(tmp_path, rows6=[table_row(peer, gate, other_uid(), six=True)])
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason) == (False, "other_uid")


def test_a_table_row_of_the_other_family_does_not_stand_for_a_v6_peer(tmp_path):
    """O61: an address is compared whole: the IPv4 row of 127.0.0.1 is not the row of ::1."""
    with connection() as (accepted, _client):
        peer, gate = pair_of(accepted)
        tables = write_tables(tmp_path, rows6=[table_row(("::1", peer[1]), ("::1", gate[1]), MINE, six=True)])
        verdict = gatepeer.OwnerCheck(MINE, tables=tables).decide(accepted)
    assert (verdict.served, verdict.reason) == (False, "not_found")


@pytest.mark.skipif(sys.byteorder != "little", reason="the literal rows below are as a little-endian host prints them")
def test_the_kernels_printed_forms_parse_to_the_addresses_they_stand_for():
    """O61: 0100007F is 127.0.0.1, and the four words of ::1 and ::ffff:127.0.0.1 read as the kernel prints them."""
    v4 = gatepeer.parse_row(
        "   0: 0100007F:1F90 0200007F:C000 01 00000000:00000000 00:00000000 00000000  1234 0 55 1 0 100 0 0 10 0"
    )
    assert v4 == gatepeer.Row(gatepeer.endpoint("127.0.0.1", 0x1F90), gatepeer.endpoint("127.0.0.2", 0xC000), 1, 1234)
    loop6 = "00000000000000000000000001000000"
    mapped = "0000000000000000FFFF00000100007F"
    v6 = gatepeer.parse_row(
        f"   1: {loop6}:1F90 {mapped}:C000 01 00000000:00000000 00:00000000 00000000  7 0 55 1", six=True
    )
    assert v6 == gatepeer.Row(gatepeer.endpoint("::1", 0x1F90), gatepeer.endpoint("::ffff:127.0.0.1", 0xC000), 1, 7)
    assert gatepeer.endpoint("127.0.0.1", 1) == gatepeer.endpoint("::ffff:127.0.0.1", 1) != gatepeer.endpoint("::1", 1)


@pytest.mark.parametrize(
    "line",
    [
        "",
        HEADER,
        "   0: 0100007F:1F90",
        "   0: 0100007F:1F9 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1 0 5",
        "   0: 0100007F:1F90 0100007F:1F90 0 00000000:00000000 00:00000000 00000000  1 0 5",
        "   0: 0100007F:1F90 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  -1 0 5",
        "   0: 0x00007F:1F90 0100007F:1F90 01 00000000:00000000 00:00000000 00000000  1 0 5",
        "   0: 0100007F:1F90 0100007F:1F90 01 00000000:00000000 00:00000000 00000000",
    ],
)
def test_a_malformed_row_is_no_row(line):
    assert gatepeer.parse_row(line) is None


@pytest.mark.parametrize("uid", [0, OVERFLOW])
def test_a_check_for_a_uid_the_tables_also_print_for_nobody_is_refused_at_its_start(uid):
    """O61: such a gate could not tell a socket that has no owner from its own: it refuses to start, naming why."""
    with pytest.raises(ValueError, match="no owner"):
        gatepeer.OwnerCheck(uid)


def test_a_check_whose_ipv4_table_cannot_be_read_refuses_to_start(tmp_path):
    """O61: such a gate would refuse every connection and still say ready; it names the table instead."""
    check = gatepeer.OwnerCheck(MINE, tables=(str(tmp_path / "missing"), str(tmp_path / "also-missing")))
    with pytest.raises(ValueError, match="cannot be read"):
        check.require_readable()


@needs_proc
def test_the_kernels_ipv4_table_passes_the_start_check():
    gatepeer.OwnerCheck(MINE).require_readable()


def test_the_peer_check_imports_nothing_but_the_standard_library():
    """It runs beside gate.py, alone: no platform layer, no third-party package."""
    import ast

    tree = ast.parse(Path(gatepeer.__file__).read_text(encoding="utf-8"))
    names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.level == 0}
    assert names - set(sys.stdlib_module_names) == set()
    assert "socket" not in names  # the platform layer imports it nowhere but client.py and the gate (O09)


def test_addresses_are_compared_by_these_exact_bytes():
    """O61: an address is 16 bytes, an IPv4 one as ::ffff:a.b.c.d, a scope dropped, and the port beside it."""
    v4 = bytes(10) + b"\xff\xff" + bytes([127, 0, 0, 1])
    assert gatepeer.endpoint("127.0.0.1", 1) == (v4, 1)
    assert gatepeer.endpoint("::ffff:127.0.0.1", 1) == (v4, 1)
    assert gatepeer.endpoint("::ffff:7f00:1", 1) == (v4, 1)
    assert gatepeer.endpoint("::1", 2) == (bytes(15) + b"\x01", 2)
    assert gatepeer.endpoint("0.0.0.0", 3) == (bytes(10) + b"\xff\xff" + bytes(4), 3)
    assert gatepeer.endpoint("::", 3) == (bytes(16), 3)
    assert gatepeer.endpoint("2001:db8::ff00:42:8329", 4) == (bytes.fromhex("20010db8000000000000ff0000428329"), 4)
    link_local = (bytes([0xFE, 0x80]) + bytes(13) + b"\x01", 5)
    assert gatepeer.endpoint("fe80::1", 5) == link_local
    assert gatepeer.endpoint("fe80::1%lo", 5) == link_local
    assert gatepeer.endpoint("fe80::1%eth0", 5) == link_local


@pytest.mark.parametrize(
    "bad", ["", "not-an-address", "127.0.0", "1.2.3.4.5", "999.0.0.1", "::g", "127.0.0.1 ", " ::1", "1::2::3", "%lo"]
)
def test_an_address_that_is_neither_ipv4_nor_ipv6_is_a_value_error(bad):
    with pytest.raises(ValueError, match="does not appear to be"):
        gatepeer.endpoint(bad, 1)


def test_a_peer_with_no_address_is_refused():
    """O61: a socket that is not connected has no peer to attribute."""
    with socket.socket() as loose:
        verdict = gatepeer.OwnerCheck(MINE).decide(loose)
    assert (verdict.served, verdict.reason) == (False, "no_address")
