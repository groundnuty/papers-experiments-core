"""The smaller modules of the sandbox: the gate log summary, earlyoom's host checks, the reaper, the registry."""

import hashlib
import json
import os
from pathlib import Path

import pytest
from fake_host import FakeHost

from expcore_onedata.sandbox import earlyoom, gatelog, netpool, reap
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.registry import Registry

COMM = "test-driver"


def lines(*records) -> list[str]:
    return [json.dumps(r) for r in records]


def test_o16_the_summary_counts_a_policy_refusal_apart_from_an_error():
    """O16: the enforcement working (a 403) is not an error of the gate or of the platform."""
    got = gatelog.summarize(
        lines(
            {"tag": "content", "decision": "allow", "status": 200, "bytes": 10},
            {"decision": "deny", "status": 403},
            {"tag": "content", "decision": "allow", "status": 502, "error_origin": "gate", "client_saw": "5xx"},
            {"tag": "content", "decision": "allow", "status": 503, "error_origin": "upstream", "client_saw": "5xx"},
        )
    )
    assert got["errors"]["policy_refusals"] == 1
    assert (got["errors"]["gate_5xx"], got["errors"]["upstream_5xx"]) == (1, 1)
    assert got["agent_saw_gate_error"] is True
    assert got["agent_saw_upstream_error"] is True
    assert got["content_requests"] == 1
    assert got["refused_requests"] == 1


def test_o16_a_cut_transfer_is_counted_with_the_bytes_the_client_never_read():
    """O16: A cut transfer is counted with the bytes the client never read."""
    got = gatelog.summarize(
        lines({"tag": "content", "decision": "allow", "status": 200, "bytes": 5, "cut": True, "unread_bytes": 95})
    )
    assert (got["cut_transfers"], got["unread_bytes"]) == (1, 95)
    assert got["errors"]["gate_5xx"] == 0


def test_o16_a_connection_error_is_blamed_by_origin():
    """O16: A connection error is blamed by origin."""
    got = gatelog.summarize(
        lines(
            {"event": "connection_error", "error_origin": "gate", "client_saw": "reset"},
            {"event": "connection_error", "error_origin": "upstream", "client_saw": "reset"},
            {"event": "connection_error", "error_origin": "client", "client_saw": None},
        )
    )
    assert got["errors"]["gate_resets"] == 1
    assert got["errors"]["upstream_resets"] == 1
    assert got["errors"]["connection_errors_client"] == 1


def test_o16_a_gate_log_that_cannot_be_read_is_unknown_never_zero_errors():
    """O16: A gate log that cannot be read is unknown never zero errors."""
    got = gatelog.unknown("no log")
    assert got["errors"] is None
    assert got["agent_saw_gate_error"] is None
    assert got["unavailable"] == "no log"


def test_earlyoom_is_checked_through_the_host_and_must_ignore_the_driver():
    """O17: where the host's killer runs it must be told to spare this driver; its arguments come from the service."""
    host = FakeHost()
    host.add_process(900, start=5)
    argv = f"earlyoom\0{earlyoom.earlyoom_args(COMM).replace(' ', chr(0))}\0"
    host.files["/proc/900/cmdline"] = argv
    host.files["/proc/self/comm"] = COMM + "\n"
    host.run_handlers["systemctl"] = lambda a: (
        CmdResult(0, "active\n") if a[1] == "is-active" else CmdResult(0, "900\n")
    )
    got = earlyoom.require_driver_exempt(host, COMM)
    assert got["earlyoom_active"] is True
    host.files["/proc/self/comm"] = "python\n"
    with pytest.raises(RuntimeError, match="could kill this driver"):
        earlyoom.require_driver_exempt(host, COMM)


def test_earlyoom_that_does_not_run_passes_and_one_that_cannot_be_asked_does_not():
    host = FakeHost()
    host.run_handlers["systemctl"] = lambda a: CmdResult(3, "inactive\n")
    assert earlyoom.require_driver_exempt(host, COMM) == {"earlyoom_active": False}
    host.run_handlers["systemctl"] = lambda a: CmdResult(124, "", "timed out")
    with pytest.raises(RuntimeError):
        earlyoom.require_driver_exempt(host, COMM, own_comm=COMM)


def test_the_journal_is_read_through_the_host_and_unreadable_is_none():
    host = FakeHost()
    host.journal = (
        '1790000100.300 h earlyoom[1]: sending SIGTERM to process 7 uid 1 "python": '
        "oom_score 1, oom_score_adj 2, VmRSS 3 MiB\n"
    )
    kills = earlyoom.read_kills(host, 1790000000)
    assert [k.pid for k in kills] == [7]
    host.run_handlers["journalctl"] = lambda a: CmdResult(1, "", "no journal")
    assert earlyoom.read_kills(host, 1790000000) is None


def test_the_ignore_pattern_spares_the_driver_and_the_hosts_services():
    pattern = earlyoom.ignore_pattern(COMM)
    argv = ["--ignore", pattern]
    assert earlyoom.harness_protected(argv, COMM)
    assert earlyoom.driver_exempt(argv, COMM)
    assert not earlyoom.driver_exempt(argv, "python")


def test_the_registry_retries_a_pending_cleanup_until_it_succeeds():
    class Stubborn:
        def __init__(self):
            self.fail = True

        def stop(self):
            if self.fail:
                raise RuntimeError("still there")

    registry, sandbox = Registry(), Stubborn()
    registry.add_pending(sandbox)
    registry.add_pending(sandbox)
    assert registry.pending() == 1
    assert registry.drain() == 1
    sandbox.fail = False
    assert registry.drain() == 0
    registry.note_pids({4, 5})
    assert registry.has_seen(4)
    assert not registry.has_seen(6)


@pytest.fixture
def dead_run(world):
    """A directory of a run whose owner is gone: a work disk with a file, a gate log, and no container."""
    root = world.s.work_root
    directory = root / "login-data-0123abcd"
    (directory / "work" / "out").mkdir(parents=True)
    (directory / "work" / "out" / "result.json").write_text("{}", encoding="utf-8")
    (directory / "gatelog").mkdir()
    (directory / "gatelog" / "requests.jsonl").write_text("{}\n", encoding="utf-8")
    old = 1_000_000_000
    os.utime(directory, (old, old))
    return directory


def run_handlers(host):
    """The root commands the reaper uses, performed on the test's real temporary directory."""

    def test_e(argv):
        return CmdResult(0 if Path(argv[-1]).exists() else 1)

    def find(argv):
        files = [p for p in Path(argv[1]).rglob("*") if p.is_file()]
        return CmdResult(0, "".join(f"{p.stat().st_size}\n" for p in files))

    host.run_handlers.update({"test": test_e, "find": find})


def test_reaping_a_dead_run_copies_checks_and_only_then_removes_the_directory(world, dead_run, tmp_path):
    run_handlers(world.host)
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    done = reap.reap_run(world.host, world.s, "login-data", log_dir=log_dir, outputs=["out"])
    assert done is True
    assert not dead_run.exists()
    kept = log_dir / "reaped" / dead_run.name
    assert (kept / "work" / "out" / "result.json").read_text() == "{}"
    assert (kept / "gatelog" / "requests.jsonl").exists()
    record = json.loads((log_dir / reap.REAPED).read_text().splitlines()[0])
    assert [k["what"] for k in record["kept"]] == ["out", "gate log"]


def test_o12_the_reapers_record_names_a_directory_by_its_id_and_digest_and_its_mounts_by_label(
    world, dead_run, tmp_path
):
    """O12: the record of what the reaper removed holds no configured value: the directory's name holds the lease's
    login and its path the work root, so it is named by its id and the SHA-256 of its name, a mount by its place."""
    run_handlers(world.host)
    world.host.mounts[str(dead_run / "work")] = "ext4"
    log_dir = tmp_path / "log"
    log_dir.mkdir()
    assert reap.reap_run(world.host, world.s, "login-data", log_dir=log_dir, outputs=["out"]) is True
    text = (log_dir / reap.REAPED).read_text(encoding="utf-8")
    assert "login-data" not in text
    assert str(world.s.work_root) not in text
    record = json.loads(text.splitlines()[0])
    assert record["dir"] == {"id": "0123abcd", "sha256": hashlib.sha256(dead_run.name.encode()).hexdigest()}
    assert record["mounts"] == ["work"]


def test_o12_a_reaper_that_cannot_ask_docker_logs_the_directory_by_its_id(world, dead_run, tmp_path, caplog):
    """O12: the line that says a directory is not shown clear of containers names it by its id, never its name."""
    world.host.docker = lambda *args, **kwargs: CmdResult(1, "", "daemon down")
    with caplog.at_level("ERROR"):
        assert reap.reap_dirs(world.host, tmp_path, world.s.work_root, reap.sid_pattern("login-data"), "x") is False
    assert "login-data" not in caplog.text
    assert "0123abcd" in caplog.text


def test_o12_a_mount_that_will_not_go_is_logged_by_its_digest_never_its_path(world, caplog, monkeypatch):
    """O12: a real host's unmount that leaves a mount in place logs it by the digest of its path, which holds the
    configured work root, never by the path."""
    from expcore_onedata.sandbox.api import SandboxError
    from expcore_onedata.sandbox.hostops import DockerHost, name_digest

    path = world.s.work_root / "login-data-0123abcd" / "work"
    host = DockerHost()
    table = f"1 0 8:1 / / rw - ext4 /dev/root rw\n40 1 7:3 / {path} rw - ext4 /dev/loop3 rw\n"
    monkeypatch.setattr(host, "read_text", lambda which: table)
    monkeypatch.setattr(host, "run", lambda argv, timeout_s, **kwargs: CmdResult(0))
    with caplog.at_level("ERROR"), pytest.raises(SandboxError):
        host.unmount(path)
    assert str(path) not in caplog.text
    assert name_digest(path) in caplog.text


def test_a_directory_with_a_container_or_a_young_one_is_left_alone(world, dead_run, tmp_path):
    run_handlers(world.host)
    world.host.docker("run", "-d", "--name", "alive", "--label", f"org.test.dir={dead_run}", "agent:1")
    assert reap.reap_run(world.host, world.s, "login-data", log_dir=tmp_path) is False
    assert dead_run.exists()
    world.host.docker("rm", "-f", "alive")
    os.utime(dead_run, None)  # now: too young to be shown nobody's
    assert reap.reap_run(world.host, world.s, "login-data", log_dir=tmp_path) is False
    assert dead_run.exists()


def test_a_failed_copy_keeps_the_disks_and_reports_not_gone(world, dead_run, tmp_path):
    run_handlers(world.host)
    world.host.run_handlers["cp"] = lambda argv: CmdResult(1, "", "disk full")
    assert reap.reap_run(world.host, world.s, "login-data", log_dir=tmp_path, outputs=["out"]) is False
    assert dead_run.exists()


def test_the_reaper_does_not_touch_the_directory_of_another_key(world, dead_run, tmp_path):
    run_handlers(world.host)
    other = world.s.work_root / "login-data2-0123abcd"
    other.mkdir()
    os.utime(other, (1_000_000_000, 1_000_000_000))
    assert reap.reap_run(world.host, world.s, "login-data", log_dir=tmp_path) is True
    assert other.exists()


def test_a_sandbox_root_that_cannot_be_read_is_not_shown_gone(world, tmp_path):
    missing = tmp_path / "no-such-root"
    assert reap.reap_dirs(world.host, tmp_path, missing, reap.sid_pattern("k"), netpool.Labels("org.test").dir) is False


def test_o12_a_failed_docker_command_is_logged_by_the_size_and_digest_of_its_stderr(caplog, monkeypatch):
    """O12: a real host's failure holds the subcommand and exit code, and the log the digest, not the tool's words."""
    import hashlib

    from expcore_onedata.sandbox.api import SandboxError
    from expcore_onedata.sandbox.hostops import CmdResult, DockerHost

    words = "invented: no such image registry.example"
    host = DockerHost()
    monkeypatch.setattr(host, "_run", lambda *a, **k: CmdResult(125, "", words))
    with caplog.at_level("ERROR"), pytest.raises(SandboxError, match=r"docker run failed \(125\)") as caught:
        host.docker("run", "x")
    assert words not in str(caught.value)
    assert words not in caplog.text
    assert f"stderr {len(words)} bytes, sha256 {hashlib.sha256(words.encode()).hexdigest()}" in caplog.text
