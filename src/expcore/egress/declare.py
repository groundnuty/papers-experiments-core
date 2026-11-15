# Provenance: new
"""What a run's egress classification rests on, and the classification itself: the data gate, loopback, or outside.

A `Declaration` is the run's own statement, made by the platform when it launched the run and recorded in the
instrument's snapshot, so a reader of the record classifies with what the run declared and nothing else:

- `gate`: the data gate's endpoints, each `(address, port)`, as the agent's cell reaches them (on HPC, a loopback
  address and the gate's high port);
- `canary`: the endpoint of the launcher's canary, one connect made inside the cell, under the shim, after the
  collector started and before the agent ran (the gate's endpoint, say). The log's first record must be that connect:
  it proves the shim was loaded and its records reached the collector, so a cell in which the shim never loaded
  cannot leave an empty log that reads as clean (`expcore.egress.log`, `no_canary`);
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
KEYS = {"gate", "canary", "loopback", "hosts", "network_calls"}

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def address_of(text: str) -> Address:
    """The address a text names, an IPv4-mapped IPv6 address as its IPv4 one; ValueError when it names none."""
    address = ipaddress.ip_address(text)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def endpoint(address: Any, port: Any, where: str) -> tuple[str, int]:
    """An `(address, port)` in its canonical form; ValueError naming `where` for anything else."""
    if not isinstance(address, str):
        raise ValueError(f"{where}: an address is text, not {address!r}")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port <= 65535:
        raise ValueError(f"{where}: a port is a whole number from 1 to 65535, not {port!r}")
    return str(address_of(address)), port


@dataclass(frozen=True)
class Declaration:
    """The data gate's endpoints, the canary's endpoint, the loopback ranges, the platform's host names and whether
    the data path is the agent's own network calls; validated when built, so a run cannot declare what no reader could
    check."""

    gate: tuple[tuple[str, int], ...]
    canary: tuple[str, int]
    loopback: tuple[str, ...] = DEFAULT_LOOPBACK
    hosts: tuple[str, ...] = ()
    network_calls: bool = False

    def __post_init__(self) -> None:
        gate = tuple(endpoint(a, p, f"gate endpoint {a!r}") for a, p in self.gate)
        if not isinstance(self.canary, tuple | list) or len(self.canary) != 2:
            raise ValueError("the canary is one (address, port)")
        canary = endpoint(*self.canary, "the canary")
        loopback = tuple(str(ipaddress.ip_network(r, strict=True)) for r in self.loopback)
        hosts = tuple(sorted({h.lower().rstrip(".") for h in self.hosts}))
        if any(not h or any(c.isspace() for c in h) for h in hosts):
            raise ValueError("a platform host name is a non-empty name without spaces")
        if not isinstance(self.network_calls, bool):
            raise ValueError("network_calls is true or false")
        object.__setattr__(self, "gate", tuple(sorted(set(gate))))
        object.__setattr__(self, "canary", canary)
        object.__setattr__(self, "loopback", loopback)
        object.__setattr__(self, "hosts", hosts)

    def to_json(self) -> dict[str, Any]:
        """The declaration as the snapshot records it."""
        return {
            "gate": [[address, port] for address, port in self.gate],
            "canary": list(self.canary),
            "loopback": list(self.loopback),
            "hosts": list(self.hosts),
            "network_calls": self.network_calls,
        }

    @classmethod
    def from_json(cls, data: Any) -> "Declaration":
        """Read a recorded declaration back; ValueError for anything that is not one."""
        if not isinstance(data, Mapping) or set(data) != KEYS:
            raise ValueError("not a recorded egress declaration")
        gate, canary, loopback, hosts = data["gate"], data["canary"], data["loopback"], data["hosts"]
        if not all(isinstance(v, list) for v in (gate, canary, loopback, hosts)):
            raise ValueError("a recorded declaration's gate, canary, loopback and hosts are lists")
        if not all(isinstance(e, list) and len(e) == 2 for e in gate):
            raise ValueError("a recorded gate endpoint is [address, port]")
        if not all(isinstance(v, str) for v in (*loopback, *hosts)):
            raise ValueError("recorded loopback ranges and host names are text")
        if len(canary) != 2:
            raise ValueError("a recorded canary is [address, port]")
        endpoints = tuple((a, p) for a, p in gate)
        return cls(endpoints, (canary[0], canary[1]), tuple(loopback), tuple(hosts), data["network_calls"])

    def classify(self, address: str, port: int) -> str:
        """`gate`, `loopback` or `outside` for one destination; ValueError for a text that names no address."""
        where = address_of(address)
        if (str(where), port) in self.gate:
            return GATE
        if any(where in ipaddress.ip_network(r) for r in self.loopback):
            return LOOPBACK
        return OUTSIDE

    def is_canary(self, address: str, port: int) -> bool:
        """True for the canary's endpoint."""
        try:
            return (str(address_of(address)), port) == self.canary
        except ValueError:
            return False

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
    canary: tuple[str, int],
    *,
    loopback: Sequence[str] = DEFAULT_LOOPBACK,
    hosts: Sequence[str] = (),
    network_calls: bool = False,
) -> Declaration:
    """Build a declaration from plain sequences."""
    return Declaration(tuple(gate), (canary[0], canary[1]), tuple(loopback), tuple(hosts), network_calls)
