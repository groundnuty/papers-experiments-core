"""The [sandbox] table: every image, pool, limit, mount option and data path is read from platform.toml (O12-O14)."""

import pytest
from sandbox_support import (
    AGENT_IMAGE_ID,
    COMPLETE_LEVEL,
    FED_LOCAL,
    FED_NETWORK,
    FED_ZONE,
    GATE_IMAGE_ID,
    LEVELS,
    POOL,
    add_federation,
    add_sandbox,
)

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.sandbox.settings import FederationNetwork
from expcore_onedata.testing import NOW, write_platform


def load(tmp_path, edit=None, *, with_table=True):
    """Load a platform document that has the default `[sandbox]` table, after an optional edit of it."""

    def both(data):
        if with_table:
            add_sandbox(data)
        if edit is not None:
            edit(data)

    return load_platform(write_platform(tmp_path, both), require_secrets=False).sandbox


def test_the_default_document_loads_with_every_value_from_the_file(tmp_path):
    sandbox = load(tmp_path)
    assert sandbox.agent.id == AGENT_IMAGE_ID
    assert sandbox.gate.id == GATE_IMAGE_ID
    assert sandbox.network.pools == (POOL,)
    assert sandbox.mount.path == "/data"
    assert sandbox.work_root == (tmp_path / "work").resolve()
    assert sandbox.label("sandbox") == "org.test.sandbox"


def test_a_platform_file_without_the_table_has_no_sandbox(tmp_path):
    assert load(tmp_path, with_table=False) is None


def test_the_levels_of_the_data_path_factor_map_to_the_three_paths(tmp_path):
    sandbox = load(tmp_path)
    assert {level: sandbox.mode_of(level).value for level in LEVELS} == LEVELS
    assert sandbox.mode_of("mounted") is DataPathMode.MOUNT
    assert DataPathMode.MOUNT.interface == "oneclient"
    assert DataPathMode.REST.interface == "rest"
    assert DataPathMode.NONE.interface is None


def test_a_level_the_file_does_not_map_is_refused_by_name(tmp_path):
    with pytest.raises(ValueError, match="does not map the level"):
        load(tmp_path).mode_of("unmapped")


@pytest.mark.parametrize(
    ("pools", "why"),
    [
        (["10.77.0.5/24"], "no host bits"),  # strict mode: the host bits are set
        (["not-a-network"], "valid IPv4 network"),
        (["fd00::/64"], "IPv4"),
        ([], "at least one"),
        (["10.77.0.0/24", "10.77.0.0/24"], "each once"),
    ],
)
def test_o14_a_pool_that_is_not_a_strict_network_fails_at_load(tmp_path, pools, why):
    """O14: the pool's ranges are validated in strict mode at load, and the message does not echo the value."""

    def edit(data):
        data["sandbox"]["network"]["pools"] = pools

    with pytest.raises(ConfigError, match=why) as caught:
        load(tmp_path, edit)
    assert "10.77.0.5" not in str(caught.value)


@pytest.mark.parametrize(
    "pools",
    [
        ["192.0.2.0/24", "192.0.2.128/25"],  # the second nested in the first
        ["198.51.100.0/25", "203.0.113.0/24", "198.51.100.0/24"],  # the first nested in the third, apart in the list
    ],
)
def test_o14_overlapping_pools_are_refused_at_load_by_position(tmp_path, pools):
    """O14: two pools that share addresses could hand two sandboxes the same network; the message names the entries by
    their position, never by their value."""

    def edit(data):
        data["sandbox"]["network"]["pools"] = pools
        data["sandbox"]["network"]["prefix"] = 28

    with pytest.raises(ConfigError, match="overlap") as caught:
        load(tmp_path, edit)
    message = str(caught.value)
    assert "pools" in message
    first, second = (0, 1) if len(pools) == 2 else (0, 2)
    assert f"entries {first} and {second} overlap" in message
    for value in pools:
        assert value.split("/")[0] not in message


def test_o14_pools_that_only_touch_are_not_an_overlap(tmp_path):
    """O14: adjacent pools share no address, so they load."""

    def edit(data):
        data["sandbox"]["network"]["pools"] = ["192.0.2.0/25", "192.0.2.128/25"]
        data["sandbox"]["network"]["prefix"] = 28

    assert load(tmp_path, edit).network.pools == ("192.0.2.0/25", "192.0.2.128/25")


def test_o14_the_prefix_must_fit_inside_every_pool(tmp_path):
    """O14: a sandbox network larger than its pool is refused at load."""

    def edit(data):
        data["sandbox"]["network"]["prefix"] = 16

    with pytest.raises(ConfigError, match="prefix"):
        load(tmp_path, edit)


def test_o13_an_image_must_be_pinned_by_an_id_not_a_tag(tmp_path):
    """O13: the configuration carries the id each image is pinned to, and a tag is not one."""

    def edit(data):
        data["sandbox"]["images"]["agent"]["id"] = "agent:1"

    with pytest.raises(ConfigError, match="image id"):
        load(tmp_path, edit)


def test_the_gate_runs_on_the_agent_image_unless_the_file_names_another(tmp_path):
    def edit(data):
        del data["sandbox"]["images"]["gate"]

    sandbox = load(tmp_path, edit)
    assert sandbox.gate == sandbox.agent


def test_a_mount_data_path_needs_the_mount_table_and_the_sidecar_image(tmp_path):
    def edit(data):
        del data["sandbox"]["images"]["oneclient"]

    with pytest.raises(ConfigError, match="required when a data path is a mount"):
        load(tmp_path, edit)


def test_the_tls_flag_cannot_be_smuggled_through_the_extra_arguments(tmp_path):
    def edit(data):
        data["sandbox"]["mount"]["extra_args"] = ["--insecure"]

    with pytest.raises(ConfigError, match="TLS"):
        load(tmp_path, edit)


# Every form oneclient's parser (boost::program_options, default style: abbreviations and clustered short options
# allowed) reads as its log-directory option, as the operator may write it in `extra_args`, with the option it names.
LOG_DIR_FORMS = [
    (["-l/oc-log"], "-l"),
    (["-l", "/oc-log"], "-l"),
    (["-fl/oc-log"], "-l"),  # `-f -l/oc-log`: a cluster of switches ends in the option
    (["-dfl", "/oc-log"], "-l"),
    (["--log-dir", "/oc-log"], "--log-dir"),
    (["--log-dir=/oc-log"], "--log-dir"),
    (["--log-d", "/oc-log"], "--log-d"),  # an abbreviation oneclient accepts: nothing else starts `log-d`
    (["--log-di=/oc-log"], "--log-di"),
    (["--log", "/oc-log"], "--log"),  # ambiguous in oneclient, which refuses it itself: refused here all the same
    (["-f", "--log-dir"], "--log-dir"),  # no value: oneclient would take the mountpoint
    (["-f", "-l"], "-l"),
]


@pytest.mark.parametrize(("args", "option"), LOG_DIR_FORMS)
def test_o05_the_log_directory_option_is_refused_in_every_form_oneclient_reads(tmp_path, args, option):
    """O05: the sandbox owns oneclient's log directory (it binds the run's own there, as evidence), so `extra_args`
    never carries the option, however it is written; the message names the option, never a value."""

    def edit(data):
        data["sandbox"]["mount"]["extra_args"] = args

    with pytest.raises(ConfigError, match="log directory") as e:
        load(tmp_path, edit)
    assert f"({option})" in str(e.value)
    assert "/oc-log" not in str(e.value)


@pytest.mark.parametrize(
    "args",
    [
        ["--provider-timeout", "30", "--storage-timeout=30"],
        ["--log-read-write-perf"],  # another option that starts `log-`
        ["-f", "-d", "-s"],
        ["-o", "allow_root", "-oallow_other,nonempty"],  # an `l` inside `-o`'s value is not an option
        ["-Hhost-l"],  # nor one in the value of `-H`
        ["--opt=nolock"],
    ],
)
def test_o05_arguments_that_are_not_the_log_directory_option_are_kept(tmp_path, args):
    """O05: the refusal is of the log directory's option alone."""

    def edit(data):
        data["sandbox"]["mount"]["extra_args"] = args

    assert load(tmp_path, edit).mount.extra_args == tuple(args)


@pytest.mark.parametrize(
    ("table", "key", "value", "named"),
    [
        ("data_paths", "mounted", "elsewhere", "entry 0"),  # a level is the study's word: named by position
        ("mount", "path", "/", "path"),
        ("mount", "io_mode", "x", "io_mode"),
    ],
)
def test_a_value_outside_its_vocabulary_is_refused_naming_the_key_or_the_position(tmp_path, table, key, value, named):
    def edit(data):
        data["sandbox"][table][key] = value

    with pytest.raises(ConfigError, match=named) as caught:
        load(tmp_path, edit)
    if len(value) > 2:
        assert value not in str(caught.value)


def test_an_unknown_key_is_refused_so_a_misspelling_cannot_do_nothing(tmp_path):
    def edit(data):
        data["sandbox"]["limits"]["memory"] = 1

    with pytest.raises(ConfigError, match="not a known key"):
        load(tmp_path, edit)


def test_c19_a_users_table_in_the_sandbox_is_refused_and_points_to_the_slots(tmp_path):
    """C19: there is one pool of leased users, the slots. A `[sandbox.users]` table, which made a second pool that could
    lease a user a slot also holds, is refused at load, naming the key and pointing to `[slots]`, never echoing a
    login or an id."""

    def edit(data):
        data["sandbox"]["users"] = {"login-old": "u-old"}

    with pytest.raises(ConfigError, match=r"\[sandbox\]: key 'users'") as caught:
        load(tmp_path, edit)
    assert "[slots" in str(caught.value)
    assert "login-old" not in str(caught.value)
    assert "u-old" not in str(caught.value)


@pytest.mark.parametrize(("table", "key"), [("data_paths", "mounted")])
def test_o12_a_level_with_a_bad_value_is_named_by_position_and_never_by_itself(tmp_path, table, key):
    """O12: the key of this table is itself a configured value (a level), so the message cannot echo it."""

    def edit(data):
        data["sandbox"][table][key] = 7

    with pytest.raises(ConfigError, match=r"entry \d+: must be a non-empty string, got int") as caught:
        load(tmp_path, edit)
    assert key not in str(caught.value)


def test_o21_a_rest_level_selects_its_listing_mode_and_one_without_it_pages(tmp_path):
    """O21: a level's entry is its kind, or a table of its kind and a REST level's listing mode; a REST level that names
    none pages its listings as the server does, and a level of another kind has no listing mode."""

    def edit(data):
        data["sandbox"]["data_paths"]["via-gate-full"] = {"kind": "rest", "listing": "complete"}
        data["sandbox"]["data_paths"]["via-gate-pages"] = {"kind": "rest", "listing": "paged"}
        data["sandbox"]["data_paths"]["mounted-too"] = {"kind": "mount"}

    sandbox = load(tmp_path, edit)
    assert sandbox.mode_of("via-gate-full") is DataPathMode.REST
    listings = {level: sandbox.listing_of(level) for level in sandbox.data_paths}
    assert listings == {
        "mounted": None,
        "via-gate": "paged",
        "isolated": None,
        COMPLETE_LEVEL: "complete",
        "via-gate-full": "complete",
        "via-gate-pages": "paged",
        "mounted-too": None,
    }
    assert sandbox.data_path_record()["via-gate-full"] == {"kind": "rest", "listing": "complete"}
    assert sandbox.data_path_record()["mounted-too"] == {"kind": "mount"}


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"kind": "mount", "listing": "complete"}, "applies to a rest data path only"),
        ({"kind": "none", "listing": "paged"}, "applies to a rest data path only"),
        ({"kind": "rest", "listing": "everything"}, "key 'listing' must be one of: paged, complete"),
        ({"kind": "rest", "listing": 1}, "key 'listing' must be one of"),
        ({"listing": "complete"}, "key 'kind' is required"),
        ({"kind": "rest", "pages": "all"}, "key 'pages' is not a known key"),
        ({"kind": "elsewhere"}, "must be one of: mount, rest, none"),
    ],
)
def test_o21_o12_a_listing_mode_where_it_cannot_apply_is_refused_naming_the_level_by_position(tmp_path, entry, message):
    """O21, O12: a listing mode on a mount or no data path, one the gate does not know, or a table without a kind is
    refused at load; the level is named by its position, never by itself."""

    def edit(data):
        data["sandbox"]["data_paths"]["via-gate"] = entry

    with pytest.raises(ConfigError, match=rf"entry \d: .*{message}") as caught:
        load(tmp_path, edit)
    assert "via-gate" not in str(caught.value)


def test_o21_a_campaign_does_not_resume_after_a_level_changed_its_listing_mode(tmp_path):
    """O21, C06: the platform's half of the fingerprint holds each level's kind and a REST level's listing mode, so a
    campaign started with one listing mode for a level is refused a resume with the other; the same mode resumes."""
    from campaign_support import make_fingerprint, make_manifest, make_schema

    from expcore.campaign.resume import FingerprintMismatch, open_campaign

    def platform(listing):
        def edit(data):
            add_sandbox(data)
            data["sandbox"]["data_paths"]["via-gate"] = {"kind": "rest", "listing": listing}

        (tmp_path / listing).mkdir()
        return load_platform(write_platform(tmp_path / listing, edit), require_secrets=False).fingerprint()

    paged, complete = platform("paged"), platform("complete")
    assert paged["data_paths"] == {
        "mounted": {"kind": "mount"},
        "via-gate": {"kind": "rest", "listing": "paged"},
        "isolated": {"kind": "none"},
        COMPLETE_LEVEL: {"kind": "rest", "listing": "complete"},
    }
    assert complete["data_paths"]["via-gate"] == {"kind": "rest", "listing": "complete"}
    schema, manifest = make_schema(), make_manifest(make_schema())

    def resume(fingerprint):
        store, _ = open_campaign(
            tmp_path / "campaign.db", schema, manifest, make_fingerprint(schema, platform=fingerprint), now=NOW,
            max_attempts=3,
        )  # fmt: skip
        store.close()

    resume(paged)
    with pytest.raises(FingerprintMismatch) as caught:
        resume(complete)
    assert caught.value.differences
    assert all(line.startswith("platform.data_paths") for line in caught.value.differences)
    resume(paged)


def test_o22_the_caps_of_the_tables_of_the_bytes_served_are_declared_and_default_to_1000_1000_and_64(tmp_path):
    """O22: `[sandbox.served_bytes]` declares how many files, directories and ranges a file a run's tables of the bytes
    served may list; a table that declares none, or only some, keeps the defaults for the rest."""
    assert load(tmp_path).served_caps.record() == {
        "max_files": 1000,
        "max_directories": 1000,
        "max_ranges_per_file": 64,
    }

    def edit(data):
        data["sandbox"]["served_bytes"] = {"max_files": 4000, "max_ranges_per_file": 256}

    assert load(tmp_path, edit).served_caps.record() == {
        "max_files": 4000,
        "max_directories": 1000,
        "max_ranges_per_file": 256,
    }


@pytest.mark.parametrize("key", ["max_files", "max_directories", "max_ranges_per_file"])
@pytest.mark.parametrize("value", [0, -5, 2.5, True, "2000"], ids=["zero", "negative", "fraction", "boolean", "text"])
def test_o22_a_cap_that_is_not_a_whole_number_of_at_least_one_is_refused_at_load_naming_the_file_and_key(
    tmp_path, key, value
):
    """O22: a cap must be an integer of at least 1, so a table can always list something and a typo cannot pass as a
    number; the error names the platform file and the key."""

    def edit(data):
        data["sandbox"]["served_bytes"] = {key: value}

    with pytest.raises(ConfigError, match=rf"\[sandbox\.served_bytes\]: key '{key}' must be an integer of at least 1"):
        load(tmp_path, edit)
    with pytest.raises(ConfigError, match=r"platform\.toml"):
        load(tmp_path, edit)


def test_o22_an_unknown_key_of_the_caps_is_refused_so_a_misspelt_cap_cannot_leave_the_default(tmp_path):
    """O22: a misspelt cap would silently keep the default and cut the table it was meant to widen; it is refused."""

    def edit(data):
        data["sandbox"]["served_bytes"] = {"max_file": 4000}

    with pytest.raises(ConfigError, match=r"\[sandbox\.served_bytes\]: key 'max_file' is not a known key"):
        load(tmp_path, edit)


def caps_campaign(tmp_path):
    """A campaign store's resume under a platform fingerprint: `resume(fingerprint)` opens it, refusing a change."""
    from campaign_support import make_fingerprint, make_manifest, make_schema

    from expcore.campaign.resume import open_campaign

    schema, manifest = make_schema(), make_manifest(make_schema())

    def resume(fingerprint):
        store, _ = open_campaign(
            tmp_path / "campaign.db", schema, manifest, make_fingerprint(schema, platform=fingerprint), now=NOW,
            max_attempts=3,
        )  # fmt: skip
        store.close()

    return resume


def platform_with_caps(tmp_path, name, caps=None):
    """The loaded platform of a document with the default `[sandbox]`, and `[sandbox.served_bytes]` with caps given."""

    def edit(data):
        add_sandbox(data)
        if caps is not None:
            data["sandbox"]["served_bytes"] = caps

    (tmp_path / name).mkdir()
    return load_platform(write_platform(tmp_path / name, edit), require_secrets=False)


def test_c06_a_campaign_does_not_resume_under_raised_served_bytes_caps(tmp_path):
    """C06: the caps of the tables of the bytes served decide what a run's evidence can show, and a study scores on it,
    so a declared `[sandbox.served_bytes]` is in the platform's fingerprint: a resume under raised caps is refused,
    naming the caps, and the same caps resume."""
    from expcore.campaign.resume import FingerprintMismatch

    resume = caps_campaign(tmp_path)
    started = platform_with_caps(tmp_path, "started", {"max_files": 2000}).fingerprint()
    raised = platform_with_caps(tmp_path, "raised", {"max_files": 4000, "max_ranges_per_file": 128}).fingerprint()
    assert started["served_bytes"] == {"max_files": 2000, "max_directories": 1000, "max_ranges_per_file": 64}
    resume(started)
    with pytest.raises(FingerprintMismatch) as caught:
        resume(raised)
    assert caught.value.differences
    assert all(line.startswith("platform.served_bytes") for line in caught.value.differences)
    with pytest.raises(FingerprintMismatch):
        resume(platform_with_caps(tmp_path, "dropped").fingerprint())  # the table taken away: the defaults differ
    resume(started)


def test_c06_a_platform_without_the_caps_table_keeps_the_fingerprint_it_had_and_still_resumes(tmp_path):
    """C06: a campaign started before the caps could be declared ran under the defaults, and its store holds the
    fingerprint of that version: the TLS mode and the data paths, nothing of the caps. A platform.toml that still
    declares no `[sandbox.served_bytes]` gives that same fingerprint, so the campaign resumes; only a declared table
    enters it."""
    resume = caps_campaign(tmp_path)
    config = platform_with_caps(tmp_path, "plain")
    recorded = {"tls": config.tls.record(), "data_paths": config.sandbox.data_path_record()}  # the earlier version's
    resume(recorded)
    assert config.fingerprint() == recorded
    resume(config.fingerprint())
    resume(platform_with_caps(tmp_path, "again").fingerprint())


# -- [sandbox.federation]: the network the sidecar reaches the federation on -------------------------------------------


def federated(tmp_path, edit=None):
    """Load the default document with the `[sandbox.federation]` table of `add_federation`, after an optional edit."""

    def both(data):
        add_federation(data)
        if edit is not None:
            edit(data["sandbox"]["federation"])

    return load_platform(write_platform(tmp_path, both), require_secrets=False).sandbox


def test_o03_the_federation_table_gives_the_sidecar_a_network_and_the_local_providers_name_on_it(tmp_path):
    """O03: a federation whose providers the host reaches at addresses no container can use (a Compose project's
    loopback ports) is reached on its own docker network, where the zone and the local provider have names of their
    own; the table names that network and those names there, and nothing else."""
    sandbox = federated(tmp_path)
    assert sandbox.federation == FederationNetwork(network=FED_NETWORK, local_provider=FED_LOCAL, zone=FED_ZONE)
    (tmp_path / "plain").mkdir()
    assert load(tmp_path / "plain").federation is None  # optional: without it the sidecar starts as it always has


@pytest.mark.parametrize(
    ("change", "named"),
    [
        (lambda t: t["providers"].clear(), "the local provider"),  # the provider the agent is pinned to is missing
        (lambda t: t["providers"].update(beta="beta.fed-net.test"), "entry 1"),  # a declared provider, not the local
        (lambda t: t["providers"].update(gamma="gamma.fed-net.test"), "entry 1"),  # a provider nobody declared
    ],
)
def test_o03_the_federation_table_names_the_local_provider_and_no_other(tmp_path, change, named):
    """O03: the agent is pinned to its local provider, so the sidecar dials the local provider's name and no other.
    The table keys the name by the provider it belongs to, so a `[support] local` changed without it is refused at
    load instead of mounting through another provider; an entry for any other provider is refused rather than read
    by nothing. A provider's name is configured, so it is named by its position."""
    with pytest.raises(ConfigError, match=named) as caught:
        federated(tmp_path, change)
    for value in ("beta", "gamma", "alpha"):
        assert value not in str(caught.value)


@pytest.mark.parametrize("network", ["bridge", "host", "none", "default", "container:other", "a b", ""])
def test_o05_the_federation_network_is_a_named_network_of_its_own_never_the_host_or_none(tmp_path, network):
    """O05: the sidecar joins the federation's own network instead of docker's default bridge. The host's network
    (no isolation at all), none (no federation), the default bridge (where no name resolves), a container's network
    and a malformed name are refused at load, naming the key and never the value."""

    def change(table):
        table["network"] = network

    with pytest.raises(ConfigError, match="'network'") as caught:
        federated(tmp_path, change)
    if network:
        assert f"'{network}'" not in str(caught.value)


@pytest.mark.parametrize("address", ["alpha.fed-net.test:443", "https://alpha.fed-net.test", "a b", "-x", ""])
def test_o03_the_local_providers_name_on_the_network_is_a_host_name(tmp_path, address):
    """O03: the sidecar dials the name as oneclient's provider host on its default port, so it is a plain host name:
    no scheme, no port, no space. It is a configured value, named by its position."""

    def change(table):
        table["providers"]["alpha"] = address

    with pytest.raises(ConfigError, match="entry 0") as caught:
        federated(tmp_path, change)
    if address:
        assert address not in str(caught.value)


def with_levels(data_paths, change=None):
    """An `edit` for `write_platform`: the `[sandbox.federation]` table of `add_federation`, with these levels."""

    def edit(data):
        add_federation(data)
        data["sandbox"]["data_paths"] = data_paths
        if change is not None:
            change(data["sandbox"]["federation"])

    return edit


def test_o05_a_federation_table_with_neither_a_mount_nor_a_rest_data_path_is_refused(tmp_path):
    """O05: only the mount sidecar and the REST data path's gate read the table, so with no data path that is either
    it would change nothing: refused, as a misspelt key is, so a configuration never seems to do what it does not."""
    edit = with_levels({"isolated": "none"})
    with pytest.raises(ConfigError, match=r"\[sandbox\]: key 'federation' is read by the mount sidecar and the data"):
        load_platform(write_platform(tmp_path, edit), require_secrets=False)


def test_o03_a_rest_data_path_alone_takes_the_federation_table_with_the_zones_name(tmp_path):
    """O30: the REST data path's gate reaches the zone and the local provider on the federation's network (#203), so
    the table is read with a rest level and no mount, and gives the gate both names there."""
    edit = with_levels({"via-gate": "rest", "isolated": "none"})
    sandbox = load_platform(write_platform(tmp_path, edit), require_secrets=False).sandbox
    assert sandbox.federation == FederationNetwork(network=FED_NETWORK, local_provider=FED_LOCAL, zone=FED_ZONE)


def test_o03_a_rest_data_path_on_the_federation_needs_the_zones_name_there(tmp_path):
    """O30: the gate dials the zone on the federation's network too, by the name its certificate holds; a table that
    gives the gate no zone there is refused at load, naming the key, rather than leaving the zone to a loopback URL."""
    edit = with_levels({"via-gate": "rest"}, lambda table: table.pop("zone"))
    with pytest.raises(ConfigError, match=r"\[sandbox\.federation\]: key 'zone' is required") as caught:
        load_platform(write_platform(tmp_path, edit), require_secrets=False)
    assert FED_NETWORK not in str(caught.value)


def test_o05_the_zones_name_is_refused_where_no_gate_reads_it(tmp_path):
    """O30: a mount sidecar never dials the zone, so a `zone` in a table no REST level reads would do nothing: it is
    refused, as a misspelt key is. A mount level's table without it loads as before."""
    edit = with_levels({"mounted": "mount", "isolated": "none"})
    with pytest.raises(ConfigError, match=r"\[sandbox\.federation\]: key 'zone' is read by the REST data path's gate"):
        load_platform(write_platform(tmp_path, edit), require_secrets=False)
    (tmp_path / "mount-only").mkdir()
    edit = with_levels({"mounted": "mount"}, lambda table: table.pop("zone"))
    sandbox = load_platform(write_platform(tmp_path / "mount-only", edit), require_secrets=False).sandbox
    assert sandbox.federation == FederationNetwork(network=FED_NETWORK, local_provider=FED_LOCAL)


@pytest.mark.parametrize("address", ["zone.fed-net.test:443", "https://zone.fed-net.test", "a b", "-x", "", 7])
def test_o03_the_zones_name_on_the_network_is_a_host_name(tmp_path, address):
    """O30: the gate dials the zone's name on the network's port 443, so it is a plain host name: no scheme, no port,
    no space. It is a configured value: the key is named, never the value."""

    def change(table):
        table["zone"] = address

    with pytest.raises(ConfigError, match="'zone'") as caught:
        federated(tmp_path, change)
    if isinstance(address, str) and address:
        assert address not in str(caught.value)


def test_o05_an_unknown_key_of_the_federation_table_is_refused(tmp_path):
    """O05: a misspelt key cannot do nothing: the table holds the network and the zone's and the local provider's
    names only."""

    def change(table):
        table["gate"] = "gate.fed-net.test"

    with pytest.raises(ConfigError, match=r"\[sandbox\.federation\]: key 'gate' is not a known key"):
        federated(tmp_path, change)


def test_o23_the_placeholder_token_defaults_to_a_neutral_word_and_a_file_can_name_another(tmp_path):
    """O23: on a REST data path the agent's environment holds a placeholder token, so the published client starts; it
    is `placeholder` unless `[sandbox] placeholder_token` names another, which a study may need to pass its own check of
    the words its agent sees."""
    assert load(tmp_path).placeholder_token == "placeholder"

    def edit(data):
        data["sandbox"]["placeholder_token"] = "not-a-credential"

    assert load(tmp_path, edit).placeholder_token == "not-a-credential"


@pytest.mark.parametrize(
    "value",
    ["", "two words", "line\nbreak", "tab\there", "café", "x" * 129, 7, True, ["placeholder"]],
    ids=["empty", "space", "line-break", "tab", "not-ascii", "too-long", "number", "boolean", "list"],
)
def test_o23_a_placeholder_token_that_could_not_be_a_header_value_is_refused_naming_the_key_never_the_value(
    tmp_path, value
):
    """O23, O12: the placeholder is sent as a header value by the client and set in the agent's environment, so it
    must be 1 to 128 printable ASCII characters without white space; the error names the file and the key."""

    def edit(data):
        data["sandbox"]["placeholder_token"] = value

    with pytest.raises(ConfigError, match=r"\[sandbox\]: key 'placeholder_token' must be 1 to 128 printable") as caught:
        load(tmp_path, edit)
    assert "platform.toml" in str(caught.value)
    if isinstance(value, str) and len(value) > 3:
        assert value not in str(caught.value)
