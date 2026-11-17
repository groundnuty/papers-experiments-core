"""O90: the health check's text detail is exactly what it was before the structured reading existed.

Every expected string here is a literal captured from the check as it was on main before the reading, never one built
through `compare()`, so that a change to the text, or to which space it names, fails here. A study reads the structured
reading beside the text (`test_health_reading.py`); it never has to parse the text, and the text never changes for it.
"""

import pytest
from health_support import down, four_spaces

from expcore.campaign.healthgate import Health
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.health import PlatformHealth
from expcore_onedata.testing import ALPHA_ID, BETA_ID, NOW, write_platform
from expcore_onedata.tls import WARNING
from expcore_onedata.tokens import TokenIssuer

PASSED = "ok: each of the 4 spaces is supported by its declared set"
SPACE_ONE = "spaces[1]: declared provider(s) reported offline: providers[1]"
SPACE_ZERO = "spaces[0]: declared provider(s) reported offline: providers[2]"
NOT_DESCRIBED = "health check failed: zone: the scope answer does not describe the study space"
MISSING = "spaces[1]: the providers supporting the space differ from the declared set: missing providers[1]"
SECOND_ID, THIRD_ID = "sp-second-0002", "sp-third-0003"  # the second and third space of the four-space file


def test_o90_four_spaces_all_online_pass_with_the_count_text(tmp_path, fake):
    """O90: the pass names how many spaces were checked."""
    world = four_spaces(tmp_path, fake)
    assert world.health.space() == Health(True, PASSED)
    assert world.health() == Health(True, PASSED)


def test_o90_only_the_first_failing_space_is_named_by_its_position(tmp_path, fake):
    """O90: the target is down in the second and fourth spaces; the text names the second alone."""
    world = four_spaces(tmp_path, fake)
    down(fake, "beta")
    assert world.health.space() == Health(False, SPACE_ONE)
    assert world.health() == Health(False, SPACE_ONE)


def test_o90_a_second_provider_down_in_an_earlier_space_is_the_one_named(tmp_path, fake):
    """O90: with the target and another provider both down, the first failing space in order is the first, and the text
    says nothing of the other spaces."""
    world = four_spaces(tmp_path, fake)
    down(fake, "beta", "gamma")
    assert world.health.space() == Health(False, SPACE_ZERO)


def test_o90_a_later_space_that_cannot_be_read_never_changes_an_earlier_failures_text(tmp_path, fake):
    """O90: the third space is not described by the answer, but the second fails first: its text is what it was."""
    world = four_spaces(tmp_path, fake)
    down(fake, "beta")
    del fake.space_supports[THIRD_ID]
    assert world.health.space() == Health(False, SPACE_ONE)


def test_o90_a_space_that_cannot_be_read_alone_fails_with_the_check_failed_text(tmp_path, fake):
    """O90: a scope answer that does not describe a space is a failed check, whose text names no position."""
    world = four_spaces(tmp_path, fake)
    del fake.space_supports[THIRD_ID]
    assert world.health.space() == Health(False, NOT_DESCRIBED)


def test_o90_a_missing_provider_in_the_second_space_is_named_with_its_position(tmp_path, fake):
    """O90: a membership difference keeps its text."""
    world = four_spaces(tmp_path, fake)
    del fake.space_supports[SECOND_ID][BETA_ID]
    assert world.health.space() == Health(False, MISSING)


def test_o90_one_space_form_keeps_its_text_without_a_position(tmp_path, fake):
    """O90: the one-space form names no position."""
    config = load_platform(write_platform(tmp_path))
    client = OnezoneClient(config, http=fake.client_http())
    health = PlatformHealth(config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW)
    fake.providers[ALPHA_ID]["online"] = False
    expected = Health(False, "declared provider(s) reported offline: providers[0]")
    assert health.space() == expected
    assert health() == expected
    fake.providers[ALPHA_ID]["online"] = True
    assert health.space() == Health(True, "ok: the space is supported by exactly the 1 declared provider(s)")


@pytest.mark.parametrize("shown", ["space", "call"])
def test_o90_the_insecure_tls_warning_follows_the_unchanged_text(tmp_path, fake, shown):
    """O90: with `insecure_tls` the warning follows the text, which is the text without it."""
    world = four_spaces(tmp_path, fake, edit=lambda data: data.__setitem__("tls", {"insecure_tls": True}))
    down(fake, "beta")
    reading = world.health.space() if shown == "space" else world.health()
    assert reading == Health(False, f"{SPACE_ONE}; {WARNING}")


def test_o90_a_platform_with_faults_keeps_the_text_too(tmp_path, fake):
    """O90: the fault gate around the check changes nothing of the space's text."""
    world = four_spaces(tmp_path, fake, faults={})
    down(fake, "beta")
    assert world.health.space() == Health(False, SPACE_ONE)
    assert world.health() == Health(False, SPACE_ONE)
