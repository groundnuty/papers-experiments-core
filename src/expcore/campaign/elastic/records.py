# Provenance: new
"""What goes into the elastic ledger from a site, and what it shows of its runs, leases and federations (C42).

Lease states:

| State | Meaning | Cap slot | Charged |
|---|---|---|---|
| `issued` | written before its file goes out | held | the grant |
| `sent` | its file landed in the federation's inbox | held | the grant |
| `expired` | its run went back to pending; its holder may still run | held until proven ended | grant, or settled |
| `reported` | its completion was read; the archive is not verified yet | free | the grant |
| `counted` | the latest lease's completion, archive verified: the run's outcome | free | measured |
| `superseded` | a completion of a lease that was not its run's latest: evidence | free | measured |
| `lost` | its archive never verified | free | grant, or settled |
| `returned` | returned unstarted by its federation | free | what the return names |
| `cancelled` | its file never landed | free | nothing |

A **settlement** (`Settlement`, `Ledger.settle`) is not a state: a lease expired with its holder proven ended, or
`lost`, is charged its spend as read from the dead federation instead of its grant ("settled" above), and keeps its
state, so a completion or a return read later still takes it through the usual path.

A run is `pending`, `leased` (a lease in flight, or a counting completion waiting for its archive), `done`,
`infra_exhausted` or `harness_error`.
"""

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

LIVE = ("issued", "sent")
COMPLETION_STATES = ("done", "infra_fail", "harness_error", "interrupted", "outage_discarded")
HELD = ("issued", "sent", "expired", "reported", "lost")  # states charged their grant until a spend settles them
SETTLE_SOURCES = ("root", "pulled")  # where a settled spend was read: the surviving root, or evidence pulled back
PROOFS = ("terminal", "cancelled")  # what a caller may give as a job's proven end; "walltime" is the ledger's own
_HEX64 = re.compile(r"[0-9a-f]{64}")


class LedgerError(RuntimeError):
    """The ledger refused an operation: an unknown lease or federation, or a lease in the wrong state."""


class JoinRefused(LedgerError):
    """A federation was refused at join: another fingerprint or environment, or a label already used."""


class CompletionConflict(LedgerError):
    """A completion or return that contradicts what the ledger already read for the same lease."""


class SettleRefused(LedgerError):
    """A lease that is not lost, one already settled with other values, or a spend above its grant."""


class CeilingRefused(LedgerError):
    """A ceiling that is not above the current one, is below what is committed, or has nothing to raise."""


def _lease_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"a lease id is a whole number of at least 1, got {value!r}")
    return value


def _spend(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"spend_usd must be a finite number of dollars, not below zero, got {value!r}")
    return float(value)


@dataclass(frozen=True)
class Completion:
    """A lease's completion file: the lease store's last attempt state, its charged attempts, its measured spend and
    the SHA-256 of its archive."""

    lease_id: int
    state: str
    charged: int
    spend_usd: float
    archive_sha256: str

    def __post_init__(self) -> None:
        _lease_id(self.lease_id)
        if self.state not in COMPLETION_STATES:
            raise ValueError(f"completion state {self.state!r} must be one of: {', '.join(COMPLETION_STATES)}")
        if isinstance(self.charged, bool) or not isinstance(self.charged, int) or self.charged < 0:
            raise ValueError(f"charged must be a whole number of attempts, got {self.charged!r}")
        object.__setattr__(self, "spend_usd", _spend(self.spend_usd))
        if not isinstance(self.archive_sha256, str) or not _HEX64.fullmatch(self.archive_sha256):
            raise ValueError("archive_sha256 must be 64 lower-case hex digits")

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "charged": self.charged,
            "spend_usd": self.spend_usd,
            "archive_sha256": self.archive_sha256,
        }


@dataclass(frozen=True)
class Return:
    """A lease returned unstarted, with what its store spent (its admission probes)."""

    lease_id: int
    spend_usd: float

    def __post_init__(self) -> None:
        _lease_id(self.lease_id)
        object.__setattr__(self, "spend_usd", _spend(self.spend_usd))


@dataclass(frozen=True)
class SiteReading:
    """One read of a site: the heartbeat counters by lease id, the completion and return files found, and the jobs
    shown ended (a terminal state, or cancelled by the controller), by federation label."""

    heartbeats: Mapping[int, int] = field(default_factory=dict)
    completions: Sequence[Completion] = ()
    returns: Sequence[Return] = ()
    ended: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for lease_id, counter in self.heartbeats.items():
            _lease_id(lease_id)
            if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
                raise ValueError(f"lease {lease_id}'s heartbeat counter must be a whole number, got {counter!r}")
        if not all(isinstance(c, Completion) for c in self.completions):
            raise ValueError("completions must be Completion values")
        if not all(isinstance(r, Return) for r in self.returns):
            raise ValueError("returns must be Return values")
        for label, proof in self.ended.items():
            if proof not in PROOFS:
                raise ValueError(f"federation {label}'s end is proven by one of {', '.join(PROOFS)}, not {proof!r}")

    def lease_ids(self) -> set[int]:
        """Every lease the reading names."""
        return {*self.heartbeats, *(c.lease_id for c in self.completions), *(r.lease_id for r in self.returns)}


@dataclass(frozen=True)
class Wait:
    """No lease for the federation now, and why."""

    reason: str


@dataclass(frozen=True)
class Lease:
    """One lease as the ledger holds it. `slot`: it holds its model's cap slot; `charge_micro`: what the budget counts
    for it now; `verdict`: for a completion read, whether it counts or is superseded; `proven`: its federation's job is
    proven ended."""

    lease_id: int
    run_key: str
    model: str
    condition: str
    federation: str
    site: str
    fingerprint: str
    attempt_base: int
    grant_micro: int
    issued_at: float
    renewed_at: float
    counter: int | None
    hard_term_at: float
    state: str
    slot: bool
    charge_micro: int
    verdict: str | None
    completion_state: str | None
    proven: bool

    @property
    def grant_usd(self) -> float:
        return self.grant_micro / 1_000_000


@dataclass(frozen=True)
class Run:
    """One run as the ledger holds it: its state, the charged attempts of its counted leases, and its latest lease."""

    run_key: str
    seq: int
    model: str
    condition: str
    set_index: int
    state: str
    charged: int
    lease_id: int | None


@dataclass(frozen=True)
class Federation:
    """A federation that joined: where it runs, its jobs, their recorded end and KillWait, when it was drained, and its
    proven end."""

    label: str
    site: str
    job_ids: tuple[str, ...]
    job_end_at: float
    kill_wait_s: float
    joined_at: float
    drained_at: float | None
    ended_at: float | None
    end_proof: str | None


@dataclass(frozen=True)
class Demand:
    """What a controller's target reads: the current set (None: no run is pending), its pending runs per model, and
    the cap slots held per model (leases in flight, and expired ones whose holder is not proven ended)."""

    set_index: int | None
    pending: Mapping[str, int]
    slots: Mapping[str, int]


@dataclass(frozen=True)
class Settlement:
    """A lost lease's spend settled: when, from where (`source`), the spend and the grant it replaced."""

    lease_id: int
    at: float
    source: str
    spend_micro: int
    grant_micro: int
    charged: int | None = None  # the lease's charged attempts as its log gave them, None: not read

    @property
    def released_micro(self) -> int:
        """What settling gave back to the budget."""
        return self.grant_micro - self.spend_micro


@dataclass(frozen=True)
class Ceiling:
    """One raise of a dollar ceiling: of the campaign's (`model` None) or of a model's, with when, why and by whom."""

    ceiling_id: int
    at: float
    model: str | None
    old_micro: int
    new_micro: int
    reason: str
    actor: str


@dataclass(frozen=True)
class Ceilings:
    """The ceilings in force: the campaign's (None: no budget) and each model's own, as pinned and then raised."""

    max_micro: int | None
    per_model_micro: Mapping[str, int]


@dataclass(frozen=True)
class Status:
    """The ledger's money in micro-dollars. `ceiling_micro` is None with no budget (then `remaining_micro` is too);
    `locked_*` is what unsettled leases hold as their grants (in flight, expired, reported, lost), by lease state and
    by model; `ceilings` is the history of raises, oldest first."""

    ceiling_micro: int | None
    committed_micro: int
    remaining_micro: int | None
    locked_micro: int
    locked_by_state: Mapping[str, int]
    locked_by_model: Mapping[str, int]
    model_ceiling_micro: Mapping[str, int]
    model_remaining_micro: Mapping[str, int]
    ceilings: tuple[Ceiling, ...]
