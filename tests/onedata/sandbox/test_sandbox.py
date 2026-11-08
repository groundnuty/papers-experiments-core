"""The sandbox over an in-memory host: the three data paths, images pinned by id, read-only at every point, teardown."""

import ipaddress
import json

import pytest
from sandbox_support import TOKEN, make_spec, start_options

from expcore_onedata.sandbox.api import DataPathMode, ExecResult, SandboxError, StartRefused
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox


def start(world, level="via-gate", **kw):
    spec = make_spec(world, level, **{k: v for k, v in kw.items() if k in ("lease_key", "run_key")})
    return DockerSandbox.start(world.host, world.s, spec, **start_options(world))


def flat(container) -> str:
    """Every argument docker was given for a container, as text (its environment is separate)."""
    arguments = {k: v for k, v in container.options.items() if k != "_env"}
    return json.dumps(arguments, default=str) + " ".join(container.command)


def nothing_left(world) -> bool:
    return not world.host.containers and not world.host.networks and not world.host.mounts


def test_the_rest_path_has_a_gate_and_an_agent_on_one_internal_network(world):
    sbx = start(world, "via-gate")
    host = world.host
    assert set(host.containers) == {sbx.names.agent, sbx.names.gate}
    assert set(host.networks) == {sbx.names.net}
    args = host.networks[sbx.names.net]["args"]
    assert "--internal" in args
    assert ipaddress.ip_network(host.networks[sbx.names.net]["subnet"]).prefixlen == 28
    assert sbx.names.net in host.containers[sbx.names.agent].networks
    assert sbx.names.net in host.containers[sbx.names.gate].networks
    sbx.stop()
    assert nothing_left(world)


def test_o03_the_gate_serves_the_zone_and_the_pinned_local_provider_only(world):
    """O03: The gate serves the zone and the pinned local provider only."""
    sbx = start(world, "via-gate")
    gate_json = json.loads((sbx.dir / "gate.json").read_text())
    served = {name: h["role"] for name, h in gate_json["hosts"].items()}
    assert served == {"zone.test": "onezone", "alpha.test": "oneprovider"}  # beta.test is declared but not pinned
    agent = world.host.containers[sbx.names.agent]
    assert {a.split(":")[0] for a in agent.options["--add-host"]} == set(served)
    sbx.stop()


def test_o05_the_agent_is_read_only_with_no_capability_and_no_new_privilege(world):
    """O05: The agent is read only with no capability and no new privilege."""
    sbx = start(world, "via-gate")
    options = world.host.containers[sbx.names.agent].options
    assert options["--read-only"] is True
    assert options["--cap-drop"] == ["ALL"]
    assert options["--security-opt"] == "no-new-privileges"
    assert options["--user"] == "10001:10001"
    assert "--cap-add" not in options
    sbx.stop()


def test_o05_the_start_proves_that_the_root_refuses_a_write(world):
    """O05: the sandbox is not handed out on the strength of its flags; it is asked to write and must be refused."""
    sbx = start(world, "via-gate")
    assert sbx.metadata()["read_only_verified"] == {"root": True}
    sbx.stop()


def test_o05_a_writable_root_refuses_the_start_and_leaves_nothing_behind(world):
    """O05: fail closed. A probe that is not refused with a read-only error ends the start, and the teardown runs."""
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(0, "WRITABLE\n")
    with pytest.raises(SandboxError, match="not read-only"):
        start(world, "via-gate")
    assert nothing_left(world)


def test_o05_a_probe_that_cannot_run_also_refuses_the_start(world):
    """O05: A probe that cannot run also refuses the start."""
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(1, "", "no python here")
    with pytest.raises(SandboxError, match="not read-only"):
        start(world, "via-gate")
    assert nothing_left(world)


def test_the_token_is_in_the_agents_environment_on_the_rest_path_and_in_no_argument(world):
    sbx = start(world, "via-gate")
    agent = world.host.containers[sbx.names.agent]
    assert agent.options["_env"]["ONEDATA_TOKEN"] == TOKEN
    assert TOKEN not in flat(agent)
    assert TOKEN not in flat(world.host.containers[sbx.names.gate])
    assert TOKEN not in json.dumps(sbx.metadata())
    sbx.stop()


def test_the_mount_path_has_a_read_only_mount_a_sidecar_and_no_gate(world):
    sbx = start(world, "mounted")
    host = world.host
    assert set(host.containers) == {sbx.names.agent, sbx.names.sidecar}
    agent, sidecar = host.containers[sbx.names.agent], host.containers[sbx.names.sidecar]
    assert any(
        spec.endswith("readonly,bind-propagation=rslave") and "dst=/data" in spec for spec in agent.options["--mount"]
    )
    assert "ro,allow_other" in sidecar.command
    assert sidecar.options["--cap-add"] == ["SYS_ADMIN"]
    assert sbx.metadata()["read_only_verified"] == {"root": True, "mount": True}
    sbx.stop()
    assert nothing_left(world)


def test_the_mount_token_belongs_to_the_sidecar_and_the_agent_has_none(world):
    """O05: the agent has neither the token nor FUSE nor a route; the sidecar takes the token from its environment."""
    sbx = start(world, "mounted")
    agent, sidecar = (world.host.containers[n] for n in (sbx.names.agent, sbx.names.sidecar))
    assert sidecar.options["_env"]["ONECLIENT_ACCESS_TOKEN"] == TOKEN
    assert TOKEN not in flat(sidecar)
    assert TOKEN not in json.dumps(agent.options["_env"])
    assert "--device" not in agent.options
    assert sbx.names.net in agent.networks
    assert "bridge" not in agent.networks
    sbx.stop()


def test_the_tls_mode_of_the_mount_comes_from_the_settings_alone(world):
    sbx = start(world, "mounted")
    command = world.host.containers[sbx.names.sidecar].command
    assert "--insecure" not in command
    assert command[:2] == ["-u", "ONECLIENT_INSECURE"]
    sbx.stop()


def test_no_data_path_means_no_gate_no_mount_no_token_and_still_its_own_network(world):
    sbx = start(world, "isolated")
    assert set(world.host.containers) == {sbx.names.agent}
    agent = world.host.containers[sbx.names.agent]
    assert "_env" not in agent.options or not agent.options["_env"]
    assert "--mount" not in agent.options
    assert set(world.host.networks) == {sbx.names.net}
    sbx.stop()
    assert nothing_left(world)


def test_o13_a_moved_tag_refuses_the_start_before_anything_is_created(world):
    """O13: the tag now resolves to another image than the one pinned; no container, network or directory exists."""
    world.host.add_image(world.s.agent.name, "sha256:" + "9" * 64)
    with pytest.raises(StartRefused, match="agent image no longer resolves"):
        start(world, "via-gate")
    assert nothing_left(world)
    assert not world.s.work_root.exists() or not list(world.s.work_root.iterdir())


def test_o13_each_image_the_path_uses_is_checked_and_started_by_its_id(world):
    """O13: Each image the path uses is checked and started by its id."""
    world.host.add_image(world.s.gate.name, "sha256:" + "8" * 64)
    with pytest.raises(StartRefused, match="gate image no longer"):
        start(world, "via-gate")
    sbx = start(world, "isolated")  # the gate image is not used by a run without a data path
    assert world.host.containers[sbx.names.agent].image == world.s.agent.id
    assert sbx.metadata()["images"] == {"agent": world.s.agent.id}
    sbx.stop()


def test_o13_the_sidecar_image_is_pinned_too(world):
    """O13: The sidecar image is pinned too."""
    world.host.add_image(world.s.oneclient.name, "sha256:" + "7" * 64)
    with pytest.raises(StartRefused, match="oneclient image no longer"):
        start(world, "mounted")


@pytest.mark.parametrize("run_key", ["", "has space", "x" * 201, "tab\tkey"])
def test_a_run_key_that_cannot_be_a_header_value_is_refused(world, run_key):
    with pytest.raises(StartRefused, match="run key"):
        start(world, "isolated", run_key=run_key)


def test_a_token_without_a_data_path_or_a_data_path_without_a_token_is_refused(world):
    spec = make_spec(world, "via-gate")
    from dataclasses import replace

    with pytest.raises(StartRefused, match="data-path token"):
        DockerSandbox.start(world.host, world.s, replace(spec, token=None), **start_options(world))
    with pytest.raises(StartRefused, match="data-path token"):
        DockerSandbox.start(
            world.host, world.s, replace(make_spec(world, "isolated"), token=spec.token), **start_options(world)
        )
    assert nothing_left(world)


def test_o14_two_sandboxes_get_two_disjoint_networks_from_the_pool(world):
    """O14: Two sandboxes get two disjoint networks from the pool."""
    a, b = start(world, "via-gate", run_key="a"), start(world, "isolated", run_key="b")
    nets = [ipaddress.ip_network(world.host.networks[n]["subnet"]) for n in (a.names.net, b.names.net)]
    assert not nets[0].overlaps(nets[1])
    assert all(n.subnet_of(ipaddress.ip_network(world.s.network.pools[0])) for n in nets)
    a.stop()
    b.stop()


def test_a_failure_while_starting_stops_everything_that_was_started_in_order(world):
    """O15: the teardown runs on a failed start, with the mount's order, and verifies the result."""
    world.host.fail_run.add("never")  # nothing special: fail the agent only
    sbx_name = {}

    def fail_agent(argv):
        return CmdResult(0)

    del fail_agent, sbx_name
    original = world.host._run_container

    def failing(args, env):
        if "--init" in args:  # the agent's `docker run` is the only one with --init
            return CmdResult(125, "", "cannot start the agent")
        return original(args, env)

    world.host._run_container = failing
    with pytest.raises(SandboxError, match="cannot start the agent"):
        start(world, "mounted")
    assert nothing_left(world)
    kinds = [c[0] for c in world.host.calls if c[0] in ("removed", "network_removed", "unmount")]
    assert kinds.index("removed") < kinds.index("network_removed")  # containers before the network


def test_stop_is_idempotent_and_marks_the_sandbox_stopped(world):
    sbx = start(world, "via-gate")
    assert not sbx.stopped
    sbx.stop()
    sbx.stop()
    assert sbx.stopped
    with pytest.raises(SandboxError, match="not running"):
        sbx.exec("print(1)", 5)


def test_a_stop_that_cannot_remove_a_container_raises_and_can_be_retried(world):
    sbx = start(world, "via-gate")
    world.host.fail_remove.add(sbx.names.gate)
    with pytest.raises(SandboxError, match="cleanup incomplete"):
        sbx.stop()
    assert not sbx.stopped
    world.host.fail_remove.clear()
    sbx.stop()
    assert sbx.stopped
    assert nothing_left(world)


def test_a_failed_start_whose_cleanup_fails_too_is_registered_for_the_next_start(world):
    """The sandbox that could not be cleaned up is reachable on the exception and retried by the next start."""
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(0, "WRITABLE\n")
    world.host.fail_remove.add(f"{world.s.name_prefix}-ghost")
    original = world.host.docker

    def stubborn(*args, **kwargs):
        if args[0] == "rm" and "-gate" in args[-1]:
            return CmdResult(1, "", "removal failed")
        return original(*args, **kwargs)

    world.host.docker = stubborn
    with pytest.raises(SandboxError) as caught:
        start(world, "via-gate")
    assert caught.value.sandbox is not None
    assert world.registry.pending() == 1
    world.host.docker = original
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(0, "blocked 30\n")
    sbx = start(world, "isolated")
    assert world.registry.pending() == 0
    assert sbx.metadata()["pending_cleanups_left"] == 0
    sbx.stop()
    assert nothing_left(world)


def test_resources_carry_the_lease_key_in_their_names_and_labels(world):
    sbx = start(world, "via-gate", lease_key="login-data")
    assert sbx.sid.startswith("login-data-")
    for container in world.host.containers.values():
        assert container.labels["org.test.lease"] == "login-data"
        assert container.labels["org.test.sandbox"] == "1"
    assert world.host.networks[sbx.names.net]["labels"]["org.test.lease"] == "login-data"
    sbx.stop()


def test_exec_runs_the_code_as_the_unprivileged_user_in_the_working_directory(world):
    sbx = start(world, "isolated")
    seen = []

    def handler(container, argv, stdin):
        seen.append((argv, stdin))
        return CmdResult(0, "42\n")

    world.host.exec_handler = handler
    result = sbx.exec("print(42)", 30)
    assert isinstance(result, ExecResult)
    assert (result.stdout, result.exit_code, result.timed_out, result.kill) == ("42\n", 0, False, None)
    argv, stdin = seen[0]
    assert argv[:5] == ["timeout", "-s", "KILL", argv[3], "python"]
    assert stdin == "print(42)"
    exec_calls = [c for c in world.host.calls if c[0] == "docker" and c[1] == "exec"]
    assert exec_calls
    with pytest.raises(ValueError, match="at least 1 second"):
        sbx.exec("print(1)", 0.5)
    sbx.stop()


def test_a_collection_copies_the_named_outputs_and_the_gate_log(world, tmp_path):
    sbx = start(world, "via-gate")
    (sbx.dir / "work" / "output").mkdir(parents=True)
    (sbx.dir / "work" / "output" / "report.json").write_text("{}", encoding="utf-8")
    (sbx.dir / "gatelog" / "requests.jsonl").write_text(
        json.dumps({"tag": "content", "decision": "allow", "status": 200, "bytes": 7}) + "\n", encoding="utf-8"
    )
    got = sbx.collect(tmp_path / "evidence", outputs=["output/report.json", "output/missing.json"])
    assert got["outputs"] == {"output/report.json": True, "output/missing.json": False}
    assert (tmp_path / "evidence" / "work" / "output" / "report.json").read_text() == "{}"
    assert (tmp_path / "evidence" / "gate-requests.jsonl").exists()
    meta = json.loads((tmp_path / "evidence" / "metadata.json").read_text())
    assert meta["gate"]["content_requests"] == 1
    assert meta["gate"]["errors"]["gate_5xx"] == 0
    sbx.stop()


def test_a_collection_refuses_an_output_outside_the_working_directory(world, tmp_path):
    sbx = start(world, "isolated")
    for bad in ("../x", "/etc/passwd"):
        with pytest.raises(SandboxError, match="relative path"):
            sbx.collect(tmp_path / "e", outputs=[bad])
    sbx.stop()


def test_a_failed_collection_keeps_the_disks_when_the_sandbox_stops(world, tmp_path):
    """A source is never deleted after a failed copy."""
    sbx = start(world, "isolated")
    (sbx.dir / "work" / "out.txt").write_text("x", encoding="utf-8")
    world.host.run_handlers["cp"] = lambda argv: CmdResult(1, "", "disk full")
    with pytest.raises(SandboxError, match="copying"):
        sbx.collect(tmp_path / "e", outputs=["out.txt"])
    world.host.run_handlers.pop("cp")
    sbx.stop()
    kept = world.s.work_root / "_kept_evidence" / sbx.sid
    assert (kept / "work" / "out.txt").read_text() == "x"


def test_the_data_path_modes_of_a_start_are_recorded_by_name(world):
    sbx = start(world, "mounted")
    assert sbx.metadata()["data_path"] == DataPathMode.MOUNT.value
    sbx.stop()
