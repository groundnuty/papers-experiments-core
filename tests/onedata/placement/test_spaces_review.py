"""Each space keeps to itself: its own data provider, required and supporting it; mutations bound to it alone; its
eviction plan checked against its support set; the health token only where the data path reaches; and the
`[[spaces]]` form in the fingerprint whatever its length (O04, O09, C06)."""

import base64

import pytest
from campaign_support import make_fingerprint, make_manifest, make_schema
from fake_reset import FakeReset

from expcore.campaign.resume import FingerprintMismatch, open_campaign
from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient, OutOfScope
from expcore_onedata.config import load_platform
from expcore_onedata.eviction import evict_ids
from expcore_onedata.health import PlatformHealth
from expcore_onedata.provision import Provisioner, ProvisionError, Refused, resolve_provider_ids
from expcore_onedata.rangemap import load_placement
from expcore_onedata.reset import SpaceReset
from expcore_onedata.testing import (
    ADMIN_ENV,
    ALPHA_ID,
    BASELINE,
    BETA_ID,
    DATA,
    NOW,
    SERVICE_USER,
    SPACE_NAME,
    USERS,
    with_placement,
    write_platform,
)
from expcore_onedata.testing.world import (
    PROBE_SPACE_ID,
    PROBE_SPACE_NAME,
    SPACE_IDS,
    SPACE_LABELS,
    SPACE_NAMES,
    with_link_check,
    with_spaces,
)
from expcore_onedata.tokens import TokenIssuer


@pytest.fixture
def fake(monkeypatch, ticker) -> FakeReset:
    """The study space and a second space, both supported by alpha and beta, and the users in both."""
    federation = FakeReset(space_name=SPACE_NAME)
    federation.clock = ticker.clock
    federation.add_provider(BETA_ID, "beta.test")
    federation.add_data_space(SPACE_IDS[1], SPACE_NAMES[1], [ALPHA_ID, BETA_ID])
    monkeypatch.setenv(ADMIN_ENV, federation.admin_token)
    for login, (password, user_id) in USERS.items():
        federation.add_user(login, password, user_id)
        for space in SPACE_IDS[:2]:
            federation.grant(user_id, BASELINE, space=space)
    return federation


def written(tmp_path, edit):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return write_platform(tmp_path, edit)


def bound(tmp_path, fake, ticker, edit, label):
    """A provisioner of the space a label names, over a configuration the edit writes."""
    path = written(tmp_path, edit)
    config = load_platform(path)
    client = OnezoneClient(config, http=fake.client_http())
    placement = load_placement(path, config)
    return Provisioner(
        client,
        config,
        placement.settings,
        provider_ids=resolve_provider_ids(config),
        log=tmp_path / "changes.jsonl",
        space=label,
        clock=ticker.clock,
        sleep=ticker.sleep,
    )


def two_spaces(data):
    with_placement(data)
    with_spaces(data, 2)
    data["spaces"][1]["holder"] = "beta"


def test_o09_a_spaces_entry_without_its_data_provider_is_refused_at_load(tmp_path, fake):
    """O09: a default from [placement] could make another owner's copy evictable, so an entry must name its own."""
    with pytest.raises(ConfigError, match=r"spaces\[1\]: key 'holder' is required"):
        load_platform(written(tmp_path, lambda d: (two_spaces(d), d["spaces"][1].pop("holder"))))


def test_o09_the_one_space_forms_data_provider_must_support_the_space_too(tmp_path, fake, ticker):
    """O09: in the one-space form `[placement] holder` is the data provider; one the support set leaves out is
    refused when the provisioner is built, before anything is read."""

    def edit(data):
        with_placement(data, holder="beta")
        data["support"]["providers"] = ["alpha"]

    with pytest.raises(ProvisionError, match="does not support the space"):
        bound(tmp_path, fake, ticker, edit, None)
    assert fake.requests == []


def test_o09_a_provisioner_bound_to_one_space_never_mutates_another_under_a_shared_prefix(tmp_path, fake, ticker):
    """O09: both spaces' names start with the same prefix; the second space's provisioner asked to evict a file of
    the first is refused by the gate, bound to its own space, and no transfer is posted."""
    owned = bound(tmp_path, fake, ticker, two_spaces, SPACE_LABELS[1])
    foreign = fake.add_file("study/theirs.bin", DATA)
    fake.blocks[(foreign, ALPHA_ID)] = [[0, len(DATA)]]
    with pytest.raises(OutOfScope, match="another space than the one the request is bound to"):
        evict_ids(owned, [foreign], "alpha")
    assert [c for c in fake.provider_calls if c[2] == "/transfers"] == []


def test_o25_a_provisioner_bound_to_one_space_never_reads_another_spaces_file_by_range(tmp_path, fake, ticker):
    """O25 with O09: a range read through a provider can make it fetch bytes, so `read_file` is bound to the
    provisioner's space as its writes are. The second space's provisioner asked to read a file of the first, under the
    same prefix, is refused by the gate before any range is sent."""
    owned = bound(tmp_path, fake, ticker, two_spaces, SPACE_LABELS[1])
    foreign = fake.add_file("study/theirs.bin", DATA)
    with pytest.raises(OutOfScope, match="another space than the one the request is bound to"):
        owned.read_file(foreign)
    assert [r for r in fake.requests if r.range] == []


def test_o09_the_gate_asks_a_provider_only_for_the_roots_of_the_spaces_it_supports(tmp_path, fake, ticker):
    """O09: the gate walks a file's parent links to the root of a declared space, and asks a provider only for the
    roots of the spaces whose support set names it: how a provider answers for a space it does not support is
    unmeasured. Here beta supports only the second space and answers the first's root with a 403, which the gate never
    reads, so the second space's provisioner writes through beta."""

    def edit(data):
        two_spaces(data)
        data["spaces"][0]["providers"] = ["alpha"]

    owned = bound(tmp_path, fake, ticker, edit, SPACE_LABELS[1])
    fake.statuses = [[f"/spaces/{SPACE_IDS[0]}", 10**6, 403]]
    owned.put_file("study/mine.bin", DATA)
    asked = {c[2] for c in fake.provider_calls if c[0] == BETA_ID and c[1] == "GET"}
    assert f"/spaces/{SPACE_IDS[1]}" in asked
    assert f"/spaces/{SPACE_IDS[0]}" not in asked


def test_o09_a_reset_plan_naming_a_provider_that_does_not_support_its_space_is_refused_before_any_stage(
    tmp_path, fake, ticker
):
    """O09: the second space is supported by alpha alone; a plan that evicts from beta there is refused before the
    first stage, and nothing is read or sent."""

    def edit(data):
        two_spaces(data)
        data["spaces"][1].update(providers=["alpha"], holder="alpha")

    owned = bound(tmp_path, fake, ticker, edit, SPACE_LABELS[1])
    with pytest.raises(Refused, match="does not support the space"):
        SpaceReset(owned, now=ticker.clock).run({"beta": ["study"]})
    assert fake.requests == []


def test_o04_the_health_token_reaches_only_the_spaces_the_data_path_reaches(tmp_path, fake, ticker):
    """O04: the second space is the tool server's alone, and the service user has no rights in it; the health check's
    token is scoped to the data path's space, so it is minted and the check passes on that space. The second space is
    beta's too, so the link check (O35) probes alpha <- beta in a probe space of its own."""

    def edit(data):
        two_spaces(data)
        data["spaces"][0]["providers"] = ["alpha"]
        data["spaces"][1]["tokens"] = ["tool_server"]
        with_link_check(data)

    fake.supports.pop(BETA_ID)
    del fake.privileges[(SPACE_IDS[1], SERVICE_USER.user_id)]
    fake.add_data_space(PROBE_SPACE_ID, PROBE_SPACE_NAME, [ALPHA_ID, BETA_ID])
    config = load_platform(written(tmp_path, edit))
    client = OnezoneClient(config, http=fake.client_http())
    issuer = TokenIssuer(client, config, clock=ticker.clock)
    health = PlatformHealth(config, client, issuer, clock=ticker.clock, monotonic=ticker.clock, sleep=ticker.sleep)
    verdict = health()
    assert verdict.ok, verdict.detail
    caveats = next(iter(fake.issued.values()))["caveats"]
    paths = next(c["whitelist"] for c in caveats if c["type"] == "data.path")
    assert [base64.b64decode(p).decode() for p in paths] == [f"/{SPACE_IDS[0]}"]


def test_c06_the_spaces_table_enters_the_fingerprint_with_one_entry_too(tmp_path, fake):
    """C06: a `[[spaces]]` table of one entry is fingerprinted, so a resume after its data provider or its support
    set changed is refused; the one-space form's fingerprint stays the TLS setting alone."""

    def one(holder="alpha", providers=("alpha", "beta")):
        def edit(data):
            with_spaces(data, 1)
            data["spaces"][0].update(providers=list(providers), holder=holder)

        return edit

    first = load_platform(written(tmp_path / "a", one()))
    assert set(first.fingerprint()) == {"tls", "spaces"}
    assert set(load_platform(written(tmp_path / "e", None)).fingerprint()) == {"tls"}
    schema, manifest = make_schema(), make_manifest(make_schema())

    def resume(config):
        fingerprint = make_fingerprint(schema, platform=config.fingerprint())
        store, _ = open_campaign(tmp_path / "campaign.db", schema, manifest, fingerprint, now=NOW, max_attempts=3)
        store.close()

    resume(first)
    for name, edit in (("b", one(holder="beta")), ("c", one(providers=("alpha",)))):
        with pytest.raises(FingerprintMismatch):
            resume(load_platform(written(tmp_path / name, edit)))
    resume(first)
