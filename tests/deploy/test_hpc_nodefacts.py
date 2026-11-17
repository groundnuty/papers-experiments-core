"""O87: the node's facts step of the HPC federation driver (`bundle/nodefacts.py`: stdlib only, Python 3.9, run with -I
on the node) also reads the node's OS release, glibc version and kernel release, as text, never executed, and gives
`unknown` with a reason for one it cannot read. The sandbox's own reader (`sandbox/hostfacts.py`) reads the
same three: the two must agree on every input, since a reader compares the runs' records with the state's."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from hpc_fed_support import hpc_on_path

from expcore_onedata.sandbox import hostfacts

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
    here, there = machine(tmp_path, text, **more), machine(tmp_path, text, **more)
    assert nodefacts.readings(**here) == hostfacts.readings(**there)


BODY = "ID=rocky\nVERSION_ID=9.4\n"


def cut_after(kept: int) -> str:
    """An os-release longer than the read bound, whose first `kept` bytes of `BODY` fall inside it."""
    text = "#" * (hostfacts.READ_BYTES - kept - 1) + "\n" + BODY
    assert len(text.encode()) == hostfacts.READ_BYTES + len(BODY) - kept > hostfacts.READ_BYTES
    return text


@pytest.mark.parametrize(
    ("kept", "os_read", "reasons"),
    [
        (6, {"id": "unknown", "version_id": "unknown"}, {"os.id": "too long", "os.version_id": "no VERSION_ID line"}),
        (19, {"id": "rocky", "version_id": "unknown"}, {"os.version_id": "too long"}),
        (23, {"id": "rocky", "version_id": "unknown"}, {"os.version_id": "too long"}),
    ],
    ids=["inside ID", "inside the key VERSION_ID", "inside VERSION_ID's value"],
)
def test_o87_an_os_release_cut_inside_a_value_is_unknown_in_both_readers_and_never_a_prefix(
    nodefacts, tmp_path, kept, os_read, reasons
):
    """O87 (Codex, 17 Nov 2026): a file longer than the read bound whose cut fell inside the `ID=` line gave the prefix
    of the value (`roc` for `rocky`) as a verified `os.id`. The bound is read plus one byte, the line it cut is not one
    that was read, and a key the cut line may have assigned is unknown ("too long"), the same in both readers."""
    assert nodefacts.READ_BYTES == hostfacts.READ_BYTES
    found = [reader.readings(**machine(tmp_path, cut_after(kept))) for reader in (nodefacts, hostfacts)]
    assert found[0] == found[1]
    assert found[0]["os"] == os_read
    assert found[0]["unknown"] == reasons


def test_o87_a_bound_that_cuts_a_multibyte_character_leaves_the_whole_lines_before_it_readable(nodefacts, tmp_path):
    """O87: the cut line is dropped whole, so a bound that falls inside a character of a later line (which was `not
    text`, and so unknown for the wrong reason) leaves `ID` and `VERSION_ID` as the lines before it hold them."""
    bound = hostfacts.READ_BYTES
    text = BODY + "#" * (bound - len(BODY) - 2) + "\n" + "\u00e9" * 4  # the first byte of an `é` is the bound's last
    for reader in (nodefacts, hostfacts):
        assert reader.readings(**machine(tmp_path, text))["os"] == {"id": "rocky", "version_id": "9.4"}


def test_o87_an_os_release_that_ends_exactly_at_the_read_bound_is_read_whole(nodefacts, tmp_path):
    """O87: the check refuses nothing a file of the bound's size holds: its last byte is the bound's, nothing is cut."""
    bound = hostfacts.READ_BYTES
    body = "ID=rocky\nVERSION_ID=9.4\n"
    text = body + "#" * (bound - len(body) - 1) + "\n"
    assert len(text.encode()) == bound
    for reader in (nodefacts, hostfacts):
        assert reader.readings(**machine(tmp_path, text))["os"] == {"id": "rocky", "version_id": "9.4"}


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


@pytest.fixture(scope="module")
def boot():
    with hpc_on_path() as modules:
        yield modules.boot


GIVEN = [
    {"os": {"id": "invented", "version_id": "9.4"}, "glibc": GLIBC, "kernel": KERNEL},
    {
        "os": {"id": "unknown", "version_id": "9"},
        "glibc": "unknown",
        "kernel": KERNEL,
        "unknown": {"os.id": "no ID line", "glibc": "ValueError"},
    },
    {"os": {"id": "a", "version_id": "1"}, "glibc": GLIBC, "kernel": KERNEL, "extra": "dropped"},
    {"os": {"id": "unknown", "version_id": "9"}, "glibc": GLIBC, "kernel": KERNEL},  # unknown with no reason
    {"os": {"id": "a" * 80, "version_id": "1"}, "glibc": GLIBC, "kernel": KERNEL},
    {"os": {"id": "a", "version_id": "1"}, "glibc": "musl 1.2", "kernel": KERNEL},
    {"os": {"id": "a", "version_id": "1"}, "glibc": GLIBC, "kernel": KERNEL, "unknown": {"kernel": "x"}},
    {"os": {"id": "a", "version_id": "1"}, "glibc": GLIBC, "kernel": KERNEL, "unknown": {"other": "x"}},
    {"os": {"id": "a", "version_id": "1"}, "glibc": "unknown", "kernel": KERNEL, "unknown": {"glibc": "line\nbreak"}},
    {"os": {"id": "a", "version_id": "1"}, "glibc": "unknown", "kernel": KERNEL, "unknown": {"glibc": "a/path"}},
    {"os": "rocky"},
    {},
    "text",
    5,
    ["os"],
]


@pytest.mark.parametrize("given", GIVEN, ids=range(len(GIVEN)))
def test_o87_the_driver_keeps_a_nodes_readings_in_the_shape_the_sandbox_checks_them_in(boot, given):
    """O87: what a facts step printed is rebuilt from the known keys alone, or unknown for the same reason in both
    places, so the state's and a run record's readings read alike."""
    assert boot.load_readings(given) == hostfacts.checked(given)


def test_o87_a_step_that_printed_no_readings_is_unknown_for_that_reason(boot):
    assert boot.load_readings(None)["unknown"] == dict.fromkeys(boot.READINGS_KEYS, "the step gave none")
