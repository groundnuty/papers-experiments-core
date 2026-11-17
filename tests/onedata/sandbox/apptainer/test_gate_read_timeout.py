"""O85: on the Apptainer backend the declared `[sandbox.gate] upstream_read_timeout_s` reaches the gate's configuration
through `gaterun.config`, which no `gate_options` can override, and is recorded in the REST run's
`infrastructure_facts`. The setting's validation and the gate's use of it are `../test_gate_read_timeout.py`'s."""

import dataclasses
from pathlib import Path

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import FakeNode

from expcore.config.loading import Section
from expcore_onedata.sandbox.apptainer import gaterun
from expcore_onedata.sandbox.apptainer.settings import GATE_OWN_KEYS
from expcore_onedata.sandbox.settings import load_sandbox
from expcore_onedata.tls import TlsSettings

KEY = "upstream_read_timeout_s"
FACT = "gate_upstream_read_timeout_s"  # the run's `infrastructure_facts` key
LIMITS = {"cpus": 2.0, "memory_bytes": 1 << 30, "pids": 64, "nofile": 64, "tmp_bytes": 1 << 20}
LIMITS |= {"shm_bytes": 1 << 20, "work_disk_bytes": 1 << 22}


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


def _arguments(**changes):
    base = {
        "run_key": "r", "tls": TlsSettings(), "served": {}, "listing": "paged", "directory": Path("/x"),
        "tls_dir": Path("/y"), "token_file": Path("/z"), "port": 1, "upstream_ca": None, "options": {},
    }  # fmt: skip
    return {**base, **changes}


def _document():
    """A minimal Apptainer `[sandbox]` document with a REST level."""
    return {
        "backend": "apptainer",
        "work_root": "node-tmp",
        "content_check_file": "checks/content.bin",
        "limits": LIMITS,
        "data_paths": {"via-gate": "rest", "isolated": "none"},
        "apptainer": {
            "image": "files/agent.sif", "image_sha256": "a" * 64,
            "egress_shim": "files/egresslog.so", "egress_shim_sha256": "b" * 64,
            "egress_collector": "files/egress-collector.py", "egress_collector_sha256": "c" * 64,
        },
    }  # fmt: skip


def _load(tmp_path, document):
    return load_sandbox(Section(document, tmp_path / "platform.toml", "[sandbox]"), tmp_path)


def test_o85_the_gates_configuration_carries_the_value_it_is_given_and_takes_it_with_no_default():
    """O85: `gaterun.config` writes the value it is given, the default's included, and takes it by keyword with no
    default of its own, so a caller that forgets it fails instead of leaving a backend on the constant."""
    assert gaterun.config(**_arguments(), upstream_read_timeout_s=14400.0)[KEY] == 14400.0
    assert gaterun.config(**_arguments(), upstream_read_timeout_s=600.0)[KEY] == 600.0
    with pytest.raises(TypeError):
        gaterun.config(**_arguments())


def test_o85_a_gate_option_cannot_set_the_value_the_sandbox_writes():
    """O85: `[sandbox.apptainer.gate_options]` may not name the key the declared value is written to, where the
    configuration is made and, below, where the settings load."""
    assert KEY in GATE_OWN_KEYS
    with pytest.raises(Exception, match=KEY):
        gaterun.config(**_arguments(options={KEY: 5}), upstream_read_timeout_s=14400.0)


@pytest.mark.parametrize("declared", [None, 14400.0, 600.0, 2.5])
def test_o85_the_apptainer_gate_is_given_the_effective_value_and_the_run_records_it(node, tmp_path, declared):
    """O85: the configuration the node's gate starts with carries the value (the default when none is declared), and
    the run's `infrastructure_facts` records the same number; a run with no gate has no such fact."""
    settings = make_settings(tmp_path)
    if declared is not None:
        settings = dataclasses.replace(settings, gate_upstream_read_timeout_s=declared)
    effective = 14400.0 if declared is None else declared
    box = start(node, settings)
    try:
        assert node.gates[-1].config[KEY] == effective
        assert box.metadata()["infrastructure_facts"][FACT] == effective
    finally:
        box.stop()
    isolated = start(node, settings, make_spec("isolated"))
    try:
        assert FACT not in isolated.metadata()["infrastructure_facts"]
    finally:
        isolated.stop()


def test_o85_the_declared_table_loads_on_this_backend_and_a_gate_option_of_the_key_is_refused(tmp_path):
    """O85: `[sandbox.gate]` is read on the Apptainer backend as on Docker's, and is the one place the value is set."""
    document = _document()
    document["gate"] = {KEY: 900}
    assert _load(tmp_path, document).gate_upstream_read_timeout_s == 900.0
    document["apptainer"]["gate_options"] = {KEY: 900}
    with pytest.raises(Exception, match=KEY):
        _load(tmp_path, document)
    assert _load(tmp_path, _document()).gate_upstream_read_timeout_s == 14400.0
