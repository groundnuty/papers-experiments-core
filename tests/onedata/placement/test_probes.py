"""Probes: a failed probe is recorded as failed (O08), and the platform's snapshot is an instrument's output."""

import json

import httpx
import pytest

from expcore.plugin import Instrument
from expcore.records.instruments import finish_instruments, start_instruments
from expcore_onedata.client import OnezoneClient
from expcore_onedata.probes import NAMESPACE, PlatformProbes, bytes_by_pair, run_probe
from expcore_onedata.rangemap import RangeRow
from expcore_onedata.ranges import place_ranges
from expcore_onedata.testing import ALPHA_ID, BETA_ID, DATA

PATH = "study/data.bin"
ROW = RangeRow(PATH, "beta", ((0, 100), (2000, 500)))


@pytest.fixture
def probes(client, provisioner, ticker):
    return PlatformProbes(client, provisioner, {"data": PATH}, ["alpha", "beta"], sleep=ticker.sleep, now=ticker.clock)


def read_attributes(client, fake):
    file_id = fake.id_of(PATH)
    return lambda: client.get(f"/data/{file_id}", provider="alpha")


def calls_for(fake, route_part):
    return [c for c in fake.provider_calls if route_part in c[2]]


def test_o08_a_real_answer_is_one_request_and_a_failed_probe_with_no_value(client, seeded, ticker):
    """O08: a file that is not there (400 posix enoent on Onedata 25.0, a 404 before) is an answer; it is not retried,
    and the probe is failed with the value None."""
    result = run_probe(lambda: client.get("/data/missing", provider="alpha"), sleep=ticker.sleep)
    assert (result.ok, result.value, result.attempts, result.status) == (False, None, 1, 400)
    assert len(calls_for(seeded, "/data/missing")) == 1
    assert ticker.slept == []


def test_o08_a_server_error_is_an_answer_too_and_is_not_retried(client, seeded, ticker):
    """O08: a 500 is a real answer from the provider, so it is recorded and not repeated."""
    seeded.statuses.append(["/data/", 5, 500])
    result = run_probe(read_attributes(client, seeded), sleep=ticker.sleep)
    assert (result.ok, result.value, result.attempts, result.status) == (False, None, 1, 500)
    assert len(calls_for(seeded, "/data/")) == 1


def test_o08_two_resets_then_an_answer_give_the_value(client, seeded, ticker):
    """O08: network resets are retried; the third try is answered and the value is the answer's."""
    seeded.resets.append(["/data/", 2])
    result = run_probe(read_attributes(client, seeded), attempts=3, backoff_s=0.5, sleep=ticker.sleep)
    assert result.ok
    assert result.value["size"] == len(DATA)
    assert result.attempts == 3
    assert len(calls_for(seeded, "/data/")) == 3
    assert ticker.slept == [0.5, 0.5]


def test_o08_resets_on_every_try_give_probe_ok_false_and_a_value_of_none(client, seeded, ticker):
    """O08: after the bounded number of tries the probe is failed, with no value and no status."""
    seeded.resets.append(["/data/", 99])
    result = run_probe(read_attributes(client, seeded), attempts=3, sleep=ticker.sleep)
    assert (result.ok, result.value, result.attempts, result.status) == (False, None, 3, None)
    assert len(calls_for(seeded, "/data/")) == 3
    assert result.to_json()["probe_ok"] is False


def test_o08_an_answer_that_is_not_json_is_an_answer_and_is_not_retried(config, ticker):
    """O08: only a missing answer is retried; a 200 that cannot be read is final."""
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, content=b"<html>not json</html>")

    client = OnezoneClient(config, http=httpx.Client(transport=httpx.MockTransport(handler)))
    result = run_probe(lambda: client.get("/data/x", provider="alpha"), sleep=ticker.sleep)
    assert (result.ok, result.value, result.attempts) == (False, None, 1)
    assert len(sent) == 1


def test_a_defect_in_a_probe_is_not_recorded_as_a_failed_probe():
    """Only platform failures are probe failures; a bug in the reading code propagates."""
    with pytest.raises(KeyError):
        run_probe(lambda: {}["x"])


def test_the_probe_failure_text_holds_no_url_or_host(client, seeded, ticker):
    """The recorded error names a status or the lack of an answer, never a URL, a host or a body."""
    seeded.resets.append(["/data/", 99])
    failed = run_probe(read_attributes(client, seeded), sleep=ticker.sleep)
    answered = run_probe(lambda: client.get("/data/missing", provider="beta"), sleep=ticker.sleep)
    for result in (failed, answered):
        text = json.dumps(result.to_json())
        assert "test" not in text.replace("attempts", "")  # no .test host
        assert "http" not in text


def test_bytes_by_pair_attributes_a_gain_to_the_one_provider_that_held_it():
    """Bytes the destination gained that exactly one other provider held at the start come from that provider."""
    before = {"a": [(0, 100)], "b": [], "c": [(50, 100)]}
    after = {"a": [(0, 100)], "b": [(0, 150)], "c": [(50, 100)]}
    result = bytes_by_pair(before, after)
    assert result["b"] == {
        "gained_bytes": 150,
        "from": {"a": 50, "c": 50},
        "ambiguous_bytes": 50,  # [50, 100) was held by both, so it belongs to neither
        "unattributed_bytes": 0,
        "lost_bytes": 0,
    }
    assert result["a"]["gained_bytes"] == 0
    assert result["a"]["from"] == {}


def test_bytes_by_pair_counts_bytes_nobody_held_apart_and_notes_loss():
    before = {"a": [(0, 10)], "b": [(100, 40)]}
    after = {"a": [(0, 10), (20, 5)], "b": [(100, 10)]}
    result = bytes_by_pair(before, after)
    assert result["a"]["unattributed_bytes"] == 5  # fetched from a provider that is not tracked
    assert result["a"]["from"] == {}
    assert result["b"]["lost_bytes"] == 30
    assert result["b"]["gained_bytes"] == 0


def test_the_snapshot_records_the_distribution_before_and_after_and_the_bytes_per_pair(probes, provisioner, seeded):
    """Before and after a range placement, the snapshot shows the growth and where the bytes came from."""
    probes.start({"run": "r1"})
    place_ranges(provisioner, [ROW])
    snapshot = probes.finish()
    assert snapshot["ok"] is True
    beta = snapshot["files"]["data"]["providers"]["providers[1]"]
    assert beta["before"]["held_bytes"] == 0
    assert beta["after"]["held_bytes"] == 600
    assert beta["after"]["blocks"] == [[0, 100], [2000, 500]]
    assert snapshot["files"]["data"]["size"] == len(DATA)
    assert snapshot["pairs"]["providers[1]"]["from"] == {"providers[0]": 600}
    assert snapshot["pairs"]["providers[0]"]["gained_bytes"] == 0
    assert snapshot["files"]["data"]["providers"]["providers[0]"]["after"]["held_bytes"] == len(DATA)
    json.dumps(snapshot, allow_nan=False)  # plain JSON


def test_the_snapshot_holds_the_transfer_records_as_the_provider_reports_them(probes, provisioner, seeded):
    """A transfer the provisioner scheduled is read back with its state and counters; none is derived or guessed."""
    probes.start({})
    provisioner.replicate(PATH, "beta")
    transfers = probes.finish()["transfers"]
    assert len(transfers) == 1
    assert transfers[0]["kind"] == "replication"
    assert transfers[0]["state"] == "completed"
    assert transfers[0]["bytes_replicated"] == len(DATA)
    assert transfers[0]["probe_ok"] is True


def test_o08_a_provider_that_cannot_be_read_makes_its_readings_none_and_the_snapshot_incomplete(
    probes, provisioner, seeded
):
    """O08 with R10: the failed provider's readings and everything derived from them are None, not zero."""
    probes.start({})
    place_ranges(provisioner, [ROW])
    seeded.resets.append(["/distribution", 99, BETA_ID])
    snapshot = probes.finish()
    assert snapshot["ok"] is False
    beta = snapshot["files"]["data"]["providers"]["providers[1]"]
    assert beta["after"]["probe_ok"] is False
    assert beta["after"]["held_bytes"] is None
    assert beta["after"]["blocks"] is None
    assert snapshot["files"]["data"]["pairs"] is None
    assert snapshot["pairs"] is None
    assert snapshot["files"]["data"]["providers"]["providers[0]"]["after"]["held_bytes"] == len(DATA)


def test_o08_a_block_map_without_a_blocks_field_is_a_failed_probe_with_a_reason_not_an_empty_reading(probes, seeded):
    """O08 with O01: an answer that gives no blocks is recorded as probe_ok False with a reason, its holdings None; it
    is not an empty reading that would show a loss, or no gain, from the providers' real holdings."""
    probes.start({})
    seeded.omit_blocks = True
    snapshot = probes.finish()
    assert snapshot["ok"] is False
    for provider in ("providers[0]", "providers[1]"):
        after = snapshot["files"]["data"]["providers"][provider]["after"]
        assert after["probe_ok"] is False
        assert after["error"]
        assert (after["held_bytes"], after["blocks"]) == (None, None)
    assert snapshot["files"]["data"]["pairs"] is None
    assert snapshot["pairs"] is None


def test_o08_resolving_a_tracked_file_does_not_retry_a_space_root_that_is_not_there_yet(probes, seeded, ticker):
    """O08: "not there" from the space root (400 posix enoent on Onedata 25.0), then a 200 on a second request, is a
    failed probe. The provisioner's wait for a new space root would have asked again after a pause and succeeded,
    inside one attempt."""
    seeded.root_enoent_polls = 1
    probes.start({})
    assert len(calls_for(seeded, "/spaces/")) == 1
    assert ticker.slept == []  # no wait of the space-root kind
    snapshot = probes.finish()
    before = snapshot["files"]["data"]["providers"]["providers[0]"]["before"]
    assert before["probe_ok"] is False
    assert before["status"] == 400
    assert snapshot["ok"] is False
    # the next reading is its own
    assert snapshot["files"]["data"]["providers"]["providers[0]"]["after"]["probe_ok"] is True


def test_o08_a_reset_inside_the_resolution_is_retried_and_counted_in_the_probes_attempts(probes, seeded, ticker):
    """O08: the only retry of a resolution is the probe's own, of a network reset, and the entry's attempts say how
    many requests of that kind it took."""
    probes.start({})
    seeded.resets.append(["/children", 2])
    snapshot = probes.finish()
    assert snapshot["files"]["data"]["entry"]["probe_ok"] is True
    assert snapshot["files"]["data"]["entry"]["attempts"] == 3
    assert ticker.slept.count(0.0) == 2  # the probe's backoff; the 30 s are the transfer listing's settle wait


def test_o08_a_provider_the_block_map_leaves_out_is_a_failed_probe_with_a_reason_not_an_empty_reading(probes, seeded):
    """O08 with O01: a partial answer that does not list a tracked provider is recorded as probe_ok False with a
    reason, its holdings None; it is not a reading of nothing held."""
    probes.start({})
    seeded.omit_providers = {BETA_ID}
    snapshot = probes.finish()
    assert snapshot["ok"] is False
    beta = snapshot["files"]["data"]["providers"]["providers[1]"]["after"]
    assert beta["probe_ok"] is False
    assert beta["error"]
    assert (beta["held_bytes"], beta["blocks"]) == (None, None)
    assert snapshot["files"]["data"]["providers"]["providers[0]"]["after"]["probe_ok"] is True
    assert snapshot["files"]["data"]["pairs"] is None
    assert snapshot["pairs"] is None


def test_o01_a_block_with_a_negative_length_is_a_failed_probe_not_a_zero_reading(probes, seeded):
    """O01 with O08: an invalid block makes the provider's reading unknown (probe_ok False, no holdings), not zero."""
    probes.start({})
    seeded.blocks[(seeded.id_of(PATH), BETA_ID)] = [[0, -1]]
    snapshot = probes.finish()
    beta = snapshot["files"]["data"]["providers"]["providers[1]"]["after"]
    assert beta["probe_ok"] is False
    assert (beta["held_bytes"], beta["blocks"]) == (None, None)
    assert snapshot["ok"] is False
    assert snapshot["pairs"] is None


def test_a_snapshot_without_a_start_has_no_before_and_no_pairs(probes, seeded):
    """Without the reading at the start nothing can be said about growth: the snapshot is incomplete, not zero."""
    snapshot = probes.finish()
    assert snapshot["ok"] is False
    assert snapshot["pairs"] is None
    assert snapshot["files"]["data"]["providers"]["providers[0]"]["before"] is None


def test_a_tracked_file_that_is_gone_is_a_failed_probe_not_an_error(probes, seeded):
    probes.start({})
    seeded._delete(seeded.id_of(PATH))
    snapshot = probes.finish()
    assert snapshot["ok"] is False
    assert snapshot["files"]["data"]["size"] is None
    assert snapshot["files"]["data"]["entry"]["status"] == 404


def test_the_probes_are_an_instrument_and_their_snapshot_goes_into_the_record(probes, provisioner, seeded):
    """The record holds the snapshot under the namespace; a scorer needs nothing else."""
    from record_support import hand_made

    assert isinstance(probes, Instrument)
    assert probes.namespace == NAMESPACE == "platform_probes"
    start_instruments([probes], {"run": "r1"})
    place_ranges(provisioner, [ROW])
    record = finish_instruments(hand_made(), [probes])
    snapshot = record.instruments[NAMESPACE]
    assert snapshot["ok"] is True
    assert snapshot["pairs"]["providers[1]"]["gained_bytes"] == 600


def test_the_retry_attempts_come_from_the_placement_settings(probes, seeded, ticker):
    """probe_attempts and probe_backoff_s of [placement] bound the probes' retries (3 attempts in these tests)."""
    seeded.resets.append(["/distribution", 99, BETA_ID])
    probes.start({})
    assert len([c for c in seeded.provider_calls if c[0] == BETA_ID and "distribution" in c[2]]) == 3
    assert len([c for c in seeded.provider_calls if c[0] == ALPHA_ID and "distribution" in c[2]]) == 1
