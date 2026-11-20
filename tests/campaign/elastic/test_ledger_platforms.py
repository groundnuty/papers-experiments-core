"""C50: a campaign that declares its platforms pins the set in its ledger; a federation joins on one of them or is
refused, and each lease's run keeps the platform its federation joined on. A campaign that declares none is as it was.
"""

import hashlib
import json

import pytest
from elastic_support import ENVIRONMENT, FINGERPRINT, SITE, config, leased, ledger_path, make

from expcore.campaign.elastic import JoinRefused, LedgerConfig
from expcore.campaign.export_join import PLATFORM_KEY, by_run_export
from expcore.campaign.ledger_read import read_ledger, take_ledger_snapshot

APPTAINER = hashlib.sha256(b"an Apptainer site's platform").hexdigest()
DOCKER = hashlib.sha256(b"a Docker site's platform").hexdigest()
OTHER = hashlib.sha256(b"a platform nobody declared").hexdigest()


def join_on(ledger, clock, label: str, platform: str | None) -> None:
    ledger.join(label, site=SITE, job_ids=(f"job-{label}",), job_end_at=clock() + 86_400.0, fingerprint=FINGERPRINT,
                environment=ENVIRONMENT, kill_wait_s=180.0, platform=platform)  # fmt: skip


def facts_of(tmp_path):
    snapshot = tmp_path / "snapshot.sqlite"
    take_ledger_snapshot(ledger_path(tmp_path), snapshot)
    data = snapshot.read_bytes()
    return read_ledger(data, hashlib.sha256(data).hexdigest())


def test_c50_federations_on_two_declared_platforms_both_join_and_each_lease_keeps_its_federations(tmp_path):
    """C50: one Apptainer and one Docker federation join a campaign that declares both, and each lease's run is read
    back with the platform its federation joined on."""
    ledger, clock = make(tmp_path, platforms=(DOCKER, APPTAINER))
    join_on(ledger, clock, "fed-hpc", APPTAINER)
    join_on(ledger, clock, "fed-vm", DOCKER)
    on_hpc, on_vm = leased(ledger, "fed-hpc"), leased(ledger, "fed-vm")
    ledger.close()
    leases = facts_of(tmp_path).leases
    assert (leases[on_hpc.lease_id].platform, leases[on_vm.lease_id].platform) == (APPTAINER, DOCKER)


@pytest.mark.parametrize("platform", [OTHER, None])
def test_c50_a_federation_on_a_platform_the_campaign_does_not_declare_is_refused_at_its_join(tmp_path, platform):
    """C50: an undeclared platform, or none given, is refused naming the campaign's key, and nothing joins."""
    ledger, clock = make(tmp_path, platforms=(APPTAINER, DOCKER))
    with pytest.raises(JoinRefused, match="platform_fingerprints"):
        join_on(ledger, clock, "fed-x", platform)
    assert ledger.db.execute("SELECT COUNT(*) FROM federations").fetchone()[0] == 0


def test_c50_a_campaign_that_declares_none_joins_as_before_and_pins_the_configuration_it_did(tmp_path):
    """C50: with no set the platform given at a join is not read, no lease carries one, and the pinned configuration's
    text has no new field, so a ledger made before the field reads back as it was."""
    ledger, clock = make(tmp_path)
    join_on(ledger, clock, "fed-a", OTHER)
    lease = leased(ledger, "fed-a")
    assert "platforms" not in json.loads(ledger.config.to_json())
    ledger.close()
    assert facts_of(tmp_path).leases[lease.lease_id].platform is None
    before = json.loads(config().to_json())
    assert LedgerConfig.from_json(json.dumps(before)) == config()


def test_c50_the_set_is_pinned_sorted_and_a_malformed_one_is_refused():
    assert config(platforms=(DOCKER, APPTAINER)).platforms == tuple(sorted((DOCKER, APPTAINER)))
    pinned = config(platforms=(DOCKER, APPTAINER))
    assert LedgerConfig.from_json(pinned.to_json()) == pinned
    for bad in ((APPTAINER, APPTAINER), ("not a digest",)):
        with pytest.raises(ValueError, match="platforms"):
            config(platforms=bad)


def test_c50_each_run_of_a_batch_merged_by_run_names_the_platform_it_ran_under():
    """C50: a run's entry gains the platform of its counted lease's federation beside its site, when there is one."""
    joined = {"runs": [{"run_key": "r1", "shard": 1}, {"run_key": "r2", "shard": 2}], "planned_runs": 2}
    leases = [
        {"lease_id": 1, "federation": "fed-hpc", "site": "site-a", PLATFORM_KEY: APPTAINER},
        {"lease_id": 2, "federation": "fed-old", "site": "site-b"},
    ]
    runs = by_run_export(joined, leases)["runs"]
    assert [run.get(PLATFORM_KEY) for run in runs] == [APPTAINER, None]
    assert runs[0]["site"] == "site-a"


ENV_HPC = hashlib.sha256(b"an HPC member's environment").hexdigest()
ENV_VM = hashlib.sha256(b"a VM member's environment").hexdigest()


def join_with(ledger, clock, label: str, platform: str, environment: str) -> None:
    ledger.join(label, site=SITE, job_ids=(f"job-{label}",), job_end_at=clock() + 86_400.0, fingerprint=FINGERPRINT,
                environment=environment, kill_wait_s=180.0, platform=platform)  # fmt: skip


def test_c50_each_declared_platform_pins_the_environment_of_its_first_join(tmp_path, capsys):
    """C50: an HPC member and a VM member whose environment identities differ (each holds its backend's own digests)
    both join; a second member on the HPC platform with another environment is refused; `status` shows each pin."""
    from expcore.campaign.elastic.__main__ import main

    ledger, clock = make(tmp_path, platforms=(APPTAINER, DOCKER))
    join_with(ledger, clock, "fed-hpc", APPTAINER, ENV_HPC)
    join_with(ledger, clock, "fed-vm", DOCKER, ENV_VM)
    join_with(ledger, clock, "fed-hpc-2", APPTAINER, ENV_HPC)
    with pytest.raises(JoinRefused, match="first join on its platform"):
        join_with(ledger, clock, "fed-hpc-3", APPTAINER, ENV_VM)
    assert ledger.platform_environments() == {APPTAINER: ENV_HPC, DOCKER: ENV_VM}
    assert ledger.db.execute("SELECT value FROM meta WHERE key='environment'").fetchone() is None
    ledger.close()
    assert facts_of(tmp_path).environments == {APPTAINER: ENV_HPC, DOCKER: ENV_VM}
    assert main(["status", str(ledger_path(tmp_path))]) == 0
    shown = capsys.readouterr().out
    assert f"platform {APPTAINER}: environment {ENV_HPC}" in shown
    assert f"platform {DOCKER}: environment {ENV_VM}" in shown


def test_c50_without_a_set_the_first_join_pins_the_one_environment_as_before(tmp_path):
    """C50: no set, one pin under the key it always had, and another environment is refused whatever its platform."""
    ledger, clock = make(tmp_path)
    join_with(ledger, clock, "fed-a", APPTAINER, ENV_HPC)
    with pytest.raises(JoinRefused, match=r"pinned at the first join$"):
        join_with(ledger, clock, "fed-b", DOCKER, ENV_VM)
    assert ledger.db.execute("SELECT value FROM meta WHERE key='environment'").fetchone()[0] == ENV_HPC
    assert ledger.platform_environments() == {}
