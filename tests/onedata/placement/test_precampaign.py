"""The pre-campaign check of the content type the data gate streams on (O05, O11), run once before any run starts."""

import pytest
from placement_support import DATA, with_placement
from platform_support import SPACE_NAME, write_platform
from sandbox_support import add_sandbox

from expcore.campaign.healthgate import Health
from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.day0.registry import REGISTRY
from expcore_onedata.day0.runner import build_context
from expcore_onedata.precampaign import PreCampaignHealth, content_type_check

CHECK_FILE = "study/probe.bin"  # an invented path under the space root
CONFIGURED = ("probe.bin", "study/", "alpha", SPACE_NAME, ".test")  # what a message must never hold (O12)


def edit(data, *, rest=True):
    with_placement(data)
    add_sandbox(data)
    data["sandbox"]["content_check_file"] = CHECK_FILE
    if not rest:
        data["sandbox"]["data_paths"] = {"mounted": "mount", "isolated": "none"}
        del data["sandbox"]["content_check_file"]


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, edit)


@pytest.fixture
def seeded(fake):
    fake.add_file(CHECK_FILE, DATA)  # held by the data provider, which is the agent's local one
    return fake


def provider_reads(fake):
    return [call for call in fake.provider_calls if call[2].endswith("/content")]


class Inner:
    """The platform's own health check, counting its calls."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return Health(True, "the platform is up")


def assert_clean(detail):
    assert not [value for value in CONFIGURED if value in detail], detail


@pytest.mark.parametrize("wrong", ["application/json", "text/plain; charset=utf-8", None])
def test_o05_a_content_type_other_than_octet_stream_refuses_the_campaign_before_any_run(config, client, seeded, wrong):
    """O05, O11: when the content endpoint answers a file's content with another type, every REST read over 4 MiB would
    be refused mid-campaign and set aside, clustering in the REST condition; so the first health call refuses the
    campaign, names the problem without a configured value, and keeps refusing without asking again."""
    seeded.content_type = wrong
    inner = Inner()
    health = PreCampaignHealth(inner, lambda: content_type_check(config, client))
    first = health()
    reads = len(provider_reads(seeded))
    assert first.ok is False
    assert "application/octet-stream" in first.detail
    assert "refused" in first.detail
    assert_clean(first.detail)
    assert inner.calls == 0
    assert health().ok is False
    assert len(provider_reads(seeded)) == reads == 2  # measured once, whole and by range, before any run
    ranges = [r.range for r in seeded.requests if r.path.endswith("/content")]
    assert ranges == [None, "bytes=0-0"]


def test_o05_the_right_content_type_admits_the_campaign_and_the_platform_check_runs(config, client, seeded):
    """O05: a whole read answered 200 and a read by range answered 206, both application/octet-stream, admit the
    campaign; after that the platform's own check decides, at every call, and the content is not read again."""
    inner = Inner()
    health = PreCampaignHealth(inner, lambda: content_type_check(config, client))
    assert health() == Health(True, "the platform is up")
    assert health().ok is True
    assert inner.calls == 2
    assert len(provider_reads(seeded)) == 2
    assert content_type_check(config, client).ok is True


@pytest.mark.parametrize("fault", ["missing", "server-error", "reset", "status"])
def test_o05_a_content_type_that_cannot_be_measured_refuses_the_campaign(config, client, fake, fault):
    """O05: unknown is not a pass. A check file that is not there, a provider that fails or resets, or a whole read that
    is not answered 200, refuses the campaign, naming the problem and no configured value."""
    if fault != "missing":
        fake.add_file(CHECK_FILE, DATA)
    if fault == "server-error":
        fake.statuses.append(["/content", 1, 500])
    if fault == "reset":
        fake.resets.append(["/content", 2])
    verdict = content_type_check(config, client)
    if fault == "status":  # a server that answers a read by range with the whole file
        fake.statuses.clear()
        verdict = content_type_check(config, OnezoneClient(config, http=_whole_for_ranges(fake)))
    assert verdict.ok is False
    assert "refused" in verdict.detail
    assert_clean(verdict.detail)
    health = PreCampaignHealth(Inner(), lambda: verdict)
    assert health().ok is False


def _whole_for_ranges(fake):
    """An HTTP client to the fake that drops the Range header, so a read by range is answered 200 with the file."""
    import httpx

    def handler(request):
        if request.url.path.endswith("/content"):
            request.headers.pop("Range", None)
        return fake.respond(request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_o05_no_rest_condition_skips_the_check_and_reads_nothing(tmp_path, fake):
    """O05: with no data path over REST the gate streams nothing, so nothing is read and the campaign is admitted."""
    config = load_platform(write_platform(tmp_path, lambda d: edit(d, rest=False)))
    client = OnezoneClient(config, http=fake.client_http())
    fake.content_type = "text/html"
    verdict = content_type_check(config, client)
    assert verdict.ok is True
    assert fake.provider_calls == []
    assert PreCampaignHealth(Inner(), lambda: verdict)().ok is True


def test_o05_a_rest_data_path_without_a_check_file_is_refused_at_load(tmp_path):
    """O05: the check needs a provisioned file to read, so a platform.toml with a REST data path names one, or loading
    it fails naming the key."""

    def without(data):
        edit(data)
        del data["sandbox"]["content_check_file"]

    with pytest.raises(ConfigError, match="content_check_file"):
        load_platform(write_platform(tmp_path, without), require_secrets=False)


@pytest.mark.parametrize("bad", ["/abs/file.bin", "a/../b", "", "a//b"])
def test_o05_a_check_file_outside_the_space_is_refused_at_load(tmp_path, bad):
    """O05: the check file is a plain path under the space root."""

    def outside(data):
        edit(data)
        data["sandbox"]["content_check_file"] = bad

    with pytest.raises(ConfigError, match="content_check_file"):
        load_platform(write_platform(tmp_path, outside), require_secrets=False)


def test_o05_the_check_is_a_day0_check_too(platform_file, fake, tmp_path):
    """O05: an operator runs the same check before a campaign: failed on a wrong type, passed on the right one, and
    unknown (not a pass) when it cannot be measured."""
    assert "content_type" in REGISTRY
    assert REGISTRY["content_type"].mutates is False

    def result():
        client = OnezoneClient(load_platform(platform_file), http=fake.client_http())
        return REGISTRY["content_type"].run(build_context(platform_file, tmp_path / "out", client=client))

    assert result().ok is None
    fake.add_file(CHECK_FILE, DATA)
    assert result().ok is True
    fake.content_type = "application/json"
    found = result()
    assert found.ok is False
    assert_clean(found.summary)


@pytest.mark.parametrize("table", ["no sandbox", "no rest level"])
def test_o08_a_day0_content_type_check_that_measured_nothing_is_unknown_never_passed(tmp_path, fake, table):
    """O08, O05: with no [sandbox], or none of its levels REST, the check reads nothing. A campaign is still admitted,
    since the gate streams nothing, but the day-0 check measured nothing, so it is unknown and says so, never passed."""
    edits = {"no sandbox": with_placement, "no rest level": lambda d: edit(d, rest=False)}
    platform = write_platform(tmp_path, edits[table])
    client = OnezoneClient(load_platform(platform), http=fake.client_http())
    found = REGISTRY["content_type"].run(build_context(platform, tmp_path / "out", client=client))
    assert found.ok is None
    assert "nothing was measured" in found.summary
    assert fake.provider_calls == []
    assert content_type_check(load_platform(platform), client).ok is True


SMALL = {"tasks": ("task-a",), "seeds": (1,)}


def standard_gate(config, client):
    """The platform's standard health check, as a study gets it, with nothing composed around it."""
    from platform_support import NOW

    from expcore_onedata.health import PlatformHealth
    from expcore_onedata.tokens import TokenIssuer

    return PlatformHealth(config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW)


def run_campaign(tmp_path, health, *, settle_s):
    """Run a small campaign on the core's scheduler with `health` as its only health check, for at most `settle_s`
    after its gate first closes; return the executor and the outage records."""
    import threading
    import time

    from scheduler_support import FakeExecutor, build, settings_for

    from expcore.campaign.healthgate import META, parse

    executor = FakeExecutor()
    settings = settings_for(recheck_s=0.01, health_timeout_s=5)
    with build(tmp_path / "campaign", executor=executor, settings=settings, health=health, plan=SMALL) as scheduler:
        worker = threading.Thread(target=scheduler.run, daemon=True)
        worker.start()
        deadline = time.monotonic() + 20
        while worker.is_alive() and not scheduler.gate.closed and time.monotonic() < deadline:
            time.sleep(0.01)
        worker.join(settle_s)  # a campaign the gate admits ends by itself; one it holds is still waiting
        scheduler.stop.set()
        worker.join(20)
        assert not worker.is_alive()
        outages = parse(scheduler.store.get_meta(META)) or []
    return executor, outages


def test_o05_a_study_with_only_the_standard_gate_and_a_wrong_type_never_admits_a_run(config, client, seeded, tmp_path):
    """O05, O11: a study that uses the platform's standard health check and nothing else, with a REST data path, gets
    the content-type check without asking for it: with a wrong type the gate closes at the first pre-run check and
    stays closed through every recheck, so the executor never runs, and the type is measured once for the campaign."""
    seeded.content_type = "application/json"
    executor, outages = run_campaign(tmp_path, standard_gate(config, client), settle_s=0.5)
    assert executor.requests == []
    [outage] = outages
    assert outage["end_utc"] is None
    assert outage["checks"] >= 3  # rechecked, and refused each time
    assert "refused" in outage["first_detail"]
    assert "typed application/json" in outage["last_detail"]  # the core keeps the head of the detail
    assert_clean(outage["last_detail"])
    assert len(provider_reads(seeded)) == 2  # once, whole and by range, kept for the campaign


def test_o05_the_standard_gate_admits_the_campaign_when_the_type_is_right(config, client, seeded, tmp_path):
    """O05: the control: with the right type the standard check passes and every run of the campaign is executed."""
    executor, outages = run_campaign(tmp_path, standard_gate(config, client), settle_s=20)
    assert len(executor.requests) == 3
    assert outages == []
    assert len(provider_reads(seeded)) == 2


def test_o05_the_standard_gate_never_reads_the_check_file_without_a_rest_data_path(tmp_path, fake):
    """O05: with no REST data path the standard check reads no file content and lists no directory, at any call."""
    config = load_platform(write_platform(tmp_path, lambda d: edit(d, rest=False)))
    client = OnezoneClient(config, http=fake.client_http())
    fake.add_file(CHECK_FILE, DATA)
    fake.content_type = "text/html"
    health = standard_gate(config, client)
    assert [health().ok for _ in range(3)] == [True, True, True]
    assert [c for c in fake.provider_calls if c[2].endswith("/content") or c[2].endswith("/children")] == []
