"""Transfer records for the tests of the state rule (O32): three live ones (an ended and an enqueued replication, an
ended eviction), and the other eviction and migration shapes derived from op-worker's source, since no live record of
them has been saved.

Every record op-worker answers carries its summary `transferStatus` beside both halves, `replicationStatus` and
`evictionStatus` (op-worker f118ed9, the federation's build, `transfer_rest_translator.erl:119-123`), and the half a job
does not do is `skipped` from its creation (`transfer.erl:157-166`); `transferStatus` is `transfer:status/1`
(`transfer.erl:474-495`) and `effectiveJobStatus` is the same of the newest job the record was rerun as, or of itself
(`transfer_rest_translator.erl:78-88`)."""

PLACEHOLDER = "<provider id>"  # where a live record named a provider by its id

# LIVE: the first federation study's replication records, verbatim but for the provider id. Every one of the 1,080
# answers of its run at core 39bba713 (54 polls of 20 replications) was the ended one, and a real Onedata run of 13 Nov
# answered the enqueued one before it; every real answer saved since 13 Nov marks the unused half `skipped`.
LIVE_REPLICATION = {
    "type": "replication",
    "transferStatus": "completed",
    "replicationStatus": "completed",
    "evictionStatus": "skipped",
    "effectiveJobStatus": "completed",
    "replicatingProviderId": PLACEHOLDER,
    "evictingProviderId": None,
}
LIVE_REPLICATION_ENQUEUED = {
    "type": "replication",
    "transferStatus": "enqueued",
    "replicationStatus": "enqueued",
    "evictionStatus": "skipped",
    "effectiveJobStatus": "enqueued",
    "replicatingProviderId": PLACEHOLDER,
    "evictingProviderId": None,
}

# LIVE since 15 Nov: a reset's eviction record, read once after that study's Day-0 fact 3, verbatim but for the provider
# id. It equals what op-worker f118ed9 gives: an eviction's replication half is `skipped` from its creation
# (`transfer.erl:157-166`) and its summary is its eviction half's, `active` read as `evicting` (`transfer.erl:486-490`).
EVICTION_ENDED = {
    "type": "eviction",
    "transferStatus": "completed",
    "replicationStatus": "skipped",
    "evictionStatus": "completed",
    "effectiveJobStatus": "completed",
    "replicatingProviderId": None,
    "evictingProviderId": PLACEHOLDER,
}
EVICTION_ACTIVE = {
    **EVICTION_ENDED,
    "transferStatus": "evicting",
    "evictionStatus": "active",
    "effectiveJobStatus": "evicting",
}
# DERIVED FROM OP-WORKER f118ed9, not live: a migration both of whose halves completed (`transfer.erl:475-485`), and
# one halfway, its replication completed and its eviction still active, which its summary reads `evicting`.
MIGRATION_ENDED = {
    "type": "migration",
    "transferStatus": "completed",
    "replicationStatus": "completed",
    "evictionStatus": "completed",
    "effectiveJobStatus": "completed",
    "replicatingProviderId": PLACEHOLDER,
    "evictingProviderId": PLACEHOLDER,
}
MIGRATION_HALFWAY = {
    **MIGRATION_ENDED,
    "transferStatus": "evicting",
    "evictionStatus": "active",
    "effectiveJobStatus": "evicting",
}

STATUS_FIELDS = ("transferStatus", "replicationStatus", "evictionStatus", "effectiveJobStatus")


def statuses(shape: dict) -> dict:
    """A shape's status fields alone, for a test that sets them on a record the fake answers."""
    return {k: v for k, v in shape.items() if k in STATUS_FIELDS}


def live(shape: dict, **ids: str) -> dict:
    """A shape with its placeholder provider ids replaced: `replicatingProviderId=...`, `evictingProviderId=...`."""
    return {k: ids.get(k, v) if v == PLACEHOLDER else v for k, v in shape.items()}


def _phase(shape: dict, summary: str, replication: str, eviction: str) -> dict:
    return {
        **shape,
        "transferStatus": summary,
        "replicationStatus": replication,
        "evictionStatus": eviction,
        "effectiveJobStatus": summary,
    }


# What a poll of each kind of job answers, in order, until it ended and after: the live replications' with op-worker's
# phase between them, and op-worker-derived ones of an eviction and a migration (`transfer.erl:474-495`).
POLLS = {
    "replication": [
        LIVE_REPLICATION_ENQUEUED,
        _phase(LIVE_REPLICATION, "replicating", "active", "skipped"),
        LIVE_REPLICATION,
        LIVE_REPLICATION,
    ],
    "eviction": [
        _phase(EVICTION_ENDED, "scheduled", "skipped", "scheduled"),
        EVICTION_ACTIVE,
        EVICTION_ENDED,
        EVICTION_ENDED,
    ],
    "migration": [
        _phase(MIGRATION_ENDED, "scheduled", "scheduled", "scheduled"),
        _phase(MIGRATION_ENDED, "replicating", "active", "scheduled"),
        _phase(MIGRATION_ENDED, "evicting", "completed", "scheduled"),
        MIGRATION_HALFWAY,
        MIGRATION_ENDED,
        MIGRATION_ENDED,
    ],
}
FIRST_FINAL = {kind: [p["transferStatus"] for p in polls].index("completed") for kind, polls in POLLS.items()}
