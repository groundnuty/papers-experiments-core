"""What the live tests of the state-changing day-0 checks require: a check that ran nothing never passes (no live
platform is contacted here)."""

from pathlib import Path

import pytest
from fault_support import faults_table
from live_support import (
    prerequisite_missing,
    require_cleaned,
    require_every_provider_refused,
    require_passed,
    require_restored,
)
from outage_support import GAMMA_ID, SERVICE

from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.day0.report import Entry
from expcore_onedata.day0.runner import build_context
from expcore_onedata.testing import with_placement, write_platform

LIVE = Path(__file__).resolve().parents[2] / "live" / "test_contract.py"


def entry(ok, **facts):
    return Entry("check", "A check", True, ok, "a summary", facts, 0.0)


def context(tmp_path, fake, edit):
    tmp_path.mkdir(exist_ok=True)
    path = write_platform(tmp_path, edit)
    client = OnezoneClient(load_platform(path), http=fake.client_http())
    return build_context(path, tmp_path / "out", client=client, allow_mutation=True)


def both_supporting(data):
    with_placement(data)
    data["support"]["providers"] = ["alpha", "beta"]


def test_a_check_that_was_unknown_or_failed_does_not_pass():
    """Live: `ok` must be True. An unknown (None) is a check that measured nothing; it is not a pass."""
    require_passed(entry(True))
    for ok in (None, False):
        with pytest.raises(AssertionError, match="did not pass"):
            require_passed(entry(ok))


def test_a_cleanup_result_that_is_absent_or_not_true_is_not_a_cleanup():
    """Live: the round trip must report `cleanup_verified` explicitly and true; a missing key is not a true one."""
    require_cleaned(entry(True, cleanup_verified=True))
    with pytest.raises(AssertionError, match="no cleanup result"):
        require_cleaned(entry(True))
    for value in (False, None, 1, "yes"):
        with pytest.raises(AssertionError, match="did not show"):
            require_cleaned(entry(True, cleanup_verified=value))


def test_o28_a_provider_that_was_not_shown_restored_does_not_pass_the_live_outage_test():
    """O28: the outage check's pass must carry a restore cleared and verified by the injector, by a changed identity,
    and the provider's REST answering again."""
    good = {"cleared": True, "verified": True, "how": "restored", "rest_answers_again": True}
    require_restored(entry(True, restore=good))
    with pytest.raises(AssertionError, match="no restore"):
        require_restored(entry(True))
    for bad in (
        {**good, "cleared": False},
        {**good, "verified": False},
        {**good, "verified": None},
        {**good, "how": "never_down"},
        {**good, "rest_answers_again": False},
        {key: value for key, value in good.items() if key != "rest_answers_again"},
    ):
        with pytest.raises(AssertionError, match=r"back|restore|never down|answer again"):
            require_restored(entry(True, restore=bad))


def test_o28_with_several_spaces_the_restore_and_the_cleanup_are_read_in_every_space():
    """O28: with several spaces the facts sit under `spaces`, so the live helpers read each space's: a run whose spaces
    were all restored and cleaned passes them, and one space without either fails them, never a false failure from
    reading the top level."""
    good = {"restore": {"cleared": True, "verified": True, "how": "restored", "rest_answers_again": True}}
    both = {"spaces[0]": {**good, "cleanup_verified": True}, "spaces[1]": {**good, "cleanup_verified": True}}
    require_restored(entry(True, spaces=both))
    require_cleaned(entry(True, spaces=both))
    unrestored = {**both, "spaces[1]": {"restore": {**good["restore"], "verified": False}, "cleanup_verified": True}}
    with pytest.raises(AssertionError, match="did not verify the restore"):
        require_restored(entry(True, spaces=unrestored))
    uncleaned = {**both, "spaces[1]": {**good, "cleanup_verified": False}}
    with pytest.raises(AssertionError, match="scratch entry gone"):
        require_cleaned(entry(True, spaces=uncleaned))
    with pytest.raises(AssertionError, match="no space"):
        require_restored(entry(True, spaces={}))


def with_outage(data):
    with_placement(data)
    data["providers"]["gamma"] = {"url": "https://gamma.test", "id": GAMMA_ID}
    data["support"] = {"providers": ["alpha", "beta", "gamma"], "local": "beta"}
    data["faults"] = faults_table(services={"alpha": SERVICE})


def test_o28_the_outage_check_names_what_the_platform_file_lacks(tmp_path, fake):
    """O28: no sanctioned [faults] table and a data provider that is the local one are each named; a platform with
    three providers, a sanction and a data provider apart from the local one has nothing missing."""
    fake.add_provider(GAMMA_ID, "gamma.test")
    ctx = context(tmp_path / "one", fake, both_supporting)
    ctx.allow_faults = True
    assert "sanctioned = true" in prerequisite_missing("provider_outage", ctx)
    ctx = context(tmp_path / "two", fake, with_outage)
    ctx.allow_faults = True
    assert prerequisite_missing("provider_outage", ctx) is None
    ctx.allow_faults = False
    assert "EXPCORE_LIVE_FAULTS=1" in prerequisite_missing("provider_outage", ctx)
    ctx = context(tmp_path / "three", fake, lambda data: (with_outage(data), data["support"].update(local="alpha")))
    ctx.allow_faults = True
    assert "local provider" in prerequisite_missing("provider_outage", ctx)


def test_a_read_only_check_that_ran_on_no_provider_or_left_something_behind_does_not_pass():
    """Live: a pass names the providers it ran on, each refused, and none with a scratch entry left."""
    require_every_provider_refused(entry(True, providers={"p": {"enforced": True, "status": 403}}))
    for facts in ({}, {"providers": {}}, {"providers": None}):
        with pytest.raises(AssertionError, match="no provider"):
            require_every_provider_refused(entry(True, **facts))
    with pytest.raises(AssertionError, match="not refused"):
        require_every_provider_refused(entry(True, providers={"p": {"enforced": None}}))
    with pytest.raises(AssertionError, match="left a scratch"):
        require_every_provider_refused(entry(True, providers={"p": {"enforced": True, "cleaned": False}}))


def test_the_read_only_check_needs_a_fixed_data_path_user_and_the_skip_names_it(tmp_path, fake):
    """Live: the prerequisite of read_only is named, so the live test is skipped with a reason and never passed."""
    ctx = context(tmp_path, fake, lambda data: data["principals"].pop("data_path"))
    assert "[principals.data_path]" in prerequisite_missing("read_only", ctx)


def test_the_round_trip_names_what_the_platform_file_lacks(tmp_path, fake):
    """Live: no [placement] table, no second supporting provider and no provider id are each named."""
    ctx = context(tmp_path / "one", fake, lambda data: None)
    assert "[placement]" in prerequisite_missing("round_trip", ctx)
    ctx = context(tmp_path / "two", fake, lambda data: with_placement(data))  # the support set holds alpha only
    assert "supporting provider besides" in prerequisite_missing("round_trip", ctx)
    ctx = context(tmp_path / "three", fake, both_supporting)
    assert prerequisite_missing("round_trip", ctx) is None
    assert prerequisite_missing("read_only", ctx) is None
    ctx.provider_ids = {}
    assert "id of the data provider" in prerequisite_missing("round_trip", ctx)
    with pytest.raises(ValueError, match="read_only, round_trip and provider_outage"):
        prerequisite_missing("listing", ctx)


def test_the_live_test_of_the_state_changing_checks_asks_for_a_pass_and_skips_only_by_a_named_prerequisite():
    """Live: no `is not False`, no defaulted cleanup result, and the skip text is a variable the helper names."""
    live = LIVE.read_text(encoding="utf-8")
    assert "ok is not False" not in live
    assert 'facts.get("cleanup_verified", True)' not in live
    assert "require_passed(entry)" in live
    assert "require_cleaned(entry)" in live
    assert "pytest.skip(missing)" in live
