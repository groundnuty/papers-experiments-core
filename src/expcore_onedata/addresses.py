# Provenance: new
"""The address map of a federation reached by the names its layout declares (O70).

A federation on a batch cluster registers each service under a name the layout declares (a zone's domain, a provider's
domain), which resolves nowhere: the cluster's nodes have names and addresses of their own, and an unprivileged user
cannot add a name to the node's resolver. So the services' URLs carry the declared names, and each client that dials
one asks this map for the address to connect to. The URL stays the declared name, so the TLS handshake asks for it, the
certificate is checked against it and the request's `Host` is it; only the connection goes elsewhere.

The map is a file the federation's `up` writes into the root, 0600 and never committed, and `[addresses]` of
`platform.toml` names it:

```toml
[addresses]
file = "secrets/address-map.json"   # relative to this file; a private file of yours (mode 0600), never a link
```

```json
{"format": "expcore-address-map/1",
 "hosts": {"<declared name>": {"address": "<the node's address>", "nodes": ["<the node's own names>"]}}}
```

`address` is where the declared name is dialled; `nodes` are the node's own names, which nothing agent-visible may
carry (`node_names` turns them into the tool server's `ONEDATA_MCP_NODE_NAMES`). A real host, an address or a node name,
belongs to one declared name only. The file is read once, when `platform.toml` is loaded: it is no part of the campaign
fingerprint (the addresses change at every `up`), of any record, or of any message, and nothing the agent can read ever
holds it. A refusal names the file's key, never a name or an address.
"""

import ipaddress
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from expcore.config.loading import Section

FORMAT = "expcore-address-map/1"
FILE_MODE = 0o600
MAX_BYTES = 1 << 20
MAX_NAMES = 64
MAX_NODE_NAMES = 8
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")  # a name in lower case; at most 253 characters (checked beside it)
DOCUMENT_KEYS = frozenset({"format", "hosts"})
ENTRY_KEYS = frozenset({"address", "nodes"})


class AddressMapError(ValueError):
    """The address map cannot be used. The message names a key of the file, never a name or an address."""


@dataclass(frozen=True)
class Node:
    """The node a declared name runs on: the address it is dialled at and the node's own names, in lower case."""

    address: str
    names: tuple[str, ...] = ()

    def __repr__(self) -> str:
        return "Node(...)"


def _name(value: Any, what: str, *, declared: bool) -> str:
    """A host name in lower case; a declared name has at least two labels and is no address."""
    if not isinstance(value, str):
        raise AddressMapError(f"{what} must be a string")
    text = value.strip().lower()
    if not text or len(text) > 253 or not HOST.fullmatch(text):
        raise AddressMapError(f"{what} must be a host name of letters, digits, '-' and '.'")
    if declared and "." not in text:
        raise AddressMapError(f"{what} must be a domain: at least two labels")
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return text
    raise AddressMapError(f"{what} must be a name, not an address")


def _address(value: Any) -> str:
    """An IPv4 or IPv6 address that something can be dialled at, in its canonical form."""
    try:
        found = ipaddress.ip_address(value) if isinstance(value, str) else None
    except ValueError:
        found = None
    if found is None or found.is_unspecified or found.is_multicast:
        raise AddressMapError("an address must be an IPv4 or IPv6 address a connection can go to")
    return str(found)


def _node(entry: Any) -> Node:
    if not isinstance(entry, Mapping) or "address" not in entry:
        raise AddressMapError("each entry of hosts must be a table with an address")
    if set(entry) - ENTRY_KEYS:
        raise AddressMapError("an entry of hosts holds only address and nodes")
    names = entry.get("nodes", [])
    if not isinstance(names, Sequence) or isinstance(names, str) or len(names) > MAX_NODE_NAMES:
        raise AddressMapError(f"nodes must be a list of at most {MAX_NODE_NAMES} names")
    return Node(
        _address(entry["address"]),
        tuple(dict.fromkeys(_name(n, "a node name", declared=False) for n in names)),
    )


def _claims(hosts: Mapping[str, Node]) -> None:
    """Refuse a real host under two declared names, and a declared name that is also some node's real host."""
    owner: dict[str, str] = {}
    for declared, node in hosts.items():
        for real in (node.address, *node.names):
            if owner.setdefault(real, declared) != declared:
                raise AddressMapError("a node's name or address belongs to one declared name only")
    if set(owner) & set(hosts):
        raise AddressMapError("a declared name must not be a node's own name")


@dataclass(frozen=True)
class AddressMap:
    """Each declared name of the federation with the node it runs on, immutable once built."""

    hosts: Mapping[str, Node]

    def __post_init__(self) -> None:
        if not self.hosts:
            raise AddressMapError("hosts must name at least one declared name")
        if len(self.hosts) > MAX_NAMES:
            raise AddressMapError(f"hosts holds at most {MAX_NAMES} declared names")
        object.__setattr__(self, "hosts", MappingProxyType(dict(self.hosts)))
        _claims(self.hosts)

    def __repr__(self) -> str:
        return f"AddressMap({len(self.hosts)} declared names)"

    def address_of(self, host: str) -> str | None:
        """The address a declared name is dialled at, or None for a host the map does not hold."""
        node = self.hosts.get(host.lower())
        return None if node is None else node.address

    def dial(self, host: str) -> str:
        """Where to connect for `host`: its mapped address, or `host` itself when the map does not hold it."""
        found = self.address_of(host)
        return host if found is None else found

    def node_names(self, ports: Sequence[int] = ()) -> dict[str, str]:
        """Every real name and address the federation is reachable by, each bare and with each of `ports`, mapped to
        the declared name it belongs to: onedata-mcp's `ONEDATA_MCP_NODE_NAMES` (a key is `host`, `host:port` or
        `[ipv6]:port`, a value the declared name, one host under one name only). A declared name maps to itself, bare
        and with each port, so a port printed after it goes with it as it does after a real name."""
        out: dict[str, str] = {}
        for declared, node in sorted(self.hosts.items()):
            for host in (declared, node.address, *node.names):
                bracketed = f"[{host}]" if ":" in host else host
                out[host] = declared
                out.update({f"{bracketed}:{port}": declared for port in ports})
        return out

    def to_document(self) -> dict[str, Any]:
        """The file's content: what `from_document` reads."""
        return {
            "format": FORMAT,
            "hosts": {
                name: {"address": node.address, "nodes": list(node.names)} for name, node in sorted(self.hosts.items())
            },
        }

    @classmethod
    def from_document(cls, document: Any) -> "AddressMap":
        """The map a document holds; AddressMapError, naming a key, when it holds no usable one."""
        if not isinstance(document, Mapping) or set(document) != DOCUMENT_KEYS or document["format"] != FORMAT:
            raise AddressMapError(f"the file is not an {FORMAT} document of exactly format and hosts")
        table = document["hosts"]
        if not isinstance(table, Mapping):
            raise AddressMapError("hosts must be a table")
        built: dict[str, Node] = {}
        for declared, entry in table.items():
            name = _name(declared, "a declared name", declared=True)
            if name in built:
                raise AddressMapError("a declared name is held once (case aside)")
            built[name] = _node(entry)
        return cls(built)

    @classmethod
    def load(cls, path: Path) -> "AddressMap":
        """Read the map's file: a regular file of yours, no link, mode 0600, small, and a valid document."""
        try:
            info = os.lstat(path)
        except OSError as error:
            raise AddressMapError(f"the file cannot be read ({type(error).__name__})") from None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise AddressMapError("the file must be a regular file of yours, never a link")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise AddressMapError("the file must be private (mode 0600): it holds the nodes' addresses")
        if info.st_size > MAX_BYTES:
            raise AddressMapError("the file is larger than a map of a federation can be")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as handle:
                content = handle.read(MAX_BYTES + 1)
        except OSError as error:
            raise AddressMapError(f"the file cannot be read ({type(error).__name__})") from None
        try:
            document = json.loads(content.decode("utf-8"))
        except ValueError:
            raise AddressMapError("the file is not JSON") from None
        return cls.from_document(document)

    def write(self, path: Path) -> None:
        """Write the map to `path` as a new file of mode 0600, replacing the one a failed earlier `up` left, never
        through a link: the file is made beside it and renamed over it."""
        text = json.dumps(self.to_document(), indent=2, sort_keys=True) + "\n"
        scratch = path.with_name(f".{path.name}.{os.getpid()}.part")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(scratch, flags, FILE_MODE)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), FILE_MODE)
                handle.write(text)
            os.replace(scratch, path)
        except BaseException:
            scratch.unlink(missing_ok=True)
            raise


def load_addresses(section: Section, base: Path) -> AddressMap:
    """Read `[addresses]` of platform.toml: `file`, the map's file, relative to the platform file. A ConfigError names
    the table's key and never a name, an address or the path."""
    section.reject_unknown(("file",))
    path = Path(os.path.abspath(base / section.string("file")))  # not resolved: a link as the file itself is refused
    try:
        return AddressMap.load(path)
    except AddressMapError as error:
        raise section.fail("file", str(error)) from None


def build(hosts: Mapping[str, tuple[str, Sequence[str]]]) -> AddressMap:
    """The map of `hosts`, each declared name with its node's address and the node's own names (a driver's call)."""
    return AddressMap.from_document(
        {"format": FORMAT, "hosts": {name: {"address": a, "nodes": list(n)} for name, (a, n) in hosts.items()}}
    )


__all__ = ["FORMAT", "AddressMap", "AddressMapError", "Node", "build", "load_addresses"]
