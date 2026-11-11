"""Without EXPCORE_LIVE_PLATFORM or EXPCORE_LIVE_DOCKER the live tests are skipped as modules: nothing is contacted."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_live_tests_are_skipped_when_no_platform_is_named():
    gates = ("EXPCORE_LIVE_PLATFORM", "EXPCORE_LIVE_DOCKER", "EXPCORE_LIVE_MUTATE")
    env = {k: v for k, v in os.environ.items() if k not in gates}
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
    assert "EXPCORE_LIVE_DOCKER=1, EXPCORE_LIVE_MUTATE=1 and EXPCORE_LIVE_PLATFORM are needed" in done.stdout
    assert "3 skipped" in done.stdout


def test_o09_no_call_is_left_unconfirmed_and_the_refuted_path_read_is_gone():
    """O09: the live contract tests against Onedata 25.0 confirmed every call the layer makes that no source did, the
    gate's walk by `parent_id` among them, and refuted the file's `path` attribute the gate read before, which no
    release at hand answers. The gate no longer asks for it, so nothing is left unconfirmed; the client's docstring
    records what refuted it."""
    from expcore_onedata import client

    assert client.UNVERIFIED_ENDPOINTS == ()
    assert "badValueNotAllowed" in client.__doc__
    assert '"attribute": "path"' not in Path(client.__file__).read_text(encoding="utf-8")


def test_every_endpoint_the_live_federation_confirmed_keeps_its_live_test():
    """O05, O08, O09: an entry that left the list on a live run keeps the test that confirmed it, so a release that
    answers otherwise fails it."""
    live = (ROOT / "tests" / "live" / "test_contract.py").read_text(encoding="utf-8")
    for method, path in (
        ("GET", "/data/{id}"),
        ("GET", "/spaces/{space}"),
        ("POST", "/tokens/examine"),
        ("POST", "/tokens/infer_access_token_scope"),
        ("GET", "/data/{file}/content"),
        ("GET", "/spaces/{space}/transfers"),
        ("GET", "/transfers/{id}"),
    ):
        assert f"# covers: {method} {path}\n" in live


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
