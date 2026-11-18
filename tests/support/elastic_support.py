"""Helpers for the elastic ledger's tests: a fake clock, a plan, a ledger, and the invariants every step must keep."""

import hashlib
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from expcore.campaign.elastic import Completion, Lease, Ledger, LedgerConfig, LedgerRun, SiteReading
from expcore.campaign.elastic.config import micro_down

FINGERPRINT = hashlib.sha256(b"an invented campaign").hexdigest()
ENVIRONMENT = hashlib.sha256(b"an invented environment").hexdigest()
CONDITIONS = ("mount-read", "api-none")
HARD_TERM_S = 3600.0
PRIOR_S = 600.0
SITE = "site-a"


class FakeClock:
    """A clock the test moves by hand, in seconds since the epoch."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def plan(
    models: Sequence[str] = ("m1",),
    conditions: Sequence[str] = CONDITIONS,
    tasks: int = 2,
    sets: Mapping[str, int] | None = None,
) -> list[LedgerRun]:
    """One run per task, model and condition; seq goes task by task, so the conditions interleave. `sets` gives a
    condition's declared set (0 when it is not named)."""
    out: list[LedgerRun] = []
    for task in range(tasks):
        for model in models:
            for condition in conditions:
                out.append(
                    LedgerRun(
                        run_key=f"task=task-{task}|model={model}|condition={condition}",
                        seq=len(out),
                        model=model,
                        condition=condition,
                        set_index=(sets or {}).get(condition, 0),
                    )
                )
    return out


def config(**changes) -> LedgerConfig:
    """A ledger configuration with the invented fingerprint; keyword arguments replace its fields."""
    fields = {"fingerprint": FINGERPRINT, "hard_term_s": HARD_TERM_S, "prior_run_s": PRIOR_S}
    fields.update(changes)
    return LedgerConfig(**fields)


def make(tmp_path: Path, runs: Sequence[LedgerRun] | None = None, **changes) -> tuple[Ledger, FakeClock]:
    """A new ledger under `tmp_path/out` on a fake clock."""
    clock = FakeClock()
    ledger = Ledger.create(ledger_path(tmp_path), plan() if runs is None else runs, config(**changes), clock=clock)
    return ledger, clock


def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "out" / "ledger.sqlite"


def join(
    ledger: Ledger,
    clock: FakeClock,
    label: str,
    site: str = SITE,
    job_end_in_s: float = 86_400.0,
    kill_wait_s: float = 180.0,
) -> None:
    """Join a federation with the campaign's fingerprint and environment."""
    ledger.join(
        label,
        site=site,
        job_ids=(f"job-{label}",),
        job_end_at=clock() + job_end_in_s,
        fingerprint=FINGERPRINT,
        environment=ENVIRONMENT,
        kill_wait_s=kill_wait_s,
    )


def archive(lease_id: int) -> str:
    """The SHA-256 of a lease's archive, as its completion names it."""
    return hashlib.sha256(f"the archive of lease {lease_id}".encode()).hexdigest()


def completion(lease: Lease | int, spend_usd: float = 0.25, state: str = "done", charged: int = 1) -> Completion:
    """A completion of the lease naming its archive."""
    lease_id = lease if isinstance(lease, int) else lease.lease_id
    return Completion(
        lease_id=lease_id, state=state, charged=charged, spend_usd=spend_usd, archive_sha256=archive(lease_id)
    )


def finish(ledger: Ledger, lease: Lease, site: str = SITE, **fields) -> str:
    """Read the lease's completion at its site, then verify its archive; return the verdict."""
    ledger.observe_site(site, SiteReading(completions=(completion(lease, **fields),)))
    return ledger.verify_archive(lease.lease_id, archive(lease.lease_id))


def leased(ledger: Ledger, label: str) -> Lease:
    """Issue the next lease to a federation, and fail the test if it has to wait."""
    got = ledger.issue(label)
    assert isinstance(got, Lease), f"{label} was told to wait: {got}"
    return got


def assert_invariants(ledger: Ledger) -> None:
    """What every step must keep: one live lease per run and per federation, a live lease being its run's latest, no
    budget or ceiling passed, no cap passed by live and held-back leases together, and at most one counted done lease
    per run, which is the run's latest when the run is done."""
    leases = ledger.leases()
    runs = {run.run_key: run for run in ledger.runs()}
    live = [lease for lease in leases if lease.state in ("issued", "sent")]
    assert len({lease.run_key for lease in live}) == len(live), "a run is leased twice at once"
    holders = [lease.federation for lease in leases if lease.slot]
    assert len(set(holders)) == len(holders), "a federation holds two cap slots"
    for lease in live:
        run = runs[lease.run_key]
        assert run.lease_id == lease.lease_id, f"live lease {lease.lease_id} is not its run's latest"
        assert run.state == "leased", f"the run of live lease {lease.lease_id} is {run.state}"
    for run in runs.values():
        if run.state == "leased":
            latest = next(lease for lease in leases if lease.lease_id == run.lease_id)
            assert latest.state in ("issued", "sent") or (latest.state == "reported" and latest.verdict == "counts")
    cfg = ledger.config
    total, per_model = ledger.committed_micro()
    assert total == sum(lease.charge_micro for lease in leases)
    if cfg.max_usd is not None:
        assert total <= micro_down(cfg.max_usd), f"committed {total} passes max_usd {cfg.max_usd}"
        for model, ceiling in cfg.per_model_usd.items():
            assert per_model.get(model, 0) <= micro_down(ceiling), f"model {model} passes its ceiling"
    for lease in leases:
        assert lease.charge_micro <= lease.grant_micro or lease.state in ("counted", "superseded", "returned")
    slots = Counter(lease.model for lease in leases if lease.slot)
    for model, cap in cfg.caps.items():
        assert slots[model] <= cap, f"model {model} holds {slots[model]} slots over its cap {cap}"
    done = Counter(lease.run_key for lease in leases if lease.state == "counted" and lease.completion_state == "done")
    assert all(count == 1 for count in done.values()), "a run counted done twice"
    for run in runs.values():
        if run.state == "done":
            assert done[run.run_key] == 1
            latest = next(lease for lease in leases if lease.lease_id == run.lease_id)
            assert latest.state == "counted"
            assert latest.completion_state == "done"
