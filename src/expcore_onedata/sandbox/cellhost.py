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
starts with it, is refused at the start (`is_node_name`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, MutableMapping
from typing import Any

CELL_HOSTNAME = "sandbox"
FACT = "cell_hostname"  # the key of the run's `infrastructure_facts`
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")  # one DNS label: what a host name without a domain is
REFUSAL = (
    "the cells' fixed host name is also the name of a node of the federation: a node's own name may not be the neutral "
    "one the agent reads (O73)"
)


def valid(name: str) -> bool:
    """Whether `name` is one lower-case DNS label, as a host name that needs no domain is."""
    return LABEL.fullmatch(name) is not None


def is_node_name(node_names: Iterable[str], name: str = CELL_HOSTNAME) -> bool:
    """Whether one of the federation's real node names or addresses is `name`, or has it as its first label (a fully
    qualified name), in any case: the clash the start refuses."""
    return any(n.lower().split(".", 1)[0] == name for n in node_names)


def note(meta: MutableMapping[str, Any], name: str = CELL_HOSTNAME) -> None:
    """Put the name the run's cells were given in `meta`'s `infrastructure_facts`, beside what that holds."""
    meta.setdefault("infrastructure_facts", {})[FACT] = name


__all__ = ["CELL_HOSTNAME", "FACT", "REFUSAL", "is_node_name", "note", "valid"]
