"""O87: the node's facts step of the HPC federation driver (`bundle/nodefacts.py`: stdlib only, Python 3.9, run with -I
on the node) also reads the node's OS release, glibc version and kernel release, as text, never executed, and gives
`unknown` with a reason for one it cannot read. The sandbox's own reader (`sandbox/apptainer/hostfacts.py`) reads the
same three: the two must agree on every input, since a reader compares the runs' records with the state's."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from expcore_onedata.sandbox.apptainer import hostfacts

BUNDLE = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "bundle"
GLIBC = "glibc 2.34"
KERNEL = "5.14.0-invented.el9.x86_64"


@pytest.fixture(scope="module")
def nodefacts():
    spec = importlib.util.spec_from_file_location("nodefacts_under_test", BUNDLE / "nodefacts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def raising(error):
    def fail(*_):
        raise error

    return fail


def machine(tmp_path, text: str | bytes | None, **more):
    """The three sources of an invented machine; `text` None is an os-release file that is not there."""
    path = tmp_path / "os-release"
    if text is not None:
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return {
        "os_release": path,
        "confstr": lambda name: GLIBC,
        "uname": lambda: SimpleNamespace(release=KERNEL),
        **more,
    }


INPUTS = [
    pytest.param('ID=invented\nVERSION_ID="9.4"\n', {}, id="plain"),
    pytest.param("ID='rocky'\nVERSION_ID=8.10\nID=last\n", {}, id="quotes-and-last-wins"),
    pytest.param("ID=$(touch x)\nVERSION_ID=`id`\n", {}, id="shell-syntax"),
    pytest.param("NAME=x\n", {}, id="no-lines"),
    pytest.param("ID=\nVERSION_ID=\n", {}, id="empty"),
    pytest.param(b"ID=\xff\nVERSION_ID=1\x00\n", {}, id="not-text"),
    pytest.param("ID=" + "a" * 200 + "\n", {}, id="long"),
    pytest.param(None, {}, id="no-file"),
    pytest.param("ID=a\nVERSION_ID=1\n", {"confstr": lambda name: None}, id="no-glibc-answer"),
    pytest.param("ID=a\nVERSION_ID=1\n", {"confstr": raising(ValueError("x"))}, id="no-glibc-name"),
    pytest.param("ID=a\nVERSION_ID=1\n", {"uname": raising(OSError("x"))}, id="no-uname"),
    pytest.param("ID=a\nVERSION_ID=1\n", {"confstr": lambda name: "musl 1.2\nx"}, id="glibc-not-plain"),
]


@pytest.mark.parametrize(("text", "more"), INPUTS)
def test_o87_the_node_facts_step_and_the_sandbox_read_the_same_machine_alike(nodefacts, tmp_path, text, more):
    """O87: one reading for the same inputs, in both places, whatever they hold."""
    assert nodefacts.readings(**machine(tmp_path, text, **more)) == hostfacts.readings(**machine(tmp_path, text, **more))


def test_o87_the_node_facts_step_carries_the_three_readings(nodefacts, tmp_path):
    assert nodefacts.readings(**machine(tmp_path, 'ID=invented\nVERSION_ID="9.4"\n')) == {
        "os": {"id": "invented", "version_id": "9.4"},
        "glibc": GLIBC,
        "kernel": KERNEL,
    }


def test_o87_a_node_facts_step_whose_reading_fails_still_prints_its_other_facts(nodefacts, tmp_path, monkeypatch):
    """O87: nothing is refused: a reader that raises anything is `unknown` with its type, never an exit of the step."""
    monkeypatch.setattr(nodefacts, "readings", raising(RuntimeError("x")))
    found = nodefacts.readings_or_unknown()
    assert found["kernel"] == "unknown"
    assert set(found["unknown"].values()) == {"RuntimeError"}
    json.dumps(found)  # one JSON object on stdout with the node's other facts
