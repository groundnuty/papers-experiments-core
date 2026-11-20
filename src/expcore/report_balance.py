# Provenance: new
"""The report's sections of a batch merged by run (#462 step 4, C45): balance, and the federation-effect rerun.

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
LOST_NOTES = (
    "Runs the ledger ended as exhausted because the federations that held them were lost, counted by condition and by "
    "the site of each run's last lease. They have no analysed attempt and enter the analysis as exhausted runs (pairs "
    "missing for cause `exhausted`). A condition that loses federations more often than another (an outage condition, "
    "say) shows here as a source of bias.",
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
    spend = balance.get("ledger_spend")
    if spend is not None:
        ceiling = "no ceiling" if spend["max_usd"] is None else f"a ceiling of USD {spend['max_usd']}"
        lines += [
            f"Money at the ledger's snapshot: USD {spend['total_usd']} charged to the leases ({ceiling}), of which USD "
            f"{spend['locked_usd']} is locked in unsettled leases (charged their grant until their spend is settled).",
            "",
        ]
    for model in balance["models"]:
        for key, label in (("by_site", "site"), ("by_federation", "federation")):
            cross = model[key]
            places = sorted({place for counts in cross.values() for place in counts})
            rows = [[condition, *(cross[condition][place] for place in places)] for condition in model["conditions"]]
            lines += [f"### {model['model']}: condition by {label}", "", *table(["Condition", *places], rows), ""]
    return lines + _lost_lines(balance, table)


def _lost_lines(balance: Mapping[str, Any], table: Table) -> list[str]:
    """The runs exhausted by federation loss, per model, condition by site; a sentence when there are none."""
    lost = [
        model for model in balance["models"] if any(n for c in model["exhausted_by_loss"].values() for n in c.values())
    ]
    if not lost:
        return ["No run was exhausted by federation loss.", ""]
    lines = [*LOST_NOTES, ""]
    for model in lost:
        cross = model["exhausted_by_loss"]
        places = sorted({place for counts in cross.values() for place in counts})
        rows = [[condition, *(cross[condition][place] for place in places)] for condition in model["conditions"]]
        lines += [f"### {model['model']}: runs exhausted by federation loss, condition by site", ""]
        lines += [*table(["Condition", *places], rows), ""]
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
