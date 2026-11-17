"""A transfer record's state, read per kind from the half of the job it does (O32).

Every record op-worker answers carries both halves, `replicationStatus` and `evictionStatus`, beside its summary
`transferStatus` (op-worker f118ed9, `transfer_rest_translator.erl:119-121`), and the half a job does not do is
`skipped` from its creation (`transfer.erl:157-166`). The replication records marked live in `transfer_shapes` are
real answers; no eviction or migration record has been saved live, so theirs, and every phase below, are derived from
that source."""

import pytest
from transfer_shapes import (
    EVICTION_ACTIVE,
    EVICTION_ENDED,
    FIRST_FINAL,
    LIVE_REPLICATION,
    LIVE_REPLICATION_ENQUEUED,
    MIGRATION_ENDED,
    MIGRATION_HALFWAY,
    POLLS,
    live,
    statuses,
)

from expcore_onedata.consistency import ENDED_STATES, transfer_state
from expcore_onedata.day0.outage_tools import state_of as outage_state_of
from expcore_onedata.testing import ALPHA_ID, BETA_ID
from expcore_onedata.transferrecord import CONFLICTING, job_of, record_of, state_of
from expcore_onedata.transferwindow import FINAL_STATES

REPLICATED = statuses(LIVE_REPLICATION)  # the live ended replication's status fields

# Each phase of each kind as op-worker f118ed9 records it: (kind, transferStatus, replicationStatus, evictionStatus,
# the state the job is in). `transferStatus` is `transfer:status/1` (`transfer.erl:474-495`): `active` reads
# `replicating` or `evicting`, and a migration whose replication completed reads its eviction's. A migration replicates
# first and evicts after, and a failure or cancel of its replication ends the eviction alike
# (`replication_status.erl:205-281`); one of its eviction leaves the completed replication as it was
# (`replica_eviction_status.erl:216-269`).
PHASES = [
    ("replication", "scheduled", "scheduled", "skipped", "scheduled"),
    ("replication", "enqueued", "enqueued", "skipped", "enqueued"),
    ("replication", "replicating", "active", "skipped", "active"),
    ("replication", "aborting", "aborting", "skipped", "aborting"),
    ("replication", "completed", "completed", "skipped", "completed"),
    ("replication", "failed", "failed", "skipped", "failed"),
    ("replication", "cancelled", "cancelled", "skipped", "cancelled"),
    ("eviction", "scheduled", "skipped", "scheduled", "scheduled"),
    ("eviction", "enqueued", "skipped", "enqueued", "enqueued"),
    ("eviction", "evicting", "skipped", "active", "active"),
    ("eviction", "aborting", "skipped", "aborting", "aborting"),
    ("eviction", "completed", "skipped", "completed", "completed"),
    ("eviction", "failed", "skipped", "failed", "failed"),
    ("eviction", "cancelled", "skipped", "cancelled", "cancelled"),
    ("migration", "scheduled", "scheduled", "scheduled", "scheduled"),
    ("migration", "enqueued", "enqueued", "scheduled", "enqueued"),
    ("migration", "replicating", "active", "scheduled", "active"),
    ("migration", "aborting", "aborting", "aborting", "aborting"),
    ("migration", "failed", "failed", "failed", "failed"),
    ("migration", "cancelled", "cancelled", "cancelled", "cancelled"),
    ("migration", "evicting", "completed", "scheduled", "scheduled"),
    ("migration", "evicting", "completed", "enqueued", "enqueued"),
    ("migration", "evicting", "completed", "active", "active"),
    ("migration", "aborting", "completed", "aborting", "aborting"),
    ("migration", "completed", "completed", "completed", "completed"),
    ("migration", "failed", "completed", "failed", "failed"),
    ("migration", "cancelled", "completed", "cancelled", "cancelled"),
]


def record(kind: str, fields: dict, **more) -> dict:
    """A record of `kind` with these status fields, the provider ids op-worker gives that kind, and an ended job's
    times and counters."""
    return {
        "type": kind,
        "fileId": "file-1",
        "replicatingProviderId": None if kind == "eviction" else BETA_ID,
        "evictingProviderId": {"replication": None, "eviction": BETA_ID, "migration": ALPHA_ID}[kind],
        "scheduleTime": 1000,
        "startTime": 1001,
        "finishTime": 1010,
        "bytesReplicated": 0 if kind == "eviction" else 4096,
        "filesReplicated": 0 if kind == "eviction" else 1,
        "filesEvicted": 0 if kind == "replication" else 1,
        "filesFailed": 0,
        **fields,
        **more,
    }


def phase(kind: str, summary: str, replication: str, eviction: str) -> dict:
    return record(kind, {"transferStatus": summary, "replicationStatus": replication, "evictionStatus": eviction})


def whole(shape: dict) -> dict:
    """A shape of `transfer_shapes` as a whole record: its provider ids, and a job's times and counters."""
    ids = live(
        shape, replicatingProviderId=BETA_ID, evictingProviderId=ALPHA_ID if shape["type"] != "eviction" else BETA_ID
    )
    return record(shape["type"], ids)


# Every reader of a transfer's state a study, a tool or the harness polls or records.
READERS = {
    "transferrecord.state_of (the ended walk's stop)": state_of,
    "job_of (the transfer log and its window)": lambda body: job_of("tid-1", "ended", body).state,
    "record_of (the snapshot's records)": lambda body: record_of(job_of("tid-1", "ended", body), label=str)["state"],
    "consistency.transfer_state (the waits for a transfer's end)": transfer_state,
    "day0.outage_tools.state_of (the outage check's poll)": outage_state_of,
}
KNOWN = [
    pytest.param(LIVE_REPLICATION, "completed", True, id="live: a replication that ended"),
    pytest.param(LIVE_REPLICATION_ENQUEUED, "enqueued", False, id="live: a replication enqueued"),
    pytest.param(EVICTION_ENDED, "completed", True, id="derived: an eviction that ended"),
    pytest.param(EVICTION_ACTIVE, "active", False, id="derived: an eviction under way"),
    pytest.param(MIGRATION_ENDED, "completed", True, id="derived: a migration that ended"),
    pytest.param(MIGRATION_HALFWAY, "active", False, id="derived: a migration halfway"),
]


@pytest.mark.parametrize("reader", list(READERS))
@pytest.mark.parametrize(("shape", "expected", "final"), KNOWN)
def test_o32_every_reader_reads_the_live_and_the_derived_records_alike(reader, shape, expected, final):
    """O32: the live case. The live replication that ended (`evictionStatus: skipped` beside `replicationStatus:
    completed`) reads completed, a final state, in every reader, where it read conflicting in the transfer log; the live
    enqueued one reads enqueued and never final; and the eviction and migration shapes derived from op-worker read
    their own half's state, final only when the job ended."""
    state = READERS[reader](whole(shape))
    assert state == expected
    assert (state in FINAL_STATES) is final
    assert (state in ENDED_STATES) is final


@pytest.mark.parametrize("kind", list(POLLS))
def test_o32_a_poller_waiting_for_a_final_state_stops_at_the_first_final_answer(kind):
    """O32: a client that polls a job until `job_of(...).state in FINAL_STATES`, as a study's scripted persona does,
    stops at the first answer that says the job ended, for each kind: the live replication's third, where it never
    stopped and ran on to its token cap."""
    polls = 0
    for answer in POLLS[kind]:
        polls += 1
        if job_of("tid-1", "ongoing", whole(answer)).state in FINAL_STATES:
            break
    assert polls == FIRST_FINAL[kind] + 1


@pytest.mark.parametrize(("kind", "summary", "replication", "eviction", "expected"), PHASES)
def test_o32_each_phase_of_each_kind_reads_the_state_of_the_half_the_job_is_in(
    kind, summary, replication, eviction, expected
):
    """O32: a replication reads its replication half, an eviction its eviction half, and a migration its replication
    half until that ends and then its eviction half, so a migration is final only once both halves are."""
    assert state_of(phase(kind, summary, replication, eviction)) == expected


@pytest.mark.parametrize(("kind", "summary", "replication", "eviction", "expected"), PHASES)
def test_o32_the_state_is_final_exactly_when_op_workers_own_summary_is(kind, summary, replication, eviction, expected):
    """O32: on every shape op-worker makes, the state read from the halves is final exactly when the record's
    `transferStatus`, op-worker's own summary of them, is: no phase reads final before the platform says it ended."""
    assert (state_of(phase(kind, summary, replication, eviction)) in FINAL_STATES) is (summary in FINAL_STATES)


@pytest.mark.parametrize(("kind", "summary", "replication", "eviction", "expected"), PHASES)
def test_o32_the_consistency_waits_read_the_state_as_the_transfer_log_does(
    kind, summary, replication, eviction, expected
):
    """O32: `consistency.transfer_state`, which the waits for a transfer's end read, gives the transfer log's state, so
    the two never disagree on whether a job has ended."""
    body = phase(kind, summary, replication, eviction)
    assert transfer_state(body) == state_of(body)
    assert (transfer_state(body) in ENDED_STATES) is (state_of(body) in FINAL_STATES)


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        pytest.param(
            "replication",
            {"transferStatus": "completed", "replicationStatus": "active", "evictionStatus": "skipped"},
            id="a replication's summary says completed and its half active",
        ),
        pytest.param(
            "replication",
            {"transferStatus": "replicating", "replicationStatus": "completed", "evictionStatus": "skipped"},
            id="a replication's summary says replicating and its half completed",
        ),
        pytest.param(
            "replication",
            {"replicationStatus": "completed", "evictionStatus": "active"},
            id="a replication's eviction half ran",
        ),
        pytest.param(
            "replication",
            {"replicationStatus": "completed", "evictionStatus": "completed"},
            id="a replication's eviction half ended",
        ),
        pytest.param(
            "replication",
            {"transferStatus": "skipped", "replicationStatus": "skipped", "evictionStatus": "skipped"},
            id="a replication's own half skipped",
        ),
        pytest.param(
            "eviction",
            {"transferStatus": "completed", "replicationStatus": "completed", "evictionStatus": "completed"},
            id="an eviction's replication half ended",
        ),
        pytest.param(
            "eviction",
            {"replicationStatus": "skipped", "evictionStatus": "skipped"},
            id="an eviction's own half skipped",
        ),
        pytest.param(
            "migration",
            {"transferStatus": "failed", "replicationStatus": "failed", "evictionStatus": "completed"},
            id="a migration's replication failed and its eviction completed",
        ),
        pytest.param(
            "migration",
            {"replicationStatus": "completed", "evictionStatus": "skipped"},
            id="a migration's eviction half skipped",
        ),
        pytest.param(
            "migration",
            {"replicationStatus": "skipped", "evictionStatus": "completed"},
            id="a migration's replication half skipped",
        ),
        pytest.param(
            "migration",
            {"replicationStatus": "active", "evictionStatus": "completed"},
            id="a migration's eviction ended before its replication",
        ),
        pytest.param(
            "migration",
            {"replicationStatus": "cancelled", "evictionStatus": "scheduled"},
            id="a migration's replication cancelled and its eviction still scheduled",
        ),
        pytest.param(
            "migration",
            {"transferStatus": "completed", "replicationStatus": "completed", "evictionStatus": "active"},
            id="a migration's summary says completed while it evicts",
        ),
        pytest.param(
            "replication",
            {"transferStatus": "completed", "transferState": "failed", "replicationStatus": "completed"},
            id="two summaries disagree",
        ),
        pytest.param(
            "replication",
            {"transferStatus": "done", "replicationStatus": "completed", "evictionStatus": "skipped"},
            id="a summary op-worker never gives",
        ),
        pytest.param(
            "replication",
            {"replicationStatus": ["completed"], "evictionStatus": "skipped"},
            id="a half that is no string",
        ),
        pytest.param(
            "eviction",
            {"replicationStatus": "skipped", "evictionStatus": "completed", "transferStatus": 3},
            id="a summary that is no string",
        ),
        pytest.param(
            "replication",
            {**REPLICATED, "evictingProviderId": ALPHA_ID},
            id="a replication's record that names an evicting provider, a migration's",
        ),
        pytest.param(
            "replication",
            {**REPLICATED, "replicatingProviderId": None},
            id="a replication's record that names no replicating provider",
        ),
        pytest.param(
            "eviction",
            {**statuses(EVICTION_ENDED), "replicatingProviderId": BETA_ID},
            id="an eviction's record that names a replicating provider",
        ),
        pytest.param(
            "migration",
            {**statuses(MIGRATION_ENDED), "evictingProviderId": None},
            id="a migration's record that names no evicting provider, a replication's",
        ),
        pytest.param(
            "migration",
            {**statuses(MIGRATION_ENDED), "replicatingProviderId": ""},
            id="a migration's record whose replicating provider is empty",
        ),
    ],
)
def test_o32_status_fields_that_contradict_each_other_read_conflicting_never_final(kind, fields):
    """O32: a contradiction is still one: a summary that disagrees with the half the job is in, a half the job does not
    do that is not `skipped`, the half it does marked `skipped`, a migration whose replication failed but whose eviction
    completed (op-worker fails both halves, `replication_status.erl:244-258`), a field that cannot be read, and provider
    ids that name another kind than the record's `type`, which op-worker derives from them (`transfer.erl:397-403`)."""
    assert state_of(record(kind, fields)) == CONFLICTING
    assert CONFLICTING not in FINAL_STATES
    assert transfer_state(record(kind, fields)) not in ENDED_STATES


@pytest.mark.parametrize(
    ("kind", "statuses", "expected"),
    [
        pytest.param(
            "replication", {"replicationStatus": "completed"}, "completed", id="a replication, no eviction half"
        ),
        pytest.param("eviction", {"evictionStatus": "completed"}, "completed", id="an eviction, no replication half"),
        pytest.param(
            "replication", {"replicationStatus": "completed", "evictionStatus": None}, "completed", id="a null half"
        ),
        pytest.param("migration", {"replicationStatus": "active"}, "active", id="a migration replicating, no eviction"),
        pytest.param("migration", {"replicationStatus": "completed"}, CONFLICTING, id="a migration, eviction unread"),
        pytest.param("migration", {"evictionStatus": "completed"}, CONFLICTING, id="a migration, replication unread"),
        pytest.param(
            "replication",
            {"transferStatus": "completed", "evictionStatus": "skipped"},
            CONFLICTING,
            id="a replication with only the half it does not do",
        ),
        pytest.param(
            "eviction",
            {"transferStatus": "completed", "replicationStatus": "skipped"},
            CONFLICTING,
            id="an eviction with only the half it does not do",
        ),
    ],
)
def test_o32_a_missing_half_the_state_rests_on_never_reads_final(kind, statuses, expected):
    """O32: a half the job does not do may be absent, as in the fake's records. A half the state comes from may not: a
    migration whose other half is not in the record is not final, nor is a record that holds only the half its job
    does not do."""
    assert state_of(record(kind, statuses)) == expected


@pytest.mark.parametrize(
    ("kind", "statuses", "expected"),
    [
        ("replication", {"transferStatus": "completed"}, "completed"),
        ("migration", {"transferStatus": "evicting"}, "evicting"),
        ("migration", {"transferStatus": "completed", "transferState": "completed"}, "completed"),
        ("eviction", {"transferStatus": "completed", "transferState": "evicting"}, CONFLICTING),
        ("replication", {}, None),
    ],
)
def test_o32_a_record_with_neither_half_is_read_from_its_summary(kind, statuses, expected):
    """O32: a record that carries no half is read from its summary, op-worker's own reading of both halves, final only
    once both have ended (`transfer.erl:474-495`); two summaries that disagree are conflicting, and a record with no
    status field has no state."""
    assert state_of(record(kind, statuses)) == expected


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({"replicationStatus": "paused", "evictionStatus": "skipped"}, "paused"),
        ({"transferStatus": "paused", "replicationStatus": "paused", "evictionStatus": "skipped"}, "paused"),
        ({"replicationStatus": "completed", "evictionStatus": "pending"}, CONFLICTING),
    ],
)
def test_o32_a_status_op_worker_never_gives_is_no_final_state(statuses, expected):
    """O32: a status word outside op-worker's is a state of its own, never a final one, on the half the job does; on
    the half it does not do, anything but `skipped` contradicts it."""
    state = state_of(record("replication", statuses))
    assert state == expected
    assert state not in FINAL_STATES


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ({"replicationStatus": "completed"}, "completed"),
        (statuses(LIVE_REPLICATION), CONFLICTING),
        ({"replicationStatus": "completed", "evictionStatus": "active"}, CONFLICTING),
        ({}, None),
    ],
)
def test_o32_a_record_of_no_known_type_is_final_only_when_every_status_field_agrees(statuses, expected):
    """O32: without a type there is no half the job does not do, so every status field it carries must say the same,
    as before; `job_of` refuses such a record anyway, and the walk of the ended listing keeps it, which fails closed."""
    assert state_of({"type": "copy", **statuses}) == expected
    assert state_of(dict(statuses)) == expected


def test_o32_a_provider_id_the_record_does_not_carry_is_not_checked():
    """O32: the kind's provider ids are checked against its `type` only where the record carries them; a record that
    names neither is read from its halves."""
    assert state_of({"type": "replication", **REPLICATED}) == "completed"
    assert state_of({"type": "eviction", **statuses(EVICTION_ENDED)}) == "completed"
