# Provenance: new
"""The elastic fleet's ledger (#462): runs leased one at a time to federations that come and go (C42).

Step 1 of the design note: the ledger, its lease state machine with fencing, expiry and the proof of a holder's end,
grants at lease time, per-model caps, the declared set order and dispatch by remaining work. Nothing here is reached
from today's forms: an unsharded run, a shard, a selection and their merges are unchanged.
"""

from expcore.campaign.elastic.config import LedgerConfig, LedgerRun, hard_term_s, lease_grant_usd, ledger_runs
from expcore.campaign.elastic.ledger import Ledger
from expcore.campaign.elastic.records import (
    Completion,
    CompletionConflict,
    Federation,
    JoinRefused,
    Lease,
    LedgerError,
    Return,
    Run,
    SiteReading,
    Wait,
)

__all__ = [
    "Completion",
    "CompletionConflict",
    "Federation",
    "JoinRefused",
    "Lease",
    "Ledger",
    "LedgerConfig",
    "LedgerError",
    "LedgerRun",
    "Return",
    "Run",
    "SiteReading",
    "Wait",
    "hard_term_s",
    "lease_grant_usd",
    "ledger_runs",
]
