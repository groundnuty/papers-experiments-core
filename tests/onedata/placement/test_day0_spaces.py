"""Day-0 on a platform of several spaces: every check runs, names each space by its position, takes the worst verdict
over the spaces, and the report scrubs every space's name and id (O12)."""

import json

import pytest
from fake_reset import FakeReset
from placement_support import ALPHA_ID, BETA_ID, with_placement
from platform_support import (
    ADMIN_ENV,
    BASELINE,
    SPACE_IDS,
    SPACE_LABELS,
    SPACE_NAME,
    SPACE_NAMES,
    USERS,
    with_spaces,
    write_platform,
)

from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.day0.report import sensitive_strings
from expcore_onedata.day0.runner import EXIT_FAIL, EXIT_UNKNOWN, build_context, run, run_checks

POSITIONS = ["spaces[0]", "spaces[1]"]


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeReset:
    """The study space and a second space, each supported by alpha and beta, and the users in both."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    federation.add_data_space(SPACE_IDS[1], SPACE_NAMES[1], [ALPHA_ID, BETA_ID])
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        for space in SPACE_IDS[:2]:
            federation.grant(user_id, BASELINE, space=space)
    for space in SPACE_IDS[:2]:  # each root holds a file, so each listing measures something
        federation.add_file("item.bin", b"x", space=space)
    return federation


def two_spaces(data):
    with_placement(data)
    with_spaces(data, 2)
    data["spaces"][1]["holder"] = "beta"  # the second space's own data provider, so its round trip places on alpha


@pytest.fixture
def platform(tmp_path):
    directory = tmp_path / "target"
    directory.mkdir()
    return write_platform(directory, two_spaces)


def run_all(platform, fake, ticker, tmp_path, **options):
    client = OnezoneClient(load_platform(platform), http=fake.client_http())
    options = {"tcp": lambda *a: 2.0, "now": lambda: 1_800_000_000.0, "allow_mutation": True, **options}
    code, json_path, md_path = run(
        platform, tmp_path / "reports", client=client, sleep=ticker.sleep, clock=ticker.clock, **options
    )
    report = json.loads(json_path.read_text(encoding="utf-8"))
    return code, report, json_path.read_text(encoding="utf-8") + md_path.read_text(encoding="utf-8")


def checks_of(report):
    return {check["name"]: check for check in report["checks"]}


def one_check(platform, fake, ticker, tmp_path, name):
    client = OnezoneClient(load_platform(platform), http=fake.client_http())
    ctx = build_context(
        platform, tmp_path / "out", client=client, allow_mutation=True, clock=ticker.clock, sleep=ticker.sleep
    )
    return run_checks(ctx, [name])[0]


def test_a_two_space_platform_runs_every_check_and_writes_a_report(platform, fake, ticker, tmp_path):
    """Every check runs in each space it is about and reaches its own verdict; none raises, and the report is
    written without a space's name, id or label in it."""
    code, report, text = run_all(platform, fake, ticker, tmp_path)
    checks = checks_of(report)
    assert {name: check["status"] for name, check in checks.items()} == {
        "reachability": "pass",
        "health": "pass",
        "tokens": "unknown",  # the fixed tool-server user holds the baseline only, as in the one-space runs
        "content_type": "unknown",  # no data path is REST, so nothing is measured (O08)
        "listing": "pass",
        "read_only": "pass",
        "round_trip": "pass",
    }
    assert code == EXIT_UNKNOWN
    assert not [check["name"] for check in checks.values() if "raised" in check["summary"]]
    for name in ("listing", "read_only", "round_trip"):
        spaces = checks[name]["facts"]["spaces"]
        assert list(spaces) == POSITIONS, name
        assert {row["result"] for row in spaces.values()} == {"pass"}, name
    for space in checks["read_only"]["facts"]["spaces"].values():
        assert {name: row["status"] for name, row in space["providers"].items()} == {"alpha": 403, "beta": 403}
    for needle in (*SPACE_NAMES[:2], *SPACE_IDS[:2], *SPACE_LABELS[:2], "alpha.test", "beta.test", "zone.test"):
        assert needle not in text, needle
    assert not [n for n in fake.nodes.values() if n.name.startswith("day0-")]  # each space's scratch is gone


def test_a_support_difference_in_the_second_space_fails_the_run_and_names_that_space(platform, fake, ticker, tmp_path):
    """The federation reports the second space without beta, which platform.toml declares: the run fails, the report is
    written, and the failure names the second space by its position alone."""
    fake.space_supports[SPACE_IDS[1]] = {ALPHA_ID: {"readonly": False}}
    code, report, text = run_all(platform, fake, ticker, tmp_path, allow_mutation=False)
    assert code == EXIT_FAIL
    health = checks_of(report)["health"]
    assert health["status"] == "fail"
    assert health["summary"].startswith("spaces[1]: ")
    assert "spaces[0]" not in health["summary"]
    assert SPACE_NAMES[1] not in text
    assert SPACE_IDS[1] not in text


def test_a_listing_that_fails_in_the_second_space_alone_fails_and_names_it(platform, fake, ticker, tmp_path):
    """The worst verdict over the spaces: the second space's root cannot be read on beta, so the listing fails and
    names that space and provider, while the first space's listing is kept as the pass it is."""
    fake.resets.append([f"/spaces/{SPACE_IDS[1]}", 99, BETA_ID])
    entry = one_check(platform, fake, ticker, tmp_path, "listing")
    assert entry.ok is False
    assert "spaces[1]" in entry.summary
    assert "beta" in entry.summary
    assert "spaces[0]" not in entry.summary
    spaces = entry.facts["spaces"]
    assert (spaces["spaces[0]"]["result"], spaces["spaces[1]"]["result"]) == ("pass", "fail")
    assert spaces["spaces[1]"]["providers"]["beta"]["error"] == "PlatformError"


def test_the_data_path_token_reaches_both_spaces_and_a_privilege_difference_in_the_second_fails(
    platform, fake, ticker, tmp_path
):
    """Tokens: the data path's token is minted as a run mints it, over every space the plane reaches; the data-path
    user holding less in the second space refuses it, naming that space by its position."""
    passing = one_check(platform, fake, ticker, tmp_path, "tokens")
    assert passing.facts["planes"]["data_path"]["examined"] is True
    assert passing.facts["planes"]["data_path"]["spaces"] == POSITIONS
    fake.grant(USERS["login-data"][1], ["space_view"], space=SPACE_IDS[1])
    entry = one_check(platform, fake, ticker, tmp_path, "tokens")
    assert entry.ok is False
    assert entry.facts["planes"]["data_path"]["refusal"].startswith("spaces[1]: ")


def test_the_report_withholds_every_declared_spaces_name_and_id(platform, fake):
    config = load_platform(platform)
    assert set(sensitive_strings(config)) >= {*SPACE_NAMES[:2], *SPACE_IDS[:2]}
