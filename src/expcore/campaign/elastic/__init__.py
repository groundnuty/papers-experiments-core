# Provenance: new
"""The elastic fleet's ledger (#462): runs leased one at a time to federations that come and go (C42).

Step 1 of the design note: the ledger, its lease state machine with fencing, expiry and the proof of a holder's end,
grants at lease time, per-model caps, the declared set order and dispatch by remaining work, a lost lease's settling to
its real spend, a ceiling that can be raised, and the status of the money (`python -m expcore.campaign.elastic`).
Nothing here is reached from today's forms: an unsharded run, a shard, a selection and their merges are unchanged.
"""

from expcore.campaign.elastic.config import LedgerConfig, LedgerRun, hard_term_s, lease_grant_usd, ledger_runs
from expcore.campaign.elastic.ledger import Ledger
from expcore.campaign.elastic.records import (
    Ceiling,
    CeilingRefused,
    Ceilings,
    Completion,
    CompletionConflict,
    Demand,
    Federation,
    JoinRefused,
    Lease,
    LedgerError,
    Return,
    Run,
    Settlement,
    SettleRefused,
    SiteReading,
    Status,
    Wait,
)
from expcore.campaign.elastic.views import LedgerReader

__all__ = [
    "Ceiling",
    "CeilingRefused",
    "Ceilings",
    "Completion",
    "CompletionConflict",
    "Demand",
    "Federation",
    "JoinRefused",
    "Lease",
    "Ledger",
    "LedgerConfig",
    "LedgerError",
    "LedgerReader",
    "LedgerRun",
    "Return",
    "Run",
    "SettleRefused",
    "Settlement",
    "SiteReading",
    "Status",
    "Wait",
    "hard_term_s",
    "lease_grant_usd",
    "ledger_runs",
]
