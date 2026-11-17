"""The service verbs over M1's own objects (O58): `hpcfed.py stop|start|status|exec` after an `up`, through the hook,
into `hpcfed_control.run` with M1's `Launcher` and `StateStore` over the fake Slurm of hpc_fed_support; the read-only
proof's hook as `hpcfed.py` builds it; and the login node's fault relay as `up` and `down` start and stop it, a real
process of this core's module.

Nothing here reaches a cluster: every step is the fake Slurm's, and the relay is a local process that runs no verb.
"""

import contextlib
import inspect
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeProcess, FakeSlurm, agent_site, hpc_on_path

PROVIDERS = ["aspen", "larch", "rowan", "alder", "hazel"]
END_TIME = "2026-11-16T12:00:00"  # the fake scontrol's EndTime: the login node's local time, with no zone


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcfed_control
        import hpcrelay

        modules.control, modules.relay = hpcfed_control, hpcrelay
        yield modules


@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}


def stop_line(ended="exit", seconds=3.5):
    return f'kept stop ended\nhpcop-stop {{"ended": "{ended}", "seconds": {seconds}}}\n'


class Federation:
    """A federation `up` brought up over the fake Slurm, and the node side of the service verbs: status.sh reads a
    service alive (with its ready marker) while its step is listed, teardown.sh --keep ends its step, exec.sh answers
    what `exec_answer` says."""

    def __init__(self, hpc, tmp_path, site, monkeypatch):
        self.hpc, self.fake = hpc, FakeSlurm()
        self.root = tmp_path / "fed" / "root"
        driver = hpc.cli.make_driver(LAYOUT, str(self.root), runner=self.fake)
        driver.sleep, driver.poll_s = (lambda s: None), 0.0
        driver.slurm.sleep, driver.slurm.poll_s = (lambda s: None), 0.0
        driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        driver.relay_available = lambda: False
        driver.up(hpc.settings.site_from_environment(site))
        self.driver = driver
        self.exec_answer = (0, "pong\n", "")
        self.teardowns = []
        self.fake.on_control = self.node
        monkeypatch.setattr(hpc.control, "_SLEEP", lambda s: None)

    def state(self):
        return self.driver.store.read()

    def step_of(self, service):
        return self.state()["services"][service]["step"]["name"]

    def node(self, srun):
        """The node side of a verb's control step; None for every other step (the fake's own answer)."""
        from hpcslurm import Completed

        script = Path(srun.command[1]).name if len(srun.command) > 1 else ""
        if script == "status.sh":
            service = self.service_at(srun.command[srun.command.index("--workdir") + 1])
            alive = self.step_of(service) in self.fake.steps
            reading = {"alive": alive, "pid": 4321 if alive else None, "start": "88" if alive else None}
            return FakeProcess(Completed(0, json.dumps({**reading, "ready": alive}) + "\n", ""))
        if script == "teardown.sh":  # with --keep a verb's stop; without it down's: either ends the service's step
            self.teardowns.append(srun)
            service = self.service_at(srun.command[srun.command.index("--workdir") + 1])
            self.fake.steps.pop(self.step_of(service), None)
            return FakeProcess(Completed(0, stop_line() if "--keep" in srun.command else "", ""))
        if script == "exec.sh":
            return FakeProcess(Completed(*self.exec_answer))
        return None

    def service_at(self, workdir):
        (name,) = [n for n, r in self.state()["services"].items() if r.get("workdir") == workdir]
        return name

    def verb(self, *words):
        """`hpcfed.py <words>` as the relay runs it, its stdout parsed when it printed one JSON line."""
        out, err = io.StringIO(), io.StringIO()
        argv = ["--layout", str(LAYOUT), "--root", str(self.root), *words]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sys, "stdout", out)
            patch.setattr(sys, "stderr", err)
            status = self.hpc.cli.main(argv, {}, runner=self.fake)
        return status, out.getvalue(), err.getvalue()


@pytest.fixture
def fed(hpc, tmp_path, site, monkeypatch):
    return Federation(hpc, tmp_path, site, monkeypatch)


def test_o58_a_stop_through_hpcfed_runs_teardown_keep_as_an_exact_control_step_and_records_its_evidence(fed):
    """O58: the stop is one control step of M1's launcher on the provider's own node, `--exact` in the control
    reserve, teardown.sh --keep with the allocation's end (Slurm's EndTime, the login node's local time) and the
    layout's stop_seconds; M1's state file then says stopped and holds the stop's evidence, which the verb prints."""
    status, out, err = fed.verb("stop", "aspen")
    assert status == 0, err
    evidence = json.loads(out)
    assert (evidence["stopped"], evidence["ended"], evidence["service_seconds"]) == (True, "exit", 3.5)
    assert evidence["seconds"] >= 0
    [teardown] = fed.teardowns
    record = fed.state()["services"]["aspen"]
    assert teardown.node == record["node"]
    assert (teardown.exact, teardown.overlap, teardown.cpus, teardown.mem) == (True, False, 1, "1024M")
    assert teardown.name.startswith(f"{fed.state()['allocation']['name']}-aspen-stop-")
    until = int(datetime.fromisoformat(END_TIME).timestamp())
    script = str(fed.hpc.control.BUNDLE / "teardown.sh")
    keep = ["--keep", "--keep-until", str(until), "--timeout", "50"]
    assert teardown.command == ["bash", script, "op", "--workdir", record["workdir"], *keep]
    assert record["state"] == "stopped"
    assert record["stops"] == [evidence]


def test_o58_status_start_and_status_again_go_through_m1s_launcher_and_state_store(fed):
    """O58: status reads the step list and status.sh; a start of the stopped provider is a detached service step of
    the provider's size running restart.sh, its log in the site's log directory, its srun client recorded by M1's
    launcher, and the state says running with the new launch; the identity changed."""
    before = json.loads(fed.verb("status", "aspen")[1])
    assert (before["running"], before["ready"], before["launch"]) == (True, True, 1)
    assert fed.verb("stop", "aspen")[0] == 0
    assert json.loads(fed.verb("status", "aspen")[1])["running"] is False
    status, out, err = fed.verb("start", "aspen")
    assert (status, out) == (0, ""), err
    state = fed.state()
    record = state["services"]["aspen"]
    name = f"{state['allocation']['name']}-aspen-run-2"
    assert (record["state"], record["launch"], record["step"]["name"]) == ("running", 2, name)
    assert record["step"]["id"] == fed.fake.steps[name]
    assert record["log"] == str(Path(state["site"]["logs"]) / f"{name}.log")
    restart = fed.fake.spawned[-1]
    assert restart.command == ["bash", str(fed.hpc.control.BUNDLE / "restart.sh"), "op", "--workdir", record["workdir"]]
    assert (restart.node, restart.cpus, restart.mem, restart.output) == (record["node"], 4, "10240M", record["log"])
    assert name in state["clients"]
    after = json.loads(fed.verb("status", "aspen")[1])
    assert after["running"] is True
    assert after["identity"] != before["identity"]


def test_o58_exec_through_hpcfed_passes_the_commands_output_and_reads_an_unknown_end_as_124(fed):
    """O58: exec's argv after -- reaches exec.sh whole; a step whose end M1's launcher could not read (returncode
    None) is 124, never the 0 its output might suggest."""
    status, out, _ = fed.verb("exec", "aspen", "--", "/usr/sbin/op_worker", "ping")
    assert (status, out) == (0, "pong\n")
    workdir = fed.state()["services"]["aspen"]["workdir"]
    assert fed.fake.sruns[-1].command[2:] == ["op", "--workdir", workdir, "--", "/usr/sbin/op_worker", "ping"]
    fed.exec_answer = (None, "pong\n", "")
    assert fed.verb("exec", "aspen", "--", "/usr/sbin/op_worker", "ping")[0] == 124


def test_o58_a_status_step_that_outlives_its_bound_is_cancelled_by_m1_and_is_a_reading_that_failed(hpc, fed):
    """O58: M1's launcher cancels a control step still listed at its bound and raises StepTimeout (a TimeoutError):
    the status is exit 1 with nothing on stdout, and only that step was cancelled."""
    ticks = iter(range(0, 10**6, 20))
    driver = hpc.cli.make_driver(LAYOUT, str(fed.root), runner=fed.fake)
    driver.slurm.clock = lambda: float(next(ticks))  # 20 s pass at every look: the bound comes at once
    driver.slurm.poll_s = 0.0

    def hang(srun):
        if Path(srun.command[1]).name == "status.sh":
            fed.fake.steps[srun.name] = "4242.99"
            return FakeProcess(None, hangs=True)
        return None

    fed.fake.on_control = hang
    out, err = io.StringIO(), io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "stdout", out)
        patch.setattr(sys, "stderr", err)
        assert hpc.cli.control_hook(driver, "status", "aspen", []) == 1
    assert out.getvalue() == ""
    assert "could not be read" in err.getvalue()
    assert fed.fake.cancelled == ["4242.99"]


def test_o58_down_takes_down_a_federation_whose_provider_the_verbs_stopped_and_started(fed):
    """O58: after a stop and a start, M1's down tears every service down (without --keep), ends every recorded client,
    cancels the held job and removes the root."""
    assert fed.verb("stop", "larch")[0] == 0
    assert fed.verb("start", "larch")[0] == 0
    assert fed.driver.down().startswith("down: the held job 4242 is gone")
    assert not fed.root.exists()
    plain = [s for s in fed.teardowns if "--keep" not in s.command]
    assert len(plain) == len(PROVIDERS) + 1


# -- the read-only proof's hook (O59, #315) -------------------------------------------------------------------------


def test_o58_without_the_shared_import_the_hook_hands_the_verbs_no_check(hpc, fed, monkeypatch):
    """O58: a core with no `hpcimport` (the shared import, #315, not merged) gives `mounts=None`."""
    monkeypatch.setitem(sys.modules, "hpcimport", None)  # an import of it fails, whatever the path holds
    assert hpc.cli.mount_check(fed.driver) is None


def test_o58_with_the_shared_import_the_hook_builds_the_check_from_its_three_calls(hpc, fed, monkeypatch):
    """O58: once `hpcimport` is importable, the verbs get the owner's shared spaces from the layout, the check's argv
    on the bundle beside the driver with the node's Python, and its report reader."""
    calls = []
    fake = SimpleNamespace(
        shared_imports_of=lambda layout, provider: calls.append((layout, provider)) or [SimpleNamespace(name="cohort")],
        mount_check_argv=lambda bundle, workdir, python: ["check", bundle, workdir, python],
        mount_check_problems=lambda report, spaces: ["unproven"],
    )
    monkeypatch.setitem(sys.modules, "hpcimport", fake)
    mounts = hpc.cli.mount_check(fed.driver)
    assert mounts.spaces("aspen") == ["cohort"]
    assert calls == [(fed.driver.layout, "aspen")]
    assert mounts.argv("/tmp/hpcop-op.aspxyz", "python3.11") == [
        "check",
        str(fed.hpc.control.BUNDLE),
        "/tmp/hpcop-op.aspxyz",
        "python3.11",
    ]
    assert mounts.problems is fake.mount_check_problems


def test_o58_a_restarted_owner_through_hpcfed_is_ready_only_after_the_check_on_its_node(hpc, fed, monkeypatch):
    """O58 (O59's hook): over M1's objects, a start clears the owner's proof, status runs the check as a control step
    on the owner's node once it is otherwise ready, and the proof is recorded for the new launch."""
    fed.driver.set(lambda s: s["services"]["aspen"].update(mounts_proven=True))  # what #315's up records
    fake = SimpleNamespace(
        shared_imports_of=lambda layout, provider: [SimpleNamespace(name="cohort")] if provider == "aspen" else [],
        mount_check_argv=lambda bundle, workdir, python: [python, "-I", "-B", f"{bundle}/importstorage.py", "mounts"],
        mount_check_problems=lambda report, spaces: [] if json.loads(report) == [{"space": "cohort"}] else ["no"],
    )
    monkeypatch.setitem(sys.modules, "hpcimport", fake)
    node = fed.node

    def checking(srun):
        from hpcslurm import Completed

        if srun.command[-1] == "mounts":
            return FakeProcess(Completed(0, json.dumps([{"space": "cohort"}]) + "\n", ""))
        return node(srun)

    fed.fake.on_control = checking
    assert fed.verb("stop", "aspen")[0] == 0
    assert fed.verb("start", "aspen")[0] == 0
    assert fed.state()["services"]["aspen"]["mounts_proven"] is False
    assert json.loads(fed.verb("status", "aspen")[1])["ready"] is True
    [check] = [s for s in fed.fake.sruns if s.command[-1] == "mounts"]
    assert check.node == fed.state()["services"]["aspen"]["node"]
    assert check.name.startswith(f"{fed.state()['allocation']['name']}-aspen-mounts-")
    assert fed.state()["services"]["aspen"]["mounts_proven"] is True


def test_o58_the_shared_imports_calls_are_the_ones_the_hook_names(hpc):
    """O58: #315's `hpcimport` is in this core, and its three calls take what the hook passes them."""
    with hpc_on_path():
        import hpcimport
    assert list(inspect.signature(hpcimport.shared_imports_of).parameters) == ["layout", "provider"]
    assert list(inspect.signature(hpcimport.mount_check_argv).parameters) == ["bundle", "workdir", "python"]
    assert list(inspect.signature(hpcimport.mount_check_problems).parameters) == ["report", "spaces"]


# -- the read-only proof over this core's own hpcimport and the pilot-shape-import layout (O59) -----------------------


class ImportFederation(Federation):
    """`up` of the pilot-shape-import layout (each data space a shared tree its owner imports), the mount check on
    each owner's node answered from `held`: the space and how many of its processes hold it writable."""

    def __init__(self, hpc, tmp_path, site, monkeypatch):
        from hpc_import_support import IMPORT_LAYOUT, SPACES, make_environ

        self.spaces, self.writable, self.checks = SPACES, 0, []
        self.environ = make_environ(tmp_path)
        self.layout = IMPORT_LAYOUT
        self.hpc, self.fake = hpc, FakeSlurm()
        self.root = tmp_path / "fed" / "root"
        self.fake.on_control = self.node
        driver = hpc.cli.make_driver(IMPORT_LAYOUT, str(self.root), runner=self.fake, environ=self.environ)
        driver.sleep, driver.poll_s = (lambda s: None), 0.0
        driver.slurm.sleep, driver.slurm.poll_s = (lambda s: None), 0.0
        driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        driver.relay_available = lambda: False
        self.driver, self.upped = driver, False
        self.exec_answer = (0, "pong\n", "")
        self.teardowns = []
        driver.up(hpc.settings.site_from_environment(site))
        self.upped = True  # from here on the node side answers the verbs' steps too
        monkeypatch.setattr(hpc.control, "_SLEEP", lambda s: None)

    def node(self, srun):
        from hpcslurm import Completed

        if any(Path(word).name == "importstorage.py" for word in srun.command):
            self.checks.append(srun)
            owner = self.service_at(srun.command[srun.command.index("--workdir") + 1])
            facts = [
                {"space": space, "processes": 3, "read_only": 3 - self.writable, "writable": self.writable}
                for space, (who, _) in self.spaces.items()
                if who == owner
            ]
            return FakeProcess(Completed(0, json.dumps(facts) + "\n", ""))
        return super().node(srun) if self.upped else None

    def verb(self, *words):
        out, err = io.StringIO(), io.StringIO()
        argv = ["--layout", str(self.layout), "--root", str(self.root), *words]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(sys, "stdout", out)
            patch.setattr(sys, "stderr", err)
            status = self.hpc.cli.main(argv, self.environ, runner=self.fake)
        return status, out.getvalue(), err.getvalue()


@pytest.fixture
def imported(hpc, tmp_path, site, monkeypatch):
    return ImportFederation(hpc, tmp_path, site, monkeypatch)


def test_o59_with_this_cores_hpcimport_the_hook_is_on_for_an_owner_and_off_for_a_mirror(hpc, imported):
    """O59: `hpcfed.mount_check` builds the proof from this core's `hpcimport`: an owner's shared spaces, none for
    aspen, which owns no tree, and the check's argv on the bundle beside the driver with the node's Python."""
    mounts = hpc.cli.mount_check(imported.driver)
    assert mounts.spaces("larch") == ["alpha-space"]
    assert mounts.spaces("aspen") == []
    argv = mounts.argv("/tmp/hpcop-op.larxyz", "python3.11")
    script = str(hpc.control.BUNDLE / "importstorage.py")
    assert argv == ["python3.11", "-I", "-B", script, "mounts", "--workdir", "/tmp/hpcop-op.larxyz"]


def test_o59_a_restarted_owner_is_ready_only_once_this_cores_check_reproves_its_tree_for_that_launch(imported):
    """O59: up proved larch's tree read-only; a stop and a start clear the proof; the first status that finds larch
    otherwise ready runs `importstorage.py mounts` on larch's own node, reads its report with `mount_check_problems`
    against alpha-space, and only then records the proof and answers ready."""
    assert imported.state()["services"]["larch"]["mounts_proven"] is True  # up's own check
    up_checks = len(imported.checks)
    assert imported.verb("stop", "larch")[0] == 0
    assert imported.verb("start", "larch")[0] == 0
    assert imported.state()["services"]["larch"]["mounts_proven"] is False
    status, out, err = imported.verb("status", "larch")
    assert (status, json.loads(out)["ready"]) == (0, True), err
    [check] = imported.checks[up_checks:]
    record = imported.state()["services"]["larch"]
    assert check.node == record["node"]
    assert check.command[-2:] == ["--workdir", record["workdir"]]
    assert (record["mounts_proven"], record["launch"]) == (True, 2)
    assert imported.verb("status", "larch")[0] == 0
    assert len(imported.checks) == up_checks + 1  # proven for this launch: not checked again


def test_o59_a_restarted_owner_whose_tree_is_writable_in_a_process_is_never_ready(imported):
    """O59: the report says a process holds alpha-space writable: larch runs but is not ready, the proof stays
    cleared, and the message names the space, never a path."""
    assert imported.verb("stop", "larch")[0] == 0
    assert imported.verb("start", "larch")[0] == 0
    imported.writable = 1
    status, out, err = imported.verb("status", "larch")
    reading = json.loads(out)
    assert (status, reading["running"], reading["ready"]) == (0, True, False)
    assert imported.state()["services"]["larch"]["mounts_proven"] is False
    assert "alpha-space" in err
    assert str(imported.root) not in err


def test_o59_a_mirror_that_owns_no_tree_is_ready_again_with_no_check(imported):
    """O59: aspen owns no shared tree: its restart needs no proof, and no check runs on its node."""
    before = len(imported.checks)
    assert imported.verb("stop", "aspen")[0] == 0
    assert imported.verb("start", "aspen")[0] == 0
    assert json.loads(imported.verb("status", "aspen")[1])["ready"] is True
    assert len(imported.checks) == before
    assert "mounts_proven" not in imported.state()["services"]["aspen"]


# -- the agent's node is never a fault verb's (O57's sandbox record) -------------------------------------------------


def agent_layout(folder):
    """The pilot-shape layout with the agent's node held as component 1 of the job, beside its platform template."""
    folder.mkdir()
    control = 'control = { cpus = 1, memory = "1g" }'
    text = LAYOUT.read_text(encoding="utf-8")
    assert control in text
    agent = f'{control}\nsandbox = {{ cpus = 8, memory = "17g" }}'
    (folder / LAYOUT.name).write_text(text.replace(control, agent), encoding="utf-8")
    template = LAYOUT.parent / "pilot-shape.platform.toml.template"
    (folder / template.name).write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    return folder / LAYOUT.name


@pytest.mark.parametrize(
    "words",
    [("stop", "sandbox"), ("start", "sandbox"), ("status", "sandbox"), ("exec", "sandbox", "--", "op_worker", "ping")],
)
def test_o58_no_fault_verb_acts_on_the_agents_node(hpc, tmp_path, site, monkeypatch, words):
    """O58: the agent's node is a role of the held job (`services.sandbox`, kind `sandbox`) but no provider's: every
    verb on it is exit 2, no step runs on any node, and the state file is unchanged; the relay never lists it."""
    from expcore_onedata.fault_relay import services_of

    layout = agent_layout(tmp_path / "layout")
    fed = Federation.__new__(Federation)
    fed.hpc, fed.fake, fed.root = hpc, FakeSlurm(), tmp_path / "fed" / "root"
    driver = hpc.cli.make_driver(layout, str(fed.root), runner=fed.fake)
    driver.sleep, driver.poll_s = (lambda s: None), 0.0
    driver.slurm.sleep, driver.slurm.poll_s = (lambda s: None), 0.0
    driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
    driver.relay_available = lambda: False
    driver.up(hpc.settings.site_from_environment(site))
    state = driver.store.read()
    assert state["services"]["sandbox"]["kind"] == "sandbox"
    assert state["sandbox"]["node"] == state["services"]["sandbox"]["node"]
    steps = len(fed.fake.sruns)
    out, err = io.StringIO(), io.StringIO()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, "stdout", out)
        patch.setattr(sys, "stderr", err)
        status = hpc.cli.main(["--layout", str(layout), "--root", str(fed.root), *words], {}, runner=fed.fake)
    assert status == 2
    assert out.getvalue() == ""
    assert "neither an Oneprovider nor an Onezone" in err.getvalue()
    assert len(fed.fake.sruns) == steps
    assert driver.store.read() == state
    assert services_of(fed.root) == set(PROVIDERS)  # the relay's allowlist: the providers alone


# -- the login node's fault relay, as up and down run it ----------------------------------------------------------


def test_o58_this_core_has_the_relay_up_starts(hpc):
    """O58: `up` starts the relay only when it finds this module, and it does."""
    assert hpc.relay.available() is True
    assert hpc.relay.RELAY == "expcore_onedata.fault_relay"


def test_o58_the_relay_up_starts_beats_and_the_stop_down_runs_ends_it(hpc, tmp_path):
    """O58: M1's start of the relay (its argv, behind env -i, through the real runner) brings up this core's relay,
    which beats for `alive`; M1's stop ends it, its heartbeat goes, and its process is gone. It runs no verb."""
    from hpcslurm import Runner

    root = tmp_path / "root"
    root.mkdir(mode=0o700)
    log = tmp_path / "relay.log"
    command = [sys.executable, "-I", "-B", "/nowhere/hpcfed.py", "--layout", "L", "--root", str(root)]
    recorded = []
    environ = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(tmp_path)}
    runner = Runner()
    client = hpc.relay.start(
        runner, sys.executable, root, command, log, environ, record=recorded.append, clock=time.monotonic,
        sleep=time.sleep,
    )  # fmt: skip
    try:
        assert recorded == [client]
        assert runner.client_alive(client)
        beat = json.loads((root / "fault-relay" / "heartbeat.json").read_text(encoding="utf-8"))
        assert beat["pid"] == client.pid
        assert hpc.relay.stop(runner, sys.executable, root) is True
        assert not (root / "fault-relay" / "heartbeat.json").exists()
        deadline = time.monotonic() + 10
        while runner.client_alive(client) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not runner.client_alive(client)
    finally:
        runner.end_client(client, grace_s=2.0)
        with contextlib.suppress(ChildProcessError):
            os.waitpid(client.pid, os.WNOHANG)
