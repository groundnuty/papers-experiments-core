"""`[sandbox] readlog_collector_sha256` (O91): the pin of the read-log collector, 64 lower-case hex digits, which turns
the channel on for every run of the Docker backend. Without it a run has no channel and the record is what it was."""

import json
import os

import pytest
from sandbox_support import add_sandbox

from expcore.config.loading import ConfigError, Section
from expcore_onedata.config import load_platform
from expcore_onedata.runsettings import sandbox_record
from expcore_onedata.sandbox.settings import load_sandbox
from expcore_onedata.testing import write_platform

PIN = "ab" * 32
LIMITS = {"cpus": 2.0, "memory_bytes": 1 << 30, "pids": 64, "nofile": 64, "tmp_bytes": 1 << 20}
LIMITS |= {"shm_bytes": 1 << 20, "work_disk_bytes": 1 << 22}


def load(tmp_path, **sandbox):
    tmp_path.mkdir(parents=True, exist_ok=True)

    def edit(data):
        add_sandbox(data)
        data["sandbox"].update(sandbox)

    return load_platform(write_platform(tmp_path, edit), require_secrets=False).sandbox


def test_a_platform_that_does_not_declare_the_pin_has_no_channel(tmp_path):
    assert load(tmp_path).readlog_collector_sha256 is None


def test_a_declared_pin_is_loaded_whole(tmp_path):
    assert load(tmp_path, readlog_collector_sha256=PIN).readlog_collector_sha256 == PIN


def test_o91_a_docker_platform_that_pins_the_collector_loads_with_a_work_root_of_any_length(tmp_path):
    """O91: the Apptainer backend bounds the work root where it pins the collector, because that collector binds a Unix
    socket under it (107 bytes). Docker's collector binds a path inside its container, so the work root is only the
    source of a bind and no length of it is refused."""
    settings = load(tmp_path, readlog_collector_sha256=PIN, work_root="w" * 200)
    assert len(os.fsencode(settings.work_root)) > 107
    assert settings.readlog_collector_sha256 == PIN


@pytest.mark.parametrize("bad", ["A" * 64, "a" * 63, "a" * 65, "", "sha256:" + "a" * 64, 7, ["a" * 64]])
def test_a_pin_that_is_not_64_lower_case_hex_digits_is_refused_by_key_and_never_echoed(tmp_path, bad):
    with pytest.raises(ConfigError, match="readlog_collector_sha256") as refused:
        load(tmp_path, readlog_collector_sha256=bad)
    assert len(str(bad)) < 8 or str(bad) not in str(refused.value)  # a value is never echoed
    assert "64 lower-case hexadecimal digits" in str(refused.value)


def test_the_apptainer_backend_takes_its_pin_in_its_own_table_and_refuses_the_dockers_key(tmp_path):
    document = {
        "backend": "apptainer",
        "work_root": "node-tmp",
        "content_check_file": "checks/content.bin",
        "limits": LIMITS,
        "data_paths": {"via-gate": "rest", "isolated": "none"},
        "readlog_collector_sha256": PIN,
        "apptainer": {
            "image": "files/agent.sif",
            "image_sha256": "a" * 64,
            "egress_shim": "files/egresslog.so",
            "egress_shim_sha256": "b" * 64,
            "egress_collector": "files/egress-collector.py",
            "egress_collector_sha256": "c" * 64,
        },
    }
    with pytest.raises(ConfigError, match="Docker backend alone"):
        load_sandbox(Section(document, tmp_path / "platform.toml", "[sandbox]"), tmp_path)


def test_without_the_pin_the_record_of_the_run_is_byte_equal_to_what_it_was(tmp_path):
    """A setting at its default and one left out are one experiment: a platform without the channel keeps its record."""
    plain = sandbox_record(load(tmp_path), [])
    assert "readlog" not in json.dumps(plain)
    assert json.dumps(plain, sort_keys=True) == json.dumps(sandbox_record(load(tmp_path / "again"), []), sort_keys=True)


def test_another_collector_is_another_experiment_and_the_record_says_which(tmp_path):
    plain = sandbox_record(load(tmp_path), [])
    pinned = sandbox_record(load(tmp_path / "pinned", readlog_collector_sha256=PIN), [])
    assert pinned == {**plain, "readlog_collector_sha256": PIN}
    other = sandbox_record(load(tmp_path / "other", readlog_collector_sha256="cd" * 32), [])
    assert other != pinned
