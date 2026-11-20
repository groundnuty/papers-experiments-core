"""The probes read only the harness's transfers that bear on the run's window (O08, O32): the change log is kept across
runs, and every earlier job in it read again at each run's end cost about 40 minutes a run once it held thousands. A job
scheduled since the window's start is read, and so is an earlier one unless the log noted, before the start, its end
with a finish time before the walk's cutoff (`Provisioner.note_ended`), as a reset's evictions are; one that ended after
the cutoff, or whose end was not noted, is read, so its absence from the listing still flags the listing."""

import json

import pytest

from expcore_onedata.eviction import evict_ids
from expcore_onedata.probes import PlatformProbes
from expcore_onedata.testing import BETA_ID, DATA, tool_plane_replicates, with_placement, write_platform

PATH = "study/data.bin"
EARLIER = 10_000  # earlier jobs the log shows ended
EVICTED = 5  # files a reset evicts from beta before the run


@pytest.fixture
def platform_file(tmp_path):
    """Both providers support the space, so a reset may evict from beta."""

    def document(data):
        with_placement(data)
        data["support"]["providers"] = ["alpha", "beta"]

    return write_platform(tmp_path, document)


def probes_of(client, provisioner, ticker, seeded, **bounds) -> PlatformProbes:
    seeded.clock = ticker.clock  # the fake's jobs are timed by the test's clock, as the probes' window is
    return PlatformProbes(
        client, provisioner, {"data": PATH}, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock, **bounds
    )


def record_reads(seeded, since: int = 0) -> list[str]:
    """The transfer ids whose record a provider was asked for from its `since`-th call on, once per read."""
    return [
        route.removeprefix("/transfers/")
        for _, method, route in seeded.provider_calls[since:]
        if method == "GET" and route.startswith("/transfers/") and route.count("/") == 2
    ]


def test_o08_earlier_jobs_the_log_shows_ended_cost_the_walk_no_read(client, provisioner, seeded, ticker, tmp_path):
    """O08: the log holds 10,000 earlier jobs noted ended long before the run, one noted ended 1 s before its start
    (after the walk's cutoff), one earlier job still running, and the run schedules one: the walk reads none of the
    10,000, and keeps and reads the other three."""
    old = [f"tid-old-{i:05d}" for i in range(EARLIER)]
    active = seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now, ended=False)
    late = seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now - 1)  # ended 1 s before the run
    with open(tmp_path / "changes.jsonl", "a", encoding="utf-8") as log:  # the provisioner's log, as it writes it
        for tid in [*old, active, late]:
            line = {"action": "transfer", "id": tid, "kind": "eviction", "file": "f", "provider": "beta"}
            log.write(json.dumps(line, sort_keys=True) + "\n")
    provisioner.note_ended("beta", {tid: {"finishTime": 100} for tid in old})
    provisioner.note_ended("beta", {late: {"finishTime": int(ticker.now) - 1}})
    probes = probes_of(client, provisioner, ticker, seeded)
    probes.start({})
    ticker.sleep(5)
    new = provisioner.schedule_transfer("replication", seeded.id_of(PATH), "beta")
    seeded.end_transfer(new)
    seeded.end_transfer(active)
    walk = len(seeded.provider_calls)  # the run-end walk's reads come after this
    snapshot = probes.finish()
    reads = record_reads(seeded, walk)
    assert not [tid for tid in reads if tid.startswith("tid-old-")]
    assert active in reads
    assert new in reads
    assert late in reads
    assert {record["transfer_id"] for record in snapshot["transfers"]} == {active, late, new}
    assert snapshot["transfers_before_window"] == EARLIER


def test_o08_after_a_reset_evicted_files_the_walk_reads_none_of_its_evictions(client, provisioner, seeded, ticker):
    """O08: a reset's bulk eviction of five files from beta before the run notes each batch's end, so the run-end
    walk reads none of their records; the agent's replication in the run is listed and read. Three older jobs of the
    space's history lie between them and the run, and the listing is read one id a page, so the listing's own walk,
    which reads the page of its stop and the page past it (O32), never reaches them either."""
    files = [seeded.add_file(f"study/e{i}.bin", DATA) for i in range(EVICTED)]
    for file_id in files:
        seeded.blocks[(file_id, BETA_ID)] = [[0, len(DATA)]]
    probes = probes_of(client, provisioner, ticker, seeded, page_limit=1)
    evict_ids(provisioner, files, "beta")
    evictions = {tid for _, tid, kind in provisioner.transfers() if kind == "eviction"}
    assert len(evictions) == EVICTED
    for _ in range(3):  # the space's history, after the evictions and before the run: the walk stops in it
        ticker.sleep(10)
        seeded.add_transfer("replication", PATH, BETA_ID, scheduled=ticker.now)
    ticker.sleep(60)
    probes.start({})
    ticker.sleep(5)
    tid = tool_plane_replicates(seeded, client, PATH, BETA_ID)
    seeded.end_transfer(tid)
    walk = len(seeded.provider_calls)  # the run-end walk's reads come after this
    snapshot = probes.finish()
    assert not evictions & set(record_reads(seeded, walk))
    section = snapshot["space_transfers"]
    assert section["probe_ok"] is True, section["error"]
    assert tid in {record["transfer_id"] for record in section["records"]}
    assert tid in record_reads(seeded, walk)
    assert snapshot["transfers_before_window"] == EVICTED
    assert "harness_before_window" not in section
