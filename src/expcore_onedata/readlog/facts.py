# Provenance: new
"""The read-log channel's fact in a run's `infrastructure_facts` (through the one writer, `infrafacts`, O89).

A backend writes it once, when the collector is up:

    sandbox.record_infrastructure_fact(facts.KEY, facts.fact(...), owner=facts.OWNER)

so a reader of one run's record knows which collector and which client ran, where, and which sender uid it accepted.
"""

from typing import Any

from expcore_onedata.readlog.paths import FACT_KEY as KEY
from expcore_onedata.readlog.paths import FACTS_OWNER as OWNER

PLACEMENTS = ("apptainer_helper", "docker_container")  # where the backend ran the collector


def fact(*, collector_sha256: str, client_sha256: str, placement: str, expected_peer_uid: int) -> dict[str, Any]:
    """The fact's value: `{"collector_sha256", "client_sha256", "placement", "expected_peer_uid"}`. Raises ValueError
    for a hash that is not 64 lower-case hex digits, a placement not in `PLACEMENTS` or a uid that is not a whole
    number of 0 to 2**32."""
    raise NotImplementedError


__all__ = ["KEY", "OWNER", "PLACEMENTS", "fact"]
