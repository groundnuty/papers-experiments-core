"""O56: the Apptainer sandbox on a fake node: the token stays with the gate, every exec is checked whole, and the
teardown leaves nothing."""

import json
import os
from pathlib import Path

import pytest
from apptainer_support import PLACEHOLDER, TOKEN, make_settings, make_spec, start
from fake_node import FakeNode, Unrecorded

from expcore_onedata.sandbox import completion
from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer import runs
from expcore_onedata.sandbox.apptainer.mounthook import MOUNT_REFUSED
from expcore_onedata.sandbox.hostops import CmdResult


@pytest.fixture
def node() -> FakeNode:
    return FakeNode()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def _files_under(root: Path):
    if root.is_file():
        yield root
    for base, _, names in os.walk(root):
        for name in names:
            yield Path(base, name)


def test_the_cell_holds_no_token_in_its_environment_its_argv_or_any_file_it_binds(node, settings):
    """O56, O05: the token reaches the gate's file alone; no exec's argv or environment holds it, no file under any
    bind of any exec does, nor any file left in the run's directory, and the cell's ONEDATA_TOKEN is the placeholder."""
    box = start(node, settings)
    try:
        box.exec("print('hello')", 30)
        assert node.gate_tokens == [TOKEN]
        for done in node.execs:
            assert TOKEN not in " ".join(done.argv)
            assert TOKEN not in json.dumps(done.env)
            for source, _ in done.binds.values():
                for path in _files_under(Path(source)):
                    assert TOKEN.encode() not in path.read_bytes(), path
        for path in _files_under(box.dir):
            assert TOKEN.encode() not in path.read_bytes(), path
        cell = node.execs[-1]
        assert cell.env["ONEDATA_TOKEN"] == PLACEHOLDER
        assert cell.env[AV.RUN_TAG] == box.sid
    finally:
        box.stop()


def test_the_gate_s_token_file_is_gone_once_it_is_ready(node, settings):
    """O56, O05: the run's file of the token is removed as soon as the gate said it is ready, before any cell."""
    box = start(node, settings)
    try:
        assert not (box.dir / "gate-secret" / "gate-token").exists()
        gate = node.gates[-1]
        assert gate.config["listen_host"] == "127.0.0.1"
        assert gate.config["token_file"].endswith("gate-token")
        assert "token" not in gate.config
    finally:
        box.stop()


def test_a_cell_runs_under_the_completion_bootstrap_in_the_apptainer_exec_argv(node, settings):
    """O56, O55: the cell's command is `timeout -s KILL <limit>` and the bootstrap, with the program's size and hash."""
    box = start(node, settings)
    try:
        result = box.exec("print('hi')", 30)
        command = node.execs[-1].command
        assert command[:3] == ["timeout", "-s", "KILL"]
        assert float(command[3]) > 30
        assert command[4:9] == ["python3", "-I", "-B", "-c", completion.BOOTSTRAP]
        assert command[10:] == [str(len(b"print('hi')")), command[11], "python3"]
        assert result.exec_check == {"source_whole": True, "exit_status": 0, "nonce_matched": True}
        assert node.execs[-1].flags[-2:] == ["-q", "exec"]
    finally:
        box.stop()


def test_a_cell_whose_program_arrived_in_part_raises_and_never_reads_as_a_success(node, settings):
    """O56, O55: a program the container's stdin got in part is never run; the exec raises, naming it."""
    box = start(node, settings)
    try:
        node.exec_delivers = lambda data: data[: len(data) // 2]
        with pytest.raises(completion.ExecIncomplete) as raised:
            box.exec("print('a longer program')", 30)
        assert "did not arrive whole" in raised.value.cause
    finally:
        box.stop()


def test_a_cell_with_no_completion_record_raises_the_exit_not_recorded(node, settings):
    """O56, O55: an exec whose record never came (apptainer reported exit 0 and nothing else) raises."""
    box = start(node, settings)
    try:
        node.cell_handler = lambda cell: Unrecorded(0, "", "")
        with pytest.raises(completion.ExecIncomplete) as raised:
            box.exec("print('x')", 30)
        assert "exit was not recorded" in raised.value.cause
    finally:
        box.stop()


def test_o56_guard_a_completion_cause_through_the_sandbox_names_apptainer_exec(node, settings):
    """O56 (a guard, not the fix's test): through the sandbox, the cause of an exec with no record names `apptainer
    exec`, never `docker exec`. This held before the fix, when the cell runner rewrote the shared check's words with a
    `.replace`, and it must keep holding now the check names the runner itself; it pins the end-to-end text. The fix
    is tested in `test_exec_completion.py` (`Completion.runner`), which fails without it. The fake node's
    `Unrecorded(137)` does not reproduce the text the HPC probe's C5 read as `docker exec`: that text's source is not
    reproduced here."""
    box = start(node, settings)
    try:
        node.cell_handler = lambda cell: Unrecorded(137, "", "")
        with pytest.raises(completion.ExecIncomplete) as raised:
            box.exec("print('x')", 30)
        assert "apptainer exec reported exit 137" in raised.value.cause
        assert "docker" not in raised.value.cause
    finally:
        box.stop()


def test_o56_a_cell_has_no_name_server_and_the_record_says_dns_none(node, settings):
    """O56: every cell binds a resolv.conf naming no server and an nsswitch.conf whose hosts are files alone, read-only,
    so the C library resolves the names of /etc/hosts and no other; every run records `dns: none`."""
    box = start(node, settings)
    try:
        box.exec("print(1)", 30)
        binds = node.execs[-1].binds
        for target, text in ((AV.RESOLV_CONF, AV.RESOLV_TEXT), (AV.NSSWITCH_CONF, AV.NSSWITCH_TEXT)):
            source, writable = binds[target]
            assert not writable
            assert Path(source).read_text(encoding="utf-8") == text
        resolv = [line.split() for line in AV.RESOLV_TEXT.splitlines() if line and not line.startswith("#")]
        assert not any(words[0] == "nameserver" for words in resolv)
        assert "hosts: files\n" in AV.NSSWITCH_TEXT
        facts = box.metadata()["infrastructure_facts"]
        assert facts["dns"] == "none"
        assert "dns_none" in facts["facts"]
        assert "hidden_memory" in facts["facts"]
    finally:
        box.stop()


def test_o56_a_normal_run_s_memory_reading_is_within_in_its_infrastructure_facts(node, settings):
    """O56: a run whose cgroup holds what its visible processes hold reads `within`, with its figures, in the
    metadata's infrastructure facts and in the source of the `process_visibility` instrument."""
    box = start(node, settings)
    try:
        box.exec("print(1)", 30)
        memory = box.metadata()["infrastructure_facts"]["memory"]
        assert (memory["reading"], memory["unknown"]) == ("within", [])
        assert memory["margin_bytes"] == settings.apptainer.hidden_memory_margin_bytes
        assert memory["start_unexplained_bytes"] == 0
        seen = box.process_visibility()
        assert (seen["memory"], seen["hidden_process"], seen["hidden_memory"]) == ("within", 0, 0)
    finally:
        box.stop()


def test_a_cell_apptainer_could_not_start_raises_naming_apptainer(node, settings):
    """O56: apptainer's own FATAL exit is never a cell's exit: the exec raises, saying its program did not run."""
    box = start(node, settings)
    try:
        node.cell_handler = lambda cell: Unrecorded(255, "", "FATAL:   container creation failed\n")
        with pytest.raises(completion.ExecIncomplete) as raised:
            box.exec("print('x')", 30)
        assert raised.value.cause.startswith("apptainer could not run the cell's container")
    finally:
        box.stop()


def test_teardown_leaves_no_process_no_directory_and_no_record(node, settings):
    """O56, O15: the stop ends the gate and the collector, every cell's process, the run's directory and its record."""
    box = start(node, settings)
    box.exec("print('x')", 30)
    run_dir = box.dir
    box.stop()
    assert not run_dir.exists()
    assert not runs.record_path(settings.work_root, box.sid).exists()
    assert not [p for p in node.procs.values() if p.alive and p.role != "driver"]
    box.stop()  # idempotent
    assert box.stopped


def test_a_stop_that_cannot_show_the_gate_gone_raises_and_keeps_the_record(node, settings):
    """O56: a helper that outlives its kill fails the teardown, which keeps the run's record and directory."""
    box = start(node, settings)
    gate = node.gates[-1].proc
    gate.ignores = frozenset({15, 9})
    real = node.signal

    def deaf(pid, start_, sig):
        if pid == gate.pid:
            return True  # sent, and nothing happens: a process the kernel does not end
        return real(pid, start_, sig)

    node.signal = deaf
    with pytest.raises(SandboxError, match="gate could not be shown gone"):
        box.stop()
    assert runs.record_path(settings.work_root, box.sid).exists()
    assert box.dir.exists()
    assert not box.stopped


def test_a_mount_run_is_refused_before_anything_exists(node, settings):
    """O56: the mount data path is refused at the start until hook (c)'s helper is given, naming why."""
    with pytest.raises(StartRefused) as refused:
        start(node, settings, make_spec("mounted"))
    assert str(refused.value) == MOUNT_REFUSED
    assert not node.execs
    assert not node.spawned
    assert not list(settings.work_root.iterdir())


def test_an_image_that_is_not_the_pinned_file_refuses_the_start(node, settings):
    """O56, O13: the SIF's SHA-256 must be the pinned one, before anything exists."""
    settings.apptainer.image.path.write_bytes(b"another image\n")
    with pytest.raises(StartRefused, match="not the file it is pinned to"):
        start(node, settings)
    assert not node.spawned


def test_an_image_changed_after_its_pin_refuses_the_next_exec(node, settings):
    """O56, O13: the image's identity is checked before every exec; a file written since is never run."""
    box = start(node, settings)
    try:
        settings.apptainer.image.path.write_bytes(b"an invented SIF image, changed\n")
        with pytest.raises(SandboxError, match="changed on the node"):
            box.exec("print('x')", 30)
    finally:
        box.stop()


def test_a_cell_s_output_is_capped_and_the_cut_recorded(node, settings):
    """O56, O43: the streams are capped as on Docker, and the cut is on the result and the cell's record."""
    box = start(node, settings, output_cap_bytes=16)
    try:
        node.cell_handler = lambda cell: CmdResult(0, "x" * 100, "")
        result = box.exec("print('x' * 100)", 30)
        assert result.stdout == "x" * 16
        assert result.output_cut == {"stdout": {"produced": 100, "kept": 16}}
        assert box.metadata()["cell_output_cuts"] == [{"cell": 1, "stdout": {"produced": 100, "kept": 16}}]
    finally:
        box.stop()


def test_the_gate_s_peer_uid_check_is_on_by_default_only_where_the_gate_has_it(node, settings, monkeypatch, tmp_path):
    """O56: the check is asked of a gate that has it, never claimed for one that would not read it; the operator's
    `gate_options` still decide when they name it."""
    from expcore_onedata.sandbox.apptainer import gaterun

    monkeypatch.setattr(gaterun, "PEER_CHECK", tmp_path / "absent" / "gatepeer.py")
    box = start(node, settings)
    try:
        assert gaterun.PEER_CHECK_KEY not in node.gates[-1].config
        assert box.metadata()["gate"]["peer_uid_check"] is False
    finally:
        box.stop()
    present = tmp_path / "gatepeer.py"
    present.write_text("# the check\n")
    monkeypatch.setattr(gaterun, "PEER_CHECK", present)
    box = start(node, settings)
    try:
        assert node.gates[-1].config[gaterun.PEER_CHECK_KEY] is True
        assert box.metadata()["gate"]["peer_uid_check"] is True
    finally:
        box.stop()


def test_a_peer_uid_check_asked_of_a_gate_without_it_refuses_the_start(node, tmp_path, monkeypatch):
    """O56: an operator's `peer_uid_check = true` for a gate that would ignore it is never a claimed boundary."""
    from expcore_onedata.sandbox.apptainer import gaterun

    monkeypatch.setattr(gaterun, "PEER_CHECK", tmp_path / "absent" / "gatepeer.py")
    settings = make_settings(tmp_path, gate_options={"peer_uid_check": True})
    with pytest.raises(StartRefused, match="does not have it"):
        start(node, settings)
    assert not node.spawned


def test_this_core_s_gate_has_the_peer_uid_check_so_the_apptainer_gate_asks_for_it(node, settings):
    """O56, O61: in this tree `gatepeer.py` sits beside `gate.py`, so every Apptainer gate is asked for the check and
    the run's metadata says it is in effect."""
    from expcore_onedata.sandbox.apptainer import gaterun

    assert gaterun.PEER_CHECK.is_file()
    assert gaterun.PEER_CHECK.parent == gaterun.GATE_PY.parent
    box = start(node, settings)
    try:
        assert node.gates[-1].config[gaterun.PEER_CHECK_KEY] is True
        assert box.metadata()["gate"]["peer_uid_check"] is True
    finally:
        box.stop()


@pytest.mark.parametrize(("level", "kind"), [("via-gate", "rest"), ("isolated", "none")])
def test_o75_a_run_without_a_mount_has_the_statement_that_the_mount_guard_does_not_apply(node, settings, level, kind):
    """O75: the sandbox's own data path, not a missing watcher, makes the guard not apply; the instrument built from the
    sandbox records it, and the core judges it `not_applicable`."""
    from types import SimpleNamespace

    from expcore.mountguard import NAMESPACE, NoMount, judge, mount_guard_for

    box = start(node, settings, make_spec(level))
    try:
        assert box.data_path_kind == kind
        assert box.mount_guard_nonce is None  # no helper: which alone would say nothing of the data path
        instrument = mount_guard_for(box)
        assert isinstance(instrument, NoMount)
        run = SimpleNamespace(run_key="k", attempt=1, instruments={NAMESPACE: instrument.finish()})
        assert (judge(run, "c").verdict, judge(run, "c").not_applicable) == ("not_applicable", kind)
    finally:
        box.stop()
