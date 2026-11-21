# Provenance: new
"""A lease's grant raised by dollars typed at the command line: `elastic create --topup-allowance MODEL=USD` and
`elastic set-grant LEDGER MODEL=USD` (C66).

A lease's grant is one reservation and the bound of two admission probes (`config.lease_grant_usd`). A lease's budget
gate holds a lease whose spend and reservations pass it: a run pending again after an attempt that spent a reservation
is weighed as spent plus one more, and on a core that predates C64 a run in flight is counted as reserved and again as
the next run. The grant is the one lever a controller has over workers it cannot change: they read it from the
ledger. An amount is typed in dollars and read exactly as written (never through a float): finite, above zero, no finer
than the micro-dollar the ledger counts in, and no more than a grant held as a float of dollars keeps exactly
(`MAX_GRANT_MICRO`). A huge exponent is refused by that bound, not overflowed.
"""

import argparse
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from expcore.campaign.elastic.config import (
    MAX_GRANT_MICRO,
    MICRO,
    check_grant_held_exactly,
    lease_grant_micro,
    usd_of,
)
from expcore.campaign.elastic.records import LedgerError
from expcore.config.models import Limits, ModelSpec

FINEST_DECIMALS = 6  # the ledger counts in micro-dollars
MAX_USD = Decimal(MAX_GRANT_MICRO) / MICRO


def dollars_of(flag: str, text: str) -> Decimal:
    """The USD of `flag`'s `MODEL=USD` as typed, or an `ArgumentTypeError` naming what is wrong with it."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"{flag}: USD must be a number of dollars, got {text!r}") from None
    if not value.is_finite():
        raise argparse.ArgumentTypeError(f"{flag}: USD must be a finite number of dollars, got {text!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{flag}: USD must be above zero, got {text!r}")
    if value > MAX_USD:
        raise argparse.ArgumentTypeError(f"{flag}: USD must be at most {MAX_USD:f}, got {text!r}")
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -FINEST_DECIMALS:
        raise argparse.ArgumentTypeError(
            f"{flag}: USD must be no finer than a micro-dollar ({FINEST_DECIMALS} decimals), got {text!r}"
        )
    return value


def model_dollars(flag: str, text: str) -> tuple[str, Decimal]:
    """`MODEL=USD` as typed."""
    model, _, number = text.rpartition("=")
    if not model:
        raise argparse.ArgumentTypeError(f"{flag} is MODEL=USD, got {text!r}")
    return model, dollars_of(flag, number)


def model_allowance(text: str) -> tuple[str, Decimal]:
    """`--topup-allowance MODEL=USD` as typed."""
    return model_dollars("--topup-allowance", text)


def micro_of(usd: Decimal) -> int:
    """Dollars in whole micro-dollars, exactly: one finer than a micro-dollar, or not above zero, is refused (a caller
    that does not come through the command line gets the same bound)."""
    problem = f"{usd} USD is not a whole number of micro-dollars above zero and at most {MAX_USD:f} USD"
    if not usd.is_finite() or usd <= 0 or usd > MAX_USD:  # compared before any arithmetic, which a huge exponent breaks
        raise ValueError(problem)
    scaled = usd.scaleb(FINEST_DECIMALS)
    if scaled != scaled.to_integral_value():
        raise ValueError(problem)
    return int(scaled)


def allowances_micro(allowances: Mapping[str, Decimal]) -> dict[str, int]:
    """The allowances in whole micro-dollars, as the ledger records them."""
    return {model: micro_of(usd) for model, usd in allowances.items()}


def grants_usd(models: Mapping[str, ModelSpec], limits: Limits, allowances: Mapping[str, Decimal]) -> dict[str, float]:
    """Each model's lease grant in dollars: `lease_grant_micro`, raised by its allowance where it has one, summed as
    whole micro-dollars. A model with an allowance and no grant (a free one) is refused, since the allowance would
    change nothing, and so is a grant that a figure of dollars cannot hold exactly (`check_grant_held_exactly`)."""
    raised = allowances_micro(allowances)
    grants = {
        model: lease_grant_micro(spec, limits, topup_allowance_micro=raised.get(model, 0))
        for model, spec in models.items()
    }
    free = sorted(model for model in raised if grants.get(model, 0) == 0)
    if free:
        raise LedgerError(
            f"--topup-allowance names {', '.join(free)}, which is free (a lease of it is granted nothing): "
            "an allowance would change nothing"
        )
    for model in raised:
        check_grant_held_exactly(model, grants[model])
    return {model: usd_of(micro) for model, micro in grants.items()}
