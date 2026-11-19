"""Helpers for the merge by run (C45): a real ledger driving real lease stores of the example study.

`build_fleet` creates the campaign's ledger (`elastic.Ledger`, on a fake clock) for a copy of the example that declares
the federation rerun, joins federations at two sites, and leases every run, one at a time, to the healthy ones: each
lease is run for real by `python -m expcore run STUDY --lease FILE` in a copy of its own (a store of one run), and its
completion is read and its archive verified, as the dispatcher will. Three more federations meet the fates a merge by
run must settle:

- `fed-x` takes a lease that expires before its node reports; its run is leased again and counted elsewhere, and its
  node's completion, read late, is superseded: kept as evidence;
- `fed-y` takes a lease that expires because its node was lost mid-run: its store comes back with the attempt still
  running and no marker (`make_running`), to be settled in the merge's copy;
- `fed-z` takes a lease that expires and leaves nothing behind: it is charged its grant;
- `fed-l0`, `fed-l1`, ... each take a lease and are lost, until one run has lost `max_attempts` federations and the
  ledger ends it as exhausted; every loss is then settled (`exhausted`, `exhausted_leases`).

Every store is the kernel's own; nothing of a lease store is written by hand but the running attempt of `fed-y`.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from shard_support import IDENTITY, declaring, make_study
from study_support import Campaign, cli, copy_campaign

from expcore.campaign.elastic import (
    Completion,
    Lease,
    Ledger,
    LedgerConfig,
    SiteReading,
    Wait,
    lease_grant_usd,
    ledger_runs,
)
from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.markers import MARKER_NAME
from expcore.campaign.merge_by_run import environment_digest
from expcore.campaign.run_lease import declared_json
from expcore.campaign.selection_ledger import spend_of
from expcore.study.config import load_study
from expcore.study.pipeline import campaign_fingerprint
from expcore.study.selection_start import pricing_of

FAR = "2099-01-01T00:00:00Z"
ENVIRONMENT = environment_digest(IDENTITY)  # what every federation joins the ledger with: its identity's digest
HEALTHY = {"fed-a1": "site-a", "fed-a2": "site-a", "fed-b1": "site-b"}
DOOMED = {"fed-x": "site-b", "fed-y": "site-b", "fed-z": "site-b"}
RERUN = '\n[sensitivity]\nreruns = ["federation"]\n'


class FakeClock:
    """A clock the test moves by hand, in seconds since the epoch."""

    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


DECLARED = (
    "alpha-stub",
    "deploy-late",
    "a deployment joined mid-campaign",
    "operator-1",
)  # model, fingerprint, why, who


def by_run_study(base: Path, name: str) -> Path:
    """A stamped copy of the example that declares the federation rerun (every copy of a fleet is made here, so all
    plan one campaign)."""

    def declare(root: Path) -> None:
        prereg = root / "preregistration.toml"
        prereg.write_text(prereg.read_text(encoding="utf-8") + RERUN, encoding="utf-8")

    return make_study(base, name, edit=declare)


@dataclass
class Fleet:
    """The ledger, its file, the lease stores by lease id, and the three doomed leases."""

    base: Path
    env: dict[str, str]
    ledger_file: Path
    sha: str
    stores: dict[int, Campaign] = field(default_factory=dict)
    superseded: int = 0
    lost: int = 0
    vanished: int = 0
    pending_copy: Path | None = None  # the ledger as it was with runs still pending
    exhausted: str = ""  # the run the ledger exhausted for its lost leases, every one of them settled
    exhausted_leases: tuple[int, ...] = ()  # that run's leases
    lost_ended: Campaign | None = None  # the lost lease's store as it was once its run ended, before it was lost

    def counted(self) -> list[Campaign]:
        """The stores of every run's counted lease."""
        held = {self.superseded, self.lost}
        return [campaign for lease_id, campaign in sorted(self.stores.items()) if lease_id not in held]

    def outs(self, *leases: int) -> list[str]:
        """The out/ of the counted stores, then of the given leases' stores."""
        return [str(c.root / "out") for c in self.counted()] + [str(self.stores[i].root / "out") for i in leases]

    def merge(self, target: Campaign, outs: list[str], ledger: Path | None = None) -> tuple[int, str]:
        """Merge the given lease stores into `target`'s out/ under the ledger, as the command line does."""
        return cli(self.env, "merge", str(target.root / "out"), *outs, "--ledger", str(ledger or self.ledger_file))


def run_lease(base: Path, env: dict[str, str], lease: Lease, sha: str) -> Campaign:
    """Run one lease in a copy of its own, as a federation's node does."""
    root = by_run_study(base, f"lease-{lease.lease_id}")
    document = {
        "schema": "expcore-lease/1",
        "run_key": lease.run_key,
        "lease_id": lease.lease_id,
        "attempt_base": lease.attempt_base,
        "grant_micro_usd": lease.grant_micro,
        "campaign_sha256": sha,
        "expires_utc": FAR,
        **({} if lease.declared is None else {"declared": declared_json(lease.declared)}),  # as the channel sends it
    }
    path = base / f"lease-{lease.lease_id}.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        declaring(patch, IDENTITY)
        code, errors = cli(env, "run", str(root), "--lease", str(path))
    assert code == 0, errors
    return Campaign(root)


def _archive(lease_id: int) -> str:
    return hashlib.sha256(f"the archive of lease {lease_id}".encode()).hexdigest()


def complete(ledger: Ledger, lease: Lease, campaign: Campaign, site: str) -> str:
    """Read the lease store's completion at its site and verify its archive; return the lease's state."""
    rows = campaign.sql("SELECT state, charged FROM attempts ORDER BY id")
    pricing = pricing_of(load_study(campaign.root))
    spent = spend_of(lambda sql, args=(): campaign.sql(sql, *args), pricing).total or 0.0
    found = Completion(
        lease.lease_id, rows[-1]["state"], sum(r["charged"] for r in rows), spent, _archive(lease.lease_id)
    )
    ledger.observe_site(site, SiteReading(completions=(found,)))
    return ledger.verify_archive(lease.lease_id, _archive(lease.lease_id))


def make_running(campaign: Campaign) -> None:
    """Leave the store as a lost node's copy back would: its last attempt still running, its run pending, no marker."""
    store = sqlite3.connect(campaign.root / "out" / "state.sqlite")
    try:
        last = store.execute("SELECT id, run_key FROM attempts ORDER BY id DESC LIMIT 1").fetchone()
        store.execute(
            "UPDATE attempts SET state='running', charged=0, ended_utc=NULL, termination=NULL WHERE id=?", (last[0],)
        )
        store.execute("UPDATE runs SET status='pending' WHERE run_key=?", (last[1],))
        store.commit()
    finally:
        store.close()
    for marker in (campaign.root / "out" / "runs").rglob(f"*{MARKER_NAME}"):
        marker.unlink()


def _new_ledger(base: Path, sha: str, clock: FakeClock) -> tuple[Ledger, Path]:
    study = load_study(by_run_study(base, "orchestrator"))
    pricing = pricing_of(study)
    grants = {model: lease_grant_usd(spec, pricing.limits) for model, spec in pricing.models.items()}
    config = LedgerConfig(
        fingerprint=sha,
        hard_term_s=3600.0,
        prior_run_s=600.0,
        max_attempts=study.campaign.scheduler.max_attempts,
        caps=dict.fromkeys(grants, 8),
        max_usd=5.0,
        grants_usd=grants,
    )
    path = base / "orchestrator" / "ledger" / "ledger.sqlite"
    path.parent.mkdir(parents=True)
    ledger = Ledger.create(path, ledger_runs(study.manifest, study.schema), config, clock)
    for model in study.campaign.models:  # the stubs emit no fingerprint (C48)
        ledger.declare(model, None, "the stub emits no system_fingerprint", "operator-1")
    for label, site in {**HEALTHY, **DOOMED}.items():
        ledger.join(
            label,
            site=site,
            job_ids=(f"job-{label}",),
            job_end_at=clock() + 86_400.0,
            fingerprint=sha,
            environment=ENVIRONMENT,
            kill_wait_s=180.0,
        )
    return ledger, path


def build_fleet(base: Path, env: dict[str, str]) -> Fleet:
    """Lease every run of the campaign, as module docstring says; the ledger is closed at the end."""
    sha = fingerprint_sha256(campaign_fingerprint(by_run_study(base, "template")))
    clock = FakeClock()
    ledger, path = _new_ledger(base, sha, clock)
    fleet = Fleet(base, env, path, sha)
    doomed = {}
    for label in DOOMED:
        doomed[label] = ledger.issue(label)
        assert isinstance(doomed[label], Lease), doomed[label]
        ledger.mark_sent(doomed[label].lease_id)
    clock.now += 400.0  # no heartbeat since they were sent: a read of their site expires all three
    ledger.observe_site("site-b", SiteReading())
    for label in DOOMED:
        ledger.prove_ended(label, "terminal")
    ledger.close()
    fleet.pending_copy = path.with_name("ledger-with-runs-pending.sqlite")
    fleet.pending_copy.write_bytes(path.read_bytes())
    ledger = Ledger.open(path, clock)
    try:
        fleet.exhausted, fleet.exhausted_leases = _exhaust_a_run(ledger, clock, sha)
        clock.now += 60.0
        ledger.declare(DECLARED[0], DECLARED[1], DECLARED[2], DECLARED[3])  # mid-campaign, for the merge's log (C48)
        _lease_every_run(fleet, ledger)
        lx, ly = doomed["fed-x"], doomed["fed-y"]
        fleet.stores[lx.lease_id] = run_lease(base, env, lx, sha)
        assert complete(ledger, lx, fleet.stores[lx.lease_id], "site-b") == "superseded"
        fleet.stores[ly.lease_id] = run_lease(base, env, ly, sha)
        fleet.lost_ended = copy_campaign(fleet.stores[ly.lease_id], base, f"lease-{ly.lease_id}-ended")
        make_running(fleet.stores[ly.lease_id])
    finally:
        ledger.close()
    fleet.superseded, fleet.lost, fleet.vanished = (doomed[label].lease_id for label in DOOMED)
    return fleet


def _exhaust_a_run(ledger: Ledger, clock: FakeClock, sha: str) -> tuple[str, tuple[int, ...]]:
    """Lose leases to fresh federations at site-b (each sent, never heard from, its job proven ended) until a run is
    exhausted by the losses, then settle every loss at no spend and one attempt, so the run is final; the runs lost
    once go back to pending, one attempt charged."""
    lost: list[tuple[int, str]] = []
    for n in range(12):
        label = f"fed-l{n}"
        ledger.join(
            label,
            site="site-b",
            job_ids=(f"job-{label}",),
            job_end_at=clock() + 86_400.0,
            fingerprint=sha,
            environment=ENVIRONMENT,
            kill_wait_s=180.0,
        )
        lease = ledger.issue(label)
        assert isinstance(lease, Lease), lease
        ledger.mark_sent(lease.lease_id)
        clock.now += 400.0
        ledger.observe_site("site-b", SiteReading())
        ledger.prove_ended(label, "terminal")
        lost.append((lease.lease_id, lease.run_key))
        if ledger.run(lease.run_key).state == "infra_exhausted":
            break
    run_key = lost[-1][1]
    assert ledger.run(run_key).state == "infra_exhausted", "no run was exhausted by the losses"
    for lease_id, _ in lost:
        ledger.settle(lease_id, 0, "pulled", attempts_charged=1)
    assert ledger.run(run_key).state == "infra_exhausted"
    return run_key, tuple(lease_id for lease_id, key in lost if key == run_key)


def _lease_every_run(fleet: Fleet, ledger: Ledger) -> None:
    """Lease runs to the healthy federations until every run is counted."""
    while True:
        progressed = False
        for label, site in HEALTHY.items():
            lease = ledger.issue(label)
            if isinstance(lease, Wait):
                continue
            ledger.mark_sent(lease.lease_id)
            fleet.stores[lease.lease_id] = run_lease(fleet.base, fleet.env, lease, fleet.sha)
            assert complete(ledger, lease, fleet.stores[lease.lease_id], site) == "counted"
            progressed = True
        if not progressed:
            break
    assert {run.state for run in ledger.runs()} <= {"done", "infra_exhausted", "harness_error"}
