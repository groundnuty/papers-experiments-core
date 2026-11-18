# Provenance: new
"""One run of a campaign taken on a lease, in a store of its own: `expcore-shard/5` (C41, step 2 of #462).

The orchestrating host keeps a ledger of the campaign's runs and gives each federation one run at a time on a lease. The
ledger is another module; this one is the shape of what it gives and the kernel's side of the bargain. `run STUDY
--lease FILE` reads the lease from a file (`read_lease`) and runs exactly the run it names, in a store that holds that
run and no other, on the settings the lease sets (`study.lease_start`). A lease has six facts, and nothing else:

- `run_key`: the run, as its key text (`RunKey.text`) names it in the plan;
- `lease_id`: a whole number from 1, the fencing token. Which attempt counts is decided by lease order, never by what
  an attempt did, so the id is what a completion is judged by. A store takes one lease, once: a store that already
  holds the id refuses a second start of it (`shard_run.LeaseReplayed`), resumed or not;
- `attempt_base`: the run's charged attempts before this lease, in all its earlier leases. The store numbers its
  attempts from it (`Store.begin_attempt`): the first is base + 1, so the run's charged attempts stay unique across
  its leases (an uncharged one, set aside or interrupted, is run again under the number it had, as in any store),
  and the attempt limit (`[scheduler] max_attempts`) counts every lease's;
- `grant_usd`: what this lease may spend, probes, top-ups and attempts together; it is the run's whole budget;
- `campaign_sha256`: the SHA-256 of the campaign fingerprint the lease was issued for (`manifest.fingerprint_sha256`).
  A campaign whose fingerprint differs refuses the lease;
- `expires_utc`: when the lease ends, ISO 8601 UTC to the second with a `Z`. A start at or after it is refused.

The file adds a `schema` key, `LEASE_SCHEMA`. A store pins the lease as its shard spec (`META_SHARD`, `to_json`) under
`RULE_LEASE`, which is none of `shard.RULES`, as a selection's is not: a merge by run reads it by its rule, and today's
merge refuses it by name (`merge.facts_of`). Nothing here is read unless `--lease` is given.
"""

from __future__ import annotations

import datetime as dt
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.shard import ShardError

RULE_LEASE = "expcore-shard/5: one run of the campaign on a lease, in a store of its own"
"""The rule of a lease store; kept with every lease spec, never accepted by `ShardSpec` or `SelectionSpec`."""
LEASE_SCHEMA = "expcore-lease/1"
"""The `schema` key of a lease file."""
META_ATTEMPT_BASE = "lease_attempt_base"
"""Store meta key: the lease's attempt base as a whole number, pinned with the spec at the lease's first start. A store
without it numbers its attempts from 1, as every store did before leases."""

_FACTS = ("run_key", "lease_id", "attempt_base", "grant_usd", "campaign_sha256", "expires_utc")
_TIME = "%Y-%m-%dT%H:%M:%SZ"
_SHOWN = 12
_HEX = frozenset("0123456789abcdef")


class LeaseError(ShardError):
    """A lease file or spec is unusable; the message names the field and what it must be. A refusal of `run`."""


def _whole(value: object, least: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= least


def _moment(text: object) -> dt.datetime | None:
    """A lease's time in the form the store writes timestamps (whole seconds, `Z`); None for any other form."""
    if not isinstance(text, str):
        return None
    try:
        moment = dt.datetime.strptime(text, _TIME).replace(tzinfo=dt.UTC)
    except ValueError:
        return None
    return moment if moment.strftime(_TIME) == text else None


@dataclass(frozen=True)
class RunLease:
    """The lease of one run: its facts (module docstring), checked, and the rule a store pins it under."""

    run_key: str
    lease_id: int
    attempt_base: int
    grant_usd: float
    campaign_sha256: str
    expires_utc: str
    rule: str = RULE_LEASE

    def __post_init__(self) -> None:
        """Refuse a lease no ledger gives: each fact has one form."""
        if self.rule != RULE_LEASE:
            raise LeaseError(f"a lease is made under {RULE_LEASE!r}, not {self.rule!r}")
        if not isinstance(self.run_key, str) or not self.run_key:
            raise LeaseError("a lease's run_key is the key text of a run of the plan, not empty")
        if not _whole(self.lease_id, 1):
            raise LeaseError(f"a lease's lease_id is a whole number from 1, not {self.lease_id!r}")
        if not _whole(self.attempt_base, 0):
            raise LeaseError(f"a lease's attempt_base is a whole number from 0, not {self.attempt_base!r}")
        grant = self.grant_usd
        try:
            finite = isinstance(grant, int | float) and not isinstance(grant, bool) and math.isfinite(float(grant))
        except OverflowError:
            finite = False
        if not finite or grant < 0:
            raise LeaseError(f"a lease's grant_usd is a finite number of dollars, not below 0, not {grant!r}")
        object.__setattr__(self, "grant_usd", float(grant))  # 5 and 5.0 are one grant, and pin one text
        sha = self.campaign_sha256
        if not isinstance(sha, str) or len(sha) != 64 or not set(sha) <= _HEX:
            raise LeaseError("a lease's campaign_sha256 is the 64 lower-case hex digits of a SHA-256")
        if _moment(self.expires_utc) is None:
            raise LeaseError(
                f"a lease's expires_utc is a UTC time to the second as 2026-11-18T12:30:00Z, not {self.expires_utc!r}"
            )

    @property
    def name(self) -> str:
        """How a message names the lease: its id and its run."""
        return f"lease {self.lease_id} of run {self.run_key}"

    @property
    def expiry(self) -> dt.datetime:
        """The lease's end as a time (UTC)."""
        moment = _moment(self.expires_utc)
        assert moment is not None  # checked when the lease was made
        return moment

    def expired(self, now: dt.datetime) -> bool:
        """Whether the lease has ended at `now` (timezone-aware): the term ends at the expiry's own second."""
        return now >= self.expiry

    def check_campaign(self, campaign_sha256: str) -> None:
        """Refuse a campaign whose fingerprint hash is not the one this lease was issued for."""
        if self.campaign_sha256 != campaign_sha256:
            raise LeaseError(
                f"{self.name} was issued for the campaign with fingerprint sha256 {self.campaign_sha256[:_SHOWN]}, "
                f"and this campaign's is {campaign_sha256[:_SHOWN]}: a lease is run only by the campaign it was "
                "issued for"
            )

    def check_attempts_left(self, max_attempts: int) -> None:
        """Refuse a lease of a run with no attempt left: its base counts against the campaign's limit."""
        if self.attempt_base >= max_attempts:
            raise LeaseError(
                f"{self.name} follows {self.attempt_base} charged attempts of its run, and the campaign allows "
                f"{max_attempts}: the run has no attempt left"
            )

    def to_json(self) -> dict[str, Any]:
        """The lease as the store pins it and the export and a merge keep it: the rule and the six facts."""
        return {"rule": self.rule, **{name: getattr(self, name) for name in _FACTS}}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunLease:
        """Read a pinned lease back, refusing missing or extra keys."""
        if not isinstance(data, Mapping) or set(data) != {"rule", *_FACTS}:
            raise LeaseError(f"a lease spec has exactly the keys rule, {', '.join(_FACTS)}")
        return cls(*(data[name] for name in _FACTS), rule=data["rule"])


def _refuse_constant(text: str) -> Any:
    raise LeaseError(f"the lease file holds {text}, which is not a finite number")


def read_lease(path: str | Path) -> RunLease:
    """Read a lease file: a JSON object with exactly the keys `schema` (`LEASE_SCHEMA`) and the six facts.

    Refuses, naming the problem, a file that cannot be read or is not a JSON object, a key missing or extra, another
    schema, a fact of another form, and NaN or Infinity anywhere (Python's JSON reads them; a grant made of one would
    never hold).
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise LeaseError(f"cannot read the lease file: {type(error).__name__}") from None
    try:
        data = json.loads(text, parse_constant=_refuse_constant)
    except json.JSONDecodeError:
        raise LeaseError("the lease file is not JSON") from None
    if not isinstance(data, dict):
        raise LeaseError("the lease file is not a JSON object")
    if set(data) != {"schema", *_FACTS}:
        raise LeaseError(f"a lease file has exactly the keys schema, {', '.join(_FACTS)}")
    if data["schema"] != LEASE_SCHEMA:
        raise LeaseError(f"the lease file's schema is {data['schema']!r}, and this core reads {LEASE_SCHEMA!r}")
    return RunLease(*(data[name] for name in _FACTS))


__all__ = [
    "LEASE_SCHEMA",
    "META_ATTEMPT_BASE",
    "RULE_LEASE",
    "LeaseError",
    "RunLease",
    "read_lease",
]
