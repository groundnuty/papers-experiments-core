"""O87: `[sandbox] content_check_provider` names the provider the pre-campaign content check reads through, so a study
whose states want every file empty at the local provider before a run never makes the check fetch and keep the file's
blocks there. Unset, the check reads through the agent's local provider, as it always did."""

import pytest
from sandbox_support import add_sandbox

from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient
from expcore_onedata.config import load_platform
from expcore_onedata.day0.registry import REGISTRY
from expcore_onedata.day0.runner import build_context
from expcore_onedata.precampaign import measure_content_type
from expcore_onedata.testing import ALPHA_ID, BETA_ID, DATA, with_placement, write_platform
from expcore_onedata.testing.world import with_spaces

CHECK_FILE = "study/probe.bin"  # an invented path under the space root
UNKNOWN = "quince"  # a provider name no platform here declares


def document(data, *, provider=None, supported=True, rest=True):
    """A platform with a REST data path, alpha local; beta supports the space too unless `supported` is False."""
    with_placement(data)
    add_sandbox(data)
    data["support"] = {"providers": ["alpha", "beta"] if supported else ["alpha"], "local": "alpha"}
    if rest:
        data["sandbox"]["content_check_file"] = CHECK_FILE
    else:
        data["sandbox"]["data_paths"] = {"mounted": "mount", "isolated": "none"}
        data["sandbox"].pop("content_check_file", None)
    if provider is not None:
        data["sandbox"]["content_check_provider"] = provider


@pytest.fixture
def fake(fake):
    """The conftest's federation, with the second provider supporting the space too."""
    fake.supports[BETA_ID] = {"readonly": False}
    return fake


@pytest.fixture
def held_by_beta(fake):
    """The check file, held whole by beta alone: alpha, the local provider, holds none of its blocks."""
    file_id = fake.add_file(CHECK_FILE, DATA, holder=BETA_ID)
    assert fake.blocks.get((file_id, ALPHA_ID)) is None
    return file_id


def measure(tmp_path, fake, **options):
    config = load_platform(write_platform(tmp_path, lambda data: document(data, **options)))
    return measure_content_type(config, OnezoneClient(config, http=fake.client_http()))


def providers_asked(fake):
    return {call[0] for call in fake.provider_calls}


def test_o87_with_the_key_every_request_of_the_check_goes_to_the_named_provider_and_the_local_one_keeps_its_blocks(
    tmp_path, fake, held_by_beta
):
    """O87: the root, the listing, the parent walk and both content reads go through the named provider, and alpha,
    which a read of a file it lacks would fill, keeps the block map it had: none."""
    verdict = measure(tmp_path, fake, provider="beta")
    assert verdict.ok is True, verdict.detail
    assert providers_asked(fake) == {BETA_ID}
    assert fake.blocks.get((held_by_beta, ALPHA_ID)) is None
    assert fake.blocks[(held_by_beta, BETA_ID)] == [[0, len(DATA)]]
    reads = [call for call in fake.provider_calls if call[2].endswith("/content")]
    assert len(reads) == 2  # whole, then its first byte


def test_o87_without_the_key_the_check_reads_through_the_local_provider_which_then_keeps_blocks(
    tmp_path, fake, held_by_beta
):
    """O87: today's behaviour, pinned: with no key every request goes to the local provider, and a read of a file it
    does not hold makes it fetch the bytes it was asked for and keep them, which the key exists to avoid."""
    verdict = measure(tmp_path, fake)
    assert verdict.ok is True, verdict.detail
    assert providers_asked(fake) == {ALPHA_ID}
    assert fake.blocks.get((held_by_beta, ALPHA_ID)), "the fake's local provider must fetch what it was read for"


def test_o87_the_key_naming_the_local_provider_reads_as_no_key_does(tmp_path, fake, held_by_beta):
    """O87: the local provider named is the default spelt out: the same requests, the same replica."""
    verdict = measure(tmp_path, fake, provider="alpha")
    assert verdict.ok is True, verdict.detail
    assert providers_asked(fake) == {ALPHA_ID}


@pytest.mark.parametrize("provider", ["alpha", "beta", None])
def test_o87_the_detail_names_the_provider_it_read_by_position_never_by_name(tmp_path, fake, held_by_beta, provider):
    """O87: the check's record names the provider it read as `providers[i]`, its position in the file, as it always
    did for the local one, and no provider's own name."""
    verdict = measure(tmp_path, fake, provider=provider)
    position = 1 if provider == "beta" else 0
    assert f"providers[{position}]" in verdict.detail
    assert not [name for name in ("alpha", "beta") if name in verdict.detail]


def test_o87_a_failed_read_through_the_named_provider_names_that_provider(tmp_path, fake):
    """O87: a file the named provider does not show is unknown there, and the refusal says which provider was asked."""
    fake.add_file(CHECK_FILE, DATA)  # held by alpha, shown by beta only after a visibility delay
    fake.visibility_delay_s = 1e9
    verdict = measure(tmp_path, fake, provider="beta")
    assert verdict.ok is False
    assert "providers[1]" in verdict.detail
    assert providers_asked(fake) == {BETA_ID}


@pytest.mark.parametrize(
    ("case", "options", "token"),
    [
        ("undeclared", {"provider": UNKNOWN}, UNKNOWN),
        ("not a name", {"provider": 5}, None),
        ("an empty name", {"provider": ""}, None),
        ("not supporting the space", {"provider": "beta", "supported": False}, "beta"),
        ("no check file", {"provider": "beta", "rest": False}, "beta"),
    ],
)
def test_o87_a_provider_the_check_cannot_read_through_is_refused_at_load_naming_the_key_alone(
    tmp_path, fake, case, options, token
):
    """O87: the provider must be one of the platform's `[providers]` and support the space the check file lies in, and
    the key is read with `content_check_file` only; the error names the key and never the value."""
    with pytest.raises(ConfigError, match="content_check_provider") as raised:
        load_platform(write_platform(tmp_path, lambda data: document(data, **options)))
    if token is not None:
        assert token not in str(raised.value).replace(str(tmp_path), "")


def test_o87_the_day0_content_type_check_reads_through_the_named_provider(tmp_path, fake, held_by_beta):
    """O87: the day-0 check `content_type` is the same measurement, so it reads through the key's provider and says
    which."""
    platform = write_platform(tmp_path, lambda data: document(data, provider="beta"))
    client = OnezoneClient(load_platform(platform), http=fake.client_http())
    found = REGISTRY["content_type"].run(build_context(platform, tmp_path / "out", client=client))
    assert found.ok is True, found.summary
    assert "providers[1]" in found.summary
    assert providers_asked(fake) == {BETA_ID}
    assert fake.blocks.get((held_by_beta, ALPHA_ID)) is None


def spaces(data, *, first_has_beta: bool, first_tokens: list[str]):
    """Two spaces, alpha local in both: the second is supported by alpha and beta, the first by beta as well or not,
    and its token planes as given (without `data_path` the data path's first space is the second)."""
    with_placement(data)
    add_sandbox(data)
    with_spaces(data, 2)
    data["sandbox"]["content_check_file"] = CHECK_FILE
    data["sandbox"]["content_check_provider"] = "beta"
    data["spaces"][0]["providers"] = ["alpha", "beta"] if first_has_beta else ["alpha"]
    data["spaces"][0]["tokens"] = first_tokens


@pytest.mark.parametrize(
    ("first_has_beta", "first_tokens", "refused"),
    [
        (False, ["data_path", "tool_server"], True),  # the data path's first space is the first, beta lacks it
        (False, ["tool_server"], False),  # the data path's first space is the second, which beta supports
        (True, ["data_path", "tool_server"], False),
    ],
    ids=["first-space-without-beta", "data-path-starts-at-the-second", "both-support-beta"],
)
def test_o87_the_provider_must_support_the_first_space_the_data_path_reaches_and_no_other(
    tmp_path, fake, first_has_beta, first_tokens, refused
):
    """O87: with several spaces the check file lies in the first space the data path's token reaches (O05), so the
    named provider is checked against that space's support set alone, never against the first entry's by position."""
    platform = write_platform(tmp_path, lambda d: spaces(d, first_has_beta=first_has_beta, first_tokens=first_tokens))
    if refused:
        with pytest.raises(ConfigError, match="content_check_provider"):
            load_platform(platform)
    else:
        assert load_platform(platform).content_check_reader.name == "beta"
