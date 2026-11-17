# Provenance: new
"""The host name every agent cell has: one fixed, neutral string, the same on both backends (O73).

A cell that shares its node's UTS namespace reads the node's own name from `hostname`, `uname -n`, `uname -a` and
`/proc/sys/kernel/hostname`: a private detail of the site, and not what a Compose federation's cell reads. The Apptainer
backend's cells have no UTS namespace of their own unless asked for (`apptainer exec --uts`), so each exec is given
`--uts --hostname <name>` (`argv.exec_argv`); the Docker sandbox gives the agent's container `--hostname <name>`
(`containers.agent_run_args`). `/etc/hostname` is left to each: the sandbox binds no file of its own onto it.
Apptainer's source (its `addHostnameMount`, read on `main`, not on the site's release) writes the file itself with the
name when a UTS namespace and a host name are given, and never binds the node's own `/etc/hostname` (not by default,
and not at all under `--containall`); Docker's engine writes one for `--hostname`. A study's bind onto it is refused,
since it would replace the name (`apptainer.settings.OWN_TARGETS`).

The name is a constant of the core, not a setting: a study does not choose it, so it is in no campaign fingerprint, and
a change of it is a change of the core's version. Each run records the name its cells were given
(`infrastructure_facts.cell_hostname`), a fact of how they were started, never a reading from inside a cell.

It must never be a node's name. The gate counts the node names the answers it relays hold (`rest_node_name_hits`), and
the cell's own host name would be one of them; a federation with a node of that name, or whose fully qualified name
starts with it, is refused at the start (`is_node_name`). The scan holds the agent node's own host name instead, in its
fully qualified and one-label forms, as the node reports them (`scan_names`): a leak of the one name the cell no longer
shows is still counted, and the declared name, which is the allowed one, never is.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass
from typing import Any

CELL_HOSTNAME = "sandbox"
FACT = "cell_hostname"  # the key of the run's `infrastructure_facts`
LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain"})  # nobody's name: scanned, they would count false hits
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")  # one DNS label: what a host name without a domain is
REFUSAL = (
    "the cells' fixed host name is also the name of a node of the federation: a node's own name may not be the neutral "
    "one the agent reads (O73)"
)


@dataclass(frozen=True)
class OwnNames:
    """What a node says of its own names: the names it could read (its host name, its fully qualified name) and the
    reasons, never a name, that a form could not be read (`host_name_failed`, `fqdn_failed`, `fqdn_timeout`)."""

    names: tuple[str, ...] = ()
    unread: tuple[str, ...] = ()


def valid(name: str) -> bool:
    """Whether `name` is one lower-case DNS label, as a host name that needs no domain is."""
    return LABEL.fullmatch(name) is not None


def is_node_name(node_names: Iterable[str], name: str = CELL_HOSTNAME) -> bool:
    """Whether one of the federation's real node names or addresses is `name`, or has it as its first label (a fully
    qualified name), in any case: the clash the start refuses."""
    return any(n.lower().split(".", 1)[0] == name for n in node_names)


def scan_names(node_names: Iterable[str], own: Iterable[str], declared: Iterable[str] = ()) -> tuple[str, ...]:
    """The names the gate scans the answers it relays for (O73): the federation's nodes' (`node_names`, as they are),
    then the agent node's own, `own` as the node reports them, each as it is lower-cased and by its first label, since
    the scan takes a label followed by a dot for no match of the whole name. None twice in any case; never the declared
    cell name, `localhost`, a name the federation declares (`declared`, what the agent is meant to see) or a blank; an
    address is kept whole."""
    found = list(node_names)
    seen = {n.lower() for n in found}
    skip = {CELL_HOSTNAME, *LOCAL_NAMES, *(d.lower() for d in declared)}
    for raw in own:
        name = raw.strip().lower().rstrip(".")
        if not name or name in skip:
            continue
        for form in (name, *_first_label(name)):
            if form not in seen and form not in skip:
                found.append(form)
                seen.add(form)
    return tuple(found)


def _first_label(name: str) -> tuple[str, ...]:
    """The first label of a fully qualified name; nothing for a name of one label or an address."""
    if "." not in name:
        return ()
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return (name.split(".", 1)[0],)
    return ()


def note(meta: MutableMapping[str, Any], name: str = CELL_HOSTNAME) -> None:
    """Put the name the run's cells were given in `meta`'s `infrastructure_facts`, beside what that holds."""
    meta.setdefault("infrastructure_facts", {})[FACT] = name


__all__ = [
    "CELL_HOSTNAME", "FACT", "LOCAL_NAMES", "REFUSAL", "OwnNames", "is_node_name", "note", "scan_names", "valid",
]  # fmt: skip
