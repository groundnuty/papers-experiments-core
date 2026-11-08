# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/analysis/sim.py, blob 87b554dd03387cf667cdc77596a2308b48ba0a39
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Simulator of scored records with planted, independently specified effects (#14).

DELIBERATELY INDEPENDENT of the analysis pipeline: this module imports only
numpy and `dataset_76.records` (a test enforces it). The true
effects are the *inputs* (`SimSpec`), stated on the estimand's own scale:
proportions for omission/coverage (baseline and target, two separate truths)/F1/false alarm, log2 tokens for cost. All
noise is bounded (uniform), so no probability is ever clipped and the true
contrast of two options is the plain difference of the stated parameters:

    true(A - B) = mean_m [ mu[l][A] + eff[m][l][A] ] - mean_m [ mu[l][B] + eff[m][l][B] ]

(region noise, model shifts and run noise are shared or mean-zero, so they
cancel in expectation). Nuisance processes (unknown labels, time-cap and
turn-cap terminations) are independent of the outcome unless the spec says so.
"""
from __future__ import annotations

import dataclasses

import numpy as np

from dataset_76.records import Layout, OmissionLabel, Option, RunId, ScoredRow, Termination

OPTS = ("T", "D", "M")
LAYS = ("state_a", "state_b")
KINDS = ("omission", "coverage_baseline", "coverage_target", "event_f1", "false_alarm", "tokens")   # one noise draw each


@dataclasses.dataclass
class SimSpec:
    """True parameters. Dict keys: (layout, option) unless noted."""

    omission: dict            # mean omission probability among finished, labelled runs
    coverage_baseline: dict   # mean share of the required baseline extent analysed
    coverage_target: dict     # mean share of the required target extent analysed (a separate truth)
    event_f1: dict            # mean F1 on instances with events
    log2_tokens: dict         # mean log2(total tokens)
    false_alarm: dict         # false-alarm probability on event-free instances
    models: tuple = ("m1", "m2", "m3", "m4", "m5", "m6", "m7")
    n_regions: int = 25
    periods: int = 2
    seeds: tuple = (1, 2, 3)
    # planted per-model heterogeneity: (model, layout, option) -> additive shift
    # on omission / both coverages / F1 / log2 tokens (default 0)
    model_effect: dict = dataclasses.field(default_factory=dict)
    # nuisance shared across options (additive; cancels in every contrast)
    model_shift: float = 0.04          # half-width of a uniform per-model shift
    region_halfwidth: float = 0.08     # uniform region shift, shared across options
    region_option_halfwidth: float = 0.08  # uniform region x option shift (drives the SD of differences)
    run_halfwidth: float = 0.05        # uniform run-level noise on continuous outcomes
    tokens_halfwidth: float = 0.4      # log2 units (run noise)
    tokens_noise_scale: float = 5.0    # region/model noise on log2 tokens = scale x the proportion half-widths
    p_event_free: float = 0.25
    # nuisance, per option (independent of the true outcome)
    p_unknown: dict = dataclasses.field(default_factory=lambda: {o: 0.05 for o in OPTS})
    p_time_cap: dict = dataclasses.field(default_factory=lambda: {o: 0.03 for o in OPTS})
    p_turn_cap: dict = dataclasses.field(default_factory=lambda: {o: 0.03 for o in OPTS})
    p_disclose: float = 0.5            # P(self-report says incomplete | omitting)
    p_correct: float = 0.7

    def eff(self, m, layout, option) -> float:
        return self.model_effect.get((m, layout, option), 0.0)

    def true_contrast(self, outcome: str, layout: str, a: str, b: str) -> float:
        """The planted A-B contrast, by plain arithmetic on the inputs."""
        tab = {"omission": self.omission, "coverage_baseline": self.coverage_baseline,
               "coverage_target": self.coverage_target, "event_f1": self.event_f1,
               "tokens": self.log2_tokens, "false_alarm": self.false_alarm}[outcome]
        shift = lambda o: np.mean([self.eff(m, layout, o) for m in self.models])
        return (tab[(layout, a)] + shift(a)) - (tab[(layout, b)] + shift(b))

    def validate(self) -> None:
        slack = self.model_shift + self.region_halfwidth + self.region_option_halfwidth
        for name, tab, extra in (("omission", self.omission, 0.0),
                                 ("coverage_baseline", self.coverage_baseline, self.run_halfwidth),
                                 ("coverage_target", self.coverage_target, self.run_halfwidth),
                                 ("event_f1", self.event_f1, self.run_halfwidth), ("false_alarm", self.false_alarm, 0.0)):
            for (l, o), mu in tab.items():
                worst = max(abs(self.eff(m, l, o)) for m in self.models)
                if not (worst + slack + extra <= min(mu, 1 - mu)):
                    raise ValueError(f"{name}[{l},{o}]={mu}: noise could leave [0,1]; planted truth would clip")


def simulate(spec: SimSpec, rng: np.random.Generator) -> list[ScoredRow]:
    spec.validate()
    R, P = spec.n_regions, spec.periods
    u = rng.uniform
    region_sh = {k: u(-spec.region_halfwidth, spec.region_halfwidth, R) for k in KINDS}
    model_sh = {k: {m: u(-spec.model_shift, spec.model_shift) for m in spec.models} for k in KINDS}
    ro = {(k, l, o): u(-spec.region_option_halfwidth, spec.region_option_halfwidth, R)
          for k in KINDS for l in LAYS for o in OPTS}
    event_free = rng.random((R, P)) < spec.p_event_free
    rows: list[ScoredRow] = []
    for r in range(R):
        for p in range(P):
            inst = f"r{r:02d}-p{p}"
            for l in LAYS:
                for o in OPTS:
                    for m in spec.models:
                        e = spec.eff(m, l, o)   # one planted shift, applied to all five outcomes
                        k = spec.tokens_noise_scale

                        def val(k, tab):
                            return tab[(l, o)] + e + region_sh[k][r] + ro[(k, l, o)][r] + model_sh[k][m]

                        p_om = val("omission", spec.omission)
                        p_fa = val("false_alarm", spec.false_alarm)
                        for s in spec.seeds:
                            rows.append(_one(spec, rng, inst, r, bool(event_free[r, p]), l, o, m, s, e,
                                             p_om, p_fa, val("coverage_baseline", spec.coverage_baseline),
                                             val("coverage_target", spec.coverage_target), val("event_f1", spec.event_f1),
                                             spec.log2_tokens[(l, o)] + e + region_sh["tokens"][r] * k
                                             + ro[("tokens", l, o)][r] * k + model_sh["tokens"][m] * k))
    return rows


def _one(spec, rng, inst, r, ev_free, l, o, m, s, e, p_om, p_fa, cov_b, cov_t, f1, lt):
    run = RunId(inst, Option(o), Layout(l), m, s)
    x = rng.random()
    tc, tn = spec.p_time_cap[o], spec.p_turn_cap[o]
    tokens = int(round(2 ** (lt + rng.uniform(-spec.tokens_halfwidth, spec.tokens_halfwidth))))
    tokens = max(tokens, 2)
    common = dict(run=run, region_id=f"reg{r:02d}", event_free=ev_free,
                  input_tokens=int(tokens * 0.7), output_tokens=tokens - int(tokens * 0.7),
                  wall_time_s=float(tokens) / 100, bytes_between_sites=None,
                  bytes_into_context=tokens * 3, tokens_into_context=tokens // 2,
                  block="main", exclude_from_analysis=False)    # every simulated run is a main-block run
    if x < tc + tn:
        term = Termination.TIME_CAP if x < tc else Termination.TURN_CAP
        return ScoredRow(termination=term, finished=False, label=None, coverage_baseline=None,
                         coverage_target=None, self_report_complete=None, event_precision=None,
                         event_recall=None, event_f1=None, false_alarm=None, day_agreement=None,
                         cell_day_f1=None, cf_computation_f1=None, cf_omission_f1=None, correct=None,
                         end_to_end=False, **common)
    if rng.random() < spec.p_unknown[o]:
        label = OmissionLabel.UNKNOWN
    else:
        label = OmissionLabel.OMITTING if rng.random() < p_om else OmissionLabel.COMPLETE
    omitting = label is OmissionLabel.OMITTING
    c_b = cov_b + rng.uniform(-spec.run_halfwidth, spec.run_halfwidth)
    c_t = cov_t + rng.uniform(-spec.run_halfwidth, spec.run_halfwidth)
    complete_claim = not (omitting and rng.random() < spec.p_disclose)
    return ScoredRow(termination=Termination.REPORT, finished=True, label=label,
                     coverage_baseline=c_b, coverage_target=c_t, self_report_complete=complete_claim,
                     event_precision=None, event_recall=None,
                     event_f1=None if ev_free else f1 + rng.uniform(-spec.run_halfwidth, spec.run_halfwidth),
                     false_alarm=(rng.random() < p_fa) if ev_free else None, day_agreement=None,
                     cell_day_f1=None, cf_computation_f1=None, cf_omission_f1=None,
                     correct=bool(rng.random() < spec.p_correct),
                     end_to_end=bool(label is OmissionLabel.COMPLETE and rng.random() < spec.p_correct), **common)
