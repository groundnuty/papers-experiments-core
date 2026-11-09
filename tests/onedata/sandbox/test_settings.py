"""The [sandbox] table: every image, pool, limit, mount option and data path is read from platform.toml (O12-O14)."""

import pytest
from platform_support import write_platform
from sandbox_support import AGENT_IMAGE_ID, GATE_IMAGE_ID, LEVELS, POOL, add_sandbox

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
    assert dict(sandbox.users) == {"login-data": "u-data", "login-data2": "u-data2"}


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
        ("mount", "tls", "maybe", "tls"),
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


def test_two_logins_cannot_share_a_user_id(tmp_path):
    def edit(data):
        data["sandbox"]["users"] = {"login-a": "u-same", "login-b": "u-same"}

    with pytest.raises(ConfigError, match="same user id"):
        load(tmp_path, edit)


@pytest.mark.parametrize(("table", "key"), [("data_paths", "mounted"), ("users", "login-data")])
def test_o12_a_level_or_login_with_a_bad_value_is_named_by_position_and_never_by_itself(tmp_path, table, key):
    """O12: the key of these tables is itself a configured value (a level, a login), so the message cannot echo it."""

    def edit(data):
        data["sandbox"][table][key] = 7

    with pytest.raises(ConfigError, match=r"entry \d+: must be a non-empty string, got int") as caught:
        load(tmp_path, edit)
    assert key not in str(caught.value)
