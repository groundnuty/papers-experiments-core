"""The fake's QoS model the reset's removal relies on (O33, `expcore_onedata.testing.fake_qos`): an addition and a
removal reach the other providers late, a requirement is `pending` until its replication has landed, the replication
goes on landing on a provider until the removal reaches it, and a cancel can miss one file."""

import pytest
from qos_world import federation, two_providers

from expcore_onedata.client import PlatformError, answered_absent
from expcore_onedata.testing import ALPHA_ID, BETA_ID, DATA, write_platform


@pytest.fixture
def fake(monkeypatch, ticker):
    world = federation(monkeypatch, ticker)
    for name in ("a", "b", "c"):
        world.add_file(f"other/{name}.bin", DATA)  # alpha holds them, beta does not
    return world


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


def record(client, qid, provider):
    """The requirement's record on a provider, or None where it answers that it is not there."""
    try:
        return client.get(f"/qos_requirements/{qid}", provider=provider)
    except PlatformError as error:
        if answered_absent(error):
            return None
        raise


def listed(client, fake, path, qid, provider) -> bool:
    summary = client.get(f"/data/{fake.id_of(path)}/qos/summary", provider=provider)
    return qid in summary["requirements"]


def test_o33_the_fakes_addition_reaches_the_other_provider_after_its_dbsync_delay(client, fake, ticker):
    """O33: a requirement added through beta is there at once and reaches alpha `qos_sync_s` later."""
    fake.qos_sync_s = 8.0
    qid = fake.add_qos("other", through=BETA_ID)
    assert record(client, qid, "beta") is not None
    assert record(client, qid, "alpha") is None
    ticker.sleep(7.5)
    assert record(client, qid, "alpha") is None
    ticker.sleep(0.5)
    assert record(client, qid, "alpha")["fileId"] == fake.id_of("other")


def test_o33_the_fakes_removal_is_at_once_where_it_was_sent_and_late_elsewhere(client, fake, ticker):
    """O33: a removal sent through alpha is there at once; beta still answers the record and lists the requirement
    in its summaries until the removal reaches it, 12 s later, and only then does the requirement leave `qos`."""
    fake.qos_sync_by_provider = {BETA_ID: 12.0}
    qid = fake.add_qos("other")
    client.mutate("DELETE", f"/qos_requirements/{qid}", provider="alpha")
    assert record(client, qid, "alpha") is None
    assert not listed(client, fake, "other/a.bin", qid, "alpha")
    assert record(client, qid, "beta") is not None
    assert listed(client, fake, "other/a.bin", qid, "beta")
    assert fake.qos_removed[qid] == {ALPHA_ID: ticker.now, BETA_ID: ticker.now + 12.0}
    ticker.sleep(12.0)
    assert record(client, qid, "beta") is None
    assert not listed(client, fake, "other/a.bin", qid, "beta")
    assert qid not in fake.qos


def test_o33_the_fakes_requirement_is_pending_until_its_replication_has_landed(client, fake, ticker):
    """O33: the record and the summary say `pending` while a file is still to land on beta, and `fulfilled` once the
    last has; a status a test gives a requirement stands whatever its replication does."""
    fake.qos_replicates_to, fake.qos_land_after_s, fake.qos_land_every_s = BETA_ID, 10.0, 5.0
    qid = fake.add_qos("other")
    assert record(client, qid, "alpha")["status"] == "pending"
    ticker.sleep(10.0)  # the first of three files lands, at the next call
    assert record(client, qid, "beta")["status"] == "pending"
    assert fake.held("other/a.bin", BETA_ID) == [[0, len(DATA)]]
    summary = client.get(f"/data/{fake.id_of('other/a.bin')}/qos/summary", provider="beta")
    assert (summary["requirements"], summary["status"]) == ({qid: "pending"}, "pending")
    ticker.sleep(10.0)
    assert record(client, qid, "alpha")["status"] == record(client, qid, "beta")["status"] == "fulfilled"
    fake.qos_status_of[qid] = "impossible"
    assert record(client, qid, "beta")["status"] == "impossible"


def test_o33_the_fakes_replication_lands_on_a_provider_until_the_removal_reaches_it(client, fake, ticker):
    """O33: removed through alpha at 1 s, the requirement reaches beta, the replicating provider, 12 s later: the files
    due at 5 s and 10 s land, the one due at 15 s is cancelled."""
    fake.qos_replicates_to, fake.qos_land_after_s, fake.qos_land_every_s = BETA_ID, 5.0, 5.0
    fake.qos_sync_by_provider = {BETA_ID: 12.0}
    start, qid = ticker.now, fake.add_qos("other")
    ticker.sleep(1.0)
    client.mutate("DELETE", f"/qos_requirements/{qid}", provider="alpha")
    ticker.sleep(30.0)
    record(client, qid, "beta")  # any call lands what is due
    assert [(when - start, fake.nodes[f].name) for when, _, f, _ in fake.qos_landed] == [
        (5.0, "a.bin"),
        (10.0, "b.bin"),
    ]
    assert fake.held("other/c.bin", BETA_ID) == []


def test_o33_the_fakes_missed_cancel_lands_the_file_next_due_and_drops_the_rest(client, fake, ticker):
    """O33: with `qos_missed_cancel`, a removal that reaches beta at once still lets the file next due land whole, and
    nothing after it: at most one file's size."""
    fake.qos_replicates_to, fake.qos_land_after_s, fake.qos_land_every_s = BETA_ID, 100.0, 50.0
    fake.qos_missed_cancel = True
    qid = fake.add_qos("other", through=BETA_ID)
    client.mutate("DELETE", f"/qos_requirements/{qid}", provider="beta")
    assert record(client, qid, "beta") is None
    ticker.sleep(500.0)
    record(client, qid, "alpha")
    assert [fake.nodes[f].name for _, _, f, _ in fake.qos_landed] == ["a.bin"]
    assert fake.held("other/a.bin", BETA_ID) == [[0, len(DATA)]]
    assert fake.held("other/b.bin", BETA_ID) == fake.held("other/c.bin", BETA_ID) == []
