"""O91: the channel's fact in the run's infrastructure facts, and the two files a backend ships."""

import hashlib
import threading

import pytest

from expcore_onedata.readlog import facts, files, paths
from expcore_onedata.sandbox import infrafacts

SHA_A, SHA_B = "ab" * 32, "cd" * 32


def test_o91_the_fact_holds_the_four_things_a_reader_of_one_run_needs():
    value = facts.fact(
        collector_sha256=SHA_A, client_sha256=SHA_B, placement="apptainer_helper", expected_peer_uid=1000
    )
    assert value == {
        "collector_sha256": SHA_A,
        "client_sha256": SHA_B,
        "placement": "apptainer_helper",
        "expected_peer_uid": 1000,
    }
    assert facts.KEY == paths.FACT_KEY == "read_log"
    assert facts.OWNER == paths.FACTS_OWNER == "readlog"


@pytest.mark.parametrize(
    "bad",
    [
        {"collector_sha256": "AB" * 32},
        {"client_sha256": "ab" * 31},
        {"client_sha256": None},
        {"placement": "somewhere"},
        {"expected_peer_uid": -1},
        {"expected_peer_uid": True},
        {"expected_peer_uid": "1000"},
        {"expected_peer_uid": 2**33},
    ],
)
def test_o91_a_fact_that_is_not_well_formed_is_refused(bad):
    fields = {
        "collector_sha256": SHA_A,
        "client_sha256": SHA_B,
        "placement": "docker_container",
        "expected_peer_uid": 10001,
        **bad,
    }
    with pytest.raises(ValueError, match="read-log fact"):
        facts.fact(**fields)


def test_o91_the_fact_goes_through_the_runs_one_writer_under_its_own_owner():
    """O89: the key is the first owner's: another writer is refused, the owner may write again."""
    meta: dict = {}
    writer = infrafacts.InfrastructureFacts(meta, threading.RLock())
    value = facts.fact(
        collector_sha256=SHA_A, client_sha256=SHA_B, placement="apptainer_helper", expected_peer_uid=1000
    )
    writer.write(facts.OWNER, {facts.KEY: value})
    writer.write(facts.OWNER, {facts.KEY: value})
    assert writer.get(facts.KEY) == value
    with pytest.raises(infrafacts.FactsConflict):
        writer.write("another", {facts.KEY: value})


def test_o91_the_shipped_files_are_the_packages_own_and_hash_as_their_bytes():
    assert files.collector_file().name == "collector.py"
    assert files.client_file().name == "client.py"
    for path in (files.collector_file(), files.client_file()):
        assert path.is_file()
        assert files.sha256_of(path) == hashlib.sha256(path.read_bytes()).hexdigest()
    assert files.sha256_of(files.collector_file()) != files.sha256_of(files.client_file())
