# Provenance: new
"""The one writer of a run's `infrastructure_facts` (O89), on the Docker sandbox and the Apptainer sandbox alike.

Several features put a fact there: the work disk's record and its proof (`apptainer.facts`), the node's readings
(`hostfacts`, O87), the federation's window (`fedwindow`, O86), the readings `metadata()` makes when it is read (the
watch's memory, the limits, the gate's scans, the cells' host name and the scan of the agent node's own names,
`cellhost`, O73), and a fact a platform adds (`record_infrastructure_fact`). On 16 Nov 2026
one of them replaced the dict three times and dropped what another had put there. This module closes that class: every
write goes through an `InfrastructureFacts`, which is made of a sandbox's metadata and its lock, and which

- **merges**, under that lock: a write adds its keys to the table and never takes another's out. There is no call that
  replaces the table, removes a key or hands the dict to a caller; and the sandbox's `record(**fields)` refuses a field
  named for the table (`refuse_replacement`);
- **refuses to overwrite**, with `FactsConflict` (a bug of the writer, never of the run), a key that another writer
  owns. Each call names its `owner`; a key belongs to the owner it is declared for (`OWNED`: the sandbox's own keys,
  claimed before any of them is written, so that the stray write is the one refused and not the sandbox's), or else to
  the first owner that wrote it. An owner may write its own keys again as often as it likes; an update that names one
  key of another owner writes none of its keys.

The table's key, `infrastructure_facts`, is written in this module only: a scanner
(`tests/onedata/sandbox/test_infrafacts.py`) fails for any other source of `src` that names it, as a string, a keyword
argument or by this module's private name for it, so a new writer is this one's or is found by the test. A fact of the
run that exists only when `metadata()` is read is added to the copy that call returns (`annotate`), under the same
owners.

A feature that adds a fact: write it with its own `owner`, and, if it is the sandbox's own, declare its key in `OWNED`
(`tests/onedata/sandbox/test_infrafacts.py` checks the declared keys against the modules that make them).
"""

from __future__ import annotations

import copy
import threading
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

# The key of the run's metadata that holds the table. Private: the scanner fails for any reference to it outside this
# module (the literal, this name, or an import of it), so no writer names the table itself.
_TABLE = "infrastructure_facts"

# the owners of the sandbox's own keys
WORK_DISK = "work_disk"  # `apptainer.facts.record`: the departures, the work disk's keys, the provider's storage
NODE = "node"  # the readings of the node (O87) and of the tool server's host
WINDOW = "federation_window"  # the federation's window (O86), which the platform records from the run's preparation on
METADATA = "metadata"  # the readings made when `metadata()` is read
GATE = "gate"  # the gate's configuration as it was written (O85): the sandbox records what the file holds
CELL_RESOLV = "cell_resolv"  # `cellresolv.note`: the hash of the resolv.conf a cell read back at the start (O56)

OWNED: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        WORK_DISK: frozenset({"backend", "facts", "dns", "provider_storage"}),
        NODE: frozenset({"node", "tool_server_host"}),
        WINDOW: frozenset({"federation_window"}),
        GATE: frozenset(
            {"gate_upstream_read_timeout_s"}
        ),  # `gatepolicy.UPSTREAM_READ_TIMEOUT_FACT`; a test holds them equal
        CELL_RESOLV: frozenset({"resolv_conf_sha256"}),  # `cellresolv.FACT`; a test holds the two equal
        METADATA: frozenset(
            {
                "run_ended",
                "memory",
                "limits",
                "rest_node_name_hits",
                "rest_request_durations",
                "cell_hostname",  # `cellhost.FACT` (O73); a test holds the two equal: this module imports no feature
                "own_node_scan",  # the facts of the agent node's names in the gate's scan (O73)
            }
        ),
    }
)
_DECLARED: Mapping[str, str] = MappingProxyType({key: owner for owner, keys in OWNED.items() for key in keys})
if len(_DECLARED) != sum(len(keys) for keys in OWNED.values()):
    raise ValueError("a key of the run's infrastructure facts is declared for two owners")


class FactsConflict(RuntimeError):
    """A writer of a run's infrastructure facts took, or would have replaced, what another writer owns. A bug of the
    writer (never a failure of the run): the message names the key and the two owners, never a value."""


class InfrastructureFacts:
    """The writer of one sandbox's `infrastructure_facts`: made of its metadata dict and the lock that guards it."""

    def __init__(self, meta: dict[str, Any], lock: threading.RLock) -> None:
        self._meta, self._lock = meta, lock
        self._owners: dict[str, str] = dict(_DECLARED)  # key -> owner; grows as undeclared keys are first written

    def write(self, owner: str, update: Mapping[str, Any]) -> None:
        """Add `update`'s keys to the run's table, under the lock, for `owner`. Raises `FactsConflict`, and writes
        nothing, when a key belongs to another owner. The table is made by the first write: a run that has none has no
        empty one. The table keeps a copy of each value (`copy.deepcopy`), so the dict a writer still holds is no handle
        on a recorded fact: editing or clearing it afterwards changes nothing here."""
        with self._lock:
            table = self._meta.get(_TABLE, {})
            self._claim(owner, update, table)
            self._meta.setdefault(_TABLE, {}).update(copy.deepcopy(dict(update)))

    def get(self, key: str, default: Any = None) -> Any:
        """A copy of the value the run's table holds for `key`, or `default` as given: editing what it returns changes
        no recorded fact."""
        with self._lock:
            table = self._meta.get(_TABLE, {})
            return copy.deepcopy(table[key]) if key in table else default

    def annotate(self, snapshot: dict[str, Any], owner: str, update: Mapping[str, Any]) -> None:
        """Add `update`'s keys to the table of `snapshot`, a copy of the run's metadata that `metadata()` returns, for
        `owner`: a fact that exists only when the metadata is read. The run's own table is not touched. The same
        refusals as `write`, against what the copy holds."""
        with self._lock:
            table = snapshot.get(_TABLE, {})
            self._claim(owner, update, table)
            snapshot.setdefault(_TABLE, {}).update(update)

    def _claim(self, owner: str, update: Mapping[str, Any], table: Mapping[str, Any]) -> None:
        """Check every key before any is claimed, so a refusal leaves the owners and the table as they were."""
        held = {key: self._owners.get(key) for key in update}
        for key, current in held.items():
            if current is not None and current != owner:
                raise FactsConflict(
                    f"infrastructure fact {key!r} is owned by {current!r}: {owner!r} may not overwrite it"
                )
            if current is None and key in table:
                raise FactsConflict(
                    f"infrastructure fact {key!r} was written outside the helper: {owner!r} may not overwrite it"
                )
        for key in update:
            self._owners[key] = owner


def refuse_replacement(fields: Mapping[str, Any]) -> None:
    """Refuse a `record(**fields)` that names the table, which would replace it whole: its facts are written by
    `InfrastructureFacts` and by nothing else."""
    if _TABLE in fields:
        raise FactsConflict(
            "a record may not replace the run's whole infrastructure facts: write one fact, with its owner"
        )


__all__ = [
    "CELL_RESOLV",
    "GATE",
    "METADATA",
    "NODE",
    "OWNED",
    "WINDOW",
    "WORK_DISK",
    "FactsConflict",
    "InfrastructureFacts",
    "refuse_replacement",
]
