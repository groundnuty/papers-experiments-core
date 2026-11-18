# Provenance: new
"""What today's readers of a merged batch need to tell a batch merged by run (C45) from a merge of shards.

`check`, `analyze` and the verified reader import this module and nothing of the elastic ledger: the merge by run's
checks and the ledger's reader (`campaign.merge_by_run`, `campaign.merged_by_run`, `campaign.ledger_read`) are imported
only on the branch that reads a batch merged by run, so a run, a shard, a selection, their merges and the export load
no module of the ledger (C42).
"""

import json
from typing import Any

from expcore.campaign.snapshot import SNAPSHOT_FILE

MERGE_BY_RUN_SCHEMA = "expcore-merge/2"
LEASES_DIR = "leases"
EVIDENCE_DIR = "evidence"


def snapshot_rel(directory: str, lease_id: int) -> str:
    """Where a merged batch keeps a lease store's snapshot, relative to the batch's directory."""
    return f"{directory}/{lease_id}/{SNAPSHOT_FILE}"


def is_by_run(raw: bytes) -> bool:
    """Whether merge.json's bytes are of a merge by run: its schema says so (anything else is read as today's)."""
    try:
        document: Any = json.loads(raw)
    except ValueError:
        return False
    return isinstance(document, dict) and document.get("schema") == MERGE_BY_RUN_SCHEMA
