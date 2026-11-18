# Provenance: new
"""The elastic ledger's settings and its runs (#462 step 1, C42): what is pinned when the ledger is created.

Dollars are counted in whole micro-dollars, so that the budget's sums are exact: a ceiling is rounded down, and a
grant or a spend is rounded up, so rounding can only make the ledger hold more than it must, never less.

A lease's **grant** (`lease_grant_usd`) is the theoretical maximum one lease can spend: one run's reservation
(`shard_budget.reservation_usd`, the larger of the two bounds of C25 and C27), the bound of each of the two admission
probes every store start sends (`attempt_bounds.request_bound_usd`, C26), and a declared top-up allowance. The lease
store's budget gate is held to it (step 2), so a lease cannot spend more.

A lease's **hard term** (`hard_term_s`) is the run's wall cap, the reset bound and the platform's declared deferral
bound, plus 10 minutes: past it a lease expires even if its heartbeat still advances, since a hung kernel can keep its
heartbeat thread alive.
"""

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from expcore.campaign.attempt_bounds import request_bound_usd
from expcore.campaign.factors import FactorSchema
from expcore.campaign.manifest import Manifest
from expcore.campaign.shard_budget import reservation_usd
from expcore.config.models import Limits, ModelSpec

MICRO = 1_000_000
STALE_S = 300.0  # a counter read unchanged this long expires its lease
SITE_OUT_S = 900.0  # a site unread this long is out: its live leases expire
HARD_TERM_MARGIN_S = 600.0
PRIOR_RUNS = 3  # the prior of a cell's expected time counts as this many runs
_HEX64 = re.compile(r"[0-9a-f]{64}")


def micro_down(usd: float) -> int:
    """Dollars as whole micro-dollars, rounded down: a ceiling."""
    return int((Decimal(str(usd)) * MICRO).to_integral_value(rounding=ROUND_FLOOR))


def micro_up(usd: float) -> int:
    """Dollars as whole micro-dollars, rounded up: a grant or a spend."""
    return int((Decimal(str(usd)) * MICRO).to_integral_value(rounding=ROUND_CEILING))


def usd_of(micro: int) -> float:
    """Whole micro-dollars as dollars."""
    return float(Decimal(micro) / MICRO)


def _dollars(name: str, value: Any) -> float:
    """A finite, non-negative number of dollars, or ValueError naming the field."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite number of dollars, not below zero, got {value!r}")
    return float(value)


def _seconds(name: str, value: Any) -> float:
    """A finite, positive number of seconds, or ValueError naming the field."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive, finite number of seconds, got {value!r}")
    return float(value)


def _whole(name: str, value: Any, least: int) -> int:
    """An int of at least `least`, or ValueError naming the field."""
    if isinstance(value, bool) or not isinstance(value, int) or value < least:
        raise ValueError(f"{name} must be a whole number of at least {least}, got {value!r}")
    return value


def _name(what: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{what} must be a non-empty string, got {value!r}")
    return value


@dataclass(frozen=True)
class LedgerRun:
    """One planned run as the ledger holds it: its key, its place in the seeded order, its cell and declared set."""

    run_key: str
    seq: int
    model: str
    condition: str
    set_index: int = 0

    def __post_init__(self) -> None:
        for what in ("run_key", "model", "condition"):
            _name(what, getattr(self, what))
        _whole("seq", self.seq, 0)
        _whole("set_index", self.set_index, 0)


@dataclass(frozen=True)
class LedgerConfig:
    """What a ledger is created with and keeps for its whole life (pinned in its meta).

    - `fingerprint`: the campaign fingerprint's SHA-256 (`manifest.fingerprint_sha256`); a federation must present it.
    - `hard_term_s`: a lease's hard term (`hard_term_s` builds it); `prior_run_s`: a cell's expected lease time before
      any lease of it counted, by default half the wall cap.
    - `max_attempts`: charged attempts a run may use across its counted leases (C03).
    - `caps`: leases in flight or held back at once, per model; a model not listed has no cap.
    - `max_usd`, `per_model_usd`: the budget and the models' ceilings; None: no budget. `grants_usd`: each model's grant
      (`lease_grant_usd`), required for every planned model when there is a budget.
    - `stale_s`, `site_out_s`: how long a counter may be read unchanged, and how long a site may go unread.
    """

    fingerprint: str
    hard_term_s: float
    prior_run_s: float
    max_attempts: int = 3
    caps: Mapping[str, int] = field(default_factory=dict)
    max_usd: float | None = None
    per_model_usd: Mapping[str, float] = field(default_factory=dict)
    grants_usd: Mapping[str, float] = field(default_factory=dict)
    stale_s: float = STALE_S
    site_out_s: float = SITE_OUT_S

    def __post_init__(self) -> None:
        if not isinstance(self.fingerprint, str) or not _HEX64.fullmatch(self.fingerprint):
            raise ValueError("fingerprint must be the campaign fingerprint's SHA-256, 64 lower-case hex digits")
        for name in ("hard_term_s", "prior_run_s", "stale_s", "site_out_s"):
            object.__setattr__(self, name, _seconds(name, getattr(self, name)))
        _whole("max_attempts", self.max_attempts, 1)
        object.__setattr__(
            self, "caps", {_name("a capped model", m): _whole(f"caps[{m}]", c, 1) for m, c in self.caps.items()}
        )
        if self.max_usd is not None:
            object.__setattr__(self, "max_usd", _dollars("max_usd", self.max_usd))
            if self.max_usd == 0:
                raise ValueError("max_usd of 0 holds every lease: leave the budget out to run without one")
        elif self.per_model_usd:
            raise ValueError("per_model_usd needs a max_usd: a model's ceiling is part of a budget")
        for name in ("per_model_usd", "grants_usd"):
            values = getattr(self, name)
            object.__setattr__(self, name, {_name(name, m): _dollars(f"{name}[{m}]", v) for m, v in values.items()})

    def grant_micro(self, model: str) -> int:
        """A model's grant in micro-dollars; 0 without a budget, or for a model granted nothing."""
        return micro_up(self.grants_usd.get(model, 0.0)) if self.max_usd is not None else 0

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "LedgerConfig":
        return cls(**json.loads(text))


def check_plan(runs: Sequence[LedgerRun], config: LedgerConfig) -> None:
    """Refuse runs that are empty or repeat a key or a place, and a budget under which some model could never lease:
    no grant for it, or a grant above `max_usd` or above its own ceiling."""
    if not runs:
        raise ValueError("a ledger needs at least one run")
    for what in ("run_key", "seq"):
        values = [getattr(run, what) for run in runs]
        if len(set(values)) != len(values):
            repeated = next(v for v in values if values.count(v) > 1)
            raise ValueError(f"{what} {repeated!r} appears more than once in the ledger's runs")
    if config.max_usd is None:
        return
    for model in sorted({run.model for run in runs}):
        if model not in config.grants_usd:
            raise ValueError(f"model {model} has no grant: with a budget, every planned model needs one")
        grant = config.grant_micro(model)
        if grant > micro_down(config.max_usd):
            raise ValueError(f"model {model}'s grant {usd_of(grant):g} USD is above max_usd {config.max_usd:g}")
        ceiling = config.per_model_usd.get(model)
        if ceiling is not None and grant > micro_down(ceiling):
            raise ValueError(f"model {model}'s grant {usd_of(grant):g} USD is above its per_model_usd {ceiling:g}")


def lease_grant_usd(spec: ModelSpec, limits: Limits, *, topup_allowance_usd: float = 0.0) -> float:
    """The grant of one lease of the model: one run's reservation, two admission probes and the top-up allowance,
    each rounded up to a micro-dollar; 0 for a free model. A model with no price is refused."""
    reservation, probe = reservation_usd(spec, limits), request_bound_usd(spec, limits)
    if reservation is None or probe is None:
        raise ValueError(f"model {spec.key} has no price and is not marked free: its leases cannot be granted")
    if reservation == 0 and probe == 0:
        return 0.0
    allowance = _dollars("topup_allowance_usd", topup_allowance_usd)
    return usd_of(micro_up(reservation) + 2 * micro_up(probe) + micro_up(allowance))


def hard_term_s(wall_cap_s: float, reset_bound_s: float, deferral_bound_s: float) -> float:
    """A lease's hard term: the run's wall cap, the reset bound and the platform's deferral bound, plus 10 minutes."""
    parts = [("wall_cap_s", wall_cap_s), ("reset_bound_s", reset_bound_s), ("deferral_bound_s", deferral_bound_s)]
    total = 0.0
    for name, value in parts:
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
            raise ValueError(f"{name} must be a finite number of seconds, not below zero, got {value!r}")
        total += float(value)
    return total + HARD_TERM_MARGIN_S


def ledger_runs(
    manifest: Manifest, schema: FactorSchema, sets: Sequence[Mapping[str, Sequence[Any]]] = ({},)
) -> list[LedgerRun]:
    """The ledger's runs of a manifest, in its seeded order. `sets` is the declared order of sets, each a filter of
    factor (or `condition`) to the levels it takes; an empty filter takes every run. A run is in the first set that
    takes it; a run that no set takes is refused, naming it."""
    known = {*schema.order, "condition"}
    for index, chosen in enumerate(sets):
        unknown = sorted(set(chosen) - known)
        if unknown:
            raise ValueError(f"declared set {index} filters on {unknown}, which are not factors of the plan")
    out = []
    for row in manifest.rows:
        condition = schema.condition_of(row.key)
        values = {**row.key.factors, "condition": condition}
        index = next(
            (i for i, chosen in enumerate(sets) if all(values[k] in levels for k, levels in chosen.items())), None
        )
        if index is None:
            raise ValueError(f"run {row.key.text} is in no declared set: add a set that takes it")
        out.append(LedgerRun(row.key.text, row.seq, str(values["model"]), condition, index))
    return out
