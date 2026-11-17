"""O87: each run on the Apptainer backend records the OS release (ID and VERSION_ID of /etc/os-release, read as text and
never executed), the glibc version and the kernel release of the agent's node, and the same for the tool server's host
where it differs. They are recorded only: a reading that fails is `unknown` with its reason, and nothing is refused."""

import os
from types import SimpleNamespace

import pytest
from apptainer_support import make_settings, make_spec, start
from fake_node import READINGS, FakeNode

from expcore_onedata.sandbox.apptainer import hostfacts
from expcore_onedata.sandbox.apptainer.node import LocalNode

GLIBC = "glibc 2.34"
KERNEL = "5.14.0-invented.el9.x86_64"
RELEASE = 'NAME="Invented Linux"\nID=invented\nVERSION_ID="9.4"\nPRETTY_NAME="Invented Linux 9.4"\n'


def os_release(tmp_path, text=RELEASE, *, raw: bytes | None = None):
    path = tmp_path / "os-release"
    path.write_bytes(raw if raw is not None else text.encode())
    return path


def read(tmp_path, **given):
    """`readings` over an invented machine; `given` replaces one of its three sources."""
    sources = {
        "os_release": os_release(tmp_path),
        "confstr": lambda name: GLIBC,
        "uname": lambda: SimpleNamespace(release=KERNEL),
    }
    return hostfacts.readings(**{**sources, **given})


def fails(error):
    def raising(*_):
        raise error

    return raising


def test_o87_the_readings_carry_the_os_release_the_glibc_version_and_the_kernel_release(tmp_path):
    """O87: ID and VERSION_ID of the os-release file, the C library's own version string, and the kernel's release; a
    machine that gives all three has nothing marked unknown."""
    assert read(tmp_path) == {"os": {"id": "invented", "version_id": "9.4"}, "glibc": GLIBC, "kernel": KERNEL}


def test_o87_the_glibc_version_is_the_one_the_c_library_names_itself():
    """O87: the reading asks `os.confstr("CS_GNU_LIBC_VERSION")`, which a glibc answers with `glibc <version>`."""
    asked = []
    hostfacts.readings(os_release="/nonexistent/os-release", confstr=lambda name: asked.append(name) or GLIBC)
    assert asked == ["CS_GNU_LIBC_VERSION"]


@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_o87_an_os_release_value_is_read_with_or_without_its_quotes(tmp_path, quote):
    text = f"ID={quote}rocky{quote}\nVERSION_ID={quote}8.10{quote}\n"
    assert read(tmp_path, os_release=os_release(tmp_path, text))["os"] == {"id": "rocky", "version_id": "8.10"}


def test_o87_the_last_assignment_of_a_key_wins_as_a_shell_that_sources_the_file_would_read_it(tmp_path):
    text = "ID=first\nVERSION_ID=1\nID=second\n"
    assert read(tmp_path, os_release=os_release(tmp_path, text))["os"] == {"id": "second", "version_id": "1"}


def test_o87_the_file_is_read_as_text_and_never_executed(tmp_path):
    """O87: a value with shell syntax is no plain value: unknown, and nothing runs, so the file it names is not made."""
    made = tmp_path / "executed"
    text = f"ID=$(touch {made})\nVERSION_ID=`touch {made}`\n"
    found = read(tmp_path, os_release=os_release(tmp_path, text))
    assert found["os"] == {"id": hostfacts.UNKNOWN, "version_id": hostfacts.UNKNOWN}
    assert found["unknown"] == {"os.id": "not a plain value", "os.version_id": "not a plain value"}
    assert not made.exists()


def test_o87_a_machine_that_cannot_be_read_gives_unknown_with_a_reason_for_each_reading(tmp_path):
    """O87: no os-release file, a C library with no version name, no uname: every reading unknown, each with its
    reason, and no exception."""
    found = hostfacts.readings(
        os_release=tmp_path / "absent",
        confstr=fails(ValueError("no such name")),
        uname=fails(OSError("no uname")),
    )
    assert found == {
        "os": {"id": hostfacts.UNKNOWN, "version_id": hostfacts.UNKNOWN},
        "glibc": hostfacts.UNKNOWN,
        "kernel": hostfacts.UNKNOWN,
        "unknown": {
            "os.id": "FileNotFoundError",
            "os.version_id": "FileNotFoundError",
            "glibc": "ValueError",
            "kernel": "OSError",
        },
    }


@pytest.mark.parametrize(
    ("text", "id_reason", "version_reason"),
    [
        ("NAME=x\n", "no ID line", "no VERSION_ID line"),  # a rolling release has no VERSION_ID, and some files no ID
        ("ID=\nVERSION_ID=\n", "empty", "empty"),
        ("ID=" + "a" * 200 + "\nVERSION_ID=1\n", "not a plain value", None),
        ("ID=with space\nVERSION_ID=1\n", "not a plain value", None),
    ],
)
def test_o87_a_value_that_is_missing_empty_or_not_plain_is_unknown_with_its_reason(
    tmp_path, text, id_reason, version_reason
):
    found = read(tmp_path, os_release=os_release(tmp_path, text))
    assert found["unknown"]["os.id"] == id_reason
    assert found["os"]["id"] == hostfacts.UNKNOWN
    assert found["unknown"].get("os.version_id") == version_reason


def test_o87_an_os_release_file_that_is_not_text_is_unknown_and_never_raises(tmp_path):
    found = read(tmp_path, os_release=os_release(tmp_path, raw=b"ID=\xff\xfe\nVERSION_ID=1\x00\n"))
    assert found["os"] == {"id": hostfacts.UNKNOWN, "version_id": hostfacts.UNKNOWN}
    assert found["unknown"]["os.id"] == found["unknown"]["os.version_id"] == "not text"


def test_o87_only_the_start_of_a_huge_os_release_file_is_read(tmp_path):
    """O87: the read is bounded, so a file that is no os-release (a device, a huge file) costs nothing."""
    text = "ID=invented\nVERSION_ID=9\n" + "# padding\n" * 100_000
    assert read(tmp_path, os_release=os_release(tmp_path, text))["os"] == {"id": "invented", "version_id": "9"}


@pytest.mark.parametrize("answer", [None, "", 5, "glibc 2.34\nsecond line", "musl"])
def test_o87_a_c_library_answer_that_is_no_version_is_unknown(tmp_path, answer):
    found = read(tmp_path, confstr=lambda name: answer)
    assert found["glibc"] == hostfacts.UNKNOWN
    assert "glibc" in found["unknown"]


def test_o87_the_local_node_reads_this_machine_and_has_the_three_readings_whatever_it_is():
    """O87: the shape on a real machine, whose values depend on it."""
    found = LocalNode().node_readings()
    assert set(found) - {"unknown"} == {"os", "glibc", "kernel"}
    assert found["kernel"] == os.uname().release
    assert set(found["os"]) == {"id", "version_id"}


def test_o87_the_local_node_s_tool_server_runs_on_the_same_host(tmp_path):
    """O87: the driver's own child, so its host is the node's: the same readings."""
    node = LocalNode()
    assert node.tool_server_readings() == node.node_readings()


def test_o87_a_run_records_its_node_s_readings_in_its_infrastructure_facts(tmp_path):
    box = start(FakeNode(), make_settings(tmp_path), make_spec("via-gate"))
    try:
        facts = box.metadata()["infrastructure_facts"]
        assert facts["node"] == READINGS
        assert "tool_server_host" not in facts
        assert box.metadata()["host"]["kernel"] == "invented"  # the node's own facts, as they were, beside them
    finally:
        box.stop()


def test_o87_a_node_whose_readings_cannot_be_made_gives_unknown_and_the_run_starts_all_the_same(tmp_path):
    """O87: recorded only, never refused: a node whose reading raises is `unknown` with the exception's type."""
    node = FakeNode()
    node.readings_error = RuntimeError("a message that is never recorded")
    box = start(node, make_settings(tmp_path), make_spec("via-gate"))
    try:
        found = box.metadata()["infrastructure_facts"]["node"]
        assert found["os"] == {"id": hostfacts.UNKNOWN, "version_id": hostfacts.UNKNOWN}
        assert found["glibc"] == found["kernel"] == hostfacts.UNKNOWN
        assert set(found["unknown"].values()) == {"RuntimeError"}
        assert "never recorded" not in str(found)
    finally:
        box.stop()


@pytest.mark.parametrize("answer", [None, "text", ["os"], {"os": 1}])
def test_o87_a_node_whose_readings_are_no_mapping_of_the_shape_is_unknown_never_refused(tmp_path, answer):
    node = FakeNode()
    node.readings = answer
    box = start(node, make_settings(tmp_path), make_spec("via-gate"))
    try:
        assert box.metadata()["infrastructure_facts"]["node"]["kernel"] == hostfacts.UNKNOWN
    finally:
        box.stop()


def test_o87_the_tool_server_s_host_is_recorded_only_where_it_differs_from_the_node(tmp_path):
    """O87: a tool server on the node's own host adds nothing; on another host its readings are recorded, as the node's
    are, beside them."""
    other = {"os": {"id": "other", "version_id": "7"}, "glibc": "glibc 2.17", "kernel": "3.10.0-other"}
    same, apart = FakeNode(), FakeNode()
    apart.tool_server_host = other
    for name, node, expected in (("same", same, None), ("apart", apart, other)):
        (tmp_path / name).mkdir()
        box = start(node, make_settings(tmp_path / name), make_spec("via-gate"))
        try:
            with box.tool_server_process():
                facts = box.metadata()["infrastructure_facts"]
            assert facts["node"] == READINGS
            assert facts.get("tool_server_host") == expected
        finally:
            box.stop()


def test_o87_a_tool_server_host_that_cannot_be_read_is_unknown_and_the_server_still_starts(tmp_path):
    node = FakeNode()
    node.tool_server_host = RuntimeError("never recorded")
    box = start(node, make_settings(tmp_path), make_spec("via-gate"))
    try:
        with box.tool_server_process():
            found = box.metadata()["infrastructure_facts"]["tool_server_host"]
        assert found["kernel"] == hostfacts.UNKNOWN
        assert set(found["unknown"].values()) == {"RuntimeError"}
    finally:
        box.stop()
