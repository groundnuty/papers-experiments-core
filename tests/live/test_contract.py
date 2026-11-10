"""Contract tests against a real deployment, skipped unless EXPCORE_LIVE_PLATFORM names a platform.toml.

    EXPCORE_LIVE_PLATFORM=/path/to/platform.toml uv run pytest tests/live
    EXPCORE_LIVE_SLOW=1 EXPCORE_LIVE_PLATFORM=... uv run pytest tests/live   # also the test that waits out a lifetime

They check what the fake can only assume: that the endpoints the layer calls answer in the shape it parses. They read
(health, discovery, the examination of a token, and, with EXPCORE_LIVE_CONTENT_PATH naming a provisioned file by its
path in the space, that file's content) and mint a short-lived token per plane for the configured fixed users.
Tokens are never revoked, so each one ends with its lifetime. Nothing here changes a space, a file or a privilege, and
without the variable nothing is constructed or contacted.
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
    require_passed,
)

from expcore_onedata.client import API_PROVIDER, AUTH_HEADER, OnezoneClient, PlatformError  # noqa: E402
from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.federation import pin_local_provider, read_space  # noqa: E402
from expcore_onedata.health import PlatformHealth  # noqa: E402
from expcore_onedata.sandbox import gateboundary  # noqa: E402
from expcore_onedata.tokencheck import CaveatSpec  # noqa: E402
from expcore_onedata.tokens import TokenIssuer, data_path_spec  # noqa: E402

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


@SLOW
def test_a_token_stops_working_when_its_lifetime_ends(config, client):
    """Tokens are not revoked; they expire. Mint with a minimal lifetime, wait it out, expect a refusal: 401 or 403, or
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


def test_a_real_block_map_has_the_documented_shape(day0, client, config):
    """The distribution answer parses; a regular file is found by listing the first supporting provider's root."""
    from expcore_onedata import consistency
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
