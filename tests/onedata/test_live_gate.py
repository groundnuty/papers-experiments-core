"""Without EXPCORE_LIVE_PLATFORM the live contract tests are skipped as a module, so nothing is built or contacted."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_live_tests_are_skipped_when_no_platform_is_named():
    env = {k: v for k, v in os.environ.items() if k != "EXPCORE_LIVE_PLATFORM"}
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
    assert "1 skipped" in done.stdout


def test_every_unverified_endpoint_has_a_live_contract_test():
    """Each (method, path) no source confirms is named by a `covers:` line of a live test, so none goes unchecked."""
    from expcore_onedata.client import UNVERIFIED_ENDPOINTS

    live = (ROOT / "tests" / "live" / "test_contract.py").read_text(encoding="utf-8")
    assert UNVERIFIED_ENDPOINTS, "the list is empty: remove this test along with the last unconfirmed endpoint"
    missing = [f"{m} {p}" for m, p, _ in UNVERIFIED_ENDPOINTS if f"# covers: {m} {p}\n" not in live]
    assert not missing, f"no live test covers: {missing}"


def test_the_unconfirmed_details_are_exactly_the_subject_and_the_providers_name_and_online():
    """No path is unconfirmed any more: the list holds the two answer details no source confirms, and nothing else."""
    from expcore_onedata.client import UNVERIFIED_ENDPOINTS

    assert {(m, p) for m, p, _ in UNVERIFIED_ENDPOINTS} == {
        ("POST", "/tokens/examine"),
        ("POST", "/tokens/infer_access_token_scope"),
    }
    what = " ".join(w for _, _, w in UNVERIFIED_ENDPOINTS)
    assert "subject" in what
    assert "name and online" in what


def test_the_slow_live_expiry_test_needs_its_own_switch():
    """The expiry test waits out a real lifetime, so it is skipped unless EXPCORE_LIVE_SLOW=1 as well as a platform."""
    live = (ROOT / "tests" / "live" / "test_contract.py").read_text(encoding="utf-8")
    assert 'os.environ.get("EXPCORE_LIVE_SLOW") != "1"' in live
    assert "skipif" in live
