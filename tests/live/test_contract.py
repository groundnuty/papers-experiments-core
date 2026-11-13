"""Contract tests against a real deployment, skipped unless EXPCORE_LIVE_PLATFORM names a platform.toml.

    EXPCORE_LIVE_PLATFORM=/path/to/platform.toml uv run pytest tests/live
    EXPCORE_LIVE_SLOW=1 EXPCORE_LIVE_PLATFORM=... uv run pytest tests/live   # also the test that waits out a lifetime
    EXPCORE_LIVE_FAULTS=1 EXPCORE_LIVE_PLATFORM=... uv run pytest tests/live  # also the test that takes a provider down
    EXPCORE_LIVE_REVOKE=1 EXPCORE_LIVE_PLATFORM=... uv run pytest tests/live  # also the test that revokes tokens

They check what the fake can only assume: that the endpoints the layer calls answer in the shape it parses. They read
(health, discovery, the examination of a token, and, with EXPCORE_LIVE_CONTENT_PATH naming a provisioned file by its
path in the space, that file's content) and mint a short-lived token per plane for the configured fixed users.
Each one ends with its lifetime; only with EXPCORE_LIVE_REVOKE=1 does one test revoke every temporary token of the
`[principals.service]` user, as a run's teardown revokes its slot users'. Nothing here changes a space, a file or a
privilege, and without the variable nothing is constructed or contacted. Only with EXPCORE_LIVE_FAULTS=1 as well does
the fault test take a provider declared in `[faults]` down and bring it back.
"""

import os
import time

import pytest

PLATFORM = os.environ.get("EXPCORE_LIVE_PLATFORM")
if not PLATFORM:
    pytest.skip("EXPCORE_LIVE_PLATFORM is not set; these tests contact a real deployment", allow_module_level=True)

import httpx  # noqa: E402
from live_support import (  # noqa: E402 -- only once the module is known to run
    mint_for_condition,
    prerequisite_missing,
    require_cleaned,
    require_every_provider_refused,
    require_measured,
    require_passed,
    require_restored,
)

from expcore_onedata import consistency  # noqa: E402
from expcore_onedata.client import (  # noqa: E402
    API_PROVIDER,
    AUTH_HEADER,
    NotSent,
    OnezoneClient,
    PlatformError,
    _error_fields,
    answered_absent,
)
from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.federation import pin_local_provider, read_space  # noqa: E402
from expcore_onedata.health import PlatformHealth  # noqa: E402
from expcore_onedata.sandbox import gateboundary  # noqa: E402
from expcore_onedata.tokencheck import CaveatSpec  # noqa: E402
from expcore_onedata.tokens import RevocationUnconfirmed, TokenIssuer, data_path_spec  # noqa: E402
from expcore_onedata.tokenscope import admin_privileges, effective_clusters  # noqa: E402

CEILING_S = 300.0
CONTENT_PATH = os.environ.get("EXPCORE_LIVE_CONTENT_PATH")  # a provisioned, non-empty file: its path in the space
EXPIRY_S = 5  # the shortest lifetime the expiry test asks for
SLOW = pytest.mark.skipif(
    os.environ.get("EXPCORE_LIVE_SLOW") != "1",
    reason="EXPCORE_LIVE_SLOW=1 is not set; this test waits out a token's lifetime",
)


def declared_conditions() -> int:
    """How many conditions platform.toml declares, read without its secrets: the tests are one per condition."""
    toolserver = load_platform(PLATFORM, require_secrets=False).toolserver
    return len(toolserver.conditions) if toolserver else 0


CONDITIONS = declared_conditions()


@pytest.fixture(scope="module")
def config():
    return load_platform(PLATFORM)


@pytest.fixture(scope="module")
def client(config):
    client = OnezoneClient(config)
    yield client
    client.close()


@pytest.fixture(scope="module")
def issuer(client, config):
    return TokenIssuer(client, config, clock=time.time)


def service_user(config):
    if config.principals.service is None:
        pytest.skip("platform.toml declares no [principals.service] user")
    return config.principals.service


def test_a_user_mints_its_own_token_and_it_examines_cleanly(config, issuer):
    # covers: POST /tokens/examine
    user = service_user(config)
    token = issuer.mint(data_path_spec(config.space.id), user, ceiling_s=CEILING_S)  # minted and examined inside mint
    assert token.subject == user.user_id  # the answer's subject names the user the token was minted for
    assert token.expires_at > time.time()


def test_the_study_space_resolves_to_its_declared_name(config, client):
    # covers: GET /spaces/{space}
    assert client.space_name(config.space.id) == config.space.name  # the name the mutation gate checks the prefixes of


def nested_file(client, provider, root):
    """The first regular file two levels below the space root, found by listing: its id, its names from the root and
    the ids of the directories above it. Skips when the space holds none."""
    params = {"limit": 100, "attribute": ["name", "file_id", "type"]}
    for entry in client.get(f"/data/{root}/children", provider=provider, params=params)["children"]:
        if entry["type"] != "DIR":
            continue
        inner = client.get(f"/data/{entry['file_id']}/children", provider=provider, params=params)["children"]
        found = next((child for child in inner if child["type"] == "REG"), None)
        if found is not None:
            return found["file_id"], [entry["name"], found["name"]], [root, entry["file_id"]]
    pytest.skip("the space holds no regular file two levels below its root")


def test_a_nested_file_resolves_to_its_space_by_its_parent_links(config, client):
    """O09: Onedata 25.0 has no path attribute, so the gate walks a file's `parent_id` links up to the root of the
    configured space: the root's is null, each other's names the directory above it, the root is the same file on
    every supporting provider, and a file two levels down resolves to the configured space and its name on each."""
    # covers: GET /data/{id}
    roots = {p: client.get(f"/spaces/{config.space.id}", provider=p)["fileId"] for p in config.support.providers}
    assert len(set(roots.values())) == 1
    provider = config.support.providers[0]
    root = roots[provider]
    file_id, _, above = nested_file(client, provider, root)
    link = {"attribute": "parent_id"}
    assert client.get(f"/data/{root}", provider=provider, params=link) == {"parent_id": None}
    assert client.get(f"/data/{file_id}", provider=provider, params=link) == {"parent_id": above[-1]}
    assert client.get(f"/data/{above[-1]}", provider=provider, params=link) == {"parent_id": root}
    for each in config.support.providers:
        fresh = OnezoneClient(config)  # nothing kept from another provider's walk
        try:
            assert fresh.file_space_id(file_id, each) == config.space.id
            assert fresh.file_space_name(file_id, each) == config.space.name  # the name a mutation is gated on
        finally:
            fresh.close()


def test_the_pre_campaign_content_check_measures_a_nested_file_and_passes(config, tmp_path):
    """O05, O09: the check a campaign with a REST data path runs before its first run, on a file two levels down, read
    through the gate's walk on a client that has walked nothing yet: it measures a type, and it is the one the data
    gate streams on. The federation's platform.toml has no [sandbox], so the test adds one with a REST level."""
    import dataclasses

    from sandbox_support import sandbox_data

    from expcore.config.loading import Section
    from expcore_onedata.precampaign import measure_content_type
    from expcore_onedata.sandbox.settings import load_sandbox

    fresh = OnezoneClient(config)
    try:
        local = config.support.local
        root = fresh.get(f"/spaces/{config.space.id}", provider=local)["fileId"]
        _, names, _ = nested_file(fresh, local, root)
        table = {**sandbox_data(), "data_paths": {"via-rest": "rest"}, "content_check_file": "/".join(names)}
        sandbox = load_sandbox(Section(table, tmp_path / "platform.toml", "[sandbox]"), tmp_path, tls=config.tls)
        verdict = measure_content_type(dataclasses.replace(config, sandbox=sandbox), fresh)
    finally:
        fresh.close()
    assert verdict.measured, verdict.detail
    assert verdict.ok, verdict.detail


def test_discovery_finds_the_space_and_the_declared_local_provider(config, issuer, client):
    # covers: POST /tokens/infer_access_token_scope
    user = service_user(config)
    token = issuer.mint(data_path_spec(config.space.id), user, ceiling_s=CEILING_S)
    view = read_space(client, token.reveal(), config.space.id)  # refuses a provider without name, domain or online
    pinned = pin_local_provider(view, config)
    assert view.name == config.space.name
    assert pinned.provider.name == config.support.local
    for provider in view.providers:
        assert isinstance(provider.name, str)
        assert isinstance(provider.online, bool)


def test_the_health_check_reads_the_declared_support_set(config, client, issuer):
    service_user(config)
    result = PlatformHealth(config, client, issuer)()
    assert result.ok, result.detail


def lookup_absent(answer: httpx.Response) -> bool:
    """Whether a provider's raw answer says the entry is not there, read as the client reads absence
    (`answered_absent`): a 404, or Onedata 25.0's 400 posix enoent. Any other answer is not absence."""
    if answer.status_code < 400:
        return False
    code, errno = _error_fields(answer)
    return answered_absent(PlatformError("the lookup", status=answer.status_code, code=code, errno=errno))


def test_a_files_content_whole_and_by_range_comes_with_the_type_the_data_gate_streams(config, issuer):
    """O05: the data gate streams only a 200 or 206 of a file's content with the data content type, and buffers every
    other answer up to 4 MiB (gateboundary.streams). Read a provisioned file whole and by range through the provider's
    real content endpoint, with a data-path token, and check that it answers so."""
    # covers: GET /data/{file}/content
    if not CONTENT_PATH:
        pytest.skip(
            "EXPCORE_LIVE_CONTENT_PATH is not set; it names a provisioned, non-empty file by its path in the space"
        )
    token = issuer.mint(data_path_spec(config.space.id), service_user(config), ceiling_s=CEILING_S).reveal()
    base, headers = config.local_provider.url + API_PROVIDER, {AUTH_HEADER: token}
    # The platform's one [tls], as every client of the federation takes it: a federation's own CA, or the defaults.
    with httpx.Client(timeout=120, verify=config.tls.client_verify()) as http:
        found = http.post(f"{base}/lookup-file-id/{config.space.name}/{CONTENT_PATH.strip('/')}", headers=headers)
        if lookup_absent(found):  # unknown, never a pass or a failure of the platform
            pytest.skip(
                "EXPCORE_LIVE_CONTENT_PATH names a file the space does not hold: a space declared baseline = false "
                "holds none, so set it only where the file was provisioned"
            )
        assert found.status_code == 200
        content = f"{base}/data/{found.json()['fileId']}/content"
        whole = http.get(content, headers=headers)
        part = http.get(content, headers={**headers, "Range": "bytes=0-0"})
    assert (whole.status_code, part.status_code) == (200, 206)
    for answer in (whole, part):
        assert gateboundary.streams(answer.status_code, "content", answer.headers.get("content-type"))


@pytest.mark.parametrize("index", range(CONDITIONS), ids=lambda index: f"condition{index}")
def test_each_condition_mints_and_examines_its_tool_server_token(index, config, issuer):
    """O05: each declared condition is its own test, skipped only when the platform reports that the tool-server user
    holds other rights than the condition declares. Any other refusal fails it, and one skip does not skip the rest."""
    if config.toolserver is None or config.principals.toolserver is None:
        pytest.skip("platform.toml declares no tool server or no [principals.toolserver] user")
    name = list(config.toolserver.conditions)[index]
    token = mint_for_condition(issuer, config, name, config.principals.toolserver, CEILING_S)
    assert token.subject == config.principals.toolserver.user_id


def test_the_tool_server_user_holds_no_onezone_admin_privilege(config, client):
    """O09: a writing condition's token carries no data-access caveat, so its user's Onezone admin privileges are live
    on it. The admin credential reads them in the shape the layer parses (`tokenscope.admin_privileges`), and the
    tool-server user holds none."""
    # covers: GET /users/{id}/effective_privileges
    if config.principals.toolserver is None:
        pytest.skip("platform.toml declares no [principals.toolserver] user")
    assert admin_privileges(client, config.principals.toolserver.user_id) == []


def test_a_writing_tool_server_token_acts_in_no_cluster(config, client, issuer):
    """O09: a member of a provider's cluster can act through that provider whatever its spaces, so a writing token's
    user must be a member of none. Onezone's read of another user's clusters does not exist, so the call is the current
    user's, sent as the token itself: it answers 200 with a list of cluster ids (`tokenscope.effective_clusters`), and
    the tool-server user's is empty. The token has no data-access caveat, as a data-access one would refuse the read."""
    # covers: GET /user/effective_clusters
    if config.toolserver is None or config.principals.toolserver is None:
        pytest.skip("platform.toml declares no tool server or no [principals.toolserver] user")
    writing = next((n for n, c in config.toolserver.conditions.items() if c.writes), None)
    if writing is None:
        pytest.skip("platform.toml declares no condition with a write right")
    token = mint_for_condition(issuer, config, writing, config.principals.toolserver, CEILING_S)
    assert effective_clusters(client, token.reveal()) == []


@SLOW
def test_a_token_stops_working_when_its_lifetime_ends(config, client):
    """A token not revoked expires. Mint with a minimal lifetime, wait it out, expect a refusal: 401 or 403, or
    the 400 `tokenCaveatUnverified` with which Onedata 25.0 answers the scope inference of a token past its time
    caveat (seen on the Compose federation of deploy/compose-federation, 10 Nov 2026)."""
    user = service_user(config)
    caveats = CaveatSpec(config.space.id, readonly=True, interface="rest").caveats(int(time.time()) + EXPIRY_S)
    value = client.mint_own_token(user, caveats)
    read_space(client, value, config.space.id)  # it works while it lives
    time.sleep(EXPIRY_S + 5)
    with pytest.raises(PlatformError) as refused:
        read_space(client, value, config.space.id)
    answer = (refused.value.status, refused.value.code)
    assert answer[0] in (401, 403) or answer == (400, "tokenCaveatUnverified")


REVOKE = pytest.mark.skipif(
    os.environ.get("EXPCORE_LIVE_REVOKE") != "1",
    reason="EXPCORE_LIVE_REVOKE=1 is not set; this test revokes every temporary token of [principals.service]",
)


@REVOKE
def test_a_users_own_revocation_raises_its_generation_and_its_token_then_verifies_as_revoked(config, client, issuer):
    """C19: the revocation a run's teardown sends for each slot user, sent here for the health check's own user, whose
    tokens are minted per check: run it only when nothing else holds a token of that user. Before it the token is not
    revoked (it verifies, or is refused for its data-access caveats); after it the user's generation has risen and the
    token verifies as 400 `tokenRevoked`. Named by oz-worker's source alone (14b5bda, `token_routes.erl:163-176`)."""
    # covers: GET /user/tokens/temporary
    # covers: DELETE /user/tokens/temporary
    # covers: POST /tokens/verify_access_token
    user = service_user(config)
    token = issuer.mint(data_path_spec(config.space.id), user, ceiling_s=CEILING_S)
    with pytest.raises(RevocationUnconfirmed):  # it verifies, or is refused for its caveats: either way not revoked
        issuer.require_revoked(token.reveal())
    before, after = issuer.revoke_own(user)
    assert after > before
    issuer.require_revoked(token.reveal())  # answered 400 tokenRevoked, and nothing else


# -- step 4b2: placement, probes and day-0 checks ----------------------------------------------------------------------
# These read a real deployment's providers (the listing, a block map, the public configuration). The two that change
# state, the read-only enforcement and the placement round trip on a scratch file under the configured prefixes, also
# need EXPCORE_LIVE_MUTATE=1.

MUTATE = pytest.mark.skipif(
    os.environ.get("EXPCORE_LIVE_MUTATE") != "1",
    reason="EXPCORE_LIVE_MUTATE=1 is not set; this test changes state in the configured space",
)


@pytest.fixture(scope="module")
def day0(tmp_path_factory):
    from expcore_onedata.day0.runner import build_context

    return build_context(PLATFORM, tmp_path_factory.mktemp("day0"))


def test_the_read_only_day0_checks_do_not_fail_on_the_real_deployment(day0):
    from expcore_onedata.day0.runner import run_checks

    entries = run_checks(day0, ["reachability", "health", "tokens", "listing"])
    failures = {e.name: e.summary for e in entries if e.ok is False}
    assert not failures, failures


def test_the_space_roots_distribution_gives_each_supporting_providers_physical_size(day0, tmp_path):
    """O08: the reset's occupancy stage reads the space root's distribution on the data provider: every supporting
    provider has a physical size there, a whole number of bytes, or the reading is unknown and the test fails (a space
    without directory statistics cannot pass the reset). Read-only."""
    # covers: GET /data/{id}/distribution
    from expcore_onedata.occupancy import physical_sizes
    from expcore_onedata.provision import Provisioner

    if day0.placement is None:
        pytest.skip("the occupancy check needs a [placement] table, which platform.toml does not have")
    provisioner = Provisioner(
        day0.client, day0.config, day0.placement.settings, provider_ids=day0.provider_ids, log=tmp_path / "log.jsonl"
    )
    sizes = physical_sizes(provisioner, day0.config.support.providers)
    assert set(sizes) >= set(day0.config.support.providers)
    assert all(isinstance(size, int) and size >= 0 for size in sizes.values())


def test_a_real_block_map_has_the_documented_shape(day0, client, config):
    """The distribution answer parses; a regular file is found by listing the first supporting provider's root."""
    from expcore_onedata.placement import provider_blocks

    provider = config.support.providers[0]
    root = consistency.space_root(client, provider, config.space.id)
    params = {"limit": 50, "attribute": ["file_id", "type"]}
    entries = client.get(f"/data/{root}/children", provider=provider, params=params)
    files = [c for c in entries["children"] if c.get("type") == "REG"]
    if not files:
        pytest.skip("the space root holds no regular file to read a block map of")
    distribution = client.get(f"/data/{files[0]['file_id']}/distribution", provider=provider)
    # a provider that does not support the space is not listed in the answer, and an unlisted provider is unknown
    supporting = [day0.provider_ids[name] for name in config.support.providers]
    assert supporting
    assert all(isinstance(provider_blocks(distribution, pid), list) for pid in supporting)


def listed_transfers(client, config, provider, limit):
    """The first page of each state of the space's transfer listing, read as the probes read it."""
    from expcore_onedata.transferrecord import STATES, page_of

    path = f"/spaces/{config.space.id}/transfers"
    return {
        state: page_of(client.get(path, provider=provider, params={"state": state, "limit": limit})) for state in STATES
    }


def test_the_transfer_listing_answers_every_state_the_probes_read(config, client):
    """O08: the probes list the space's transfers as waiting, ongoing and ended; each state's first page parses as a
    list of plain ids with a next-page token or none, and a page asked to hold one id holds at most one. The answer has
    a Date header the provider's clock offset is read from, so the run's window can be moved onto that clock."""
    # covers: GET /spaces/{space}/transfers
    from expcore_onedata.transferwindow import offset_of

    provider = config.support.providers[0]
    pages = listed_transfers(client, config, provider, 1)
    for ids, _ in pages.values():
        assert len(ids) <= 1
    _, date = client.get_dated(f"/spaces/{config.space.id}/transfers", provider=provider, params={"state": "ended"})
    assert offset_of(date, time.time()) is not None


def test_onezones_public_current_time_answers_the_global_clock_in_milliseconds(client):
    """O32: a job's times are stamped on the platform's global clock, which Onezone's public current-time endpoint
    reads (oz-worker 14b5bda, `provider_routes.erl:192-198`: "does not require any specific privileges"), and the
    transfer log and the reset's history place their window by it. Sent with no credential header, it answers
    `{"timeMillis": N}`, epoch milliseconds, within a minute of this host's clock on a deployment whose clocks are
    kept; the same answer's Date header is the answering node's system clock, which may differ and is only recorded.
    No source sends this call and no live run had made it before this test (`client.UNVERIFIED_ENDPOINTS`)."""
    # covers: GET /provider/public/get_current_time
    from expcore_onedata.client import ZONE_TIME_PATH
    from expcore_onedata.transferwindow import zone_clock_of

    sent = time.time()
    answer, date = client.get_dated(ZONE_TIME_PATH, anonymous=True)
    received = time.time()
    assert isinstance(answer, dict), "the answer is no object"
    assert isinstance(answer.get("timeMillis"), int | float), "no timeMillis number"
    assert answer["timeMillis"] > 10**12, "timeMillis is not epoch milliseconds"
    reading = zone_clock_of(answer, date, sent, received)
    assert reading is not None
    assert abs(reading.offset) < 60, f"the global clock is {reading.offset:.1f} s from this host's"
    print(
        f"global clock offset {reading.offset:.3f} s, half trip {reading.half_trip:.3f} s, Date {reading.date_offset}"
    )


def test_a_transfer_record_has_the_fields_the_probes_read(config, client):
    """O08: a listed transfer's record has a type of replication, eviction or migration, a schedule time, and for a job
    that replicates its target and byte counter (job_of refuses a record without them), and start and finish times
    that are numbers. The times are epoch seconds, the unit the run's window is compared in: a time in milliseconds
    would put every job outside the window and record none. Skipped when the space lists no transfer."""
    # covers: GET /transfers/{id}
    from expcore_onedata.transferrecord import TYPES, job_of

    ten_years_s = 10 * 365 * 86400
    provider = config.support.providers[0]
    listed = [(tid, state) for state, (ids, _) in listed_transfers(client, config, provider, 5).items() for tid in ids]
    if not listed:
        pytest.skip("the space lists no transfer to read the record of")
    for tid, state in listed:
        body = client.get(f"/transfers/{tid}", provider=provider)
        job = job_of(tid, state, body)
        assert job.kind in TYPES
        assert job.started is not None
        assert job.finished is not None
        assert abs(job.scheduled - time.time()) < ten_years_s
        for moment in (job.started, job.finished):
            assert moment == 0 or abs(moment - time.time()) < ten_years_s  # 0 is a time not reached yet


@MUTATE
@pytest.mark.parametrize("name", ["read_only", "round_trip"])
def test_a_day0_check_that_changes_state_passes_and_shows_its_cleanup(day0, name):
    """Each state-changing check passes (`ok is True`; an unknown ran nothing) and the one that makes a scratch file
    reports its cleanup explicitly as true. It is skipped only when platform.toml lacks what it needs, and the skip
    names that."""
    from expcore_onedata.day0.runner import run_checks

    missing = prerequisite_missing(name, day0)
    if missing is not None:
        pytest.skip(missing)
    day0.allow_mutation = True
    entry = {e.name: e for e in run_checks(day0, [name])}[name]
    require_passed(entry)
    if name == "round_trip":
        require_cleaned(entry)
    else:
        require_every_provider_refused(entry)


# -- a provider's outage (issue #108) -----------------------------------------------------------------------------
# It takes a provider of the live federation down and brings it back, and starts a sandbox in mount mode and the tool
# server to read and call through as the agent does: EXPCORE_LIVE_MUTATE=1, EXPCORE_LIVE_FAULTS=1 and
# EXPCORE_LIVE_DOCKER=1, a platform.toml with a sanctioned [faults] table, a [sandbox] table with a mount level (as
# tests/support/mount_live.py derives one), [slots] and a [toolserver] condition allowing the four knowledge tools, and
# a space whose data provider is not the agent's local one, with a third provider besides. Never set it on a
# federation another run is using.

FAULTS = pytest.mark.skipif(
    os.environ.get("EXPCORE_LIVE_FAULTS") != "1" or os.environ.get("EXPCORE_LIVE_DOCKER") != "1",
    reason="EXPCORE_LIVE_FAULTS=1 and EXPCORE_LIVE_DOCKER=1 are needed: this test takes a provider of the federation "
    "down and starts a sandbox and the tool server",
)


@MUTATE
@FAULTS
def test_a_provider_outage_is_measured_and_the_provider_is_restored(day0):
    """O28: the check takes the data provider down through the fault injector, records the answers of the REST path,
    of the mount of a real sandbox and of the agent's tools through MCP, restores the provider and shows it answering
    again with another identity, and removes its scratch. It is skipped only when platform.toml lacks what it needs,
    and the skip names that."""
    from expcore_onedata.day0.runner import run_checks

    day0.allow_mutation = day0.allow_faults = True
    missing = prerequisite_missing("provider_outage", day0)
    if missing is not None:
        pytest.skip(missing)
    entry = {e.name: e for e in run_checks(day0, ["provider_outage"])}["provider_outage"]
    require_passed(entry)
    require_measured(entry)
    require_restored(entry)
    require_cleaned(entry)


# -- the reset's calls (issue #118) -----------------------------------------------------------------------------------
# Each makes scratch files under the configured prefixes, acts on them alone and tears them down: EXPCORE_LIVE_MUTATE=1.
# The last runs a whole reset of the space, which removes every QoS requirement and cancels every running transfer in
# it, so it needs EXPCORE_LIVE_RESET=1 as well: never set it on a federation another run is using.

RESET = pytest.mark.skipif(
    os.environ.get("EXPCORE_LIVE_RESET") != "1",
    reason="EXPCORE_LIVE_RESET=1 is not set; this test resets the whole configured space",
)


@pytest.fixture
def scratch(day0, tmp_path):
    """A provisioner with a scratch directory of two small files, the provider they are replicated to, and their
    teardown afterwards; skipped when platform.toml has no [placement] or no provider besides the data provider."""
    from expcore_onedata.provision import Provisioner

    missing = prerequisite_missing("round_trip", day0)
    if missing is not None:
        pytest.skip(missing)
    holder = day0.placement.settings.holder
    receiver = next(p for p in day0.config.support.providers if p != holder)
    provisioner = Provisioner(
        day0.client, day0.config, day0.placement.settings, provider_ids=day0.provider_ids, log=tmp_path / "log.jsonl"
    )
    directory = f"expcore-live-reset-{int(time.time())}"
    ids = [provisioner.put_file(f"{directory}/part-{i}.bin", bytes(range(256)) * 4) for i in range(2)]
    try:
        yield provisioner, receiver, directory, ids
    finally:
        require_torn_down(provisioner.teardown())


def require_torn_down(report):
    assert report.ok, f"the scratch files were not all removed: {len(report.left)} left"


@MUTATE
def test_a_directory_eviction_covers_every_file_under_it(scratch):
    """O01: a scratch directory's two files, replicated to a provider, leave it with one eviction of the directory,
    and both block maps then show nothing held there (`eviction.evict_tree` verifies them)."""
    # covers: POST /transfers
    from expcore_onedata.eviction import evict_tree

    provisioner, receiver, directory, _ = scratch
    for i in range(2):
        provisioner.replicate(f"{directory}/part-{i}.bin", receiver)
    record = evict_tree(provisioner, directory, receiver)
    assert (record["files"], record["transfers"], record["empty"]) == (2, 1, True)


@MUTATE
def test_a_running_transfer_is_cancelled_through_the_gate_and_then_reports_an_ended_state(scratch):
    """O11: a replication of a scratch file is cancelled at once, the gate having read the record's spaceId; the
    cancel answers 204, or 400 when the job ended first, and the record then reports an ended state."""
    # covers: DELETE /transfers/{id}
    provisioner, receiver, _, ids = scratch
    tid = provisioner.schedule_transfer("replication", ids[0], receiver)
    statuses = []
    try:
        provisioner.client.mutate("DELETE", f"/transfers/{tid}", provider=receiver)
    except NotSent:
        raise  # the gate never sent the cancel: its own read failed, so this test has not shown the DELETE
    except PlatformError as error:
        statuses.append(error.status)
    assert statuses in ([], [400])  # 400: the job ended before the cancel reached it
    done = consistency.wait_transfers(provisioner.client, receiver, [tid], timeout_s=120, poll_s=2)
    assert not done.timed_out
    assert consistency.transfer_state(done.records[tid]) in consistency.ENDED_STATES


@MUTATE
def test_a_qos_requirement_is_listed_by_its_files_summary_read_and_removed(scratch):
    """O11: a requirement put on a scratch file appears, keyed by its id, in the file's summary; its record names the
    file, which the gate of its removal resolves; once removed it answers that it is not there (`answered_absent`)."""
    # covers: GET /data/{id}/qos/summary
    # covers: GET /qos_requirements/{qid}
    # covers: DELETE /qos_requirements/{qid}
    provisioner, _, _, ids = scratch
    holder = provisioner.settings.holder
    body = {"fileId": ids[0], "expression": "anyStorage", "replicasNum": 1}
    qid = provisioner.client.mutate("POST", "/qos_requirements", provider=holder, json=body)["qosRequirementId"]
    summary = provisioner.client.get(f"/data/{ids[0]}/qos/summary", provider=holder)
    assert qid in summary["requirements"]
    assert provisioner.client.get(f"/qos_requirements/{qid}", provider=holder)["fileId"] == ids[0]
    provisioner.client.mutate("DELETE", f"/qos_requirements/{qid}", provider=holder)
    with pytest.raises(PlatformError) as gone:
        provisioner.client.get(f"/qos_requirements/{qid}", provider=holder)
    assert answered_absent(gone.value), (gone.value.status, gone.value.code, gone.value.errno)


@MUTATE
@RESET
def test_a_whole_reset_removes_a_requirement_cancels_a_job_evicts_and_records_the_history(scratch):
    """O11: with a requirement and a running replication left on the scratch files, the reset passes every stage:
    the requirement is gone, no job is waiting or ongoing, the receiver holds nothing of the directory, and the ended
    history is recorded with its count, never removed. Each stage has its seconds."""
    from expcore_onedata.reset import STAGES, SpaceReset

    provisioner, receiver, directory, ids = scratch
    holder = provisioner.settings.holder
    body = {"fileId": ids[1], "expression": "anyStorage", "replicasNum": 1}
    provisioner.client.mutate("POST", "/qos_requirements", provider=holder, json=body)
    provisioner.schedule_transfer("replication", ids[0], receiver)
    record = SpaceReset(provisioner).run({receiver: [directory]})
    assert record["ok"] is True
    assert list(record["stages"]) == list(STAGES)
    assert record["stages"]["qos"]["removed"] >= 1
    assert record["stages"]["history"]["removable"] is False
    assert all(isinstance(stage["seconds"], float) for stage in record["stages"].values())


# -- the reset's QoS listing and the occupancy's lag (issue #209, O33) -----------------------------------------------
# `[placement] qos_scan = "listing"` rests on two facts the source states and no live run has shown: the first two
# tests check them, and "summaries" stays the default until both pass. The third measures how long the space root's
# physical size lags a placement, which the reset's quiet wait assumes from the source. Each prints its figures for
# the dated facts file and leaves nothing behind: its requirement is removed and the scratch files are torn down.

QOS_SEEN_S = 120.0  # the longest a requirement added through one provider is waited for through another


def scratch_requirement(provisioner, provider, file_id):
    """Put an `anyStorage` requirement on an entry through one provider and return its id."""
    body = {"fileId": file_id, "expression": "anyStorage", "replicasNum": 1}
    return provisioner.client.mutate("POST", "/qos_requirements", provider=provider, json=body)["qosRequirementId"]


@MUTATE
def test_the_recursive_listing_takes_include_directories_and_marks_a_requirements_entry(scratch):
    """O33: the space root's recursive listing with `include_directories`, which the REST description omits, holds
    the root as "." and the scratch directory, and with a requirement on the directory, the directory says `direct`
    and its two files `ancestor`; the pages read are printed."""
    # covers: GET /data/{id}/files
    from expcore_onedata import resetqos

    provisioner, _, directory, _ = scratch
    holder = provisioner.settings.holder
    qid = scratch_requirement(provisioner, holder, provisioner.resolve(directory).file_id)
    try:
        entries, pages = resetqos.listed(provisioner, holder)
        by_path = {entry.path: entry for entry in entries.values()}
        assert "." in by_path
        assert by_path[directory].directory
        assert by_path[directory].inheritance in resetqos.DIRECT
        assert [by_path[f"{directory}/part-{i}.bin"].inheritance for i in range(2)] == ["ancestor", "ancestor"]
        print(f"recursive listing: {len(entries)} entries in {pages} page(s) of {resetqos.LISTING_PAGE}")
    finally:
        provisioner.client.mutate("DELETE", f"/qos_requirements/{qid}", provider=holder)


@MUTATE
def test_a_requirement_added_through_one_provider_is_found_by_the_listing_through_another(scratch):
    """O33: a requirement put on a scratch file through the receiver is found by the listing scan on the data
    provider once dbsync settles, within `QOS_SEEN_S`, and applies to that file; the seconds it took are printed."""
    from expcore_onedata import resetqos

    provisioner, receiver, _, ids = scratch
    holder = provisioner.settings.holder
    qid = scratch_requirement(provisioner, receiver, ids[0])
    try:
        began = time.monotonic()
        found = resetqos.by_listing(provisioner, [holder])
        while qid not in found.where:
            assert time.monotonic() - began < QOS_SEEN_S, "the data provider's listing never showed the requirement"
            time.sleep(2.0)
            found = resetqos.by_listing(provisioner, [holder])
        print(f"a requirement added through another provider was found after {time.monotonic() - began:.1f} s")
        assert ids[0] in found.files[qid]
    finally:
        provisioner.client.mutate("DELETE", f"/qos_requirements/{qid}", provider=receiver)


@MUTATE
def test_the_space_roots_occupancy_follows_a_placement_within_the_sources_bound(scratch):
    """O33: a scratch file one directory below the root is replicated to the receiver, and the space root's physical
    size there is read every quarter second until it has grown by the file's bytes. The seconds are printed, and must
    be within the source's bound for that depth (`resetqos.occupancy_lag_s(2)`, the quiet wait less its margin), plus a
    second for the reads themselves."""
    from expcore_onedata import resetqos
    from expcore_onedata.occupancy import physical_sizes

    provisioner, receiver, directory, _ = scratch
    providers = list(provisioner.space.support.providers)
    before = physical_sizes(provisioner, providers)[receiver]
    provisioner.replicate(f"{directory}/part-0.bin", receiver)
    placed, size = time.monotonic(), 1024  # the scratch files are 1024 bytes each
    while physical_sizes(provisioner, providers)[receiver] < before + size:
        assert time.monotonic() - placed < resetqos.quiet_wait_s(2), "the root's physical size never showed the file"
        time.sleep(0.25)
    lag = time.monotonic() - placed
    print(f"the space root's physical size showed the placement after {lag:.2f} s")
    assert lag <= resetqos.occupancy_lag_s(2) + 1.0


# -- large files (issue #127) -----------------------------------------------------------------------------------------

LARGE_FILE_BYTES = 40 << 20  # tens of MB: ten chunks of the size below, quick to move
LARGE_FILE_CHUNK = 4 << 20


def write_random(path, size, seed):
    """Write `size` pseudo-random bytes, a MiB at a time, and return their SHA-256."""
    import hashlib
    import random

    stream, hasher = random.Random(seed), hashlib.sha256()
    with open(path, "wb") as handle:
        for start in range(0, size, 1 << 20):
            piece = stream.randbytes(min(1 << 20, size - start))
            hasher.update(piece)
            handle.write(piece)
    return hasher.hexdigest()


def copy_prefix(source, dest, size):
    """Copy the first `size` bytes of `source` to `dest`, a MiB at a time, and return their SHA-256. A stream drawn
    again with the same seed is no prefix: `randbytes` of a length that is not a multiple of four ends otherwise."""
    import hashlib

    hasher = hashlib.sha256()
    with open(source, "rb") as reader, open(dest, "wb") as writer:
        while size > 0:
            piece = reader.read(min(1 << 20, size))
            assert piece, "the source ended before the prefix"
            hasher.update(piece)
            writer.write(piece)
            size -= len(piece)
    return hasher.hexdigest()


@MUTATE
def test_a_large_file_resumes_by_offset_writes_refuses_a_shorter_one_and_reads_back_by_its_hash(
    config, client, tmp_path
):
    """O25, O36: on the real deployment, a prefix uploaded first is resumed by offset writes, an upload run again sends
    nothing, a shorter file over the longer target is refused with nothing written (only a write without an offset,
    which truncates and is never sent, could shorten it), and the streamed read gives each file's SHA-256.
    Everything is under a fresh scratch directory and torn down; skipped without a [placement] table."""
    # covers: PUT /data/{id}/content
    import dataclasses
    import secrets

    from expcore.config.loading import ConfigError
    from expcore_onedata.provision import Provisioner, ProvisionError, resolve_provider_ids
    from expcore_onedata.rangemap import load_placement

    try:
        placement = load_placement(PLATFORM, config)
    except ConfigError:
        pytest.skip("the large-file test needs a [placement] table, which platform.toml does not have")
    settings = dataclasses.replace(placement.settings, chunk_bytes=LARGE_FILE_CHUNK)
    provisioner = Provisioner(
        client, config, settings, provider_ids=resolve_provider_ids(config), log=tmp_path / "changes.jsonl"
    )
    whole, prefix = tmp_path / "whole.bin", tmp_path / "prefix.bin"
    whole_sha = write_random(whole, LARGE_FILE_BYTES, seed=127)
    cut = 2 * LARGE_FILE_CHUNK + 4321  # ends inside a chunk, as a write cut part way leaves it
    prefix_sha = copy_prefix(whole, prefix, cut)
    path = f"expcore-live-{secrets.token_hex(6)}/large.bin"
    try:
        created = provisioner.upload(path, prefix)
        assert (created.action, created.digest.sha256) == ("created", prefix_sha)
        resumed = provisioner.upload(path, whole)
        assert (resumed.action, resumed.sent_bytes) == ("resumed", LARGE_FILE_BYTES - cut)
        assert (resumed.digest.size, resumed.digest.sha256) == (LARGE_FILE_BYTES, whole_sha)
        assert provisioner.upload(path, whole).action == "skipped"
        with pytest.raises(ProvisionError, match="longer"):  # only a truncating write could fit it, never sent
            provisioner.upload(path, prefix)
        back = tmp_path / "back.bin"
        found = provisioner.read_file(resumed.file_id, dest=back)
        assert (found.size, found.sha256) == (LARGE_FILE_BYTES, whole_sha)
        assert back.stat().st_size == LARGE_FILE_BYTES
    finally:
        report = provisioner.teardown()
    assert report.ok, report.left


DEFAULT_CHUNK_UPLOAD_BYTES = 50 << 20  # three full default chunks (16 MiB each) and a remainder of 2 MiB


@MUTATE
def test_an_upload_at_the_default_chunk_size_sends_each_full_chunk_on_a_new_connection_and_reads_back_by_its_hash(
    config, client, tmp_path, caplog, record_property
):
    """O25: the default itself, on the real deployment (#186). No `chunk_bytes` is set here: a 50 MiB file goes up in
    the chunks the settings give by default, three full ones and a remainder, each full chunk on a connection the
    client opened for it, and the file read back by its SHA-256 equals the local one. It records, per PUT, its size and
    whether its connection was new, from the client's own log, which holds no path, id or token (`record_property`,
    and on stdout). On the 25.0 development build 32 and 64 MiB chunks stalled on reused connections and 16 MiB never
    did, so a stall here is what the default must not do. Torn down; skipped without a [placement] table."""
    # covers: PUT /data/{id}/content
    import logging
    import re
    import secrets

    from expcore.config.loading import ConfigError
    from expcore_onedata.provision import Provisioner, resolve_provider_ids
    from expcore_onedata.rangemap import DEFAULT_CHUNK_BYTES, DEFAULT_FRESH_CONNECTION_BYTES, load_placement

    try:
        placement = load_placement(PLATFORM, config)
    except ConfigError:
        pytest.skip("the large-file test needs a [placement] table, which platform.toml does not have")
    settings = placement.settings  # unchanged: the point is that the default is what is verified
    assert (settings.chunk_bytes, settings.fresh_connection_bytes) == (
        DEFAULT_CHUNK_BYTES,
        DEFAULT_FRESH_CONNECTION_BYTES,
    ), "platform.toml sets chunk_bytes or fresh_connection_bytes, so this run would not verify the defaults"
    chunk = settings.chunk_bytes
    assert 3 * chunk < DEFAULT_CHUNK_UPLOAD_BYTES < 4 * chunk
    provisioner = Provisioner(
        client, config, settings, provider_ids=resolve_provider_ids(config), log=tmp_path / "changes.jsonl"
    )
    source = tmp_path / "default-chunks.bin"
    sha = write_random(source, DEFAULT_CHUNK_UPLOAD_BYTES, seed=186)
    path = f"expcore-live-{secrets.token_hex(6)}/default-chunks.bin"
    caplog.set_level(logging.INFO, logger="expcore_onedata.client")
    try:
        uploaded = provisioner.upload(path, source)
        assert (uploaded.action, uploaded.sent_bytes) == ("created", DEFAULT_CHUNK_UPLOAD_BYTES)
        assert (uploaded.digest.size, uploaded.digest.sha256) == (DEFAULT_CHUNK_UPLOAD_BYTES, sha)
        back = provisioner.read_file(uploaded.file_id)
        assert (back.size, back.sha256) == (DEFAULT_CHUNK_UPLOAD_BYTES, sha)
    finally:
        report = provisioner.teardown()
    assert report.ok, report.left
    puts = []
    for record in caplog.records:
        found = re.search(r"a PUT of (\d+) bytes .*\(connection (new|reused[^)]*)\)", record.getMessage())
        if found and record.name == "expcore_onedata.client":
            puts.append((int(found.group(1)), found.group(2)))
    for number, (size, connection) in enumerate(puts, start=1):
        record_property(f"put_{number}", f"{size} bytes, connection {connection}")
        print(f"PUT {number}: {size} bytes, connection {connection}")
    assert [size for size, _ in puts] == [chunk, chunk, chunk, DEFAULT_CHUNK_UPLOAD_BYTES - 3 * chunk]
    assert [connection for size, connection in puts if size == chunk] == ["new"] * 3


# -- the fault injector (issue #17) -----------------------------------------------------------------------------------

FAULTS = pytest.mark.skipif(
    os.environ.get("EXPCORE_LIVE_FAULTS") != "1",
    reason="EXPCORE_LIVE_FAULTS=1 is not set; this test takes a provider of the deployment down",
)


@FAULTS
def test_a_provider_taken_down_is_seen_down_and_comes_back_with_a_new_identity(config, client):
    """O11: the injector takes a provider of `[faults.services]` down (EXPCORE_LIVE_FAULT_TARGET, a label such as
    providers[1], or the first that is not the agent's own), sees it down, brings it back with a changed identity and
    waits until it serves (its container healthy, its REST read answered), leaving no injection open in the journal."""
    from expcore_onedata.faults import FaultInjector, compose_driver, load_faults, provider_serves

    settings = load_faults(PLATFORM, config)
    remote = [name for name in settings.services if name != config.support.local] or list(settings.services)
    target = os.environ.get("EXPCORE_LIVE_FAULT_TARGET") or config.provider_label(remote[0])
    driver = compose_driver(settings, probe=provider_serves(config, settings, client))
    injector = FaultInjector(config, settings, target, sanctioned=True, driver=driver)
    with injector.session({"run_key": "live-fault-check", "attempt": 1}):
        steps = [s["step"] for s in injector.snapshot()["steps"]]
        assert "injected" in steps
    snapshot = injector.finish()
    assert snapshot["ok"] is True, snapshot["steps"]
    (injection,) = snapshot["injections"]
    ready_after_s = injection.pop("ready_after_s")  # the seconds the restored provider took to serve, on this clock
    assert injection == {"target": target, "injected": True, "cleared": True, "verified": True}
    assert 0 <= ready_after_s <= settings.ready_timeout_s
