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
    # every live module is skipped whole, and none runs a test: counted from the files, so a new live module is
    # covered without editing this test
    modules = sorted((ROOT / "tests" / "live").glob("test_*.py"))
    assert modules
    assert f"{len(modules)} skipped" in done.stdout, done.stdout[-800:]
    for outcome in (" passed", " failed", " error"):
        assert outcome not in done.stdout, done.stdout[-800:]


def test_o09_only_the_resets_calls_the_truncating_write_the_tool_server_user_reads_and_the_clock_are_unconfirmed():
    """O09, O11: the live contract tests against Onedata 25.0 confirmed every call the layer makes that no source did,
    the gate's walk by `parent_id` among them, the reset's eviction of a directory and a transfer record's `spaceId`,
    and refuted the file's `path` attribute the gate read before, which no release at hand answers. The gate no longer
    asks for it; the client's docstring records what refuted it. What is left unconfirmed are the reset's calls no live
    run has made (a transfer's cancel, a QoS summary, a requirement's record and its removal), the write of content
    without an offset, which truncates the file and which a restarted upload relies on (O25), and the two reads that
    bound a writing tool-server user (#177): its Onezone admin privileges, named by Onezone's REST description alone,
    and the clusters its token acts in, the current user's read sent as the token, named by that description and the
    route of oz-worker. And, since a job's times are stamped on the platform's global clock, Onezone's reading of it
    that the transfer log and the reset's history place their window by (#207), a public call named by oz-worker's
    source alone (14b5bda, `provider_routes.erl:192-198`): the next live slot confirms it. And the three calls of a
    run's revocation at its end (C19): a user's read of its generation of temporary tokens, its own revocation of them,
    and the verification that answers a revoked token `tokenRevoked`, all named by oz-worker's source alone. And the
    four Onepanel calls that check a space held on an imported storage (O41), named by onepanel's source alone."""
    from expcore_onedata import client

    assert {(m, p) for m, p, _ in client.UNVERIFIED_ENDPOINTS} == {
        ("GET", "/provider/spaces/{space}"),  # an imported space's support and its storage, and its scan (O41)
        ("GET", "/provider/storages/{id}"),
        ("GET", "/provider/spaces/{space}/storage-import/auto/info"),
        ("POST", "/provider/spaces/{space}/storage-import/auto/force-start"),
        ("PUT", "/data/{id}/content"),
        ("DELETE", "/transfers/{id}"),
        ("GET", "/data/{id}/qos/summary"),
        ("GET", "/qos_requirements/{qid}"),
        ("DELETE", "/qos_requirements/{qid}"),
        ("GET", "/data/{id}/files"),  # the QoS scan's recursive listing, from the source only (O33)
        ("GET", "/users/{id}/effective_privileges"),
        ("GET", "/user/effective_clusters"),
        ("GET", "/provider/public/get_current_time"),
        ("GET", "/user/tokens/temporary"),  # a run's revocation at its end (C19), from the source only
        ("DELETE", "/user/tokens/temporary"),
        ("POST", "/tokens/verify_access_token"),
    }
    what = " ".join(w for _, _, w in client.UNVERIFIED_ENDPOINTS)
    assert all(
        field in what
        for field in (
            "fileId",
            "requirements",
            "400",
            "truncates",
            "oz_view_privileges",
            "clusters",
            "timeMillis",
            "generation",
            "tokenRevoked",
        )
    )
    assert "14b5bda7d84bbfe2f25bc9bedb9033af6d1a8b55" in client.__doc__  # the source that names the clock's endpoint
    assert "provider_routes.erl:192-198" in client.__doc__
    assert "58c697695fd73ff480f4a6e5fdbf733036465398" in client.__doc__  # the REST description they are named by
    assert "paths/user/effective_clusters.yaml" in client.__doc__
    assert "03af698e5759f9db97a003047eb4e29e4ba7507f" in client.__doc__  # oz-worker's route of the clusters read
    assert "badValueNotAllowed" in client.__doc__
    assert '"attribute": "path"' not in Path(client.__file__).read_text(encoding="utf-8")


def test_every_unverified_endpoint_has_a_live_contract_test():
    """Each (method, path) neither a source nor a live run confirms is named by a `covers:` line of a live test, so
    none goes unchecked: the contract tests', or, for the panel calls of an imported space, the import's (O41)."""
    from expcore_onedata.client import UNVERIFIED_ENDPOINTS

    live = "".join(path.read_text(encoding="utf-8") for path in sorted((ROOT / "tests" / "live").glob("test_*.py")))
    missing = [f"{m} {p}" for m, p, _ in UNVERIFIED_ENDPOINTS if f"# covers: {m} {p}\n" not in live]
    assert not missing, f"no live test covers: {missing}"


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
        ("POST", "/transfers"),
        ("GET", "/data/{id}/distribution"),
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
