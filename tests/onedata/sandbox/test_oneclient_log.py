"""O18: errors the agent caused are not platform errors, and what the mount's own log says is classified by origin."""

from pathlib import Path

from expcore_onedata.sandbox import oneclient_log as log

DATA = Path(__file__).resolve().parents[2] / "data" / "sandbox"


def summary(name: str) -> dict:
    return log.summarize((DATA / name).read_text(encoding="utf-8"))


def test_o18_enoent_erofs_and_eisdir_on_a_client_log_line_are_not_sidecar_errors():
    """O18: the agent asked for a path that is not there, wrote to the read-only mount, opened a directory as a file."""
    got = summary("oneclient_agent_only.log")
    assert got["by_origin"].get("sidecar", 0) == 0
    assert got["by_origin"].get("platform", 0) == 0
    assert got["fuse_total"] == 3
    assert got["fuse_caused_by_agent"] == 3
    assert got["fuse_not_caused_by_agent"] == 0
    assert {line["errno"] for line in got["fuse_lines"]} == {"ENOENT", "EROFS", "EISDIR"}
    assert all(line["caused_by_agent"] for line in got["fuse_lines"])


def test_o18_the_platform_error_count_ignores_what_the_agent_caused():
    """O18: the count that goes against the platform leaves out the three errnos, in a log that also holds real faults."""
    got = summary("oneclient.log")
    assert got["fuse_caused_by_agent"] == 3
    assert log.platform_errors(got) == got["total"] - 3
    assert log.platform_errors(summary("oneclient_agent_only.log")) == 0


def test_o18_the_errnos_the_agent_cannot_cause_by_itself_are_not_excused():
    """O18: EIO and a connection failure count; EACCES and EPERM are ambiguous and so are never counted as the agent's."""
    got = summary("oneclient.log")
    assert got["by_kind"]["fuse_error:EIO"] == 1
    assert got["by_origin"]["sidecar"] == 1  # the connection failure
    assert got["by_kind"]["provider_connection"] == 1
    permission = [line for line in got["fuse_lines"] if line["errno"] == "EACCES"]
    assert permission
    assert permission[0]["ambiguous_permission"] is True
    assert permission[0]["caused_by_agent"] is False


def test_o18_the_classification_of_single_lines():
    """O18: ENOENT is a FUSE error that nobody is blamed for; a connection failure is the sidecar's."""
    assert log.classify("fsOperations.cc", "Fuse error exception: No such file or directory") == (
        "unattributed",
        "fuse_error:ENOENT",
    )
    assert log.caused_by_agent("fuse_error:EROFS")
    assert log.caused_by_agent("fuse_error:EISDIR")
    assert not log.caused_by_agent("fuse_error:EIO")
    assert log.classify("connectionPool.cc", "anything")[0] == "sidecar"
    assert log.classify("x.cc", "oneprovider returned 500")[0] == "platform"


def test_o18_a_fuse_error_close_after_a_sidecar_error_is_context_and_not_a_blame():
    """O18: the nearest sidecar error before a FUSE error is recorded as context, and the origin stays unattributed."""
    got = summary("oneclient.log")
    assert got["fuse_preceded_by"]["provider_connection"] >= 1
    assert got["by_origin"]["unattributed"] == got["fuse_total"]


def test_the_storage_detection_phase_is_timed_from_the_logs_own_clock():
    got = log.storage_detection((DATA / "oneclient.log").read_text(encoding="utf-8"))
    assert got["storage_detection_s"] == 2.5
    assert got["result"] == "not_directly_accessible"
    assert log.storage_detection("")["storage_detection_s"] is None
