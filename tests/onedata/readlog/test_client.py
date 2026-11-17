"""O91: the read-log client: one datagram per record, an error on anything but a clean send, a cap checked first.

The client runs alone in a cell, so it is loaded from its file, as a script loads it, and tested against a plain
datagram socket that stands for the collector. A script logs BEFORE it returns data and returns NO data when a call
raises, so every failure here must be a `ReadLogError`.
"""

import ast
import inspect
import json
import shutil
import socket
import sys
import time
from pathlib import Path

import pytest
from readlog_logs import SHA
from readlog_rig import load_client, load_collector, needs_unix, short_dir

from expcore_onedata.readlog import files, paths

SOURCE = files.client_file()


@pytest.fixture
def client_module():
    return load_client()


@pytest.fixture
def server():
    """A bound datagram socket that stands for the collector, and the client pointed at it."""
    root = short_dir()
    where = str(root / "sock")
    receiving = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiving.bind(where)
    receiving.setblocking(False)
    client = load_client(where)
    try:
        yield receiving, client, where
    finally:
        receiving.close()
        shutil.rmtree(root, ignore_errors=True)


def received(receiving):
    got = []
    while True:
        try:
            got.append(receiving.recv(8192))
        except BlockingIOError:
            return got


# --- the line (no socket) ---------------------------------------------------------------------------------------------


def test_o91_a_record_is_one_ascii_json_line_with_its_fields(client_module):
    line = client_module.encode("read", "file-7", range=[0, 4096], bytes=4096, sha256=SHA)
    assert isinstance(line, bytes)
    line.decode("ascii")
    assert json.loads(line) == {
        "v": 1,
        "op": "read",
        "item": "file-7",
        "range": [0, 4096],
        "bytes": 4096,
        "sha256": SHA,
    }
    assert json.loads(client_module.encode("read", "x")) == {"v": 1, "op": "read", "item": "x"}


def test_o91_a_non_ascii_item_is_escaped_so_the_line_stays_ascii(client_module):
    line = client_module.encode("read", "café-\U0001f600")
    line.decode("ascii")
    assert json.loads(line)["item"] == "café-\U0001f600"


@pytest.mark.parametrize(
    "fields",
    [
        {"op": "write", "item": "x"},
        {"op": "read", "item": ""},
        {"op": "read", "item": 5},
        {"op": "read", "item": b"x"},
        {"op": "read", "item": "x" * (paths.MAX_ITEM_CHARS + 1)},
        {"op": "read", "item": "x", "range": [3, 1]},
        {"op": "read", "item": "x", "range": [0]},
        {"op": "read", "item": "x", "range": (0, 1, 2)},
        {"op": "read", "item": "x", "range": [-1, 2]},
        {"op": "read", "item": "x", "range": [0.5, 2]},
        {"op": "read", "item": "x", "bytes": -1},
        {"op": "read", "item": "x", "bytes": True},
        {"op": "read", "item": "x", "bytes": 1.5},
        {"op": "read", "item": "x", "sha256": "AB" * 32},
        {"op": "read", "item": "x", "sha256": "ab" * 31},
        {"op": "read", "item": "x", "sha256": 7},
    ],
)
def test_o91_a_field_the_collector_would_refuse_is_refused_by_the_client_first(client_module, fields):
    """O91: a bad field raises here, so the script returns no data, rather than becoming a malformed line that makes
    the run's whole log unknown."""
    with pytest.raises(client_module.ReadLogError):
        client_module.encode(**fields)


def test_o91_the_cap_is_checked_against_the_bytes_that_would_be_sent_not_the_characters(client_module):
    """O91: an item of 400 emoji is 4,800 bytes as ASCII JSON: refused, though it is far under the character limit."""
    with pytest.raises(client_module.ReadLogError, match="longer than"):
        client_module.encode("read", "\U0001f600" * 400)
    # an item that makes the line exactly the cap is fine, one byte more is not
    overhead = len(client_module.encode("read", "x")) - 1
    fill = paths.MAX_LINE_BYTES - overhead
    item = "é" * (fill // 6) + "a" * (fill % 6)
    assert len(client_module.encode("read", item)) == paths.MAX_LINE_BYTES
    with pytest.raises(client_module.ReadLogError, match="longer than"):
        client_module.encode("read", item + "a")


# --- the socket -------------------------------------------------------------------------------------------------------


@needs_unix
def test_o91_one_send_is_one_datagram(server):
    receiving, client, _ = server
    client.log_read("a")
    client.log_read("b", bytes=3)
    client.log_canary()
    assert [json.loads(g)["item"] for g in received(receiving)] == ["a", "b", "canary"]


@needs_unix
def test_o91_an_over_long_line_is_refused_before_anything_is_sent(server):
    """O91: the cap is checked before the socket is opened: nothing reaches the collector."""
    receiving, client, _ = server
    with pytest.raises(client.ReadLogError, match="longer than"):
        client.log_read("\U0001f600" * 400)
    assert received(receiving) == []


@needs_unix
def test_o91_a_missing_socket_raises(client_module):
    root = short_dir()
    try:
        client_module.SOCKET = str(root / "no-such-socket")
        with pytest.raises(client_module.ReadLogError) as caught:
            client_module.log_read("x")
        assert isinstance(caught.value.__cause__, OSError)
    finally:
        shutil.rmtree(root, ignore_errors=True)


@needs_unix
def test_o91_a_collector_that_is_gone_raises(server):
    """O91: the socket file stays when the collector dies; connecting to it is refused: raise, return no data."""
    receiving, client, _ = server
    receiving.close()
    with pytest.raises(client.ReadLogError):
        client.log_read("x")


@needs_unix
def test_o91_a_collector_that_stopped_reading_raises(server):
    """O91: a socket whose receiving side is shut (the collector's stop) refuses a send: none succeeds unrecorded."""
    receiving, client, _ = server
    receiving.shutdown(socket.SHUT_RD)
    with pytest.raises(client.ReadLogError):
        client.log_read("x")


@needs_unix
def test_o91_a_file_that_is_no_socket_raises(client_module):
    root = short_dir()
    try:
        not_a_socket = Path(root / "sock")
        not_a_socket.write_text("x")
        client_module.SOCKET = str(not_a_socket)
        with pytest.raises(client_module.ReadLogError):
            client_module.log_read("x")
    finally:
        shutil.rmtree(root, ignore_errors=True)


@needs_unix
def test_o91_a_queue_that_stays_full_raises_after_the_short_timeout(server):
    """O91: a collector that does not read lets its queue fill: a send waits at most SEND_TIMEOUT_S and then raises."""
    _, client, _ = server
    client.SEND_TIMEOUT_S = 0.2
    item = "x" * 1000
    started = time.monotonic()
    for _ in range(100_000):
        try:
            client.log_read(item)
        except client.ReadLogError:
            assert time.monotonic() - started < 120
            return
    pytest.skip("the collector's queue never filled on this host")


# --- what the client is --------------------------------------------------------------------------------------------


def test_o91_the_client_takes_no_path_and_reads_no_environment():
    """O91: the target is fixed. No parameter names a socket and the source never reads the environment, so an
    agent's own code cannot point a canonical script at a collector of its making through either."""
    client = load_client()
    for name in ("encode", "send", "log_read", "log_canary"):
        parameters = set(inspect.signature(getattr(client, name)).parameters)
        assert not {"path", "socket", "sock", "address", "target"} & parameters
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {
        n.id for n in ast.walk(tree) if isinstance(n, ast.Name)
    }
    assert not {"environ", "getenv", "putenv", "argv"} & names


def test_o91_the_client_and_the_collector_run_on_python_3_8_with_the_standard_library_alone():
    """O91: the cell's interpreter may be old and holds nothing installed: both files parse as Python 3.8 and import
    nothing but the standard library."""
    for path in (files.client_file(), files.collector_file()):
        tree = ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 8))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0
                imported.add((node.module or "").split(".")[0])
        assert imported <= set(sys.stdlib_module_names), (path.name, imported - set(sys.stdlib_module_names))


def test_o91_the_constants_each_single_file_repeats_are_the_ones_of_paths():
    """O91: the client and the collector run alone, so each repeats the fixed names; this holds them to `paths`."""
    client, collector = load_client(), load_collector()
    assert client.SOCKET == paths.SOCKET
    for module in (client, collector):
        assert module.MAX_LINE_BYTES == paths.MAX_LINE_BYTES
        assert module.MAX_ITEM_CHARS == paths.MAX_ITEM_CHARS
        assert module.LINE_VERSION == paths.LINE_VERSION
        assert tuple(module.OPS) == paths.OPS
    assert collector.FORMAT == paths.FORMAT
    assert collector.SOCKET_NAME == paths.SOCKET_NAME
    assert paths.MAX_LOG_BYTES == collector.MAX_LOG_BYTES
    assert paths.END_RESERVE_BYTES == collector.END_RESERVE_BYTES
    assert f"{paths.SOCKET_DIR}/{paths.SOCKET_NAME}" == paths.SOCKET
    assert f"{paths.CLIENT_DIR}/{paths.CLIENT_MODULE}.py" == paths.CLIENT
