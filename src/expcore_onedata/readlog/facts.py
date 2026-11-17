# Provenance: new
"""The read-log channel's fact in a run's `infrastructure_facts` (through the one writer, `infrafacts`, O89).

A backend writes it once, when the collector is up:

    sandbox.record_infrastructure_fact(facts.KEY, facts.fact(...), owner=facts.OWNER)

so a reader of one run's record knows which collector and which client ran, where, and which sender uid it accepted.
The key is declared for this owner in `infrafacts.OWNED`, so a stray write by another owner is the one refused, even
before this one has written.
"""

import re
from typing import Any

from expcore_onedata.readlog.paths import FACT_KEY as KEY
from expcore_onedata.readlog.paths import FACTS_OWNER as OWNER

PLACEMENTS = ("apptainer_helper", "docker_container")  # where the backend ran the collector
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _sha(name: str, value: Any) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"read-log fact: {name} is 64 lower-case hexadecimal digits")
    return value


def fact(*, collector_sha256: str, client_sha256: str, placement: str, expected_peer_uid: int) -> dict[str, Any]:
    """The fact's value: `{"collector_sha256", "client_sha256", "placement", "expected_peer_uid"}`. Raises ValueError
    for a hash that is not 64 lower-case hex digits, a placement not in `PLACEMENTS` or a uid that is not a whole
    number of 0 to 2**32."""
    if placement not in PLACEMENTS:
        raise ValueError(f"read-log fact: placement is one of {', '.join(PLACEMENTS)}")
    uid = expected_peer_uid
    if isinstance(uid, bool) or not isinstance(uid, int) or not 0 <= uid <= 2**32:
        raise ValueError("read-log fact: expected_peer_uid is a whole number of 0 to 2**32")
    return {
        "collector_sha256": _sha("collector_sha256", collector_sha256),
        "client_sha256": _sha("client_sha256", client_sha256),
        "placement": placement,
        "expected_peer_uid": uid,
    }


__all__ = ["KEY", "OWNER", "PLACEMENTS", "fact"]
