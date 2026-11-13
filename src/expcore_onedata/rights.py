# Provenance: new
"""The space privileges a platform principal can hold, as Onezone names them, and which of them write."""

from collections.abc import Iterable

# The space privileges a platform principal can hold, as Onezone names them. The baseline is what the data path has,
# and what every tool-server principal has beside any write right. The vocabulary is closed so that a typo in a
# condition's rights fails at load.
VIEW, READ_DATA = "space_view", "space_read_data"
BASELINE_RIGHTS = (VIEW, READ_DATA)
READ_RIGHTS = (*BASELINE_RIGHTS, "space_view_qos", "space_view_transfers", "space_view_statistics")
WRITE_RIGHTS = (
    "space_manage_qos",
    "space_schedule_replication",
    "space_cancel_replication",
    "space_schedule_eviction",
    "space_cancel_eviction",
)
KNOWN_RIGHTS = (*READ_RIGHTS, *WRITE_RIGHTS)
# The right to add and remove QoS requirements, so to start a replication that creates no transfer document.
MANAGE_QOS = "space_manage_qos"


def declares_qos(rights: Iterable[str]) -> bool:
    """Tell whether a set of rights holds the right to manage QoS requirements (O33): the reset's quiet check runs for
    every run whose condition declares it, whether or not a requirement is left to see when the run ends."""
    return MANAGE_QOS in rights


def declares_write(rights: Iterable[str]) -> bool:
    """Tell whether a set of rights holds any write right, which is what decides whether a token stays read-only."""
    return any(right in WRITE_RIGHTS for right in rights)
