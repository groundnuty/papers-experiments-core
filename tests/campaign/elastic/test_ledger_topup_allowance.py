"""C65: `elastic create --topup-allowance MODEL=USD` raises one model's lease grant at the creation of the ledger, so
the lease budget gate of a worker that counts its run twice never holds a lease; the allowance is recorded beside the
pinned configuration, and nothing a worker's join compares changes."""

import dataclasses
import json
import sqlite3
from decimal import Decimal

import pytest
from elastic_support import assert_invariants, join, leased, ledger_path, make, plan
from shard_support import IDENTITY, declaring
from study_support import HEAD, clean_environment, make_copy

from expcore.campaign.attempt_bounds import request_bound_usd
from expcore.campaign.elastic import LedgerConfig, LedgerReader
from expcore.campaign.elastic.__main__ import create_ledger, main
from expcore.campaign.elastic.config import micro_up
from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.shard_budget import reservation_usd
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.pipeline import campaign_fingerprint, campaign_identity
from expcore.study.selection_start import pricing_of

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

ALPHA, BETA = "alpha-stub", "beta-stub"
BUDGET = ["--max-usd", "500"]
# the keys of a pinned configuration before the allowance existed (`platforms` is left out while a ledger declares none)
TODAYS_CONFIG_KEYS = {
    "fingerprint",
    "hard_term_s",
    "prior_run_s",
    "max_attempts",
    "caps",
    "max_usd",
    "per_model_usd",
    "grants_usd",
    "stale_s",
    "site_out_s",
}


def create_args(path, study, *extra):
    return ["create", str(path), "--study", str(study), "--hard-term-s", "5400", *extra]


def plain_grant_micro(study, model):
    """What a lease of the model was granted before the flag: one reservation and the bound of two admission probes,
    each rounded up to a micro-dollar."""
    pricing = pricing_of(load_study(study))
    spec = pricing.models[model]
    return micro_up(reservation_usd(spec, pricing.limits)) + 2 * micro_up(request_bound_usd(spec, pricing.limits))


def read(path):
    reader = LedgerReader(path)
    try:
        return reader.config, reader.topup_allowances_micro()
    finally:
        reader.close()


def meta_keys(path):
    db = sqlite3.connect(path)
    try:
        return {key for (key,) in db.execute("SELECT key FROM meta")}
    finally:
        db.close()


def test_c65_the_flag_raises_only_the_named_models_grant_by_exactly_the_allowance(tmp_path, clean_environment):
    """C65: a lease of the named model is granted its reservation, the bound of its two admission probes and the
    allowance (here 125.09 USD, 125,090,000 micro-dollars); the model not named keeps the grant it had."""
    study = make_copy(tmp_path, "study")
    path = ledger_path(tmp_path)
    assert main(create_args(path, study, *BUDGET, "--topup-allowance", f"{ALPHA}=125.09")) == 0
    config, _ = read(path)
    assert config.grant_micro(ALPHA) == plain_grant_micro(study, ALPHA) + 125_090_000
    assert config.grant_micro(BETA) == plain_grant_micro(study, BETA)


def test_c65_the_allowance_is_recorded_in_micro_dollars_and_for_the_named_models_only(tmp_path, clean_environment):
    """C65: the ledger keeps what each model was given (a repeatable flag, one per model), as the grants are counted."""
    study = make_copy(tmp_path, "study")
    path = ledger_path(tmp_path)
    args = create_args(
        path, study, *BUDGET, "--topup-allowance", f"{ALPHA}=125.09", "--topup-allowance", f"{BETA}=31.27"
    )
    assert main(args) == 0
    _, recorded = read(path)
    assert recorded == {ALPHA: 125_090_000, BETA: 31_270_000}


def test_c65_the_allowance_is_kept_outside_the_pinned_configuration_so_an_older_reader_opens_the_ledger(
    tmp_path, clean_environment
):
    """C65: `LedgerConfig.from_json` takes only the fields it has, so a core that does not know the flag would fail on
    a key it does not know; the allowance is a meta row of its own, and the pinned configuration keeps today's keys."""
    study = make_copy(tmp_path, "study")
    path = ledger_path(tmp_path)
    assert main(create_args(path, study, *BUDGET, "--topup-allowance", f"{ALPHA}=125.09")) == 0
    db = sqlite3.connect(path)
    try:
        (pinned,) = db.execute("SELECT value FROM meta WHERE key='config'").fetchone()
    finally:
        db.close()
    assert set(json.loads(pinned)) == TODAYS_CONFIG_KEYS
    assert {f.name for f in dataclasses.fields(LedgerConfig)} >= set(json.loads(pinned))
    assert meta_keys(path) == {"config", "schema", "topup_allowances_micro_usd"}


def test_c65_a_create_without_the_flag_makes_the_ledger_it_made_before(tmp_path, clean_environment):
    """C65: no flag: the grants are today's, the pinned configuration has today's keys, the meta table has no row the
    flag adds, and no allowance is read back."""
    study = make_copy(tmp_path, "study")
    path = ledger_path(tmp_path)
    assert main(create_args(path, study, *BUDGET)) == 0
    config, recorded = read(path)
    assert [config.grant_micro(m) for m in (ALPHA, BETA)] == [plain_grant_micro(study, m) for m in (ALPHA, BETA)]
    assert (recorded, meta_keys(path)) == ({}, {"config", "schema"})
    assert set(json.loads(config.to_json())) == TODAYS_CONFIG_KEYS


def test_c65_the_flag_changes_no_part_of_the_campaign_fingerprint_a_worker_joins_with(tmp_path, clean_environment):
    """C65: the fingerprint a ledger pins is the same with the flag and without it, and is the one a member's identity
    read gives on the same core and study. Grants are no input to it. (The core's identity is one, through the
    environment: a ledger made on another core than its workers' is not one they join; the pull request says so.)"""
    study = make_copy(tmp_path, "study")
    before = fingerprint_sha256(campaign_fingerprint(study))
    plain, flagged = ledger_path(tmp_path), tmp_path / "flagged" / "ledger.sqlite"
    assert main(create_args(plain, study, *BUDGET)) == 0
    assert main(create_args(flagged, study, *BUDGET, "--topup-allowance", f"{ALPHA}=125.09")) == 0
    with pytest.MonkeyPatch.context() as patch:
        declaring(patch, IDENTITY)
        joined, _ = campaign_identity(study)
    assert read(plain)[0].fingerprint == read(flagged)[0].fingerprint == fingerprint_sha256(joined) == before
    assert fingerprint_sha256(campaign_fingerprint(study)) == before


@pytest.mark.parametrize(
    ("typed", "word"),
    [
        ("alpha-stub", "MODEL=USD"),
        ("alpha-stub=", "dollars"),
        ("=5", "MODEL=USD"),
        ("alpha-stub=abc", "dollars"),
        ("alpha-stub=-1", "above zero"),
        ("alpha-stub=0", "above zero"),
        ("alpha-stub=nan", "finite"),
        ("alpha-stub=inf", "finite"),
        ("alpha-stub=1.1234567", "micro-dollar"),
        ("alpha-stub=12345678901.123456", "at most"),
        ("alpha-stub=1e1000000", "at most"),
    ],
)
def test_c65_an_amount_that_is_not_a_finite_number_of_dollars_above_zero_to_the_micro_dollar_is_refused(
    tmp_path, capsys, clean_environment, typed, word
):
    """C65: the form is MODEL=USD, and USD is read exactly as typed (never through a float): finite, above zero (an
    allowance of 0 changes nothing), no finer than the micro-dollar the ledger counts in, and no more than a grant held
    as a float of dollars keeps exactly (a huge exponent is refused, not overflowed). A usage error as argparse ends it,
    before the study is read, with no ledger made."""
    path = ledger_path(tmp_path)
    with pytest.raises(SystemExit) as usage:
        main(create_args(path, tmp_path / "study", *BUDGET, "--topup-allowance", typed))
    assert usage.value.code == 2
    assert word in capsys.readouterr().err
    assert not path.exists()


def test_c65_an_allowance_that_makes_a_grant_a_figure_of_dollars_cannot_hold_exactly_is_refused(
    tmp_path, capsys, clean_environment
):
    """C65: the largest amount that can be typed, 9,007,199,254.740992 USD, on top of a grant makes more than a float of
    dollars keeps to the micro-dollar, so it would round, and a grant that rounds up passes the ceiling it was checked
    against (and one past 2^63 micro-dollars cannot be written to the file): refused, no ledger made."""
    study = make_copy(tmp_path, "study")
    path = ledger_path(tmp_path)
    args = create_args(path, study, "--max-usd", "100000000000", "--topup-allowance", f"{ALPHA}=9007199254.740992")
    assert main(args) == 2
    assert "holds exactly" in capsys.readouterr().err
    assert not path.exists()


def test_c65_an_allowance_finer_than_a_micro_dollar_from_a_direct_caller_is_refused_not_rounded(
    tmp_path, clean_environment
):
    """C65: a caller that does not come through the command line gets the same bound: 0.0000001 USD would be granted as
    a whole micro-dollar and recorded as none."""
    study = make_copy(tmp_path, "study")
    path = ledger_path(tmp_path)
    with pytest.raises(ValueError, match="micro-dollars"):
        create_ledger(path, study, hard_term_s=5400.0, max_usd=500.0, topup_allowances={ALPHA: Decimal("0.0000001")})
    assert not path.exists()


def test_c65_a_model_the_study_does_not_plan_is_refused_and_no_ledger_is_made(tmp_path, capsys, clean_environment):
    """C65: an allowance on a model that has no run would raise nothing; it is refused, naming the model."""
    study = make_copy(tmp_path, "study")
    path = ledger_path(tmp_path)
    assert main(create_args(path, study, *BUDGET, "--topup-allowance", "nobody=5")) == 1
    err = capsys.readouterr().err
    assert "nobody" in err
    assert "does not plan" in err
    assert not path.exists()


def test_c65_a_free_model_is_refused_since_its_lease_is_granted_nothing(tmp_path, capsys, clean_environment):
    """C65: a lease of a free model is granted 0 whatever it is made of, so an allowance would be recorded and change
    nothing; it is refused, naming the model."""
    study = make_copy(tmp_path, "study")
    models = study / "models.toml"
    priced = "\n[models.beta-stub.price]\ninput_per_mtok = 0.5\noutput_per_mtok = 1.5\n"
    assert priced in models.read_text()
    models.write_text(models.read_text().replace(priced, "\nfree = true\n"))
    write_stamp(study, HEAD, load_study(study).scope)  # the copy's code identity is stamped again after the edit
    path = ledger_path(tmp_path)
    assert main(create_args(path, study, *BUDGET, "--topup-allowance", f"{BETA}=5")) == 1
    err = capsys.readouterr().err
    assert BETA in err
    assert "free" in err
    assert not path.exists()


def test_c65_a_model_named_twice_is_refused_and_no_ledger_is_made(tmp_path, capsys, clean_environment):
    """C65: two allowances for one model would leave the grant to the order they are typed in."""
    study = make_copy(tmp_path, "study")
    path = ledger_path(tmp_path)
    args = create_args(path, study, *BUDGET, "--topup-allowance", f"{ALPHA}=1", "--topup-allowance", f"{ALPHA}=2")
    assert main(args) == 2
    assert "more than once" in capsys.readouterr().err
    assert not path.exists()


def test_c65_an_allowance_without_a_budget_is_refused_since_a_lease_is_granted_nothing_without_one(
    tmp_path, capsys, clean_environment
):
    """C65: with no `--max-usd` a grant is 0 whatever it was made of, so the flag would record a figure that changes
    nothing; it is refused, naming the budget."""
    study = make_copy(tmp_path, "study")
    path = ledger_path(tmp_path)
    assert main(create_args(path, study, "--topup-allowance", f"{ALPHA}=5")) == 2
    assert "--max-usd" in capsys.readouterr().err
    assert not path.exists()


def test_c65_an_allowance_that_lifts_a_grant_over_the_budget_is_refused_and_no_ledger_is_made(
    tmp_path, capsys, clean_environment
):
    """C65: the grant of a lease may not pass `max_usd` (a lease no budget could hold would never go out): refused,
    with the figures, by the ledger's own plan check."""
    study = make_copy(tmp_path, "study")
    path = ledger_path(tmp_path)
    assert main(create_args(path, study, "--max-usd", "100", "--topup-allowance", f"{ALPHA}=100")) == 2
    assert "above max_usd" in capsys.readouterr().err
    assert not path.exists()


def test_c65_every_lease_of_ten_per_model_goes_out_under_a_3600_dollar_budget_at_the_raised_grants(tmp_path):
    """C65: the issue path holds a lease back when its grant exceeds the budget left, `max_usd` less what is committed,
    which is what settled leases were charged plus the grants locked in unsettled ones (`committed_micro`). Twenty
    leases (ten of a model granted 276.99 USD, ten of one granted 69.245) commit 10 x 276.99 + 10 x 69.245 =
    3,462.35 USD, 137.65 USD short of 3,600: every one goes out."""
    runs = plan(models=("m1", "m2"), tasks=10, conditions=("api-none",))
    ledger, clock = make(
        tmp_path,
        runs=runs,
        max_usd=3600.0,
        caps={"m1": 10, "m2": 10},
        grants_usd={"m1": 276.99, "m2": 69.245},
    )
    budget = 3_600_000_000
    for number in range(20):
        label = f"fed-{number}"
        join(ledger, clock, label)
        committed = ledger.committed_micro()[0]
        lease = leased(ledger, label)
        assert committed + lease.grant_micro <= budget, "the gate passed a grant the budget left could not hold"
    total, per_model = ledger.committed_micro()
    assert per_model == {"m1": 10 * 276_990_000, "m2": 10 * 69_245_000}
    assert total == 3_462_350_000
    assert budget - total == 137_650_000
    assert ledger.slots() == {"m1": 10, "m2": 10}
    assert_invariants(ledger)
