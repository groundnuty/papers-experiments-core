"""Outage records as the health gate keeps them (`expcore.campaign.healthgate`), for the tests of the holds an operator
ends (C11): the export lists them, a merged export joins them and the report says them."""

REASON = "a failed heal: operator action needed (stuck pairs: providers[2] reading from providers[0])"
CLOSING = f"{REASON}; no run is admitted until every provider link transfers: stuck: providers[0]->providers[2]"


def outage(
    start: str,
    end: str | None,
    *,
    how: str | None = None,
    blocking: bool = False,
    charged: bool = True,
    first: str = CLOSING,
    reason: str | None = REASON,
) -> dict:
    """One outage record: open when `end` is None, and then still saying what closed it; ended, its last check is the
    pass that opened the gate, whose words are none of the reason's. `reason` is the `operator_reason` the gate kept
    (none for an outage no answer of which led with one)."""
    record = {
        "start_utc": start,
        "end_utc": end,
        "how": how,
        "checks": 2,
        "restarts": 0,
        "last_check_utc": end or start,
        "first_detail": first,
        "last_detail": "every provider pair transferred" if end else first,
    }
    if blocking:
        record["blocking"] = True
    if not charged:
        record["charged"] = False
    if reason is not None:
        record["operator_reason"] = reason
    return record
