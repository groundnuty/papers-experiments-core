"""The link check (O35, #221) against a real deployment, for the next live slot.

    EXPCORE_LIVE_MUTATE=1 EXPCORE_LIVE_PLATFORM=<root>/platform.toml uv run pytest tests/live/test_link_check.py

It writes one probe per source into the `[link_check]` probe space of platform.toml, a space no agent can see, keeps it
there, and evicts the local provider's fetched copy afterwards (`EXPCORE_LIVE_MUTATE`); nothing of a study space is
read or changed. `EXPCORE_LIVE_FACTS_OUT` names a file each check's record is written to, as JSON: pairs by position,
seconds, bytes and transfer ids, never a name or a value of the federation. Without the variables nothing is built or
contacted. It shows what the fake can only assume: that a probe written through a source is fetched through the local
provider within the bound, that the eviction afterwards and the cold check before the next fetch hold on 25.0, and how
long each step takes there.
"""

import json
import os
from pathlib import Path

import pytest

PLATFORM = os.environ.get("EXPCORE_LIVE_PLATFORM")
if not PLATFORM or os.environ.get("EXPCORE_LIVE_MUTATE") != "1":
    pytest.skip(
        "EXPCORE_LIVE_PLATFORM and EXPCORE_LIVE_MUTATE=1 are not both set; this test writes probes into a live "
        "deployment's probe space",
        allow_module_level=True,
    )

from expcore_onedata.client import OnezoneClient  # noqa: E402 -- only once the module is known to run
from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.linkcheck import check_links  # noqa: E402

FACTS_OUT = os.environ.get("EXPCORE_LIVE_FACTS_OUT")


@pytest.fixture(scope="module")
def platform():
    config = load_platform(PLATFORM)
    if config.link_check is None:
        pytest.skip("platform.toml declares no [link_check]: there is no probe space to check the links with")
    client = OnezoneClient(config)
    yield config, client
    client.close()


def test_o35_every_pair_fetches_its_probe_within_the_bound_and_is_cold_for_the_next_check(platform):
    """O35: Every pair fetches its probe within the bound and is cold for the next check."""
    config, client = platform
    first = check_links(config, client)
    second = check_links(config, client)  # the probes kept, made cold, fetched again
    if FACTS_OUT:
        Path(FACTS_OUT).write_text(json.dumps([first.to_json(), second.to_json()], indent=2) + "\n", encoding="utf-8")
    for report in (first, second):
        assert report.ok, report.detail
        assert report.pairs, "a federation with a [link_check] declares at least one pair here"
        for pair in report.pairs:
            assert pair.seconds is not None
            assert pair.seconds <= config.link_check.timeout_s
            assert pair.bytes == config.link_check.probe_bytes
    assert not any(pair.provisioned for pair in second.pairs)  # written once, kept
