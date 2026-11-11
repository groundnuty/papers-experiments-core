"""The [sandbox] table: every image, pool, limit, mount option and data path is read from platform.toml (O12-O14)."""

import pytest
from platform_support import write_platform
from sandbox_support import AGENT_IMAGE_ID, COMPLETE_LEVEL, GATE_IMAGE_ID, LEVELS, POOL, add_sandbox

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox.api import DataPathMode


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


def test_o19_a_rest_level_selects_its_listing_mode_and_one_without_it_pages(tmp_path):
    """O19: a level's entry is its kind, or a table of its kind and a REST level's listing mode; a REST level that names
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
def test_o19_o12_a_listing_mode_where_it_cannot_apply_is_refused_naming_the_level_by_position(tmp_path, entry, message):
    """O19, O12: a listing mode on a mount or no data path, one the gate does not know, or a table without a kind is
    refused at load; the level is named by its position, never by itself."""

    def edit(data):
        data["sandbox"]["data_paths"]["via-gate"] = entry

    with pytest.raises(ConfigError, match=rf"entry \d: .*{message}") as caught:
        load(tmp_path, edit)
    assert "via-gate" not in str(caught.value)


def test_o19_a_campaign_does_not_resume_after_a_level_changed_its_listing_mode(tmp_path):
    """O19, C06: the platform's half of the fingerprint holds each level's kind and a REST level's listing mode, so a
    campaign started with one listing mode for a level is refused a resume with the other; the same mode resumes."""
    from campaign_support import make_fingerprint, make_manifest, make_schema
    from platform_support import NOW

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


def test_o20_the_caps_of_the_tables_of_the_bytes_served_are_declared_and_default_to_1000_1000_and_64(tmp_path):
    """O20: `[sandbox.served_bytes]` declares how many files, directories and ranges a file a run's tables of the bytes
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
def test_o20_a_cap_that_is_not_a_whole_number_of_at_least_one_is_refused_at_load_naming_the_file_and_key(
    tmp_path, key, value
):
    """O20: a cap must be an integer of at least 1, so a table can always list something and a typo cannot pass as a
    number; the error names the platform file and the key."""

    def edit(data):
        data["sandbox"]["served_bytes"] = {key: value}

    with pytest.raises(ConfigError, match=rf"\[sandbox\.served_bytes\]: key '{key}' must be an integer of at least 1"):
        load(tmp_path, edit)
    with pytest.raises(ConfigError, match=r"platform\.toml"):
        load(tmp_path, edit)


def test_o20_an_unknown_key_of_the_caps_is_refused_so_a_misspelt_cap_cannot_leave_the_default(tmp_path):
    """O20: a misspelt cap would silently keep the default and cut the table it was meant to widen; it is refused."""

    def edit(data):
        data["sandbox"]["served_bytes"] = {"max_file": 4000}

    with pytest.raises(ConfigError, match=r"\[sandbox\.served_bytes\]: key 'max_file' is not a known key"):
        load(tmp_path, edit)
