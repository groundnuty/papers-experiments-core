"""O91: the channel's fixed names are what the other lanes bind and refuse."""

from expcore_onedata.readlog import paths


def test_o91_the_cell_targets_are_fixed_and_separate():
    assert paths.SOCKET_DIR == "/expcore-readlog"
    assert paths.SOCKET == "/expcore-readlog/sock"
    assert paths.CLIENT_DIR == "/expcore-readlog-client"
    assert paths.CLIENT == "/expcore-readlog-client/expcore_readlog_client.py"
    assert paths.TARGETS == (paths.SOCKET_DIR, paths.CLIENT_DIR)
    first, second = paths.TARGETS
    assert not first.startswith(second + "/")
    assert not second.startswith(first + "/")
    assert paths.SOCKET.startswith(paths.SOCKET_DIR + "/")
    assert paths.CLIENT.startswith(paths.CLIENT_DIR + "/")


def test_o91_the_names_of_the_log_and_the_record_are_fixed():
    assert paths.FORMAT == "expcore-readlog/1"
    assert paths.NAMESPACE == "read_log"
    assert paths.SIDE_HASH_FIELD == "instruments.read_log.side_sha256"
    assert paths.FACTS_OWNER == "readlog"
    assert paths.MAX_LINE_BYTES == 4096
    assert paths.OPS == ("read", "canary")
