"""C11: the holds a platform said only an operator could end reach the export, so the report can say them.

A campaign held for an operator (a failed heal's pairs, O35) starts no run during the hold, and a run held does not
exist as an attempt, so nothing in the run lists says it. The store's record of the health gate's outages has the
reason, the platform's words that lead an operator's hold, and the export lists each such outage under
`operator_holds`, only when there is one, so every other export is the one it was.
"""

import json

from campaign_support import planned_store
from export_support import REASON, outage

from expcore.campaign.export import REQUIRED_KEYS, export_run_state
from expcore.campaign.healthgate import META, dump

NOW = "2026-01-01T00:00:00Z"


def test_c11_an_outage_that_needs_an_operator_is_listed_in_the_export(tmp_path):
    """C11: each outage whose closing detail leads with an operator's reason is listed with when it began and ended,
    whether it was blocking, whether it charged a run and its reason; one a check passed is listed with the reason
    it closed with, not the passing check's words."""
    store, _, _ = planned_store(tmp_path)
    open_blocking = outage("2026-01-01T02:00:00Z", None, blocking=True, charged=False)
    ended = outage("2026-01-01T00:10:00Z", "2026-01-01T01:00:00Z", how="check_passed")
    store.put_meta(META, dump([ended, open_blocking]), NOW)
    data = export_run_state(store)
    assert data["operator_holds"] == [
        {
            "start_utc": "2026-01-01T00:10:00Z",
            "end_utc": "2026-01-01T01:00:00Z",
            "blocking": False,
            "charged": True,
            "checks": 2,
            "reason": REASON,
        },
        {
            "start_utc": "2026-01-01T02:00:00Z",
            "end_utc": None,
            "blocking": True,
            "charged": False,
            "checks": 2,
            "reason": REASON,
        },
    ]


def test_c11_an_export_with_no_outage_for_an_operator_is_what_it_was(tmp_path):
    """C11: no outage, an outage that any platform failure caused and one whose record cannot be read add no key."""
    store, _, _ = planned_store(tmp_path)
    assert set(export_run_state(store)) == set(REQUIRED_KEYS)
    store.put_meta(META, dump([outage("2026-01-01T00:10:00Z", None, first="the platform is unreachable")]), NOW)
    assert set(export_run_state(store)) == set(REQUIRED_KEYS)
    store.put_meta(META, "{not json", NOW)
    assert set(export_run_state(store)) == set(REQUIRED_KEYS)
    store.put_meta(META, json.dumps({"outages": []}), NOW)
    assert set(export_run_state(store)) == set(REQUIRED_KEYS)
