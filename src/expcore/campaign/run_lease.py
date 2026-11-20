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
- `attempt_base`: the run's charged attempts before this lease, in all its earlier leases (the ledger's own count: a
  lost lease's started attempts cannot be known). The store numbers its attempts from it (`Store.begin_attempt`): the
  first is base + 1, and the attempt limit (`[scheduler] max_attempts`) counts every lease's charged attempts. Within
  a run's counted lease the charged attempts are unique; an uncharged attempt (set aside or interrupted) is run again
  under the number it had, as in any store, so two leases of one run can each have an attempt of one number. **The
  lease id tells them apart**: it is pinned in the store's meta (`shard_spec`), written in every record the store's
  attempts produce (`RunRecord.lease_id`, `RunIdentity.lease_id`) and in every run row of its export, so a merge by
  run keys evidence by (run, lease, attempt) and two leases' attempt 2 never collide;
- `grant_micro_usd`: what this lease may spend, probes, top-ups and attempts together, as a whole number of
  micro-dollars: exact, with no float to round (the ledger's own unit). It is the run's whole budget, and the kernel
  turns it into dollars (`grant_usd`) only where its budget gate takes dollars;
- `campaign_sha256`: the SHA-256 of the campaign fingerprint the lease was issued for (`manifest.fingerprint_sha256`).
  A campaign whose fingerprint differs refuses the lease;
- `expires_utc`: when the lease ends, ISO 8601 UTC to the second with a `Z`. A start at or after it is refused.

A lease the elastic ledger issues carries one more fact, `declared` (C48): the model's declared fingerprints at the
lease's issue, `{"fingerprints": [...], "none": true|false}` (`expcore.models.served.DeclaredSet`). The run judges its
replies against it (`campaign.identity`): a reply under a fingerprint it does not declare, or with no fingerprint when
"none" is not declared, makes the run infrastructure, and the lease store holds the model so the lease ends. A lease
without it (one written by hand, or by a ledger made before the declaration log) is judged by models.toml's set, as
before.

The file adds a `schema` key, `LEASE_SCHEMA`. A store pins the lease as its shard spec (`META_SHARD`, `to_json`) under
`RULE_LEASE`, which is none of `shard.RULES`, as a selection's is not: a merge by run reads it by its rule, and today's
merge refuses it by name (`merge.facts_of`). Nothing here is read unless `--lease` is given.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.campaign.shard import ShardError, usd_of
from expcore.models.served import MIXED_FINGERPRINTS, DeclaredSet

RULE_LEASE = "expcore-shard/5: one run of the campaign on a lease, in a store of its own"
"""The rule of a lease store; kept with every lease spec, never accepted by `ShardSpec` or `SelectionSpec`."""
LEASE_SCHEMA = "expcore-lease/1"
"""The `schema` key of a lease file."""
LEASE_JOURNAL = "spend.jsonl"
"""The name of a lease run's spend journal (`run --spend-journal`): beside the lease's study copy, in the directory that
holds nothing else the journal's start would count (`lost_spend.open_journal` reads every `*.jsonl` of it). A reader of
a dead lease store (`study.merge_by_run.dead_store_spend`) takes the journal from there."""
META_ATTEMPT_BASE = "lease_attempt_base"
"""Store meta key: the lease's attempt base as a whole number, pinned with the spec at the lease's first start. A store
without it numbers its attempts from 1, as every store did before leases."""

_FACTS = ("run_key", "lease_id", "attempt_base", "grant_micro_usd", "campaign_sha256", "expires_utc")
DECLARED = "declared"  # the optional fact of a lease the elastic ledger issues (C48)
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
    grant_micro_usd: int
    campaign_sha256: str
    expires_utc: str
    rule: str = RULE_LEASE
    declared: DeclaredSet | None = None  # the model's declared fingerprints at issue (C48); None: models.toml's set

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
        if not _whole(self.grant_micro_usd, 0):
            raise LeaseError(
                f"a lease's grant_micro_usd is a whole number of micro-dollars from 0, not {self.grant_micro_usd!r}"
            )
        sha = self.campaign_sha256
        if not isinstance(sha, str) or len(sha) != 64 or not set(sha) <= _HEX:
            raise LeaseError("a lease's campaign_sha256 is the 64 lower-case hex digits of a SHA-256")
        if _moment(self.expires_utc) is None:
            raise LeaseError(
                f"a lease's expires_utc is a UTC time to the second as 2026-11-18T12:30:00Z, not {self.expires_utc!r}"
            )
        if self.declared is not None and not isinstance(self.declared, DeclaredSet):
            raise LeaseError("a lease's declared fingerprints are a DeclaredSet")

    @property
    def name(self) -> str:
        """How a message names the lease: its id and its run."""
        return f"lease {self.lease_id} of run {self.run_key}"

    @property
    def grant_usd(self) -> float:
        """The grant in dollars, for the budget gate, which takes dollars: the nearest float to the exact amount."""
        return usd_of(self.grant_micro_usd)

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
        """The lease as the store pins it and the export and a merge keep it: the rule and the six facts, and the
        declared fingerprints when the lease carries them."""
        out = {"rule": self.rule, **{name: getattr(self, name) for name in _FACTS}}
        if self.declared is not None:
            out[DECLARED] = declared_json(self.declared)
        return out

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> RunLease:
        """Read a pinned lease back, refusing missing or extra keys."""
        if not isinstance(data, Mapping) or set(data) - {DECLARED} != {"rule", *_FACTS}:
            raise LeaseError(f"a lease spec has exactly the keys rule, {', '.join(_FACTS)} (and {DECLARED})")
        declared = declared_of(data[DECLARED]) if DECLARED in data else None
        return cls(*(data[name] for name in _FACTS), rule=data["rule"], declared=declared)


def declared_json(declared: DeclaredSet) -> dict[str, Any]:
    """A declared set as a lease file and the ledger write it: the fingerprints in order, and whether "none" is."""
    return {"fingerprints": list(declared.fingerprints), "none": declared.none}


def declared_of(value: Any) -> DeclaredSet:
    """Read a declared set (`declared_json`), refusing any other form: a fingerprint that is empty, not a string, given
    twice or several joined into one, a `none` that is not a boolean, or another key."""
    if not isinstance(value, Mapping) or set(value) != {"fingerprints", "none"}:
        raise LeaseError(f"a lease's {DECLARED} is an object with exactly the keys fingerprints and none")
    fingerprints, none = value["fingerprints"], value["none"]
    if not isinstance(none, bool):
        raise LeaseError(f"a lease's {DECLARED}.none is true or false, not {none!r}")
    if not isinstance(fingerprints, list) or not all(isinstance(f, str) and f for f in fingerprints):
        raise LeaseError(f"a lease's {DECLARED}.fingerprints is a list of non-empty strings")
    if len(set(fingerprints)) != len(fingerprints) or any(MIXED_FINGERPRINTS in f for f in fingerprints):
        raise LeaseError(f"a lease's {DECLARED}.fingerprints names each fingerprint once, one per entry")
    return DeclaredSet(tuple(sorted(fingerprints)), none)


def _refuse_constant(text: str) -> Any:
    raise LeaseError(f"the lease file holds {text}, which is not a finite number")


def read_lease(path: str | Path) -> RunLease:
    """Read a lease file: a JSON object with exactly the keys `schema` (`LEASE_SCHEMA`) and the six facts, and the
    optional `declared` (C48).

    Refuses, naming the problem, a file that cannot be read or is not a JSON object, a key missing or extra, another
    schema, a fact of another form (a grant that is not a whole number of micro-dollars, a float included), and NaN or
    Infinity anywhere (Python's JSON reads them).
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
    if set(data) - {DECLARED} != {"schema", *_FACTS}:
        raise LeaseError(f"a lease file has exactly the keys schema, {', '.join(_FACTS)} (and {DECLARED})")
    if data["schema"] != LEASE_SCHEMA:
        raise LeaseError(f"the lease file's schema is {data['schema']!r}, and this core reads {LEASE_SCHEMA!r}")
    declared = declared_of(data[DECLARED]) if DECLARED in data else None
    return RunLease(*(data[name] for name in _FACTS), declared=declared)


__all__ = [
    "DECLARED",
    "LEASE_JOURNAL",
    "LEASE_SCHEMA",
    "META_ATTEMPT_BASE",
    "RULE_LEASE",
    "LeaseError",
    "RunLease",
    "declared_json",
    "declared_of",
    "read_lease",
]
