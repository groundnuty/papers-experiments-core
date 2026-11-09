"""Without EXPCORE_LIVE_PLATFORM or EXPCORE_LIVE_DOCKER the live tests are skipped as modules: nothing is contacted."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_live_tests_are_skipped_when_no_platform_is_named():
    env = {k: v for k, v in os.environ.items() if k not in ("EXPCORE_LIVE_PLATFORM", "EXPCORE_LIVE_DOCKER")}
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/live", "-q", "-rs", "-p", "no:cacheprovider"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode in (0, 5), done.stdout[-500:]  # 5: pytest found no test to run, only a skipped module
    assert "EXPCORE_LIVE_PLATFORM is not set" in done.stdout
    assert "EXPCORE_LIVE_DOCKER is not set" in done.stdout
    assert "2 skipped" in done.stdout


def test_every_unverified_endpoint_has_a_live_contract_test():
    """Each (method, path) no source confirms is named by a `covers:` line of a live test, so none goes unchecked."""
    from expcore_onedata.client import UNVERIFIED_ENDPOINTS

    live = (ROOT / "tests" / "live" / "test_contract.py").read_text(encoding="utf-8")
    assert UNVERIFIED_ENDPOINTS, "the list is empty: remove this test along with the last unconfirmed endpoint"
    missing = [f"{m} {p}" for m, p, _ in UNVERIFIED_ENDPOINTS if f"# covers: {m} {p}\n" not in live]
    assert not missing, f"no live test covers: {missing}"


def test_the_unconfirmed_calls_and_details_are_exactly_the_gate_reads_the_answer_details_and_the_transfer_log():
    """O05, O08: the list holds the two reads the mutation gate relies on (a space's details, a file's path attribute),
    the three answer details no source confirms (the subject of an examination, the providers' name and online fields,
    and the content type of a file's content, which the data gate streams on), and the two details of the probes'
    transfer log (the waiting state of the listing, a record's type, provider ids and times); nothing else."""
    from expcore_onedata.client import UNVERIFIED_ENDPOINTS

    assert {(m, p) for m, p, _ in UNVERIFIED_ENDPOINTS} == {
        ("GET", "/spaces/{space}"),
        ("GET", "/data/{id}"),
        ("POST", "/tokens/examine"),
        ("POST", "/tokens/infer_access_token_scope"),
        ("GET", "/data/{file}/content"),
        ("GET", "/spaces/{space}/transfers"),
        ("GET", "/transfers/{id}"),
    }
    what = " ".join(w for _, _, w in UNVERIFIED_ENDPOINTS)
    assert "subject" in what
    assert "name and online" in what
    assert "name field" in what
    assert "application/octet-stream" in what
    assert "mock" in what  # the only basis there is
    assert "waiting" in what
    assert all(field in what for field in ("type", "replicatingProviderId", "scheduleTime", "finishTime"))


def test_the_slow_live_expiry_test_needs_its_own_switch():
    """The expiry test waits out a real lifetime, so it is skipped unless EXPCORE_LIVE_SLOW=1 as well as a platform."""
    live = (ROOT / "tests" / "live" / "test_contract.py").read_text(encoding="utf-8")
    assert 'os.environ.get("EXPCORE_LIVE_SLOW") != "1"' in live
    assert "skipif" in live


def test_o05_the_live_test_of_the_conditions_is_one_test_per_condition_named_by_position():
    """O05: a condition that is skipped must not skip the others, so the tool-server token test is parametrised over
    the conditions, by position (no condition name in the report), and nothing loops over them inside one test."""
    live = (ROOT / "tests" / "live" / "test_contract.py").read_text(encoding="utf-8")
    assert '@pytest.mark.parametrize("index", range(CONDITIONS), ids=lambda index: f"condition{index}")' in live
    assert "for name in config.toolserver.conditions" not in live
    assert "mint_for_condition(" in live
    assert "pytest.skip(f" not in live
