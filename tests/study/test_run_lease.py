"""C41: `run STUDY --lease FILE` runs exactly one run of the campaign, in a store of its own, on a lease (#462).

The lease is the file a ledger would give: the run, the lease id, the attempt base, the grant, the SHA-256 of the
campaign fingerprint it was issued for, and its expiry. These tests run the example study for real through the command
line, one run per store. They hold what the lease does (one run, its attempts numbered from the base, its grant as
the run's whole budget) and each refusal (a wrong campaign, an expired lease, an unknown run, a replay, another lease
or another form on the store), which must leave the store as it was, or absent.
"""

import dataclasses
import datetime as dt
import json
from pathlib import Path

import pytest
from selection_support import make_selection_study
from shard_support import merge_into
from study_support import Campaign, clean_environment, cli, make_copy

from expcore.campaign.gate import Budget
from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.run_lease import META_ATTEMPT_BASE, RULE_LEASE, RunLease
from expcore.campaign.shard_budget import reservation_usd
from expcore.records.record import record_filename
from expcore.study.config import load_study
from expcore.study.lease_start import lease_start
from expcore.study.pipeline import campaign_fingerprint

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

FAR = "2099-01-01T00:00:00Z"
MODEL = "alpha-stub"


def sha_of(root: Path) -> str:
    """The SHA-256 of the fingerprint a start of the study pins: what a ledger issues a lease for."""
    return fingerprint_sha256(campaign_fingerprint(root))


def first_run(root: Path, model: str = MODEL) -> str:
    return next(row.key.text for row in load_study(root).manifest.rows if row.key.factors["model"] == model)


def lease_of(root: Path, **changes: object) -> dict[str, object]:
    """A lease file's content for the run of `model` that comes first, with the given fields replaced."""
    lease = {
        "schema": "expcore-lease/1",
        "run_key": first_run(root),
        "lease_id": 1,
        "attempt_base": 0,
        "grant_usd": 5.0,
        "campaign_sha256": sha_of(root),
        "expires_utc": FAR,
    }
    return lease | changes


def run_lease(env: dict[str, str], root: Path, lease: dict[str, object], *extra: str) -> tuple[int, str]:
    """Write the lease beside the study and run it; return the exit status and stderr."""
    path = root.parent / f"lease-{lease['lease_id']}.json"
    path.write_text(json.dumps(lease), encoding="utf-8")
    return cli(env, "run", str(root), "--lease", str(path), *extra)


def state_of(campaign: Campaign) -> dict[str, list]:
    """Every row a store holds that a start could change."""
    return {
        table: [list(row) for row in campaign.sql(f"SELECT * FROM {table} ORDER BY 1")]
        for table in ("meta", "runs", "attempts", "events")
    }


def meta_of(campaign: Campaign) -> dict[str, str]:
    return {row["key"]: row["value"] for row in campaign.sql("SELECT key, value FROM meta")}


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("leases")


@pytest.fixture(scope="module")
def done(base, clean_environment) -> tuple[Campaign, dict[str, object]]:
    """A run taken on lease 4 after two charged attempts, to its end."""
    campaign = Campaign(make_copy(base, "done"))
    lease = lease_of(campaign.root, lease_id=4, attempt_base=2)
    code, errors = run_lease(clean_environment, campaign.root, lease)
    assert code == 0, errors
    return campaign, lease


@pytest.fixture(scope="module")
def wide(base, clean_environment) -> Campaign:
    """A lease store of a study whose unit caps (4) let a shard or a selection start reach the store's own pin."""
    campaign = Campaign(make_selection_study(base, "wide", caps=4))
    code, errors = run_lease(clean_environment, campaign.root, lease_of(campaign.root, lease_id=5))
    assert code == 0, errors
    return campaign


def test_c41_a_lease_runs_exactly_the_one_run_it_names_in_a_store_of_its_own(done):
    """C41: the store holds that run and no other, at its place in the whole plan, and it is done."""
    campaign, lease = done
    plan = {row.key.text: row.seq for row in load_study(campaign.root).manifest.rows}
    assert len(plan) > 1
    held = [(row["run_key"], row["seq"], row["status"]) for row in campaign.sql("SELECT * FROM runs")]
    assert held == [(lease["run_key"], plan[lease["run_key"]], "done")]
    records = [p for p in (campaign.root / "out" / "runs").rglob("*.json") if not p.name.startswith("attempt-marker")]
    assert len(records) == 1


def test_c41_a_lease_store_is_pinned_under_the_lease_rule_with_the_lease_as_its_spec(done):
    """C41: `expcore-shard/5`, the spec the lease's own JSON, the attempt base and the start's time, so a merge by run
    can tell such a store from every other."""
    campaign, lease = done
    meta = meta_of(campaign)
    spec = json.loads(meta["shard_spec"])
    assert spec["rule"] == RULE_LEASE
    assert {k: v for k, v in spec.items() if k != "rule"} == {k: v for k, v in lease.items() if k != "schema"}
    assert meta[META_ATTEMPT_BASE] == "2"
    assert meta["shard_started_utc"]


def test_c41_a_leases_attempts_are_numbered_from_its_base_in_the_row_and_in_the_record(done):
    """C41: the first attempt of a lease after two charged ones is the third: its row, its record's name and its
    record's own number say so."""
    campaign, lease = done
    row = campaign.sql("SELECT attempt, state, run_dir FROM attempts")[0]
    assert (row["attempt"], row["state"]) == (3, "done")
    name = record_filename(lease["run_key"], 3)
    record = Path(row["run_dir"]) / name
    assert record.is_file()
    assert json.loads(record.read_text(encoding="utf-8"))["attempt"] == 3


def test_c41_the_leases_grant_is_the_whole_budget_of_its_run_and_its_caps_are_one_run(done):
    """C41: the shares the store pins are the grant as `max_usd`, no per-model ceiling, and one run of the model's
    unit; the campaign's own totals are not the store's."""
    campaign, lease = done
    shares = json.loads(meta_of(campaign)["shard_shares"])
    assert shares == {"budget": {"max_usd": lease["grant_usd"], "per_model_usd": {}}, "unit_caps": {MODEL: 1}}


def test_c41_a_lease_store_passes_the_replay_check_with_its_attempt_base(done, clean_environment):
    """C41: the record, the store and the file name agree whatever the base: `check` of the lease's batch passes."""
    campaign, _ = done
    code, errors = cli(clean_environment, "check", str(campaign.runs))
    assert code == 0, errors


def test_c41_a_lease_of_another_campaign_is_refused_and_leaves_nothing(base, clean_environment):
    """C41: a lease whose fingerprint hash is not this campaign's is refused before any store is made."""
    campaign = Campaign(make_copy(base, "other-campaign"))
    code, errors = run_lease(clean_environment, campaign.root, lease_of(campaign.root, campaign_sha256="ab" * 32))
    assert code == 1
    assert "campaign" in errors
    assert not (campaign.root / "out" / "state.sqlite").exists()


def test_c41_an_expired_lease_is_refused_and_leaves_nothing(base, clean_environment):
    """C41: a lease whose expiry has passed is not run."""
    campaign = Campaign(make_copy(base, "expired"))
    code, errors = run_lease(
        clean_environment, campaign.root, lease_of(campaign.root, expires_utc="2020-01-01T00:00:00Z")
    )
    assert code == 1
    assert "expired" in errors
    assert not (campaign.root / "out" / "state.sqlite").exists()


def test_c41_a_lease_of_a_run_the_plan_does_not_have_is_refused_and_leaves_nothing(base, clean_environment):
    """C41: the run key must be one of the campaign's."""
    campaign = Campaign(make_copy(base, "unknown-run"))
    lease = lease_of(campaign.root, run_key="task=nowhere|access=direct|tools=basic|model=alpha-stub|seed=1")
    code, errors = run_lease(clean_environment, campaign.root, lease)
    assert code == 1
    assert "not a run of this campaign" in errors
    assert not (campaign.root / "out" / "state.sqlite").exists()


def test_c41_a_lease_at_the_attempt_limit_is_refused(base, clean_environment):
    """C41: a base that already used every attempt the campaign allows leaves the run none."""
    campaign = Campaign(make_copy(base, "no-attempt-left"))
    code, errors = run_lease(clean_environment, campaign.root, lease_of(campaign.root, attempt_base=3))
    assert code == 1
    assert "attempt" in errors
    assert not (campaign.root / "out" / "state.sqlite").exists()


def test_c41_a_lease_used_before_in_the_store_is_refused_as_a_replay_and_changes_nothing(done, clean_environment):
    """C41: the same lease file twice on one store is refused, whatever it would have run, and the store's every row is
    what it was."""
    campaign, lease = done
    before = state_of(campaign)
    code, errors = run_lease(clean_environment, campaign.root, lease)
    assert code == 1
    assert "already" in errors
    assert "replay" in errors
    assert state_of(campaign) == before


def test_c41_another_lease_on_a_lease_store_is_refused_and_changes_nothing(done, clean_environment):
    """C41: one store per lease: a later lease id on the store of an earlier one is refused too."""
    campaign, lease = done
    before = state_of(campaign)
    code, errors = run_lease(clean_environment, campaign.root, lease | {"lease_id": 9})
    assert code == 1
    assert "belongs to lease 4" in errors
    assert "replay" not in errors
    assert state_of(campaign) == before


@pytest.mark.parametrize("extra", [(), ("--shard", "1/2"), ("--select", "access=direct", "--shard-label", "x")])
def test_c41_a_start_that_gives_no_lease_or_another_form_on_a_lease_store_is_refused_not_crashed(
    wide, clean_environment, extra
):
    """C41: the unsharded start, a shard and a selection each refuse a lease store in words, and leave it as it was."""
    before = state_of(wide)
    code, errors = cli(clean_environment, "run", str(wide.root), *extra)
    assert code == 1
    assert "belongs to lease 5" in errors
    assert "Traceback" not in errors
    assert state_of(wide) == before


def test_c41_a_lease_does_not_start_a_store_another_form_made(base, clean_environment):
    """C41: a store that began as an unsharded campaign refuses a lease start, and keeps what it holds."""
    campaign = Campaign(make_copy(base, "unsharded-first"))
    assert cli(clean_environment, "run", str(campaign.root))[0] == 0
    before = state_of(campaign)
    code, errors = run_lease(clean_environment, campaign.root, lease_of(campaign.root))
    assert code == 1
    assert "unsharded" in errors
    assert state_of(campaign) == before


@pytest.mark.parametrize("extra", [("--shard", "1/2"), ("--select", "access=direct", "--shard-label", "x")])
def test_c41_a_lease_cannot_be_combined_with_a_shard_or_a_selection(tmp_path, clean_environment, extra):
    """C41: a lease is its own form; asked together with another, `run` refuses and starts nothing."""
    campaign = Campaign(make_copy(tmp_path, "combined"))
    code, errors = run_lease(clean_environment, campaign.root, lease_of(campaign.root), *extra)
    assert code == 1
    assert "--lease" in errors
    assert not (campaign.root / "out" / "state.sqlite").exists()


def test_c41_a_grant_below_what_a_run_reserves_holds_the_run_before_any_attempt(base, clean_environment):
    """C41: the grant is the budget gate's ceiling: below one run's reservation the run starts no attempt and is left
    pending (exit 3), whatever the campaign's own budget would have allowed."""
    campaign = Campaign(make_copy(base, "short-grant"))
    study = load_study(campaign.root)
    reserved = reservation_usd(study.models.resolve(MODEL), study.campaign.agent.limits)
    assert reserved
    assert reserved > 0
    code, errors = run_lease(clean_environment, campaign.root, lease_of(campaign.root, grant_usd=reserved / 2))
    assert code == 3, errors
    assert campaign.sql("SELECT * FROM attempts") == []
    assert [row["status"] for row in campaign.sql("SELECT status FROM runs")] == ["pending"]


def test_c41_a_grant_of_nothing_for_a_priced_model_is_refused_in_words(base, clean_environment):
    """C41: a budget of 0 holds every run, and a bare ValueError would be a traceback: the lease is refused."""
    campaign = Campaign(make_copy(base, "no-grant"))
    code, errors = run_lease(clean_environment, campaign.root, lease_of(campaign.root, grant_usd=0))
    assert code == 1
    assert "grant" in errors
    assert "Traceback" not in errors
    assert not (campaign.root / "out" / "state.sqlite").exists()


def test_c41_a_release_file_or_a_model_ceiling_of_the_campaign_never_lifts_a_leases_ceiling(base):
    """C41: the lease's budget is the grant and nothing else: no release step, no release file, no per-model ceiling
    (the ledger holds those), so no operator release can take a run past what it was granted."""
    root = make_copy(base, "release")
    study = load_study(root)
    campaigns = dataclasses.replace(
        study.campaign.scheduler,
        budget=Budget(max_usd=5.0, per_model_usd={MODEL: 2.0}, release_step_usd=1.0, release_file=root / "release.txt"),
    )
    lease = lease_of(root, grant_usd=1.5)
    _, settings, start = lease_start(
        study,
        campaigns,
        RunLease(**{k: v for k, v in lease.items() if k != "schema"}),
        campaign_sha256=sha_of(root),
        environment={},
        now=dt.datetime(2026, 11, 18, tzinfo=dt.UTC),
    )
    assert settings.budget == Budget(max_usd=1.5)
    assert settings.budget.release_file is None
    assert start.shares["budget"] == {"max_usd": 1.5, "per_model_usd": {}}


def test_c41_today_s_merge_refuses_a_lease_store_by_name_and_writes_nothing(done, base, clean_environment):
    """C41: a run on a lease is merged by run, a later step; the merge of shards names the store and refuses."""
    campaign, _ = done
    target = Campaign(make_copy(base, "merge-target"))
    code, errors = merge_into(clean_environment, target, campaign)
    assert code == 1
    assert "lease" in errors
    assert "Traceback" not in errors
    assert not (target.root / "out" / "merge.json").exists()
