"""The names an HPC federation registers its services under and is reached by (O73, DESIGN.md section 5.8).

A layout declares a name for the zone and for each provider (`<service>.<domain>`, under the layout's `domain`). On a
cluster those names resolve nowhere, and the nodes have names and addresses of their own, which nothing the agent can
read may carry. So:

- every service **registers** under its declared name (the bundle's `configure.sh`: `onezone.domainName`,
  `oneprovider.domain`, the generated certificate's domain) and is given a hosts file listing every service's declared
  name with its node's address;
- every service's **URL** is `https://<declared name>:8443`, and each client of the login node dials the declared name
  at the node's address through the **address map** (`expcore_onedata.addresses`), a private file `up` writes into the
  root once the nodes' facts are read: the TLS handshake, the certificate check and `Host` carry the declared name;
- `facts.json` records what each service **registered** (read back from its `/configuration`), and the DNS check as
  advisory; a `[sandbox.federation]` table whose names are not those is **refused** (the table is usable only once the
  providers register the declared names).

Nothing here prints or records a node's name or address but the map file and the state, which are the root's own.
"""

import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import tomllib

from expcore_onedata.addresses import AddressMap, AddressMapError, build

DNS_CHECK = {
    "advisory": True,
    "expected": "unresolvable",
    "measured": False,
    "why": "Onepanel's DNS check resolves through DNS alone, and a layout's names are in the services' hosts files "
    "only, so it cannot resolve them; no step of up depends on it, and the first live slot measures it",
}


def declared_names(spec: Any) -> Dict[str, str]:
    """Each service's declared name by service name, in the layout's order: the zone's, then each provider's."""
    return {service.name: service.domain for service in spec.services}


def address_map(declared: Mapping[str, str], services: Mapping[str, Mapping[str, Any]]) -> AddressMap:
    """The address map of the placed services: each declared name with its node's address and the node's own names
    (its FQDN and its first label). RuntimeError, naming the role, when a service has no node address or name (the
    hosts file lists every peer, so a missing one stops up), or when two services share a node's name or address."""
    hosts: Dict[str, Any] = {}
    for role, name in declared.items():
        record = services.get(role) or {}
        address, fqdn = record.get("address"), record.get("fqdn")
        if not address or not fqdn:
            raise RuntimeError(
                f"{role} has no node address: every service's hosts file lists every peer, so up stops before it "
                "launches any"
            )
        hosts[name] = (address, list(dict.fromkeys([fqdn.lower(), fqdn.split(".")[0].lower()])))
    try:
        return build(hosts)
    except AddressMapError:
        raise RuntimeError(
            "two services share one node's name or address: a name belongs to one service only"
        ) from None


LAUNCHER = "resolve_launch.py"
SHIM = Path(__file__).resolve().parent / "toolshim" / LAUNCHER  # starts a Python tool server so it resolves the names


def install_toolshim(directory: Path) -> Path:
    """Copy the resolve launcher into the root (`directory`, 0700; the file 0644 and never a link), and return the
    directory: `platform.toml` names the file (`[addresses] resolver`), and the tool server is started through it
    (O73). A copy an earlier `up` left is replaced: it is the driver's own file."""
    directory.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    target = directory / LAUNCHER
    if target.is_symlink():
        raise RuntimeError("the root's resolve launcher is a link: refused")
    scratch = directory / f".{LAUNCHER}.{os.getpid()}.part"
    shutil.copyfile(SHIM, scratch)
    scratch.chmod(0o644)
    os.replace(scratch, target)
    return directory


def host_args(found: AddressMap) -> List[str]:
    """`--host=NAME=ADDRESS` for every service of the map, the bundle's input for the hosts file (sorted by name)."""
    return [f"--host={name}={node.address}" for name, node in sorted(found.hosts.items())]


def registered_domains(boot: Any) -> Dict[str, str]:
    """The domain each service reports for itself, read from its `/configuration` through the bootstrap's client: the
    zone's from Onezone, each provider's from its Oneprovider, lower-cased. A service that reports none is absent."""
    from fedrest import PROVIDER_API, ZONE_API

    found: Dict[str, str] = {}
    spec = boot.spec
    zone = boot.rest.json("GET", spec.zone.url + ZONE_API + "/configuration", label="zone configuration")
    if isinstance(zone, dict) and isinstance(zone.get("domain"), str):
        found[spec.zone.name] = zone["domain"].lower()
    for service in spec.providers:
        answer = boot.rest.json(
            "GET", service.url + PROVIDER_API + "/configuration", label=f"{service.name} configuration"
        )
        if isinstance(answer, dict) and isinstance(answer.get("domain"), str):
            found[service.name] = answer["domain"].lower()
    return found


def check_registered(declared: Mapping[str, str], registered: Mapping[str, str]) -> None:
    """Every service must have registered under its declared name. RuntimeError naming the role and its declared name,
    never the name it did register (an older registration's is its node's)."""
    for role, name in declared.items():
        if registered.get(role) != name.lower():
            raise RuntimeError(
                f"{role} registered under a name other than its declared {name!r}: the services' URLs, the clients' "
                "address map and every certificate hold the declared names, so none of them would reach it"
            )


def check_federation_table(platform_text: str, registered: Mapping[str, str]) -> bool:
    """Whether a rendered platform file declares `[sandbox.federation]` (True), refusing it unless its names are those
    the services registered under: the zone's and the local provider's, by the file's own facts (`registered`, by
    service name, as `facts.json` records them).

    The cell resolves only those names, and the published client dials the domain each provider registered, so a table
    that names anything else leaves the client with no provider to reach. RuntimeError, naming the table's own names and
    the role, never a registered name that differs; one with no record of what was registered is refused as well, since
    a name derived from the layout proves nothing about a registration. False when there is no table."""
    federation = tomllib.loads(platform_text).get("sandbox", {}).get("federation")
    if not isinstance(federation, dict):
        return False
    declared: Dict[str, Optional[str]] = {"zone": federation.get("zone")}
    providers = federation.get("providers")
    if isinstance(providers, dict):
        declared.update({str(role): name for role, name in providers.items()})
    if not registered:
        raise RuntimeError(
            "[sandbox.federation] is declared, and the facts hold no record of the names the services registered: the "
            "table is usable only once they register the layout's declared names (the ordering of O73)"
        )
    for role, name in declared.items():
        if not isinstance(name, str) or registered.get(role) != name.lower():
            raise RuntimeError(
                f"[sandbox.federation]'s name for the {role} ({name!r}) is not the name the federation registered: the "
                "table is usable only once the providers register the layout's declared names (the ordering of O73)"
            )
    return True
