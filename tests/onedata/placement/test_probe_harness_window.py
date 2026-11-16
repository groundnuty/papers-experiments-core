"""A transfer the harness scheduled before the run's window does not make the listing look incomplete (O08, O32).

The ended walk keeps no job whose record says it ended before the cutoff (`transferorder.newest_first`), so a reset's
eviction, which the harness schedules before every run, is never in the accepted listing. Its absence proves nothing of
the listing; a harness job that could lie in the window and is missing still does (the O08 test beside the split's).
"""

from expcore_onedata.probes import PlatformProbes
from expcore_onedata.testing import BETA_ID, DATA, tool_plane_replicates

PATH = "study/data.bin"  # the file the reset evicts; the study does not track it
OLDER = 64  # ended jobs of the space's history from before the reset, none of them the harness's
WINDOW_JOBS = 20  # the agent's replications into beta in the run's window
INSIDE_S = 10  # seconds a scripted job keeps from the window's edges, past the default 5 s allowance for clock skew
BEFORE_S = 60  # seconds from the last job before the run to its start, past the allowance and the walk's margin


def tracked(seeded):
    """The run's files, one per window job, each held by the data provider alone."""
    files = {f"f{i:02d}": f"study/f{i:02d}.bin" for i in range(WINDOW_JOBS)}
    for path in files.values():
        seeded.add_file(path, DATA)
    return files


def history(seeded, first: float) -> None:
    """The space's older history: `OLDER` replications into beta that ended a second apart from `first` on."""
    for i in range(OLDER):
        seeded.add_transfer("replication", PATH, BETA_ID, scheduled=first + i)


def run_window(probes, seeded, client, ticker, files) -> dict:
    """Start the run, replicate every tracked file into beta through the tool plane, and return the snapshot."""
    probes.start({})
    ticker.sleep(INSIDE_S)
    for path in files.values():
        seeded.end_transfer(tool_plane_replicates(seeded, client, path, BETA_ID))
    ticker.sleep(INSIDE_S)
    return probes.finish()


def assert_split_known(snapshot) -> None:
    """Beta gained the window's replications and nothing else: its split is known, and no bound is flagged."""
    section = snapshot["space_transfers"]
    assert section["probe_ok"] is True, section["error"]
    assert section["listed"] == WINDOW_JOBS
    assert "harness_unlisted" not in section
    assert snapshot["on_demand"]["providers[1]"] == {
        "gained_bytes": WINDOW_JOBS * len(DATA),
        "transfer_bytes_in": WINDOW_JOBS * len(DATA),
        "difference": 0,
        "on_demand_lower_bound": 0,
        "unknown": None,
    }
    assert snapshot["on_demand"]["providers[0]"]["unknown"] is None


def test_o08_a_reset_eviction_before_the_window_leaves_the_split_known(client, provisioner, seeded, ticker):
    """O08 with O32: the shape of a pilot's smoke run. The space's history holds 64 older jobs; the reset's eviction
    from beta, the harness's one job, ends after them and well before the window; then 20 replications into beta run
    in it. The walk stops at the eviction, the first job older than the cutoff, and keeps it out of the listing, as it
    keeps every such job: its record says it ended before the cutoff, so beta's split is known."""
    seeded.clock = ticker.clock
    files = tracked(seeded)
    history(seeded, first=100)
    provisioner.evict(PATH, "beta")  # the reset's eviction: the harness's, ended before the run
    (eviction,) = [tid for _, tid, kind in provisioner.transfers() if kind == "eviction"]
    ticker.sleep(BEFORE_S)
    probes = PlatformProbes(client, provisioner, files, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock)
    snapshot = run_window(probes, seeded, client, ticker, files)
    assert_split_known(snapshot)
    assert snapshot["space_transfers"]["harness_before_window"] == 1
    assert eviction not in {r["transfer_id"] for r in snapshot["space_transfers"]["records"]}


def deep_eviction(client, provisioner, seeded, ticker, files) -> tuple[str, PlatformProbes]:
    """The harness's eviction as the oldest job of the space, the 64 of the history after it, all before the run, and
    probes asking 8 ids a page: the eviction is listed on the eleventh page, past the third, which holds the walk's
    stop, and the fourth, which it checks after it, so the walk never reads its record. Return its id and the probes."""
    provisioner.evict(PATH, "beta")
    (eviction,) = [tid for _, tid, kind in provisioner.transfers() if kind == "eviction"]
    ticker.sleep(INSIDE_S + OLDER)
    history(seeded, first=ticker.now - OLDER)  # every one ended after the eviction, and before the run
    ticker.sleep(BEFORE_S)
    probes = PlatformProbes(
        client, provisioner, files, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock, page_limit=8
    )
    return eviction, probes


def test_o08_a_harness_job_pages_past_the_walks_stop_is_read_on_its_own(client, provisioner, seeded, ticker):
    """O08 with O32: the harness's eviction lies pages past the walk's stop, so the walk never reads it. Its own
    record says it ended before the cutoff: beta's split is known all the same."""
    seeded.clock = ticker.clock
    files = tracked(seeded)
    _, probes = deep_eviction(client, provisioner, seeded, ticker, files)
    snapshot = run_window(probes, seeded, client, ticker, files)
    assert_split_known(snapshot)
    assert snapshot["space_transfers"]["harness_before_window"] == 1
    # Per provider and observation: one waiting and one ongoing page, the ended listing's first three (the third
    # holds the stop) and the one checked past it; the eviction is on the eleventh.
    observations = snapshot["space_transfers"]["polls"]
    assert snapshot["space_transfers"]["pages"] == observations * 2 * (2 + 3 + 1)


def test_o08_a_harness_job_whose_record_cannot_be_read_still_flags_every_bound(client, provisioner, seeded, ticker):
    """O08: the harness's eviction before the window, pages past the walk's stop, is missing from the listing, and
    its record answers 500 on every provider: nothing says it ended before the cutoff, so the listing may be
    incomplete and every bound is unknown. The listing itself was read: only the eviction's record failed."""
    seeded.clock = ticker.clock
    files = tracked(seeded)
    eviction, probes = deep_eviction(client, provisioner, seeded, ticker, files)
    seeded.statuses.append([f"/transfers/{eviction}", 99, 500])
    snapshot = run_window(probes, seeded, client, ticker, files)
    section = snapshot["space_transfers"]
    assert section["probe_ok"] is True, section["error"]
    assert section["harness_unlisted"] == 1
    assert "harness_before_window" not in section
    for provider in ("providers[0]", "providers[1]"):
        assert snapshot["on_demand"][provider]["on_demand_lower_bound"] is None
        assert "harness_transfer_not_listed" in snapshot["on_demand"][provider]["unknown"]
