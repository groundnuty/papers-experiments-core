"""The preallocated ingest's baseline marks, as the health check reads them (O44, #107)."""

import json
import os
import subprocess
import sys

import pytest

from expcore.campaign.healthgate import read_health
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import boot_id
from expcore_onedata.health import PlatformHealth
from expcore_onedata.ingesthealth import baseline_health, last_marks, recovery_refusal, start_refusal
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.tokens import TokenIssuer


@pytest.fixture
def gone():
    """A holder whose process has exited and been reaped, in this boot."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return {"pid": child.pid, "boot_id": boot_id()}


def here():
    return {"pid": os.getpid(), "boot_id": boot_id()}


def journal_of(tmp_path, *marks, tail=""):
    path = tmp_path / "journal.jsonl"
    lines = [json.dumps({"space": "spaces[0]", "entry": "baseline", **mark}) for mark in marks]
    path.write_text("".join(f"{line}\n" for line in lines) + tail, encoding="utf-8")
    return path


def test_o44_a_journal_with_no_marks_or_none_at_all_is_healthy(tmp_path):
    """O44: no journal, and a journal of an arrival ingest, which writes no mark, say nothing against a run."""
    other = tmp_path / "arrival.jsonl"
    other.write_text(json.dumps({"space": "spaces[0]", "entry": "id", "label": "day"}) + "\n", encoding="utf-8")
    assert baseline_health([tmp_path / "missing.jsonl", other]).ok


def test_o44_an_unhealthy_mark_fails_the_check_until_a_whole_one_follows_it(tmp_path, gone):
    """O44: the last mark of the space decides: unhealthy fails, naming the space, the phase, the file and the step,
    and a later whole mark, written by a verified recovery, passes."""
    failed = {"state": "unhealthy", "phase": "start", "holder": gone, "file": "day-13", "step": "prefix"}
    health = baseline_health([journal_of(tmp_path, {"state": "open", "phase": "start", "holder": gone}, failed)])
    assert not health.ok
    assert health.detail.startswith("spaces[0]: ")
    assert "its start failed at day-13's prefix" in health.detail
    whole = {"state": "whole", "phase": "recover", "holder": here()}
    assert baseline_health([journal_of(tmp_path, failed, whole)]).ok


def test_o44_an_open_mark_fails_the_check_when_its_holder_is_gone_or_unnamed(tmp_path, gone):
    """O44: an open baseline whose holder no longer runs is a run that died with its baseline sparse or missing; one
    whose holder the mark does not name cannot be told from it. Both fail; a live holder is a run in progress."""
    assert (
        "stopped during its run"
        in baseline_health([journal_of(tmp_path, {"state": "open", "phase": "run", "holder": gone})]).detail
    )
    assert not baseline_health([journal_of(tmp_path, {"state": "open", "phase": "run"})]).ok
    assert baseline_health([journal_of(tmp_path, {"state": "open", "phase": "run", "holder": here()})]).ok


def test_o44_a_start_is_refused_on_any_baseline_that_is_not_whole_whoever_holds_it(tmp_path, gone):
    """O44: a start passes only a whole baseline or one never marked: an open one is refused even with a live holder
    (a run in progress, or an instrument of this process never restored), and so is an unhealthy one."""
    assert start_refusal(None) is None
    assert start_refusal({"state": "whole", "phase": "restore"}) is None
    assert "held open" in start_refusal({"state": "open", "phase": "run", "holder": here()})
    assert "not whole" in start_refusal({"state": "open", "phase": "start", "holder": gone})
    assert "not whole" in start_refusal({"state": "unhealthy", "phase": "restore", "holder": here()})


def test_o44_a_recovery_is_refused_for_another_plan_or_a_baseline_another_live_process_holds_open(gone):
    """O44: the recovery verifies the files and replicas of its own plan, so it is refused for a baseline another
    plan's run left not whole, or a mark that names no plan; and it writes into the planned files, so it waits for a
    run another live process is on. This process's own dropped instrument, a dead holder and an unhealthy mark of its
    plan are what it is for."""
    plan = "a" * 64
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        held = {"state": "open", "phase": "run", "plan": plan, "holder": {"pid": other.pid, "boot_id": boot_id()}}
        assert "another process" in recovery_refusal(held, plan)
    finally:
        other.kill()
        other.wait()
    assert recovery_refusal({"state": "open", "phase": "run", "plan": plan, "holder": here()}, plan) is None
    assert recovery_refusal({"state": "open", "phase": "run", "plan": plan, "holder": gone}, plan) is None
    assert recovery_refusal({"state": "unhealthy", "phase": "start", "plan": plan, "holder": gone}, plan) is None
    assert "another plan" in recovery_refusal({"state": "unhealthy", "phase": "start", "plan": "b" * 64}, plan)
    assert "another plan" in recovery_refusal({"state": "open", "phase": "run", "holder": gone}, plan)
    assert recovery_refusal({"state": "whole", "phase": "restore", "plan": "b" * 64}, plan) is None


def test_o44_a_torn_last_line_is_skipped_and_any_other_unreadable_line_fails_the_check(tmp_path, gone):
    """O44: a crash can cut the journal's last line, which is skipped as the journal's other readers skip it; a line
    before it that cannot be read, or a mark in a state that is not one of the three, fails the check closed."""
    whole = {"state": "whole", "phase": "restore", "holder": here()}
    assert baseline_health([journal_of(tmp_path, whole, tail='{"space": "spaces[0]", "entry": "base')]).ok
    broken = tmp_path / "broken.jsonl"
    broken.write_text("not json\n" + json.dumps({"space": "spaces[0]", "entry": "baseline", **whole}) + "\n")
    assert "cannot be read" in baseline_health([broken]).detail
    assert not baseline_health([journal_of(tmp_path, {"state": "sparse", "phase": "run", "holder": gone})]).ok
    assert last_marks(journal_of(tmp_path, whole)) == {
        "spaces[0]": {"space": "spaces[0]", "entry": "baseline", **whole}
    }


def test_o44_the_platforms_health_check_fails_on_an_unhealthy_baseline_before_it_asks_the_federation(
    tmp_path, fake, gone
):
    """O44: given the ingests' journals, the platform's check fails while a baseline is unhealthy, naming the space,
    and asks the federation nothing; once a verified recovery marks it whole, it answers as before."""
    config = load_platform(write_platform(tmp_path))
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    failed = {"state": "unhealthy", "phase": "restore", "holder": gone}
    journal = journal_of(tmp_path, failed)
    health = PlatformHealth(config, client, issuer, clock=lambda: NOW, ingest_journals=[journal])
    asked = len(fake.requests)
    verdict = read_health(health)
    assert (verdict.ok, verdict.detail.split(":")[0]) == (False, "spaces[0]")
    assert len(fake.requests) == asked
    journal_of(tmp_path, failed, {"state": "whole", "phase": "recover", "holder": here()})
    assert read_health(health).ok
