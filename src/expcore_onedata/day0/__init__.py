# Provenance: new
"""Day-0 checks as code: facts about a deployment, measured before a study depends on them, in a dated report.

`registry` holds the checks' vocabulary, `checks` the read-only ones, `mutating` the two that change state (and only
with consent), `report` the dated report and `runner` the run. See `python -m expcore_onedata.day0 --help`.
"""
