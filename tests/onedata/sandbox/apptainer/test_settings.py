"""O56: `[sandbox] backend = "apptainer"` and `[sandbox.apptainer]`: every file pinned, the Docker backend's tables
refused, the hooks' entries checked, and a message that names the key, never a value or a path."""

import pytest
from sandbox_support import add_sandbox

from expcore.config.loading import ConfigError, Section
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.sandbox.settings import load_sandbox

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


@pytest.mark.parametrize("table", ["images", "network", "federation", "sudo", "agent_uid"])
def test_the_docker_backend_s_tables_and_keys_are_refused(tmp_path, table):
    """O56: what only the Docker backend reads is refused, never silently ignored."""
    document = _document()
    document[table] = {} if table in ("images", "network", "federation") else 1000
    with pytest.raises(ConfigError, match="Docker backend alone"):
        _load(tmp_path, document)


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
