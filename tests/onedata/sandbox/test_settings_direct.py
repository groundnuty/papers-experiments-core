"""The direct I/O mount of `[sandbox.mount]`: the declared storage mapping and the check file (O40).

`io_mode = "direct"` reads the local provider's storage itself, so the mount declares each storage the sidecar reads,
by its id, with the directory that holds it on the host (`[sandbox.mount.storages]`), and a file the local provider
holds in full that the start reads to show direct I/O in effect (`direct_check_file`). Both are required with
`"direct"` and refused with `"proxy"`; every value is a configured one, named by its position, never echoed.
"""

import dataclasses

import pytest
from sandbox_support import add_sandbox

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox.oneclient_args import mount_problem
from expcore_onedata.sandbox.settings import MountSettings
from expcore_onedata.testing import write_platform

STORAGE_A = "0123456789abcdef0123456789abcdef"  # invented storage ids, as Onedata writes them
STORAGE_B = "fedcba9876543210fedcba9876543210"
CHECK = "space-invented/checks/direct.bin"  # an invented space's directory, then a path in it


def load(tmp_path, edit):
    def both(data):
        add_sandbox(data)
        edit(data)

    return load_platform(write_platform(tmp_path, both), require_secrets=False).sandbox


def direct(storages=None, check=CHECK):
    """An edit that declares a direct mount over invented storage directories (absolute, outside the work root)."""

    def edit(data):
        mount = data["sandbox"]["mount"]
        mount["io_mode"] = "direct"
        if storages is not None:
            mount["storages"] = storages
        if check is not None:
            mount["direct_check_file"] = check

    return edit


def test_o40_a_direct_mount_reads_its_storage_mapping_and_check_file(tmp_path):
    """O40: each storage id maps to the host directory that holds it, in the file's order (by which a message or a
    record names an entry), and the check file is read as declared."""
    sandbox = load(tmp_path, direct({STORAGE_B: "/srv/invented/b", STORAGE_A: "/srv/invented/a"}))
    assert sandbox.mount.io_mode == "direct"
    assert sandbox.mount.storages == ((STORAGE_B, "/srv/invented/b"), (STORAGE_A, "/srv/invented/a"))
    assert sandbox.mount.direct_check_file == CHECK


def test_o40_a_direct_mount_without_its_storage_mapping_is_refused_naming_what_it_needs(tmp_path):
    """O40: direct I/O reads the storage itself, and a sidecar without the storage could not read a file: the load
    refuses it, naming the table and the check file, and that proxy needs neither."""
    with pytest.raises(ConfigError, match="io_mode") as caught:
        load(tmp_path, direct(None))
    message = str(caught.value)
    assert "[sandbox.mount.storages]" in message
    assert "direct_check_file" in message
    assert '"proxy"' in message


def test_o40_a_direct_mount_with_an_empty_mapping_is_refused(tmp_path):
    """O40: an empty table maps no storage, so the sidecar could read nothing directly."""
    with pytest.raises(ConfigError, match="storages"):
        load(tmp_path, direct({}))


def test_o40_a_direct_mount_without_its_check_file_is_refused(tmp_path):
    """O40: the start proves direct I/O by reading the check file; without one it could prove nothing."""
    with pytest.raises(ConfigError, match="direct_check_file"):
        load(tmp_path, direct({STORAGE_A: "/srv/invented/a"}, check=None))


@pytest.mark.parametrize("key", ["storages", "direct_check_file"])
def test_o40_a_proxy_mount_refuses_the_direct_keys_so_a_mapping_never_does_nothing(tmp_path, key):
    """O40: a proxy mount reads every space through the provider and never the storage, so a mapping or a check file
    there would do nothing: refused at load."""

    def edit(data):
        data["sandbox"]["mount"].update(storages={STORAGE_A: "/srv/invented/a"}, direct_check_file=CHECK)
        del data["sandbox"]["mount"]["storages" if key == "direct_check_file" else "direct_check_file"]

    with pytest.raises(ConfigError, match=key) as caught:
        load(tmp_path, edit)
    assert '"direct"' in str(caught.value)


@pytest.mark.parametrize("storage_id", ["-leading-dash", "a" * 256])
def test_o40_o12_a_storage_id_oneclient_could_not_take_is_refused_by_position(tmp_path, storage_id):
    """O40, O12: a bad id in the file is named by its position in the table and never echoed."""
    with pytest.raises(ConfigError, match="entry 1") as caught:
        load(tmp_path, direct({STORAGE_A: "/srv/invented/a", storage_id: "/srv/invented/b"}))
    assert storage_id not in str(caught.value)


@pytest.mark.parametrize("storage_id", ["", "with:colon", "with space", "slash/inside", "new\nline", 7])
def test_o40_a_storage_id_with_a_character_an_override_or_an_argument_would_read_is_refused(storage_id):
    """O40: oneclient splits an override at every ':' (`options.h`, `getOverrideParams`), so an id is letters, digits,
    '.', '_' and '-' (the file's renderer here writes bare keys, so these are checked where every form is checked)."""
    found = mount_problem("direct", (), ((STORAGE_A, "/srv/a"), (storage_id, "/srv/b")), CHECK)
    assert "entry 1" in found
    assert "storages" in found


@pytest.mark.parametrize(
    "path",
    [
        "relative/dir",
        "/",
        "/srv/../etc",
        "/srv/./invented",
        "/srv//invented",
        "/srv/invented/",
        "/srv/with,comma",
        '/srv/with"quote',
        "/srv/new\nline",
        "",
    ],
)
def test_o40_o12_a_host_path_that_is_not_one_plain_absolute_directory_is_refused_by_position(tmp_path, path):
    """O40, O12: the path is bound with `--mount`, where a comma starts the next field and a quote opens one, and it
    must name one directory as written: absolute, normalised, not the root. Named by position, never echoed."""
    with pytest.raises(ConfigError, match="entry 0") as caught:
        load(tmp_path, direct({STORAGE_A: path}))
    if path not in ("", "/"):
        assert path not in str(caught.value)


def test_o40_a_host_path_that_is_not_a_string_is_refused_by_position(tmp_path):
    """O40: a host path is a string, named by its entry's position when it is not."""
    with pytest.raises(ConfigError, match="entry 0"):
        load(tmp_path, direct({STORAGE_A: 7}))


@pytest.mark.parametrize(("a", "b"), [("/srv/one", "/srv/one/two"), ("/srv/one/two", "/srv/one")])
def test_o40_two_storages_whose_directories_nest_are_refused(tmp_path, a, b):
    """O40: a directory that holds another storage's would bind that storage's files under the first one's path."""
    with pytest.raises(ConfigError, match="entries 0 and 1"):
        load(tmp_path, direct({STORAGE_A: a, STORAGE_B: b}))


def test_o40_two_storages_on_one_directory_are_refused(tmp_path):
    """O40: two storages on one directory would read each other's files."""
    with pytest.raises(ConfigError, match="entries 0 and 1"):
        load(tmp_path, direct({STORAGE_A: "/srv/one", STORAGE_B: "/srv/one"}))


@pytest.mark.parametrize("inside", ["work", "work/sub", "."])
def test_o40_a_storage_inside_or_around_the_work_root_is_refused(tmp_path, inside):
    """O40: the sandbox makes and removes directories under its work root, so a provider's storage there could lose
    its files to a teardown; a storage that holds the work root is refused as well."""
    path = str((tmp_path / inside).resolve())
    with pytest.raises(ConfigError, match="work_root"):
        load(tmp_path, direct({STORAGE_A: path}))


@pytest.mark.parametrize("check", ["one-part", "space/../x", "space//x", "/space/x", "space/./x", 7, ""])
def test_o40_a_check_file_that_is_not_a_path_in_a_space_is_refused(tmp_path, check):
    """O40: the check file is a space's directory under the mount, then a path in it: plain names, at least two."""
    with pytest.raises(ConfigError, match="direct_check_file"):
        load(tmp_path, direct({STORAGE_A: "/srv/invented/a"}, check=check))


def test_o40_settings_built_in_code_are_held_to_what_the_loader_holds_a_file_to():
    """O40: a direct mount built in code without a mapping, or a proxy one with one, raises where it is built, so the
    sidecar is never started with direct I/O and no storage."""
    with pytest.raises(ValueError, match="storages"):
        MountSettings("/data", "direct", direct_check_file=CHECK)
    with pytest.raises(ValueError, match="direct_check_file"):
        MountSettings("/data", "direct", storages=((STORAGE_A, "/srv/a"),))
    with pytest.raises(ValueError, match="storages"):
        MountSettings("/data", "proxy", storages=((STORAGE_A, "/srv/a"),))
    with pytest.raises(ValueError, match="entry 0"):
        MountSettings("/data", "direct", storages=(("bad:id", "/srv/a"),), direct_check_file=CHECK)
    proxy = MountSettings("/data", "proxy")
    with pytest.raises(ValueError, match="storages"):
        dataclasses.replace(proxy, io_mode="direct")


def test_o40_the_fingerprint_holds_the_io_mode_and_no_storage_value(tmp_path):
    """O40, C06, O12: the campaign fingerprint holds a direct mount level's I/O mode, as for proxy, and neither a
    storage id nor a host path: those name the deployment, which the platform file's hash covers."""
    config = load_platform(
        write_platform(tmp_path, lambda d: (add_sandbox(d), direct({STORAGE_A: "/srv/invented/a"})(d))),
        require_secrets=False,
    )
    fingerprint = config.fingerprint()
    assert fingerprint["data_paths"]["mounted"] == {"kind": "mount", "io_mode": "direct"}
    text = repr(fingerprint)
    assert STORAGE_A not in text
    assert "/srv/invented/a" not in text
