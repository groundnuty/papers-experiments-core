"""O56: `[sandbox] backend = "apptainer"` and `[sandbox.apptainer]`: every file pinned, the Docker backend's tables
refused, the hooks' entries checked, and a message that names the key, never a value or a path."""

import pytest
from sandbox_support import add_sandbox

from expcore.config.loading import ConfigError, Section
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.sandbox.settings import FederationNetwork, load_sandbox

SHA = "a" * 64
BUNDLE = {"oneclient_bundle": "files/oneclient-bundle.tar", "oneclient_bundle_sha256": "d" * 64}
LIMITS = {"cpus": 2.0, "memory_bytes": 1 << 30, "pids": 64, "nofile": 64, "tmp_bytes": 1 << 20}
LIMITS |= {"shm_bytes": 1 << 20, "work_disk_bytes": 1 << 22}


def _document(**apptainer_changes):
    apptainer = {
        "image": "files/agent.sif",
        "image_sha256": SHA,
        "egress_shim": "files/egresslog.so",
        "egress_shim_sha256": "b" * 64,
        "egress_collector": "files/egress-collector.py",
        "egress_collector_sha256": "c" * 64,
    }
    apptainer.update(apptainer_changes)
    return {
        "backend": "apptainer",
        "work_root": "node-tmp",
        "content_check_file": "checks/content.bin",
        "limits": LIMITS,
        "data_paths": {"via-gate": "rest", "isolated": "none"},
        "apptainer": apptainer,
    }


def _load(tmp_path, document):
    return load_sandbox(Section(document, tmp_path / "platform.toml", "[sandbox]"), tmp_path)


def test_the_apptainer_backend_loads_its_pins_and_resolves_its_paths(tmp_path):
    """O56: the image and the egress logger's files are pinned, relative to the platform file; the record holds
    the hashes alone."""
    loaded = _load(tmp_path, _document())
    assert loaded.backend == "apptainer"
    assert loaded.apptainer.image.path == tmp_path / "files" / "agent.sif"
    assert loaded.agent.id == f"sha256:{SHA}"
    assert loaded.network is None
    assert loaded.mode_of("via-gate") is DataPathMode.REST
    record = loaded.apptainer.record()
    assert record["image_sha256"] == SHA
    assert str(tmp_path) not in str(record)


@pytest.mark.parametrize("table", ["images", "network", "sudo", "agent_uid"])
def test_the_docker_backend_s_tables_and_keys_are_refused(tmp_path, table):
    """O56: what only the Docker backend reads is refused, never silently ignored."""
    document = _document()
    document[table] = {} if table in ("images", "network") else 1000
    with pytest.raises(ConfigError, match="Docker backend alone"):
        _load(tmp_path, document)


# -- [sandbox.federation] on this backend: the names the gate serves the agent (O69) -----------------------------------

DECLARED_ZONE, DECLARED_PROVIDER = "zone.layout.test", "alpha.layout.test"  # invented, as a layout declares them


def _named(**changes):
    """The document with `[sandbox.federation]`'s names for the local provider `alpha`, after `changes` to the table."""
    document = _document()
    document["federation"] = {"zone": DECLARED_ZONE, "providers": {"alpha": DECLARED_PROVIDER}, **changes}
    return document


def _load_named(tmp_path, document):
    section = Section(document, tmp_path / "platform.toml", "[sandbox]")
    return load_sandbox(section, tmp_path, providers=("alpha", "beta"), local="alpha")


def test_o69_the_federation_table_gives_this_backend_the_names_its_gate_serves_and_no_network(tmp_path):
    """O69: the zone's and the local provider's names alone; there is no network to join, and without the table the
    cell is given the URLs' hosts as before."""
    loaded = _load_named(tmp_path, _named())
    assert loaded.federation == FederationNetwork(network=None, local_provider=DECLARED_PROVIDER, zone=DECLARED_ZONE)
    assert _load_named(tmp_path, _document()).federation is None


def test_o69_a_network_in_the_federation_table_is_refused_on_this_backend(tmp_path):
    """O69: the cells share the node's network, so a docker network here would be read by nothing."""
    refused = r"\[sandbox\.federation\]: key 'network' is read by the Docker backend alone"
    with pytest.raises(ConfigError, match=refused):
        _load_named(tmp_path, _named(network="fed_net"))


def test_o69_the_federation_table_without_a_rest_level_is_refused(tmp_path):
    """O69: only the REST data path's gate serves the names; the host mount dials the provider's URL."""
    document = _named()
    document["data_paths"] = {"isolated": "none"}
    with pytest.raises(ConfigError, match=r"\[sandbox\.federation\]: is read by the REST data path's gate alone"):
        _load_named(tmp_path, document)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"zone": DECLARED_PROVIDER.upper()}, "key 'zone' must differ from the local provider's name"),
        ({"providers": {"beta": DECLARED_PROVIDER}}, "entry 0: only the local provider's name is read"),
        ({"providers": {}}, "must name the local provider"),
        ({"zone": "https://zone.layout.test:443"}, "key 'zone' must be a host name"),
        ({"other": "x"}, "key 'other' is not a known key"),
    ],
)
def test_o69_the_names_are_checked_as_docker_s_are_and_are_two(tmp_path, change, message):
    """O69: the local provider's name and no other, host names without a scheme or a port, two different names (the
    gate serves one host by a name), and the message names a key or an entry, never a name."""
    with pytest.raises(ConfigError, match=message) as raised:
        _load_named(tmp_path, _named(**change))
    assert DECLARED_PROVIDER.lower() not in str(raised.value).lower()


def test_o69_the_names_are_kept_in_lower_case_as_the_gate_matches_them(tmp_path):
    """O69: the gate matches a request's Host and SNI in lower case, as a URL's host is, so a name declared in capitals
    is served all the same."""
    loaded = _load_named(tmp_path, _named(zone=DECLARED_ZONE.upper(), providers={"alpha": DECLARED_PROVIDER.title()}))
    assert loaded.federation == FederationNetwork(network=None, local_provider=DECLARED_PROVIDER, zone=DECLARED_ZONE)


def test_o69_the_federation_table_needs_the_zone_s_name(tmp_path):
    """O69: a REST data path's gate serves the zone too, so its name is required."""
    document = _named()
    del document["federation"]["zone"]
    with pytest.raises(ConfigError, match="zone"):
        _load_named(tmp_path, document)


def test_a_pin_that_is_not_a_sha256_is_refused_naming_the_key(tmp_path):
    with pytest.raises(ConfigError, match="image_sha256"):
        _load(tmp_path, _document(image_sha256="sha256:abc"))


def test_gate_options_may_not_set_what_the_sandbox_writes(tmp_path):
    """O56, hook (a): the gate's options add keys; one the sandbox writes itself (its token file, say) is refused."""
    with pytest.raises(ConfigError, match="token_file"):
        _load(tmp_path, _document(gate_options={"token_file": "/elsewhere"}))
    loaded = _load(tmp_path, _document(gate_options={"peer_check": True}))
    assert dict(loaded.apptainer.gate_options) == {"peer_check": True}


def test_a_bind_onto_one_of_the_sandbox_s_own_paths_is_refused(tmp_path):
    """O56, hook (b): an extra bind may not cover the cell's /work, its preload list or the egress socket."""
    for target in ("/work/x", "/etc/ld.so.preload", "/hpcop-egress/sock"):
        with pytest.raises(ConfigError, match="entry 0"):
            _load(tmp_path, _document(binds=[{"path": "f", "sha256": SHA, "target": target}]))


def test_a_preload_entry_is_pinned_and_kept_in_order(tmp_path):
    """O56, hook (b): each library is pinned; the order is the loader's."""
    loaded = _load(tmp_path, _document(preload=[{"path": "a.so", "sha256": SHA}, {"path": "b.so", "sha256": "d" * 64}]))
    assert [p.path.name for p in loaded.apptainer.preload] == ["a.so", "b.so"]
    assert loaded.apptainer.record()["preload_sha256"] == [SHA, "d" * 64]


def test_an_unknown_backend_is_refused(tmp_path):
    document = _document()
    document["backend"] = "podman"
    with pytest.raises(ConfigError, match="backend"):
        _load(tmp_path, document)


def test_a_mount_level_needs_the_mount_s_path(tmp_path):
    """O56: a mount level loads with `[sandbox.mount]`, and its start is refused until hook (c) lands."""
    document = _document()
    document["data_paths"]["mounted"] = "mount"
    with pytest.raises(ConfigError, match="mount"):
        _load(tmp_path, document)
    document["mount"] = {"path": "/data", "io_mode": "proxy"}
    document["apptainer"] |= BUNDLE
    assert _load(tmp_path, document).mount.path == "/data"


def test_the_period_of_the_memory_watch_is_bounded(tmp_path):
    with pytest.raises(ConfigError, match="memory_poll_s"):
        _load(tmp_path, _document(memory_poll_s=5))


def test_o56_the_margin_of_hidden_memory_is_a_bounded_setting_with_a_default(tmp_path):
    """O56: `hidden_memory_margin_bytes` defaults to 128 MiB and is left out of the record (so a fingerprint made
    before it existed is unchanged); set, it is bounded and recorded."""
    loaded = _load(tmp_path, _document())
    assert loaded.apptainer.hidden_memory_margin_bytes == 128 << 20
    assert "hidden_memory_margin_bytes" not in loaded.apptainer.record()
    loaded = _load(tmp_path, _document(hidden_memory_margin_bytes=256 << 20))
    assert loaded.apptainer.record()["hidden_memory_margin_bytes"] == 256 << 20
    for bad in (0, 1024, 1 << 40, 1.5, True, "128MiB"):
        with pytest.raises(ConfigError, match="hidden_memory_margin_bytes"):
            _load(tmp_path, _document(hidden_memory_margin_bytes=bad))


def test_o56_a_hook_bind_may_not_give_the_cell_a_name_server(tmp_path):
    """O56: the cell's resolv.conf and nsswitch.conf are the sandbox's own (dns_none): a hook (b) bind onto either is
    refused."""
    for target in ("/etc/resolv.conf", "/etc/nsswitch.conf"):
        with pytest.raises(ConfigError, match="entry 0"):
            _load(tmp_path, _document(binds=[{"path": "f", "sha256": SHA, "target": target}]))


def test_the_campaign_fingerprint_holds_the_backend_s_pins_and_docker_s_is_unchanged(tmp_path, fake):
    """O56: a Docker and an Apptainer run are never one experiment; a Docker platform's fingerprint keeps its shape."""
    from expcore_onedata.config import load_platform
    from expcore_onedata.testing import write_platform

    def apptainer(data):
        data["sandbox"] = _document()

    (tmp_path / "a").mkdir()
    (tmp_path / "d").mkdir()
    loaded = load_platform(write_platform(tmp_path / "a", apptainer))
    assert loaded.fingerprint()["sandbox_backend"]["image_sha256"] == SHA
    docker = load_platform(write_platform(tmp_path / "d", add_sandbox))
    assert "sandbox_backend" not in docker.fingerprint()


def test_the_platform_builds_the_apptainer_sandbox_and_its_probe(tmp_path, fake):
    """O56: `SandboxPlatform` picks the backend's class and probe from `[sandbox] backend`."""
    from fake_node import FakeNode

    from expcore_onedata.config import load_platform
    from expcore_onedata.sandbox.apptainer.runs import ApptainerProbe
    from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
    from expcore_onedata.sandbox.provider import SandboxPlatform
    from expcore_onedata.testing import write_platform

    def apptainer(data):
        data["sandbox"] = _document()

    config = load_platform(write_platform(tmp_path, apptainer))
    platform = SandboxPlatform(config, issuer=None, host=FakeNode(), events=lambda *a, **k: None)
    assert isinstance(platform.sandbox_probe(), ApptainerProbe)
    assert isinstance(platform._prepare("run-key-1", "isolated", None, None, None, 600.0), ApptainerSandbox)


def test_o75_the_platform_reads_a_runs_data_path_from_the_factor_the_study_names(tmp_path, fake):
    """O75: `SandboxPlatform` maps the level of the study's data-path factor, `data_path` unless it is given the name
    of another (`[study] data_path_factor`), and refuses a name that is not one."""
    from fake_node import FakeNode

    from expcore_onedata.config import load_platform
    from expcore_onedata.sandbox.api import DataPathMode
    from expcore_onedata.sandbox.provider import DATA_PATH_FACTOR, SandboxPlatform
    from expcore_onedata.testing import write_platform

    def apptainer(data):
        data["sandbox"] = _document()

    config = load_platform(write_platform(tmp_path, apptainer))

    def built(**more):
        return SandboxPlatform(config, issuer=None, host=FakeNode(), events=lambda *a, **k: None, **more)

    default, named = built(), built(data_path_factor="path")
    assert DATA_PATH_FACTOR == "data_path"
    assert (default.data_path_factor, named.data_path_factor) == ("data_path", "path")
    assert default.mode({"data_path": "isolated"}) is DataPathMode.NONE
    assert named.mode({"path": "isolated"}) is DataPathMode.NONE
    with pytest.raises(KeyError):
        named.mode({"data_path": "isolated"})  # the name it was given is the only one it reads
    for bad in ("", " ", None, 3):
        with pytest.raises(ValueError, match="data_path_factor"):
            built(data_path_factor=bad)


def _mounting(**apptainer_changes):
    document = _document(**apptainer_changes)
    document["data_paths"]["mounted"] = "mount"
    document["mount"] = {"path": "/data", "io_mode": "proxy"}
    return document


def test_oneclient_s_bundle_is_a_pin_of_the_backend_resolved_like_the_others(tmp_path):
    """The host mount's oneclient is declared beside the SIF and the shim, pinned, never found on the node."""
    loaded = _load(tmp_path, _mounting(**BUNDLE))
    assert loaded.apptainer.oneclient_bundle.path == tmp_path / "files" / "oneclient-bundle.tar"
    assert loaded.apptainer.oneclient_bundle.sha256 == "d" * 64
    record = loaded.apptainer.record()
    assert record["oneclient_bundle_sha256"] == "d" * 64
    assert str(tmp_path) not in str(record)


def test_a_mount_level_on_the_apptainer_backend_without_the_bundle_is_refused_at_load(tmp_path):
    """O65: a mount level's oneclient is declared and pinned, or the platform does not load."""
    with pytest.raises(ConfigError, match="oneclient_bundle"):
        _load(tmp_path, _mounting())


@pytest.mark.parametrize("key", ["oneclient_bundle", "oneclient_bundle_sha256"])
def test_a_bundle_declared_by_half_is_refused_naming_the_missing_key(tmp_path, key):
    with pytest.raises(ConfigError, match=key):
        _load(tmp_path, _mounting(**{k: v for k, v in BUNDLE.items() if k != key}))


def test_a_platform_without_the_bundle_keeps_the_record_it_had(tmp_path):
    """O65: a REST-only Apptainer platform declares no bundle: its fingerprint is the one it had before the key."""
    record = _load(tmp_path, _document()).apptainer.record()
    assert "oneclient_bundle_sha256" not in record
    assert set(record) == {"backend", "image_sha256", "egress_shim_sha256", "egress_collector_sha256",
                           "memory_poll_s", "disk_poll_s", "preload_sha256", "binds", "gate_options"}  # fmt: skip


def test_two_oneclient_bundles_give_two_campaign_fingerprints(tmp_path, fake):
    """O65: HPC mount runs on another oneclient are another experiment: a resume or a merge across them is refused."""
    from expcore_onedata.config import load_platform
    from expcore_onedata.testing import write_platform

    prints = []
    for digest in ("d" * 64, "e" * 64):
        directory = tmp_path / digest[0]
        directory.mkdir()

        def apptainer(data, digest=digest):
            data["sandbox"] = _mounting(**{**BUNDLE, "oneclient_bundle_sha256": digest})

        prints.append(load_platform(write_platform(directory, apptainer)).fingerprint())
    assert prints[0] != prints[1]
    assert prints[0]["sandbox_backend"]["oneclient_bundle_sha256"] == "d" * 64
    assert prints[1]["sandbox_backend"]["oneclient_bundle_sha256"] == "e" * 64
    assert {**prints[1], "sandbox_backend": {**prints[1]["sandbox_backend"], "oneclient_bundle_sha256": "d" * 64}} == (
        prints[0]
    )


KINDS = ("tmpfs", "shared_filesystem")


def test_o57_the_providers_storage_kind_is_declared_validated_and_recorded(tmp_path):
    """O57: `provider_storage` says where the federation's providers keep their data, tmpfs or the shared filesystem:
    the record holds the word, and a word that is neither is refused naming the key and never the value."""
    for kind in KINDS:
        loaded = _load(tmp_path, _document(provider_storage=kind))
        assert loaded.apptainer.provider_storage == kind
        assert loaded.apptainer.record()["provider_storage"] == kind
    for bad in ("scratch", "TMPFS", "", 1, True, ["tmpfs"]):
        with pytest.raises(ConfigError, match="provider_storage") as refused:
            _load(tmp_path, _document(provider_storage=bad))
        if isinstance(bad, str) and bad:
            assert bad not in str(refused.value)


def test_o57_two_layouts_differing_only_in_the_storage_kind_give_two_fingerprints(tmp_path, fake):
    """O57: where the providers keep their data shapes a run's I/O timing, so a tmpfs federation and a scratch one are
    two experiments: the kind is in the campaign fingerprint, and it is the only thing that tells these two apart."""
    from expcore_onedata.config import load_platform
    from expcore_onedata.testing import write_platform

    prints = {}
    for kind in KINDS:
        directory = tmp_path / kind
        directory.mkdir()

        def apptainer(data, kind=kind):
            data["sandbox"] = _document(provider_storage=kind)

        prints[kind] = load_platform(write_platform(directory, apptainer)).fingerprint()
    tmpfs, shared = prints["tmpfs"], prints["shared_filesystem"]
    assert tmpfs != shared
    assert tmpfs["sandbox_backend"]["provider_storage"] == "tmpfs"
    assert shared["sandbox_backend"]["provider_storage"] == "shared_filesystem"
    assert {**shared, "sandbox_backend": {**shared["sandbox_backend"], "provider_storage": "tmpfs"}} == tmpfs


def test_o57_a_platform_that_declares_no_storage_kind_keeps_the_fingerprint_it_had(tmp_path, fake):
    """O57: a Compose federation (the Docker backend) has no such setting, and an Apptainer platform that declares none
    leaves the key out of the record: neither fingerprint changes, so a store started before the key resumes."""
    import json

    from expcore_onedata.config import load_platform
    from expcore_onedata.testing import write_platform

    (tmp_path / "a").mkdir()
    (tmp_path / "d").mkdir()

    def apptainer(data):
        data["sandbox"] = _document()

    undeclared = load_platform(write_platform(tmp_path / "a", apptainer)).fingerprint()
    docker = load_platform(write_platform(tmp_path / "d", add_sandbox)).fingerprint()
    assert "provider_storage" not in undeclared["sandbox_backend"]
    assert {"tls", "data_paths"} <= set(docker)  # what the Docker backend's fingerprint held before the key existed
    assert "sandbox_backend" not in docker
    assert "provider_storage" not in json.dumps(docker)  # nothing the key adds reaches a Compose federation's


def test_o71_work_disk_defaults_to_the_directory_and_is_recorded_only_when_it_is_the_image(tmp_path):
    """O71: `work_disk` is "directory" (the watch alone) unless set; the record names it only when it is "image", so
    a fingerprint made before the key existed is unchanged, while an image run is another experiment."""
    loaded = _load(tmp_path, _document())
    assert loaded.apptainer.work_disk == "directory"
    assert loaded.apptainer.work_disk_mkfs == ("mkfs.ext3",)
    assert "work_disk" not in loaded.apptainer.record()
    assert _load(tmp_path, _document(work_disk="directory")).apptainer.record() == loaded.apptainer.record()
    image = _load(tmp_path, _document(work_disk="image", work_disk_mkfs=["/usr/sbin/mkfs.ext3"])).apptainer
    assert image.work_disk == "image"
    assert image.work_disk_mkfs == ("/usr/sbin/mkfs.ext3",)
    assert image.record() == {**loaded.apptainer.record(), "work_disk": "image"}  # the tool is no experiment


@pytest.mark.parametrize(("key", "bad"), [("work_disk", "loop"), ("work_disk", 1), ("work_disk_mkfs", ["mkfs ext3"]),
                                          ("work_disk_mkfs", "mkfs.ext3")])  # fmt: skip
def test_o71_a_work_disk_setting_that_is_not_one_is_refused_naming_its_key(tmp_path, key, bad):
    """O71: the mode is one of the two, and mkfs plain command words."""
    with pytest.raises(ConfigError, match=key):
        _load(tmp_path, _document(**{key: bad}))


@pytest.mark.parametrize("bad", [["mke2fs", "-t", "ext4"], ["mkfs.ext3", "-m0"], ["-t"], ["--help"], [], [""]])
def test_o71_work_disk_mkfs_is_one_program_and_never_an_option(tmp_path, bad):
    """O71: the file system's type and options shape the image, so the setting is the tool alone, one program word, and
    never what a study could use to change them: an option-bearing value, a second word or none is refused at load,
    naming the key. It is out of the fingerprint (C06) because it can name nothing but where the node's mkfs is."""
    with pytest.raises(ConfigError, match="work_disk_mkfs"):
        _load(tmp_path, _document(work_disk="image", work_disk_mkfs=bad))


@pytest.mark.parametrize(
    "program",
    [
        "mkfs.ext3",
        "mke2fs",
        "mkfs.ext4",
        "/usr/sbin/mkfs.ext3",
        "/sbin/mke2fs",
        "/opt/site/e2fsprogs-1.47/sbin/mkfs.ext3",
    ],
)
def test_o71_work_disk_mkfs_takes_a_program_by_name_or_by_absolute_path(tmp_path, program):
    """O71: a bare program name or an absolute path loads, and is kept as the one word."""
    loaded = _load(tmp_path, _document(work_disk="image", work_disk_mkfs=[program])).apptainer
    assert loaded.work_disk_mkfs == (program,)


@pytest.mark.parametrize("words", [("mke2fs", "-t", "ext4"), ("-t",), (), ("a b",)])
def test_o71_the_settings_refuse_a_work_disk_mkfs_that_is_not_one_program_word(tmp_path, words):
    """O71: the check is the settings' own, so a platform built in code cannot pass an option either."""
    from apptainer_support import make_settings

    with pytest.raises(ValueError, match="work_disk_mkfs"):
        make_settings(tmp_path, work_disk_mkfs=words)


NOT_A_NAME_OR_ABSOLUTE_PATH = [
    ".", "..", "/", "./mkfs.ext3", "../mkfs.ext3", "sbin/mkfs.ext3", "a/b", "mkfs.ext3/",  # relative: not a program
    "/usr/sbin/", "/usr/../bin/mkfs.ext3", "/usr/./sbin/mkfs.ext3", "//mkfs.ext3", "/usr//sbin/mkfs.ext3",  # not normal
]  # fmt: skip


@pytest.mark.parametrize("bad", NOT_A_NAME_OR_ABSOLUTE_PATH)
def test_o71_work_disk_mkfs_is_a_bare_program_name_or_an_absolute_path_in_normal_form(tmp_path, bad):
    """O71: a program the node finds by its name on its `PATH`, or by an absolute path: never one relative to the
    directory the driver happens to run in (`./x`, `../x`, `a/b`), never a directory or a name that is none (`.`, `..`,
    `/`), and an absolute path has no empty, `.` or `..` part, since each of those names another file than it shows.
    Refused at load naming the key, and by the settings themselves (a platform built in code)."""
    with pytest.raises(ConfigError, match="work_disk_mkfs"):
        _load(tmp_path, _document(work_disk="image", work_disk_mkfs=[bad]))
    from apptainer_support import make_settings

    with pytest.raises(ValueError, match="work_disk_mkfs"):
        make_settings(tmp_path, work_disk_mkfs=(bad,))


def test_o87_the_apptainer_backend_reads_the_check_provider_as_the_docker_backend_does(tmp_path):
    """O87: `content_check_provider` is a `[sandbox]` key of both backends: a layout name, kept for the platform's
    load to check against its providers and the space; unset it is None, the agent's local provider."""
    assert _load(tmp_path, _document()).content_check_provider is None
    assert _load(tmp_path, {**_document(), "content_check_provider": "owner"}).content_check_provider == "owner"


@pytest.mark.parametrize("value", [5, "", "with space", ["a"]])
def test_o87_a_check_provider_that_is_no_name_is_refused_naming_the_key(tmp_path, value):
    with pytest.raises(ConfigError, match="content_check_provider"):
        _load(tmp_path, {**_document(), "content_check_provider": value})
