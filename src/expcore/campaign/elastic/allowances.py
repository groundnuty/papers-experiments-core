# Provenance: new
"""`elastic create --topup-allowance MODEL=USD` (C65): a model's lease grant raised by a dollar allowance.

A lease's grant is one reservation and the bound of two admission probes (`config.lease_grant_usd`), and workers that
predate C64 build a lease store's budget gate to count the run in flight and the next run as well, so a lease that has
spent anything is held. The grant is the one lever a controller has over workers it cannot change: they read it from
the ledger. The allowance is typed in dollars exactly as written (never through a float), finite, not below zero and no
finer than the micro-dollar the ledger counts in.
"""

import argparse
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from expcore.campaign.elastic.config import MICRO, lease_grant_usd
from expcore.campaign.elastic.records import LedgerError
from expcore.config.models import Limits, ModelSpec

FINEST_DECIMALS = 6  # the ledger counts in micro-dollars


def _dollars(text: str) -> Decimal:
    """The USD of a `--topup-allowance`, or an `ArgumentTypeError` naming what is wrong with it."""
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"--topup-allowance USD must be a number of dollars, got {text!r}") from None
    if not value.is_finite() or value < 0:
        raise argparse.ArgumentTypeError(
            f"--topup-allowance USD must be a finite number of dollars, not below zero, got {text!r}"
        )
    exponent = value.normalize().as_tuple().exponent
    if isinstance(exponent, int) and exponent < -FINEST_DECIMALS:
        raise argparse.ArgumentTypeError(
            f"--topup-allowance USD must be no finer than a micro-dollar ({FINEST_DECIMALS} decimals), got {text!r}"
        )
    if Decimal(str(float(value))) != value:  # the grant is a float of dollars: refuse a figure it would round
        raise argparse.ArgumentTypeError(
            f"--topup-allowance USD has more digits than a figure of dollars holds: {text!r}"
        )
    return value


def model_allowance(text: str) -> tuple[str, Decimal]:
    """`MODEL=USD` as typed."""
    model, _, number = text.rpartition("=")
    if not model:
        raise argparse.ArgumentTypeError(f"--topup-allowance is MODEL=USD, got {text!r}")
    return model, _dollars(number)


def grants_usd(models: Mapping[str, ModelSpec], limits: Limits, allowances: Mapping[str, Decimal]) -> dict[str, float]:
    """Each model's lease grant: `lease_grant_usd`, raised by its allowance where it has one. A model with an
    allowance and no grant (a free one) is refused, since the allowance would change nothing."""
    grants = {
        model: lease_grant_usd(spec, limits, topup_allowance_usd=float(allowances.get(model, Decimal(0))))
        for model, spec in models.items()
    }
    free = sorted(model for model in allowances if grants.get(model, 0.0) == 0.0)
    if free:
        raise LedgerError(
            f"--topup-allowance names {', '.join(free)}, which is free (a lease of it is granted nothing): "
            "an allowance would change nothing"
        )
    return grants


def allowances_micro(allowances: Mapping[str, Decimal]) -> dict[str, int]:
    """The allowances in whole micro-dollars, as the ledger records them; exact, since none is finer than one."""
    return {model: int((usd * MICRO).to_integral_exact()) for model, usd in allowances.items()}
