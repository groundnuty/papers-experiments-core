# Provenance: new
"""What a run's egress classification rests on, and the classification itself: the data gate, loopback, or outside.

A `Declaration` is the run's own statement, made by the platform when it launched the run and recorded in the
instrument's snapshot, so a reader of the record classifies with what the run declared and nothing else:

- `gate`: the data gate's endpoints, each `(address, port)`, as the agent's cell reaches them (on HPC, a loopback
  address and the gate's high port);
- `loopback`: the address ranges that never leave the node, by default `127.0.0.0/8` and `::1/128`;
- `hosts`: the platform's host names, which a URL in the agent's code may name without being flagged (the names the
  gate answers for);
- `network_calls`: true when the run's data path is the agent's own HTTP to the gate (REST), so that the network calls
  in its code (`requests`, `urllib`, `http.client`, `socket`) are the data path's, recorded and not flagged.

A destination is the gate when its address and port are one of the gate's endpoints, loopback when its address is in a
loopback range, and outside otherwise: an address of the node's own network interfaces, the unspecified address and
every address the declaration does not name are outside, so a reader errs towards a flag. An IPv4-mapped IPv6 address
(`::ffff:a.b.c.d`) is classified as its IPv4 address.
"""

import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

GATE, LOOPBACK, OUTSIDE = "gate", "loopback", "outside"
CLASSES = (GATE, LOOPBACK, OUTSIDE)
DEFAULT_LOOPBACK = ("127.0.0.0/8", "::1/128")

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def address_of(text: str) -> Address:
    """The address a text names, an IPv4-mapped IPv6 address as its IPv4 one; ValueError when it names none."""
    address = ipaddress.ip_address(text)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _port(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= 65535:
        raise ValueError(f"{where}: a port is a whole number from 1 to 65535, not {value!r}")
    return value


@dataclass(frozen=True)
class Declaration:
    """The data gate's endpoints, the loopback ranges, the platform's host names and whether the data path is the
    agent's own network calls; validated when built, so a run cannot declare what no reader could check."""

    gate: tuple[tuple[str, int], ...]
    loopback: tuple[str, ...] = DEFAULT_LOOPBACK
    hosts: tuple[str, ...] = ()
    network_calls: bool = False

    def __post_init__(self) -> None:
        gate = tuple((str(address_of(a)), _port(p, f"gate endpoint {a!r}")) for a, p in self.gate)
        loopback = tuple(str(ipaddress.ip_network(r, strict=True)) for r in self.loopback)
        hosts = tuple(sorted({h.lower().rstrip(".") for h in self.hosts}))
        if any(not h or any(c.isspace() for c in h) for h in hosts):
            raise ValueError("a platform host name is a non-empty name without spaces")
        if not isinstance(self.network_calls, bool):
            raise ValueError("network_calls is true or false")
        object.__setattr__(self, "gate", tuple(sorted(set(gate))))
        object.__setattr__(self, "loopback", loopback)
        object.__setattr__(self, "hosts", hosts)

    def to_json(self) -> dict[str, Any]:
        """The declaration as the snapshot records it."""
        return {
            "gate": [[address, port] for address, port in self.gate],
            "loopback": list(self.loopback),
            "hosts": list(self.hosts),
            "network_calls": self.network_calls,
        }

    @classmethod
    def from_json(cls, data: Any) -> "Declaration":
        """Read a recorded declaration back; ValueError for anything that is not one."""
        if not isinstance(data, Mapping) or set(data) != {"gate", "loopback", "hosts", "network_calls"}:
            raise ValueError("not a recorded egress declaration")
        gate, loopback, hosts = data["gate"], data["loopback"], data["hosts"]
        if not all(isinstance(v, list) for v in (gate, loopback, hosts)):
            raise ValueError("a recorded declaration's gate, loopback and hosts are lists")
        if not all(isinstance(e, list) and len(e) == 2 and isinstance(e[0], str) for e in gate):
            raise ValueError("a recorded gate endpoint is [address, port]")
        if not all(isinstance(v, str) for v in (*loopback, *hosts)):
            raise ValueError("recorded loopback ranges and host names are text")
        return cls(tuple((a, p) for a, p in gate), tuple(loopback), tuple(hosts), data["network_calls"])

    def classify(self, address: str, port: int) -> str:
        """`gate`, `loopback` or `outside` for one destination; ValueError for a text that names no address."""
        where = address_of(address)
        if (str(where), port) in self.gate:
            return GATE
        if any(where in ipaddress.ip_network(r) for r in self.loopback):
            return LOOPBACK
        return OUTSIDE

    def is_platform_host(self, host: str) -> bool:
        """True for one of the platform's names, a gate address or a loopback address (a URL the scan does not
        flag)."""
        name = host.lower().rstrip(".").strip("[]")
        if name in self.hosts or name == "localhost":
            return True
        try:
            where = address_of(name)
        except ValueError:
            return False
        return any(str(where) == a for a, _ in self.gate) or any(
            where in ipaddress.ip_network(r) for r in self.loopback
        )


def declaration(
    gate: Sequence[tuple[str, int]],
    *,
    loopback: Sequence[str] = DEFAULT_LOOPBACK,
    hosts: Sequence[str] = (),
    network_calls: bool = False,
) -> Declaration:
    """Build a declaration from plain sequences."""
    return Declaration(tuple(gate), tuple(loopback), tuple(hosts), network_calls)
