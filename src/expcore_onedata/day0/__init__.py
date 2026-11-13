# Provenance: new
"""Day-0 checks as code: facts about a deployment, measured before a study depends on them, in a dated report.

`registry` holds the checks' vocabulary, `checks` the read-only ones, `mutating` the two that change state (and only
with consent), `outage` the one that takes a provider down (with consent and a sanction), `report` the dated report and
`runner` the run. See `python -m expcore_onedata.day0 --help`.

The three modules of checks are imported here, in this order, so the registry's order (the report's) is the same
whatever module of the package is imported first.
"""

from expcore_onedata.day0 import checks as _checks  # noqa: F401 -- importing registers the checks
from expcore_onedata.day0 import mutating as _mutating  # noqa: F401
from expcore_onedata.day0 import outage as _outage  # noqa: F401
