"""A real lease of the example study, issued to one federation over the fake ssh, and the place its worker leaves (C44,
C46, C47): shared by the worker's tests and the ssh fleet's."""

import time
from pathlib import Path
from typing import Any, NamedTuple

from channel_support import FakeSsh, channel_of, site_of, transport
from elastic_support import SITE
from merge_by_run_support import make_running
from study_support import Campaign, cli, make_copy

from expcore.campaign import out_snapshot
from expcore.campaign.elastic import Ledger, LedgerConfig, ledger_runs
from expcore.campaign.elastic import channel as ch
from expcore.campaign.elastic.dispatch import Dispatcher
from expcore.campaign.elastic.records import Lease
from expcore.campaign.elastic.worker import Worker
from expcore.campaign.manifest import fingerprint_sha256
from expcore.study.config import load_study
from expcore.study.pipeline import campaign_fingerprint

LABEL = "fed-1"


class InProcess:
    """`expcore run <study> --lease <file>` in this process (the suite's pinned core identity), as the worker's run."""

    def __init__(self, env):
        self.env = env
        self.codes: list[tuple[int, str]] = []

    def start(self, study: Path, lease_file: Path, log: Path, journal: Path):
        outer = self

        class Done:
            def wait(self, timeout: float) -> int:
                code, errors = cli(
                    outer.env, "run", str(study), "--lease", str(lease_file), "--spend-journal", str(journal)
                )
                outer.codes.append((code, errors))
                return code

        return Done()


class Leased(NamedTuple):
    """A real lease of the example study, issued to `LABEL` and delivered over the fake ssh."""

    study: Path
    loaded: Any
    ledger: Ledger
    site: Any
    dispatch: Dispatcher
    lease: Lease
    now: list[float]


def leased_example(tmp_path: Path, none: bool = True, label: str = LABEL) -> Leased:
    """The example's first lease, delivered; with `none` the ledger declares "none" for every model first (C48): the
    stub's replies carry no fingerprint, so without it they are undeclared. `label` is the federation's ledger label."""
    study = make_copy(tmp_path, "study")
    loaded = load_study(study)
    config = LedgerConfig(
        fingerprint=fingerprint_sha256(campaign_fingerprint(study)),
        hard_term_s=3600.0,
        prior_run_s=600.0,
        max_usd=100.0,
        grants_usd=dict.fromkeys(loaded.campaign.models, 5.0),
    )
    clock_now = [time.time()]
    ledger = Ledger.create(
        tmp_path / "out" / "ledger.sqlite", ledger_runs(loaded.manifest, loaded.schema), config, lambda: clock_now[0]
    )
    for model in loaded.campaign.models if none else ():
        ledger.declare(model, None, "the stub emits no system_fingerprint", "test")
    ledger.join(
        label,
        site=SITE,
        job_ids=("job-1",),
        job_end_at=clock_now[0] + 86_400,
        fingerprint=config.fingerprint,
        environment="b" * 64,
        kill_wait_s=180.0,
    )
    ssh, site = FakeSsh(), site_of(tmp_path)
    dispatch = Dispatcher(ledger, [site], transport(ssh), tmp_path / "out" / "leases")
    dispatch.tick()
    dispatch.tick()
    (lease,) = ledger.leases()
    return Leased(study, loaded, ledger, site, dispatch, lease, clock_now)


def ran_for_real(tmp_path: Path, env: dict[str, str], label: str = LABEL) -> tuple[Leased, Path]:
    """A worker that ran the example study's lease for real; the lease's place on the channel."""
    leased = leased_example(tmp_path, label=label)
    channel = channel_of(leased.site, label)
    worker = Worker(label, channel, leased.study, tmp_path / "work", InProcess(env))
    worker.open()
    assert worker.step() == "ran"
    return leased, channel / ch.PLACES / ch.place_name(leased.lease.lease_id)


def killed_mid_lease(kept: Path, tmp_path: Path) -> None:
    """What a federation killed in the middle of its lease leaves in the lease's place: a newest snapshot of the
    store as of its last attempt's start, which is still running, beside the journal."""
    mid = tmp_path / "mid" / "study" / "out"
    manifest = out_snapshot.restore(out_snapshot.snapshots(kept / "snapshots")[0], mid)
    make_running(Campaign(tmp_path / "mid" / "study"))
    out_snapshot.take(
        mid, kept / "snapshots", scratch=tmp_path / "scratch", facts=manifest["facts"], live=False,
        now="2026-11-18T12:00:00Z",
    )  # fmt: skip
