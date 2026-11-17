"""The post-run readings a study makes of a Docker run's infrastructure, by the names both backends answer (O17, O18):
`infra_check()` is today's `infra_container_check()`, `mount_errors()` today's `sidecar_errors()`, byte for byte, and
`mount_log()` the oneclient log those read, with the token redacted. The old names still work, and warn."""

import json
import warnings
from pathlib import Path

import pytest
from sandbox_support import TOKEN, make_spec, start_options

from expcore_onedata.sandbox import oneclient_log
from expcore_onedata.sandbox.api import InfraReadings
from expcore_onedata.sandbox.containers import SIDECAR_LOG_DIR
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

LOG = (Path(__file__).resolve().parents[2] / "data" / "sandbox" / "oneclient.log").read_text(encoding="utf-8")
READ = ["cat", f"{SIDECAR_LOG_DIR}/oneclient.INFO"]


def start(world, level):
    return DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))


def serve_log(world, sbx, text, *, returncode=0, stderr=""):
    """The sidecar answers `cat` of its INFO log with `text`; every other exec as before. Returns the reads made."""
    reads, before = [], world.host.exec_handler

    def handler(container, argv, stdin):
        if container.name == sbx.names.sidecar and argv == READ:
            reads.append(list(argv))
            return CmdResult(returncode, text, stderr)
        return before(container, argv, stdin)

    world.host.exec_handler = handler
    return reads


def recorded_docker(world):
    """Every `docker` call, its arguments and its keywords, as the sandbox passed them."""
    calls, docker = [], world.host.docker

    def recording(*args, **kw):
        calls.append((args, kw))
        return docker(*args, **kw)

    world.host.docker = recording
    return calls


def test_o17_both_names_give_dockers_check_of_the_runs_other_containers_unchanged(world):
    """O17: `infra_check()` answers what `infra_container_check()` answered (the containers by role against their
    baseline, the deaths and the infrastructure's problems), and the old name warns, naming the new one."""
    sbx = start(world, "via-gate")
    try:
        assert isinstance(sbx, InfraReadings)
        check = sbx.infra_check()
        with pytest.warns(DeprecationWarning, match=r"infra_check\(\)"):
            old = sbx.infra_container_check()
        assert json.dumps(old, sort_keys=True) == json.dumps(check, sort_keys=True)
        assert set(check) == {"containers", "deaths", "death"}
        assert set(check["containers"]) == {"gate"}
        assert (check["deaths"], check["death"]) == ([], [])
    finally:
        sbx.stop()


def test_o17_a_gate_that_died_is_the_same_death_by_either_name(world):
    """O17: a gate that exited during the run is a death of infrastructure, read the same by both names."""
    sbx = start(world, "via-gate")
    try:
        gate = world.host.containers[sbx.names.gate]
        gate.running, gate.exit_code = False, 139
        check = sbx.infra_check()
        with pytest.warns(DeprecationWarning, match=r"infra_check\(\)"):
            assert sbx.infra_container_check() == check
        assert [death["role"] for death in check["deaths"]] == ["gate"]
        assert check["death"]
        assert all("gate" in line for line in check["death"])
    finally:
        sbx.stop()


def test_o18_mount_errors_is_the_sidecar_summary_of_the_same_read_with_the_token_redacted(world):
    """O18: `mount_errors()` asks the sidecar for its INFO log with the same `docker exec` as `sidecar_errors()` did,
    and gives the same summary of it, the token replaced first; the old name warns and answers the same."""
    sbx = start(world, "mounted")
    try:
        text = LOG + f"I1111 10:00:00.000000 1 options.cc:1] token {TOKEN}\n"
        reads = serve_log(world, sbx, text)
        calls = recorded_docker(world)
        found = sbx.mount_errors()
        assert found == oneclient_log.summarize(text.replace(TOKEN, "<token>"))
        assert TOKEN not in json.dumps(found)
        with pytest.warns(DeprecationWarning, match=r"mount_errors\(\)"):
            assert sbx.sidecar_errors() == found
        assert calls[0] == (("exec", sbx.names.sidecar, *READ), {"check": False, "timeout": 60})
        assert len(reads) == 2
    finally:
        sbx.stop()


def test_o18_mount_log_is_the_text_mount_errors_summarises_with_the_token_redacted(world):
    """O18: `mount_log()` replaces a study's own `host.docker("exec", <sidecar>, "cat", ...)`: the same read, its text
    whole but for the token."""
    sbx = start(world, "mounted")
    try:
        text = LOG + f"I1111 10:00:00.000000 1 options.cc:1] token {TOKEN}\n"
        serve_log(world, sbx, text)
        calls = recorded_docker(world)
        assert sbx.mount_log() == {"text": text.replace(TOKEN, "<token>")}
        assert calls == [(("exec", sbx.names.sidecar, *READ), {"check": False, "timeout": 60})]
    finally:
        sbx.stop()


def test_o18_a_sidecar_log_that_cannot_be_read_is_unavailable_by_every_name(world):
    """O18: a failed read is `{"unavailable", "stderr"}`, never an empty summary, from all three calls alike."""
    sbx = start(world, "mounted")
    try:
        serve_log(world, sbx, "", returncode=1, stderr="cat: no such file\n")
        found = sbx.mount_errors()
        assert set(found) == {"unavailable", "stderr"}
        assert sbx.mount_log() == found
        with pytest.warns(DeprecationWarning, match=r"mount_errors\(\)"):
            assert sbx.sidecar_errors() == found
    finally:
        sbx.stop()


@pytest.mark.parametrize("level", ["via-gate", "isolated"])
def test_o18_a_run_without_a_mount_has_no_mount_reading(world, level):
    """O18: no sidecar, no reading: None from both, and no `docker exec` asked."""
    sbx = start(world, level)
    try:
        calls = recorded_docker(world)
        assert (sbx.mount_errors(), sbx.mount_log()) == (None, None)
        assert calls == []
    finally:
        sbx.stop()


def test_o17_the_sandbox_itself_reads_by_the_new_names_and_keeps_its_metadata_keys(world, tmp_path):
    """O17: the collection and the metadata use the new names, so the core raises no deprecation warning of its own,
    and the metadata's keys are those it had."""
    sbx = start(world, "mounted")
    try:
        serve_log(world, sbx, LOG)
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            meta = sbx.metadata()
            sbx.collect(tmp_path / "evidence")
        assert meta["sidecar_log"] == oneclient_log.summarize(LOG)
        assert (meta["container_deaths"], meta["infra_death"]) == ([], [])
        written = json.loads((tmp_path / "evidence" / "metadata.json").read_text(encoding="utf-8"))
        assert set(written["infra_check"]) == {"containers", "deaths", "death"}
    finally:
        sbx.stop()
