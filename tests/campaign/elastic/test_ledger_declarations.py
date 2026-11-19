"""C48: each model's declared fingerprints, an append-only log in the ledger. A lease's run that saw a fingerprint its
lease did not declare pauses its model's issue, and nothing else; `declare`, while a dispatcher holds the ledger,
resumes it; a reply with no fingerprint is undeclared unless "none" is; and nothing of it changes the fingerprint."""

import dataclasses

import pytest
from elastic_support import FINGERPRINT, assert_invariants, completion, finish, join, leased, ledger_path, make, plan

from expcore.campaign.elastic import DeclareRefused, SiteReading, Wait
from expcore.campaign.elastic.__main__ import main
from expcore.campaign.elastic.channel import kernel_lease
from expcore.campaign.elastic.controlinputs import ledger_view
from expcore.models.served import DeclaredSet

TWO = {"runs": plan(models=("m1", "m2"), tasks=2, conditions=("api-none",)), "declared": {"m1": ["fp-a"]}}


def _config_bytes(ledger):
    return ledger.db.execute("SELECT value FROM meta WHERE key='config'").fetchone()[0]


def _seen_by(ledger, lease, *undeclared, state="infra_fail", charged=1):
    """The lease's completion read and verified, reporting `undeclared` seen by its run."""
    found = dataclasses.replace(completion(lease, state=state, charged=charged), undeclared=undeclared)
    ledger.observe_site("site-a", SiteReading(completions=(found,)))
    return ledger.verify_archive(lease.lease_id, found.archive_sha256)


def test_c48_an_undeclared_fingerprint_pauses_its_model_alone_and_leaves_leases_in_flight_running(tmp_path):
    """C48: m1's first lease reports fp-new, undeclared: m1 gets no new lease (a free federation takes m2's run, and
    one more waits, saying why), the controller counts none of m1's runs, m1's other lease in flight runs on and
    counts, and status shows the pause with the value."""
    ledger, clock = make(tmp_path, **TWO)
    for label in ("fed-1", "fed-2", "fed-3", "fed-4"):
        join(ledger, clock, label)
    first, other, still = leased(ledger, "fed-1"), leased(ledger, "fed-2"), leased(ledger, "fed-3")
    assert [first.model, other.model, still.model] == ["m1", "m2", "m1"]
    assert first.declared == DeclaredSet(("fp-a",), none=False)
    assert kernel_lease(first)["declared"] == {"fingerprints": ["fp-a"], "none": False}
    assert _seen_by(ledger, first, "fp-new") == "counted"
    assert ledger.run(first.run_key).state == "pending"
    assert ledger.paused() == {"m1": ("fp-new",)}
    assert leased(ledger, "fed-1").model == "m2"
    held = ledger.issue("fed-4")
    assert isinstance(held, Wait)
    assert "model m1 is paused: undeclared fingerprint(s) seen: fp-new" in held.reason
    assert ledger_view(ledger).demand.pending == {}
    assert ledger.lease(still.lease_id).state == "issued"
    assert finish(ledger, ledger.lease(still.lease_id)) == "counted"
    assert ledger.run(still.run_key).state == "done"
    status = ledger.status()
    assert status.paused == {"m1": ("fp-new",)}
    assert status.declared == {"m1": ("fp-a",), "m2": ()}
    assert_invariants(ledger)


def test_c48_declare_while_a_dispatcher_holds_the_ledger_resumes_the_model(tmp_path, capsys):
    """C48: m1 is paused on fp-new, so its run waits; with the dispatcher's lock held, `declare` records fp-new with
    its reason and actor, m1's next lease is issued at once and carries the grown set, and `status` shows the set and
    the log."""
    ledger, clock = make(tmp_path, runs=plan(tasks=2, conditions=("api-none",)), declared={"m1": ["fp-a"]})
    join(ledger, clock, "fed-1")
    assert _seen_by(ledger, leased(ledger, "fed-1"), "fp-new") == "counted"
    assert isinstance(ledger.issue("fed-1"), Wait)
    path = str(ledger_path(tmp_path))
    args = ["declare", path, "--model", "m1", "--fingerprint", "fp-new", "--reason", "a redeploy", "--actor", "op-1"]
    assert main(args) == 0
    assert "declared model m1: fp-new" in capsys.readouterr().out
    assert ledger.paused() == {}
    resumed = leased(ledger, "fed-1")
    assert resumed.model == "m1"
    assert resumed.declared == DeclaredSet(("fp-a", "fp-new"), none=False)
    assert main(["status", path]) == 0
    out = capsys.readouterr().out
    for line in (
        "model m1: declared fp-a, fp-new",
        "model m1 fp-a by models.toml",
        "model m1 fp-new by op-1: a redeploy",
    ):
        assert line in out, out
    assert "paused" not in out
    assert_invariants(ledger)


def test_c48_no_fingerprint_is_undeclared_until_none_is_declared_and_neither_declares_the_other(tmp_path, capsys):
    """C48: a lease's run reports a reply with no fingerprint and one under fp-x: declaring fp-x leaves the model
    paused on the reply with none, and declaring none resumes it; the lease then carries both."""
    ledger, clock = make(tmp_path, **TWO)
    join(ledger, clock, "fed-1")
    assert _seen_by(ledger, leased(ledger, "fed-1"), None, "fp-x") == "counted"
    assert ledger.paused() == {"m1": (None, "fp-x")}
    path = str(ledger_path(tmp_path))
    assert main(["declare", path, "--model", "m1", "--fingerprint", "fp-x", "--reason", "r", "--actor", "a"]) == 0
    assert "model m1 stays paused: undeclared fingerprint(s) seen: none" in capsys.readouterr().out
    assert ledger.paused() == {"m1": (None,)}
    assert leased(ledger, "fed-1").model == "m2"
    finish(ledger, ledger.leases()[-1])
    ledger.declare("m1", None, "the endpoint emits no system_fingerprint", "op-1")
    assert ledger.paused() == {}
    assert leased(ledger, "fed-1").declared == DeclaredSet(("fp-a", "fp-x"), none=True)
    assert_invariants(ledger)


def test_c48_a_value_already_declared_and_a_model_with_no_runs_are_refused_and_nothing_is_ever_removed(
    tmp_path, capsys
):
    """C48: re-declaring a declared value and declaring for a model the ledger has no runs of are refused (exit 1)
    with nothing written; the log keeps every row it had."""
    ledger, _ = make(tmp_path, **TWO)
    with pytest.raises(DeclareRefused, match="has 'fp-a' declared already"):
        ledger.declare("m1", "fp-a", "again", "op")
    with pytest.raises(ValueError, match="one fingerprint at a time"):
        ledger.declare("m1", "fp-a | fp-b", "joined", "op")
    path = str(ledger_path(tmp_path))
    assert main(["declare", path, "--model", "m9", "--none", "--reason", "r", "--actor", "a"]) == 1
    assert "no runs of model m9" in capsys.readouterr().err
    ledger.declare("m2", None, "r", "a")
    assert main(["declare", path, "--model", "m2", "--none", "--reason", "r", "--actor", "a"]) == 1
    assert "has none declared already" in capsys.readouterr().err
    log = ledger.declaration_log()
    assert [(d.model, d.fingerprint, d.actor) for d in log] == [("m1", "fp-a", "models.toml"), ("m2", None, "a")]


def test_c48_a_declaration_leaves_the_campaign_fingerprint_as_it_was(tmp_path):
    """C48: the pinned configuration, the fingerprint a federation joins with and the one every lease names are the
    same before and after a declaration."""
    ledger, clock = make(tmp_path, **TWO)
    join(ledger, clock, "fed-1")
    before = (
        _config_bytes(ledger),
        ledger.config.fingerprint,
        kernel_lease(leased(ledger, "fed-1"))["campaign_sha256"],
    )
    ledger.declare("m1", "fp-new", "a redeploy", "op")
    join(ledger, clock, "fed-2")  # a federation of the same campaign still joins: its fingerprint is unchanged
    after = (_config_bytes(ledger), ledger.config.fingerprint, kernel_lease(leased(ledger, "fed-2"))["campaign_sha256"])
    assert before == after == (before[0], FINGERPRINT, FINGERPRINT)
