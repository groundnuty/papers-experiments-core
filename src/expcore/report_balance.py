# Provenance: new
"""The report's sections of a batch merged by run (#462 step 4, C43): balance, and the federation-effect rerun.

- **Balance over sites and federations** (`campaign.balance`): per model, the planned runs of each condition by site
  and by federation, every cell written out. Descriptive: no test is made and nothing is enforced.
- **Sensitivity analysis: site and federation effects** (`stats.federation_effect`): each adjusted outcome's fit
  (runs, sites, federations, the residual and federation variances, or why it was not adjusted), the outcomes left as
  they are and why, and the preregistered contrasts refit on the adjusted outcomes in the primary analysis's own
  tables. It is reported beside the primary result and never replaces it.

A batch merged otherwise has neither section, so its report is byte for byte what it was.
"""

from collections.abc import Callable, Mapping
from typing import Any

from expcore.stats.federation_effect import FederationAnalysis

Table = Callable[..., list[str]]

BALANCE_NOTES = (
    "Planned runs of each condition by the site and the federation that held each run's counted lease, per model. "
    "Assignment is outcome-blind and in the seeded order, so these counts show how the conditions spread over the "
    "placements; they are never enforced, and a federation that ran one run of a model holds one condition of it by "
    "design. The federation effect is the declared sensitivity rerun below.",
)
FEDERATION_NOTES = (
    "Each outcome of a preregistered contrast of one outcome is refit with a linear mixed model: its (condition, "
    "model) cell and task as fixed effects, site as a fixed effect and federation as a random effect (REML). The "
    "estimated site effect and federation effect are subtracted from each run's outcome, and the preregistered "
    "contrasts are run again on the adjusted outcomes with the same pairing, estimator and Holm families. A column a "
    "ratio contrast reads is left as it is. This analysis is beside the primary one, never in its place.",
)
LEFT_OUT = "site and federation effects"


def balance_lines(balance: Mapping[str, Any] | None, table: Table) -> list[str]:
    """The balance section, ending with a blank line; none without a balance."""
    if balance is None:
        return []
    lines = ["## Balance over sites and federations", "", *BALANCE_NOTES, "", f"Runs: {balance['runs']}.", ""]
    for model in balance["models"]:
        for key, label in (("by_site", "site"), ("by_federation", "federation")):
            cross = model[key]
            places = sorted({place for counts in cross.values() for place in counts})
            rows = [[condition, *(cross[condition][place] for place in places)] for condition in model["conditions"]]
            lines += [f"### {model['model']}: condition by {label}", "", *table(["Condition", *places], rows), ""]
    return lines


def federation_lines(
    federation: FederationAnalysis | None, table: Table, sensitivity: Callable[[Any], list[str]]
) -> list[str]:
    """The federation-effect rerun's section, ending with a blank line; none without one. `sensitivity` renders an
    analysis result's contrasts in the primary analysis's own tables."""
    if federation is None:
        return []
    lines = ["## Sensitivity analysis: site and federation effects", "", *FEDERATION_NOTES, ""]
    rows = [
        [
            fit.outcome,
            fit.runs,
            len(fit.sites),
            len(fit.federations),
            _figure(fit.sigma2),
            _figure(fit.tau2),
            fit.refused or "adjusted",
        ]
        for fit in federation.fits
    ]
    header = ["Outcome", "Runs", "Sites", "Federations", "Residual variance", "Federation variance", "Adjustment"]
    if rows:
        lines += [*table(header, rows), ""]
    for outcome, reason in federation.unadjusted:
        lines.append(f"- {outcome}: left as it is, {reason}.")
    if federation.unadjusted:
        lines.append("")
    if federation.sensitivity is None:
        return [*lines, f"Not computed: {federation.refused}.", ""]
    return [*lines, *sensitivity(federation.sensitivity), ""]


def _figure(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4g}"
