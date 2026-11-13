"""The sandbox over an in-memory host: the three data paths, images pinned by id, read-only at every point, teardown."""

import ipaddress
import json
import os
import stat
import time
from pathlib import Path

import pytest
from fake_host import CERTIFI_BUNDLE
from sandbox_support import (
    COMPLETE_LEVEL,
    FED_LOCAL,
    FED_NETWORK,
    FED_ZONE,
    TOKEN,
    fail_copies,
    make_spec,
    start_options,
)

from expcore.errors import InfraError
from expcore_onedata.sandbox.api import DataPathMode, ExecResult, SandboxError, StartRefused
from expcore_onedata.sandbox.containers import ENV_TOKEN, GATE_TOKEN_PATH, SIDECAR_TOKEN_PATH
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import GATE_TOKEN_FILE, TOKEN_FILE, DockerSandbox


def start(world, level="via-gate", **kw):
    spec = make_spec(world, level, **{k: v for k, v in kw.items() if k in ("lease_key", "run_key")})
    return DockerSandbox.start(world.host, world.s, spec, **start_options(world))


def flat(container) -> str:
    """Every argument docker was given for a container, as text (the fake's own records, `_env` and `_files`, apart)."""
    arguments = {k: v for k, v in container.options.items() if not k.startswith("_")}
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


CREDENTIAL_WORDS = ("TOKEN", "SECRET", "PASSWORD", "KEY", "AUTH", "CREDENTIAL")


def bound_sources(container) -> list[Path]:
    """The host paths bound into a container: where its files come from."""
    out = [Path(spec.split(":")[0]) for spec in container.options.get("-v", [])]
    for spec in container.options.get("--mount", []):
        out += [Path(part[4:]) for part in spec.split(",") if part.startswith("src=")]
    return out


def files_under(path: Path) -> list[Path]:
    return [f for f in (path.rglob("*") if path.is_dir() else [path]) if f.is_file()]


def where_a_credential_is_visible(world, sbx, token: str) -> list[str]:
    """Every place the agent's code could read `token`, or any credential, from inside the agent: its environment, its
    files (what is bound into it and its working directory), its process arguments and the commands run in it."""
    agent = world.host.containers[sbx.names.agent]
    env = agent.options.get("_env", {})
    found = []
    if token in json.dumps(env):
        found.append("the environment")
    placeholder = (ENV_TOKEN, world.s.placeholder_token)  # named like a token, holding the placeholder: none (O23)
    found += [
        f"environment variable {n}"
        for n, v in env.items()
        if any(w in n.upper() for w in CREDENTIAL_WORDS) and (n, v) != placeholder
    ]
    if token in flat(agent) or any(token in word for word in agent.command):
        found.append("the process arguments")
    for source in bound_sources(agent):
        for file in files_under(source):
            if file.suffix == ".key" or token in file.read_text(encoding="utf-8", errors="replace"):
                found.append(f"the file {file.name}")
    return found


@pytest.mark.parametrize("level", ["via-gate", "mounted", "isolated"])
def test_o05_no_credential_is_visible_inside_the_agent_on_any_data_path(world, level):
    """O05: the agent's code must never be able to read a token: not in its environment, its files or its arguments."""
    seen = []
    world.host.exec_handler = lambda container, argv, stdin: (seen.append((argv, stdin)), CmdResult(0, "blocked 30\n"))[
        1
    ]
    sbx = start(world, level)
    sbx.exec("print(1)", 30)
    assert where_a_credential_is_visible(world, sbx, TOKEN) == []
    assert seen
    assert not [1 for argv, stdin in seen if TOKEN in " ".join(argv) or TOKEN in (stdin or "")]
    sbx.stop()


def test_o23_a_rest_agent_holds_the_placeholder_as_its_token_and_the_gate_the_runs(world):
    """O23, O05: the REST agent's ONEDATA_TOKEN is the configured placeholder, which is not the run's token; the gate
    holds the run's token, from its file. Neither container's environment holds the other's value."""
    sbx = start(world, "via-gate")
    agent, gate = (world.host.containers[name].options["_env"] for name in (sbx.names.agent, sbx.names.gate))
    assert agent[ENV_TOKEN] == world.s.placeholder_token != TOKEN
    assert TOKEN not in json.dumps(agent)
    assert world.s.placeholder_token not in json.dumps(gate)
    sbx.stop()


def test_o23_o05_a_placeholder_that_is_the_runs_token_refuses_the_start_before_anything_exists(world):
    """O23, O05: a placeholder configured as the run's own token would put the token in the agent's environment; the
    start is refused before anything is created, and the message names the key, never the value."""
    from dataclasses import replace

    settings = replace(world.s, placeholder_token=TOKEN)
    with pytest.raises(StartRefused, match="placeholder_token") as caught:
        DockerSandbox.start(world.host, settings, make_spec(world, "via-gate"), **start_options(world))
    assert TOKEN not in str(caught.value)
    assert nothing_left(world)
    assert not [call for call in world.host.calls if call[:2] == ("docker", "run")]


def test_o05_the_gate_takes_the_rest_token_from_a_root_owned_file_bound_into_it_alone_and_the_host_keeps_none(world):
    """O05: the token is the gate's. It reaches the gate as a file bound into the gate's container alone, read-only, at
    the path the gate's configuration names; the file is made the gate's own user's (root's: the gate has no capability
    to read another's) and owner-only; and the host's copy is gone once the gate is ready. Docker is given no
    environment for it, and no argument, record or file of the run holds it."""
    sbx = start(world, "via-gate")
    gate, agent = world.host.containers[sbx.names.gate], world.host.containers[sbx.names.agent]
    bound = gate.options["_files"]
    assert bound[GATE_TOKEN_PATH] == TOKEN
    assert [path for path, text in bound.items() if TOKEN in text] == [GATE_TOKEN_PATH]
    assert not [text for text in agent.options["_files"].values() if TOKEN in text]
    assert json.loads((sbx.dir / "gate.json").read_text())["token_file"] == GATE_TOKEN_PATH
    assert gate.options["_env"] == {}
    assert "-e" not in gate.options
    assert ["chown", "0:0", str(sbx.dir / GATE_TOKEN_FILE)] in world.host.sudo_commands
    assert not (sbx.dir / GATE_TOKEN_FILE).exists()
    assert TOKEN not in flat(gate)
    assert not [f for f in files_under(sbx.dir) if TOKEN in f.read_text(encoding="utf-8", errors="replace")]
    assert TOKEN not in json.dumps(sbx.metadata())
    sbx.stop()


def test_o05_the_gates_tls_keys_are_given_to_its_user_read_only_as_its_token_file_is(world):
    """O30 (#203): the gate runs as root with every capability dropped but NET_BIND_SERVICE, so it reads only a file
    root owns or one anyone may read. Each key it serves a name with is made owner-only (0400) and then root's, as the
    token file is, so the gate can load it and no other user of the host can; each certificate, which is no secret, is
    readable by anyone (0444), and so are the directory the keys are mounted from and the gate's configuration."""
    sbx = start(world, "via-gate")
    tls = sbx.dir / "tls"
    served = list(json.loads((sbx.dir / "gate.json").read_text())["hosts"])
    assert served
    for name in served:
        key, cert = tls / f"{name}.key", tls / f"{name}.pem"
        assert stat.S_IMODE(key.stat().st_mode) == 0o400
        assert ["chown", "0:0", str(key)] in world.host.sudo_commands
        assert stat.S_IMODE(cert.stat().st_mode) == 0o444
    assert stat.S_IMODE(tls.stat().st_mode) == 0o755
    assert stat.S_IMODE((tls / "ca.pem").stat().st_mode) == 0o444  # mounted into the agent, whatever the umask
    for bundle in tls.glob("trust-*.pem"):
        assert stat.S_IMODE(bundle.stat().st_mode) == 0o444
    assert stat.S_IMODE((sbx.dir / "gate.json").stat().st_mode) == 0o444
    gate = world.host.containers[sbx.names.gate].options
    assert (gate["--user"], gate["--cap-drop"], gate["--cap-add"]) == ("0", ["ALL"], ["NET_BIND_SERVICE"])
    sbx.stop()


def test_o05_keys_that_cannot_be_given_to_the_gates_user_refuse_the_start_and_leave_nothing(world):
    """O30 (#203): a key the gate could not read would end the gate before it says it is ready; a key that cannot be
    given to root fails the start at once, naming what failed, and the teardown leaves nothing."""
    world.host.run_handlers["chown"] = lambda argv: CmdResult(1, "", "not permitted")
    with pytest.raises(SandboxError, match="TLS keys could not be given to the gate's user"):
        start(world, "via-gate")
    assert nothing_left(world)
    assert not [c for c in world.host.calls if c[:2] == ("docker", "run")]


def test_o05_the_gates_token_file_is_gone_after_a_gate_that_never_became_ready_and_after_a_kept_run(world, tmp_path):
    """O05: the host's copy of the gate's token is removed on every path that ends a sandbox: a gate that exited
    before it was ready, and a run whose disks are kept as evidence after a failed collection."""
    original = world.host._run_container

    def exiting(args, env):
        result = original(args, env)
        for container in world.host.containers.values():
            if container.name.endswith("-gate"):
                container.running, container.logs = False, ""
        return result

    world.host._run_container = exiting
    with pytest.raises(SandboxError, match="gate exited"):
        start(world, "via-gate")
    leftovers = [p for p in world.s.work_root.rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]
    assert leftovers == []
    world.host._run_container = original
    sbx = start(world, "via-gate")
    sbx.keep_evidence = True
    sbx.stop()
    kept = world.s.work_root / "_kept_evidence"
    assert not [p for p in kept.rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]


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
    """O05: the agent has neither the token nor FUSE nor a route; the sidecar takes the token from a file bound into it
    read-only, and from nowhere else: not an argument, not its environment as docker keeps it."""
    sbx = start(world, "mounted")
    agent, sidecar = (world.host.containers[n] for n in (sbx.names.agent, sbx.names.sidecar))
    assert sidecar.options["_files"] == {SIDECAR_TOKEN_PATH: TOKEN}
    assert any(spec.endswith(f":{SIDECAR_TOKEN_PATH}:ro") for spec in sidecar.options["-v"])
    assert TOKEN not in flat(sidecar)
    assert TOKEN not in json.dumps(sidecar.options["_env"])
    assert TOKEN not in json.dumps(agent.options["_env"])
    assert not agent.options["_files"]
    assert "--device" not in agent.options
    assert sbx.names.net in agent.networks
    assert "bridge" not in agent.networks
    sbx.stop()


@pytest.mark.parametrize("level", ["mounted", "via-gate", "isolated"])
def test_o05_docker_inspect_of_every_running_container_of_the_sandbox_shows_no_token(world, level):
    """O05: whoever can ask the docker daemon must not read the run's token: `docker inspect` of every container the
    sandbox runs (its environment, its command, its mounts), the sidecar, the gate and the agent alike, shows no token,
    and the host keeps no copy of it once its holder has started (a run killed before then leaves one, which the next
    start on the root removes)."""
    sbx = start(world, level)
    assert world.host.containers  # every one the sandbox started, whatever the path
    for name in world.host.containers:
        assert TOKEN not in world.host.docker("inspect", name).stdout, name
    assert not [p for p in sbx.dir.rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]
    sbx.stop()


def test_o05_the_mounts_token_file_is_gone_after_a_failed_start_and_after_a_kept_run(world, tmp_path):
    """O05: the host's copy of the token is removed on every path that ends a sandbox: a start whose sidecar exited
    before the mount appeared, and a run whose disks are kept as evidence after a failed collection."""
    original = world.host._run_container

    def exiting(args, env):
        result = original(args, env)
        for container in world.host.containers.values():
            if container.name.endswith("-oneclient"):
                container.running = False
        return result

    world.host._run_container = exiting
    world.host.oneclient_images.clear()  # the sidecar exits and no mount appears
    with pytest.raises(SandboxError, match="sidecar exited"):
        start(world, "mounted")
    leftovers = [p for p in world.s.work_root.rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]
    assert leftovers == []
    world.host._run_container = original
    world.host.oneclient_images.add(world.s.oneclient.id)
    sbx = start(world, "mounted")
    sbx.keep_evidence = True
    sbx.stop()
    kept = world.s.work_root / "_kept_evidence"
    assert kept.exists()
    assert not [p for p in kept.rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]


def test_o05_the_mounts_token_file_is_removed_even_when_the_fuse_mount_holds_the_containers(world, monkeypatch):
    """O05: when the FUSE mount can be neither unmounted nor aborted, the teardown leaves the sidecar and the agent
    running (O15) and keeps the run's directory; the host's copy of the token goes all the same."""
    from expcore_onedata.sandbox import teardown as TD

    sbx = start(world, "mounted")
    leftover = sbx.dir / TOKEN_FILE  # as a start cut short before the mount appeared would leave it
    leftover.write_text(TOKEN, encoding="utf-8")
    held = TD.TeardownResult(fuse_safe=False, removed=[], errors=["the mount is held"], log=[])
    monkeypatch.setattr(TD, "ordered_fuse_teardown", lambda *a, **k: held)
    with pytest.raises(SandboxError, match="left running"):
        sbx.stop()
    assert not leftover.exists()
    assert sbx.dir.exists()  # everything else is left for a retry
    monkeypatch.undo()
    sbx.stop()


def test_o05_the_token_files_go_before_the_ordered_teardown_and_whatever_it_does(world, monkeypatch):
    """O05: the host's copies of the token are the first thing a teardown removes, so that a teardown step that raises
    or hangs (the ordered unmount of the FUSE mount, the longest) leaves no secret on disk behind it."""
    from expcore_onedata.sandbox import teardown as TD

    sbx = start(world, "mounted")
    leftover = sbx.dir / TOKEN_FILE  # as a start cut short before the mount appeared would leave it
    leftover.write_text(TOKEN, encoding="utf-8")
    on_disk_when_it_began = []

    def broken(*args, **kwargs):
        on_disk_when_it_began.append(leftover.exists())
        raise RuntimeError("the ordered teardown failed")

    monkeypatch.setattr(TD, "ordered_fuse_teardown", broken)
    with pytest.raises(RuntimeError, match="ordered teardown failed"):
        sbx.stop()
    assert on_disk_when_it_began == [False]
    assert not leftover.exists()
    monkeypatch.undo()
    sbx.stop()


def test_o05_a_run_whose_token_file_cannot_be_removed_is_not_moved_into_the_kept_evidence(world):
    """O05: a secret is never evidence. When the host's copy of the token cannot be removed, the run directory that
    holds it is left where it is and the teardown says why, rather than moving the file into the evidence; once the
    file can go, the next teardown keeps the evidence."""
    sbx = start(world, "mounted")
    sbx.keep_evidence = True
    leftover = sbx.dir / TOKEN_FILE  # as a start cut short before the mount appeared would leave it
    leftover.write_text(TOKEN, encoding="utf-8")

    def busy(argv):
        if "-f" in argv and argv[-1] == str(leftover):
            return CmdResult(1, "", "busy")
        return world.host._run_default(argv)

    world.host.run_handlers["rm"] = busy
    with pytest.raises(SandboxError, match="token"):
        sbx.stop()
    kept = world.s.work_root / "_kept_evidence" / sbx.sid
    assert leftover.exists()
    assert not kept.exists()
    assert sbx.metadata()["evidence_kept"]["relocated"] is False
    world.host.run_handlers.clear()
    sbx.stop()
    assert kept.is_dir()
    assert not [p for p in kept.rglob("*") if p.is_file() and TOKEN in p.read_text(errors="replace")]


def plant_token_file(world, run_directory: str, relative: Path, *, age_s: float) -> Path:
    """A file of the data-path token as a run killed outright leaves it: in a run directory of the root, written `age_s`
    seconds ago."""
    path = world.s.work_root / run_directory / relative
    path.parent.mkdir(parents=True)
    path.write_text(TOKEN, encoding="utf-8")
    then = time.time() - age_s
    os.utime(path, (then, then))
    return path


def test_o05_a_start_removes_the_token_files_a_killed_run_left_on_the_root(world):
    """O05: a run killed between writing a token file and removing it leaves the file on disk; the next start on the
    same root removes it, the mount sidecar's and the gate's alike."""
    left_by_a_mount_start = plant_token_file(world, "dead-key-1a2b3c4d", TOKEN_FILE, age_s=3600)
    left_by_a_gate_start = plant_token_file(world, "other-key-5e6f7a8b", GATE_TOKEN_FILE, age_s=3600)
    sbx = start(world, "isolated")
    assert not left_by_a_mount_start.exists()
    assert not left_by_a_gate_start.exists()
    assert sbx.metadata()["reaped_token_files"] == {"removed": 2, "left": 0}
    sbx.stop()


def test_o05_a_start_leaves_a_token_file_that_may_be_another_starts_and_what_no_sandbox_names(world):
    """O05: only the files a sandbox writes, by its own naming, and only once much older than a start keeps one: a file
    written a moment ago is a start in another process that has not yet run its container, and a file elsewhere on the
    root, or one a link points at, is not the sandbox's to remove."""
    under_way = plant_token_file(world, "live-key-1a2b3c4d", TOKEN_FILE, age_s=5)
    not_a_run_directory = plant_token_file(world, "notes", TOKEN_FILE, age_s=3600)
    not_the_name = plant_token_file(world, "dead-key-5e6f7a8b", Path("sidecar-secret", "other-file"), age_s=3600)
    target = plant_token_file(world, "elsewhere", Path("target"), age_s=3600)
    linked = world.s.work_root / "dead-key-9c0d1e2f" / TOKEN_FILE
    linked.parent.mkdir(parents=True)
    linked.symlink_to(target)
    sbx = start(world, "isolated")
    assert [p.exists() for p in (under_way, not_a_run_directory, not_the_name, target)] == [True] * 4
    assert linked.is_symlink()
    assert sbx.metadata()["reaped_token_files"] == {"removed": 0, "left": 0}
    sbx.stop()


def test_o05_a_token_file_reached_through_a_linked_directory_is_not_removed(world):
    """O05: the reaping follows no link anywhere on the way to a token file: a run directory whose secret directory is a
    link to a directory elsewhere, holding a file of a token file's name, leaves that file alone."""
    elsewhere = world.s.work_root.parent / "outside-root"
    outside = elsewhere / TOKEN_FILE.name
    outside.parent.mkdir(parents=True)
    outside.write_text(TOKEN, encoding="utf-8")
    then = time.time() - 3600
    os.utime(outside, (then, then))
    run_directory = world.s.work_root / "dead-key-3e4f5a6b"
    run_directory.mkdir(parents=True)
    (run_directory / TOKEN_FILE.parent).symlink_to(elsewhere, target_is_directory=True)
    sbx = start(world, "isolated")
    assert outside.exists()
    assert sbx.metadata()["reaped_token_files"] == {"removed": 0, "left": 0}
    sbx.stop()


def test_o05_a_token_file_the_start_cannot_remove_is_counted_and_does_not_refuse_the_start(world):
    """O05: reaping what a killed run left never fails the start that does it; the count of what stayed is recorded."""
    stuck = plant_token_file(world, "dead-key-1a2b3c4d", TOKEN_FILE, age_s=3600)

    def busy(argv):
        if argv[-1] == str(stuck):
            return CmdResult(1, "", "busy")
        return world.host._run_default(argv)

    world.host.run_handlers["rm"] = busy
    sbx = start(world, "isolated")
    assert stuck.exists()
    assert sbx.metadata()["reaped_token_files"] == {"removed": 0, "left": 1}
    world.host.run_handlers.clear()
    sbx.stop()


def test_the_tls_mode_of_the_mount_comes_from_the_settings_alone(world):
    sbx = start(world, "mounted")
    command = world.host.containers[sbx.names.sidecar].command
    assert "--insecure" not in command
    oneclient = command[command.index("-u") :]  # after the shell that reads the token file
    assert oneclient[:2] == ["-u", "ONECLIENT_INSECURE"]
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


def test_o16_the_harnesss_own_stop_of_the_gate_is_no_death_in_a_healthy_rest_run(world, tmp_path):
    """O16: the collection stops the gate itself, so that it writes its close lines; that stop is the harness's own,
    and a healthy REST run records no container death and no infrastructure death, in the file or after."""
    sbx = start(world, "via-gate")
    (sbx.dir / "gatelog" / "requests.jsonl").write_text(
        json.dumps({"tag": "content", "decision": "allow", "status": 200, "bytes": 7}) + "\n", encoding="utf-8"
    )
    got = sbx.collect(tmp_path / "evidence")
    assert got["deaths"] == []
    assert world.host.containers[sbx.names.gate].running is False  # the gate was stopped, by the harness
    written = json.loads((tmp_path / "evidence" / "metadata.json").read_text())
    for meta in (written, sbx.metadata()):
        assert (meta["container_deaths"], meta["infra_death"]) == ([], [])
    check = sbx.infra_container_check()
    assert check["containers"]["gate"]["stopped_by_harness"] is True
    assert check["containers"]["gate"]["end"]["status"] == "running"  # its state just before the harness's stop
    sbx.stop()


def test_o17_a_gate_that_died_before_the_harness_stopped_it_is_still_a_death(world, tmp_path):
    """O17: the mark covers the harness's own stop only: a gate that had already exited when the collection came to
    stop it is a death of infrastructure, in the record as before."""
    sbx = start(world, "via-gate")
    (sbx.dir / "gatelog" / "requests.jsonl").write_text("", encoding="utf-8")
    gate = world.host.containers[sbx.names.gate]
    gate.running, gate.exit_code = False, 139
    sbx.collect(tmp_path / "evidence")
    meta = json.loads((tmp_path / "evidence" / "metadata.json").read_text())
    assert [death["role"] for death in meta["container_deaths"]] == ["gate"]
    assert meta["infra_death"]
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
    undo = fail_copies(world.host)
    with pytest.raises(InfraError, match="copying"):
        sbx.collect(tmp_path / "e", outputs=["out.txt"])
    undo()
    sbx.stop()
    kept = world.s.work_root / "_kept_evidence" / sbx.sid
    assert (kept / "work" / "out.txt").read_text() == "x"


def test_the_data_path_modes_of_a_start_are_recorded_by_name(world):
    sbx = start(world, "mounted")
    assert sbx.metadata()["data_path"] == DataPathMode.MOUNT.value
    sbx.stop()


@pytest.mark.parametrize(("level", "listing"), [(COMPLETE_LEVEL, "complete"), ("via-gate", "paged")])
def test_o21_a_rest_level_tells_its_gate_its_listing_mode_and_the_run_records_it(world, level, listing):
    """O21: the gate's configuration carries the level's listing mode, and the run's metadata records it beside the
    data path, so a record says which listings its agent was served."""
    sbx = start(world, level)
    assert json.loads((sbx.dir / "gate.json").read_text())["listing"] == listing
    assert (sbx.metadata()["data_path"], sbx.metadata()["listing"]) == ("rest", listing)
    sbx.stop()
    mounted = start(world, "mounted")
    assert mounted.metadata()["listing"] is None  # no gate lists anything on a mount
    mounted.stop()


@pytest.mark.parametrize(("level", "listing"), [("mounted", "complete"), ("isolated", "complete"), ("via-gate", "all")])
def test_o21_a_listing_mode_where_no_gate_lists_or_one_it_does_not_know_refuses_the_start(world, level, listing):
    """O21: a complete listing on a mount or no data path, or a mode the gate does not know, is refused before anything
    is created."""
    from dataclasses import replace

    spec = replace(make_spec(world, level), listing=listing)
    with pytest.raises(StartRefused, match="listing mode"):
        DockerSandbox.start(world.host, world.s, spec, **start_options(world))
    assert nothing_left(world)


def test_o12_the_record_names_the_pool_by_position_and_the_configuration_by_its_hash(world):
    """O12: the metadata holds no address of the pool, no mount or evidence path, and the platform file's SHA-256."""
    sbx = start(world, "mounted")
    meta = sbx.metadata()
    assert meta["network"]["pool"] == 0  # the first of [sandbox.network] pools
    assert set(meta["mount"]) == {"io_mode", "log_dir_from", "network", "wait_s"}
    assert meta["mount"]["log_dir_from"] == "default"
    assert meta["mount"]["network"] in ("default bridge", "federation")  # the kind of network, never its name
    text = json.dumps(meta, default=str)
    assert world.s.network.pools[0] not in text
    assert str(world.s.work_root) not in text
    assert world.s.mount.path not in text
    sbx.stop()


def test_o12_a_platform_sandbox_records_the_hash_of_the_platform_file(world):
    """O12: the hash of the file as loaded reaches the run's metadata through the options of a start."""
    spec = make_spec(world, "isolated")
    sbx = DockerSandbox.start(world.host, world.s, spec, config_sha256=world.config.sha256, **start_options(world))
    assert len(world.config.sha256) == 64
    assert sbx.metadata()["platform_config_sha256"] == world.config.sha256
    sbx.stop()


def test_o12_a_failed_collection_records_where_the_evidence_is_by_id_and_key_not_by_path(world, tmp_path):
    """O12: the kept-evidence record names the setting the directory is under and its id, never the path."""
    sbx = start(world, "isolated")
    (sbx.dir / "work" / "out.txt").write_text("x", encoding="utf-8")
    undo = fail_copies(world.host)
    with pytest.raises(InfraError, match="copying"):
        sbx.collect(tmp_path / "e", outputs=["out.txt"])
    undo()
    sbx.stop()
    kept = sbx.metadata()["evidence_kept"]
    assert kept == {"under": "sandbox.work_root", "directory_id": sbx.sid.rsplit("-", 1)[-1], "relocated": True}


def test_o12_a_sidecar_that_exited_is_reported_by_the_size_and_digest_of_its_stderr(world, caplog):
    """O12: the tool's words are not copied into the error or the log: their byte count and SHA-256 are."""
    original = world.host._run_container

    def exiting(args, env):
        result = original(args, env)
        for container in world.host.containers.values():
            if container.name.endswith("-oneclient"):
                container.running = False
        return result

    world.host._run_container = exiting
    world.host.oneclient_images.clear()  # no mount appears
    world.host._logs = lambda args: CmdResult(0, "", "invented-detail-of-a-mount-failure\n")
    with pytest.raises(SandboxError, match=r"sidecar exited \(stderr 35 bytes, sha256 [0-9a-f]{64}\)") as caught:
        start(world, "mounted")
    assert "invented-detail" not in str(caught.value)
    assert "invented-detail" not in caplog.text


def rest_start_with_a_bundle(world, lease_key="login-data"):
    """A REST sandbox whose agent image has a CA bundle to extend, so a throwaway container reads it first."""
    from dataclasses import replace

    settings = replace(world.s, trust_paths=("/etc/ssl/bundle.pem",))
    spec = make_spec(world, "via-gate", lease_key=lease_key)
    return DockerSandbox.start(world.host, settings, spec, **start_options(world))


def test_c19_the_temporary_trust_bundle_container_carries_the_labels_of_every_other_container(world):
    """C19: sandbox, owner and lease key, like the gate and the agent, from the moment it is created: each temporary
    container, the one that asks the image where its certifi bundle is and the one that reads each bundle (O23)."""
    sbx = rest_start_with_a_bundle(world)
    probe, *reads = world.host.one_shots
    assert probe.options["--entrypoint"] == world.s.python
    assert [read.command for read in reads] == [["/etc/ssl/bundle.pem"], [CERTIFI_BUNDLE]]
    others = [world.host.containers[sbx.names.agent], world.host.containers[sbx.names.gate]]
    for one_shot in world.host.one_shots:
        assert one_shot.labels == others[0].labels == others[1].labels
        assert one_shot.labels["org.test.lease"] == "login-data"
        assert {"org.test.sandbox", "org.test.owner", "org.test.dir"} <= set(one_shot.labels)
    sbx.stop()


def test_c19_a_trust_bundle_container_that_docker_did_not_remove_blocks_the_probe_and_is_found_by_the_reaper(world):
    """C19: left behind by a cut-off docker call, it keeps the probe from saying True and the reaper removes it."""
    from expcore_onedata.sandbox import netpool
    from expcore_onedata.sandbox.provider import SandboxProbe

    world.host.stuck_one_shots = True
    sbx = rest_start_with_a_bundle(world)
    one_shot = world.host.one_shots[1]  # the one that read the declared bundle
    world.host.fail_remove.add(one_shot.name)
    with pytest.raises(SandboxError, match="cleanup incomplete"):
        sbx.stop()
    probe = SandboxProbe(world.host, world.s)
    assert probe.resource_gone("login-data", {}) is False  # found by the lease label
    world.host.fail_remove.clear()
    left = world.host.containers[one_shot.name]
    left.labels["org.test.owner"] = f"{netpool.owner_id(world.host).split('/')[0]}/4026531836/4242/9"  # a dead owner
    reaped = netpool.reap_orphans(world.host, netpool.Labels("org.test"))
    assert one_shot.id in reaped["containers"]
    assert one_shot.name not in world.host.containers
    sbx.stop()
    assert probe.resource_gone("login-data", {}) is True


def agent_mounts(world, sbx) -> dict[str, str]:
    """The agent container's read-only bind mounts: the path in the agent -> the host file."""
    specs = world.host.containers[sbx.names.agent].options.get("-v", [])
    return {spec.split(":")[1]: spec.split(":")[0] for spec in specs if spec.endswith(":ro")}


def test_o23_a_rest_start_extends_the_images_certifi_bundle_with_the_runs_ca_and_records_it(world):
    """O23: the published client verifies the gate against the certifi bundle of the agent's image and reads none of
    the CA variables, so a REST start asks the image's interpreter where that bundle is, in a container with no network
    and as the agent's user, and mounts the bundle with the run's CA appended over it, read-only, as it does for a
    declared trust path. The metadata says so."""
    sbx = start(world, "via-gate")
    (probe,) = world.host.one_shots[:1]
    assert (probe.options["--entrypoint"], probe.options["--network"]) == (world.s.python, "none")
    assert probe.options["--user"] == f"{world.s.agent_uid}:{world.s.agent_uid}"
    assert probe.command[0] == "-c"
    assert [read.command for read in world.host.one_shots[1:]] == [[CERTIFI_BUNDLE]]
    bundle = Path(agent_mounts(world, sbx)[CERTIFI_BUNDLE])
    ca = (sbx.dir / "tls" / "ca.pem").read_text(encoding="utf-8")
    assert bundle.read_text(encoding="utf-8") == world.host.base_bundle + "\n" + ca
    assert sbx.metadata()["certifi"] == "run_ca_added"
    sbx.stop()


def test_o23_an_agent_image_without_certifi_starts_without_it_and_the_metadata_says_so(world):
    """O23: an image with no certifi has no bundle to extend: the sandbox starts as before, mounts nothing over a
    bundle, and records `not_in_image`, so a run whose client could not verify the gate can be told apart."""
    world.host.certifi_probe = CmdResult(0, "certifi:none\n")
    sbx = start(world, "via-gate")
    assert len(world.host.one_shots) == 1  # the question, and no bundle read
    assert [path for path in agent_mounts(world, sbx) if "certifi" in path] == []
    assert sbx.metadata()["certifi"] == "not_in_image"
    sbx.stop()


@pytest.mark.parametrize(
    "answer",
    [
        CmdResult(127, "", "exec: python: not found"),
        CmdResult(1, "", "Traceback: a broken certifi"),
        CmdResult(0, "a banner\ncertifi:/usr/lib/cacert.pem\n"),
        CmdResult(0, "certifi:relative/cacert.pem\n"),
        CmdResult(0, "certifi:/usr/lib/../etc/cacert.pem\n"),
        CmdResult(0, "certifi:/usr/lib/with space/cacert.pem\n"),
        CmdResult(0, "certifi:/usr/lib/with:colon/cacert.pem\n"),
        CmdResult(0, ""),
    ],
    ids=["no-interpreter", "import-error", "extra-output", "relative", "dot-dot", "space", "colon", "silent"],
)
def test_o23_a_certifi_question_the_image_cannot_answer_plainly_refuses_the_start_and_leaves_nothing(world, answer):
    """O23: fail closed. Only `certifi:none` says the image has no certifi; an interpreter that fails, or an answer
    that is not one absolute path the sandbox can mount over, is no answer, and the start ends with nothing left. The
    message names neither the path nor the interpreter's words."""
    world.host.certifi_probe = answer
    with pytest.raises(SandboxError, match="certifi") as caught:
        start(world, "via-gate")
    assert "cacert" not in str(caught.value)
    assert "Traceback" not in str(caught.value)
    assert nothing_left(world)


def test_o23_a_declared_trust_path_that_is_the_certifi_bundle_is_extended_once(world):
    """O23: a study that already declares the certifi bundle in `trust_paths` gets it read and mounted once."""
    from dataclasses import replace

    settings = replace(world.s, trust_paths=(CERTIFI_BUNDLE,))
    sbx = DockerSandbox.start(world.host, settings, make_spec(world, "via-gate"), **start_options(world))
    assert [read.command for read in world.host.one_shots[1:]] == [[CERTIFI_BUNDLE]]
    assert list(agent_mounts(world, sbx)).count(CERTIFI_BUNDLE) == 1
    sbx.stop()


@pytest.mark.parametrize("level", ["mounted", "isolated"])
def test_o23_no_bundle_is_extended_without_a_rest_data_path(world, level):
    """O23: only a REST agent reaches the gate; a mount or no data path asks the image nothing and records nothing."""
    sbx = start(world, level)
    assert world.host.one_shots == []
    assert sbx.metadata()["certifi"] is None
    sbx.stop()


KEY = "login-data"


def test_o15_a_failed_unmount_after_a_failed_collection_leaves_the_directory_where_it_is(world, tmp_path):
    """O15: never move a run directory while a mount under it may be live; the probe then does not say True."""
    from expcore_onedata.sandbox.provider import SandboxProbe

    sbx = start(world, "isolated", lease_key=KEY)
    (sbx.dir / "work" / "out.txt").write_text("x", encoding="utf-8")
    undo = fail_copies(world.host)
    with pytest.raises(InfraError, match="copying"):
        sbx.collect(tmp_path / "e", outputs=["out.txt"])
    undo()
    world.host.stuck_mounts.add(str(sbx.dir / "work"))
    with pytest.raises(SandboxError, match="a mount under it may be live"):
        sbx.stop()
    assert not [c for c in world.host.calls_of("run") if c[1][0] == "mv"]  # nothing was moved
    assert sbx.dir.exists()
    assert not (world.s.work_root / "_kept_evidence").exists()
    assert sbx.metadata()["evidence_kept"]["relocated"] is False
    probe = SandboxProbe(world.host, world.s)
    assert probe.resource_gone(KEY, {}) is False
    world.host.stuck_mounts.clear()  # the next try unmounts, then keeps the evidence
    sbx.stop()
    assert (world.s.work_root / "_kept_evidence" / sbx.sid / "work" / "out.txt").read_text() == "x"
    assert sbx.metadata()["evidence_kept"]["relocated"] is True
    assert probe.resource_gone(KEY, {}) is True


def test_c19_the_probe_finds_a_mount_by_its_recorded_identity_wherever_its_directory_has_gone(world):
    """C19: a relocated path is still seen: the mount keeps the id and device the sandbox recorded for it."""
    from expcore_onedata.sandbox.provider import SandboxProbe

    sbx = start(world, "isolated", lease_key=KEY)
    probe = SandboxProbe(world.host, world.s)
    assert probe.resource_gone(KEY, {}) is False  # the mount is live at its own place
    world.host.relocate_mounts(str(sbx.dir), "/srv/elsewhere/a")  # a path of no pattern: only the identity is left
    assert probe.resource_gone(KEY, {}) is False
    assert probe.resource_gone("login-data2", {}) is True  # it is the lease's, nobody else's
    world.host.relocate_mounts("/srv/elsewhere/a", str(sbx.dir))
    sbx.stop()
    assert probe.resource_gone(KEY, {}) is True
    assert not (world.s.work_root / "_mounts" / f"{sbx.sid}.json").exists()  # the record goes with the sandbox


def test_o15_c19_a_run_directory_moved_while_mounted_keeps_its_record_and_the_probe_not_true(world):
    """O15, C19: a run directory moved while mounted takes its mounts to a path no pattern matches; the stop finds them
    by the identity it recorded when each was made (mount id, source, type), anywhere in the whole mount table, so it
    fails and keeps the record, and the probe does not say True. Moved back, the stop removes both."""
    from expcore_onedata.sandbox.provider import SandboxProbe

    sbx = start(world, "isolated", lease_key=KEY)
    record = world.s.work_root / "_mounts" / f"{sbx.sid}.json"
    moved = world.tmp_path / "moved-away"
    world.host.run(["mv", str(sbx.dir), str(moved)], 60, sudo=True)  # the mounts go with the directory
    with pytest.raises(SandboxError):
        sbx.stop()
    assert record.exists()
    probe = SandboxProbe(world.host, world.s)
    assert probe.resource_gone(KEY, {}) is not True
    world.host.run(["mv", str(moved), str(sbx.dir)], 60, sudo=True)
    sbx.stop()
    assert not record.exists()
    assert probe.resource_gone(KEY, {}) is True


def test_c19_a_mount_record_without_a_full_identity_is_unknown_and_keeps_the_lease_held(world, caplog):
    """C19: a recorded mount with only a path, an id and a device (no source, no type) cannot be found by its identity
    wherever it went, so it is unknown: the probe answers not True for its lease, and says why; another lease's answer
    does not change."""
    from expcore_onedata.sandbox.provider import SandboxProbe

    probe = SandboxProbe(world.host, world.s)
    records = world.s.work_root / "_mounts"
    records.mkdir(parents=True)
    assert probe.resource_gone(KEY, {}) is True
    partial = {"path": "/srv/elsewhere/gone/work", "id": "41", "dev": "0:101"}
    document = {"sid": f"{KEY}-0123abcd", "lease": KEY, "mounts": [partial]}
    (records / f"{KEY}-0123abcd.json").write_text(json.dumps(document), encoding="utf-8")
    with caplog.at_level("ERROR"):
        assert probe.resource_gone(KEY, {}) is not True
    assert "full identity" in caplog.text
    assert probe.resource_gone("login-data2", {}) is True


def test_c19_a_mount_never_identified_holds_its_lease_until_an_operator_clears_its_record(world, caplog, capsys):
    """C19: a mount whose identity could not be read when it was made fails the start; the teardown unmounts it by its
    path but never removes its record on a path check, so the probe keeps the lease held. An operator who has verified
    that nothing of it is mounted clears the record with the mount-record tool, which names it by id, and only then is
    the lease shown gone."""
    from expcore_onedata.sandbox.provider import SandboxProbe

    original, reads = world.host.read_text, []

    def first_table_unreadable(path):
        if str(path) == "/proc/self/mountinfo" and not reads:
            reads.append(path)
            raise PermissionError(13, "Permission denied")
        return original(path)

    world.host.read_text = first_table_unreadable
    with caplog.at_level("ERROR"), pytest.raises(SandboxError, match="identity of a new mount"):
        start(world, "isolated", lease_key=KEY)
    assert not world.host.mounts
    records = list((world.s.work_root / "_mounts").glob("*.json"))
    assert len(records) == 1  # kept: a path check never removes it
    probe = SandboxProbe(world.host, world.s)
    assert probe.resource_gone(KEY, {}) is False
    from expcore_onedata.sandbox import mountrecords  # the operator's tool

    record = records[0]
    record_id = record.stem.rsplit("-", 1)[-1]
    assert mountrecords.main(["list"], work_root=world.s.work_root, host=world.host) == 0
    assert f"{record_id} mounts=1 unidentified=1 in_table=0" in capsys.readouterr().out
    assert mountrecords.main(["clear", record_id], work_root=world.s.work_root, host=world.host) == 2  # not verified
    assert record.exists()
    cleared = mountrecords.main(["clear", record_id, "--verified"], work_root=world.s.work_root, host=world.host)
    assert cleared == 0
    assert not record.exists()
    assert probe.resource_gone(KEY, {}) is True


def test_c19_a_mount_record_the_probe_cannot_read_is_unknown_and_not_proof(world):
    """C19: an unreadable or malformed record, of any lease, keeps every key from being shown gone."""
    from expcore_onedata.sandbox.provider import SandboxProbe

    probe = SandboxProbe(world.host, world.s)
    records = world.s.work_root / "_mounts"
    records.mkdir(parents=True)
    assert probe.resource_gone(KEY, {}) is True
    for text in ("{not json", "[]", '{"sid": "x"}', '{"sid": "x", "lease": "k", "mounts": [{"id": 1}]}'):
        (records / "damaged.json").write_text(text, encoding="utf-8")
        assert probe.resource_gone(KEY, {}) is False, text
    (records / "damaged.json").unlink()
    assert probe.resource_gone(KEY, {}) is True


def test_o15_an_abandoned_start_creates_nothing_and_a_start_abandoned_midway_removes_what_it_made(world):
    """O15: whoever holds the sandbox can abandon its start from another thread; the start thread then cleans up."""
    sbx = DockerSandbox(world.host, world.s, make_spec(world, "via-gate"), **start_options(world))
    sbx.abandon()
    with pytest.raises(SandboxError, match="abandoned"):
        sbx.begin()
    assert nothing_left(world)
    assert not world.s.work_root.exists() or not list(world.s.work_root.iterdir())

    midway = DockerSandbox(world.host, world.s, make_spec(world, "via-gate"), **start_options(world))
    original = world.host.docker

    def abandon_when_the_gate_is_up(*args, **kwargs):
        done = original(*args, **kwargs)
        if args[0] == "network" and args[1] == "connect":  # the gate is on the network: the agent is next
            midway.abandon()
        return done

    world.host.docker = abandon_when_the_gate_is_up
    with pytest.raises(SandboxError, match="abandoned"):
        midway.begin()
    assert nothing_left(world)
    assert midway.stopped


def test_o15_a_stop_that_ran_during_the_start_is_not_the_last_word_on_the_sandbox(world):
    """O15: what the start registers after a stop began leaves the sandbox not stopped, so the next stop runs."""
    sbx = DockerSandbox(world.host, world.s, make_spec(world, "isolated"), **start_options(world))
    sbx.begin()
    sbx._register_container("sbx-late-arrival")  # created by the start thread after the stop below began
    original = sbx._remove_the_rest

    def remove_and_register(errors):
        original(errors)
        sbx._register_container("sbx-later-still")

    sbx._remove_the_rest = remove_and_register
    sbx.stop()
    assert not sbx.stopped  # something was registered while it ran
    sbx._remove_the_rest = original
    sbx.stop(force=True)
    assert sbx.stopped
    assert nothing_left(world)


# -- [sandbox.federation]: the sidecar on the federation's own network ------------------------------------------------


def test_o03_a_mount_sidecar_reaches_the_federation_on_its_network_and_the_agent_stays_on_its_own(fed_world):
    """O03: with `[sandbox.federation]` the sidecar is started on the federation's own network, and on no other, and
    mounts through the local provider's name there; the agent is on the sandbox's internal network alone. The record
    says which kind of network the sidecar was on, never its name (O12)."""
    sbx = start(fed_world, "mounted")
    sidecar, agent = (fed_world.host.containers[n] for n in (sbx.names.sidecar, sbx.names.agent))
    assert set(sidecar.networks) == {FED_NETWORK}
    assert sidecar.command[sidecar.command.index("-H") + 1] == FED_LOCAL
    assert set(agent.networks) == {sbx.names.net}
    assert sbx.metadata()["mount"]["network"] == "federation"
    sbx.stop()


def test_o03_without_the_table_the_record_says_the_sidecar_was_on_the_default_bridge(world):
    """O03: a sandbox without `[sandbox.federation]` records that its sidecar was on docker's default bridge."""
    sbx = start(world, "mounted")
    assert sbx.metadata()["mount"]["network"] == "default bridge"
    sbx.stop()


@pytest.mark.parametrize("level", ["mounted", "via-gate"])
def test_o15_a_federation_network_that_is_not_there_refuses_the_start_before_anything_is_created(fed_world, level):
    """O15: the federation's network is the federation deployment's, never the sandbox's to make; one that is not on
    the host refuses a mount or a REST sandbox's start before any container, network, mount or directory exists, so
    there is nothing to tear down, and the message names the table and not the network. A run with no data path never
    asks for that network, and starts."""
    del fed_world.host.networks[FED_NETWORK]
    with pytest.raises(StartRefused, match=r"\[sandbox\.federation\]") as caught:
        start(fed_world, level)
    assert FED_NETWORK not in str(caught.value)
    assert nothing_left(fed_world)
    assert not fed_world.s.work_root.exists() or not list(fed_world.s.work_root.iterdir())
    start(fed_world, "isolated").stop()
    assert nothing_left(fed_world)


def test_o03_a_rest_gate_reaches_the_federation_on_its_network_by_the_names_there_and_the_agent_stays_off(fed_world):
    """O30 (#203): with `[sandbox.federation]` the REST data path's gate starts on the federation's own network
    and is then connected to the sandbox's internal network, and to no other. It serves and dials the zone and the
    local provider by their names on the federation's network, port 443, never the loopback addresses of the platform
    file's URLs; its certificates are issued for those names, which the agent resolves to the gate and whose zone is
    the agent's `ONEDATA_ONEZONE_HOST`. The agent is on the sandbox's internal network alone."""
    sbx = start(fed_world, "via-gate")
    gate, agent = (fed_world.host.containers[n] for n in (sbx.names.gate, sbx.names.agent))
    assert set(gate.networks) == {FED_NETWORK, sbx.names.net}
    assert gate.options["--network"] == FED_NETWORK
    assert set(agent.networks) == {sbx.names.net}
    hosts = json.loads((sbx.dir / "gate.json").read_text())["hosts"]
    assert hosts == {
        FED_ZONE: {"role": "onezone", "addr": FED_ZONE, "port": 443, "tls": True},
        FED_LOCAL: {"role": "oneprovider", "addr": FED_LOCAL, "port": 443, "tls": True},
    }
    assert ("make_ca", (FED_ZONE, FED_LOCAL)) in fed_world.host.calls
    assert {a.split(":")[0] for a in agent.options["--add-host"]} == {FED_ZONE, FED_LOCAL}
    assert agent.options["_env"]["ONEDATA_ONEZONE_HOST"] == FED_ZONE
    sbx.stop()
    assert set(fed_world.host.networks) == {FED_NETWORK}


def test_o03_without_the_table_a_rest_gate_starts_on_the_default_bridge_and_dials_the_urls_as_before(world):
    """O30: without `[sandbox.federation]` nothing changes: the gate is on docker's default bridge and the sandbox's
    internal network, and serves and dials the hosts and ports of the zone's and the local provider's URLs."""
    sbx = start(world, "via-gate")
    gate, agent = (world.host.containers[n] for n in (sbx.names.gate, sbx.names.agent))
    assert set(gate.networks) == {"bridge", sbx.names.net}
    spec = make_spec(world, "via-gate")
    hosts = json.loads((sbx.dir / "gate.json").read_text())["hosts"]
    assert {name: (h["addr"], h["port"]) for name, h in hosts.items()} == {
        spec.zone.host: (spec.zone.host, spec.zone.port),
        spec.provider.host: (spec.provider.host, spec.provider.port),
    }
    assert agent.options["_env"]["ONEDATA_ONEZONE_HOST"] == spec.zone.host
    sbx.stop()


def test_o15_the_teardown_takes_the_gate_off_the_federation_network_and_leaves_the_network_as_it_was(fed_world):
    """O15 (#203): the gate is on the federation's network while a REST sandbox runs; the teardown removes it, which
    takes it off, and the network itself is never disconnected, removed or reaped: only looked up by name."""
    from expcore_onedata.sandbox.provider import SandboxProbe

    before = dict(fed_world.host.networks[FED_NETWORK])
    sbx = start(fed_world, "via-gate", lease_key=KEY)
    sbx.stop()
    assert set(fed_world.host.networks) == {FED_NETWORK}
    assert fed_world.host.networks[FED_NETWORK] == before
    assert not fed_world.host.containers
    touched = [c for c in fed_world.host.calls if c[0] == "docker" and c[1] == "network" and FED_NETWORK in c[2][1:]]
    assert touched == [], "only a lookup of the federation network by name is allowed"
    assert SandboxProbe(fed_world.host, fed_world.s).resource_gone(KEY, {}) is True


def test_o15_the_teardown_takes_the_sidecar_off_the_federation_network_and_leaves_the_network_as_it_was(fed_world):
    """O15: the sidecar is on the federation's network while the sandbox runs; the ordered teardown removes it, which
    takes it off that network, and the network itself is never disconnected, removed or reaped. The probe then shows
    the sandbox gone (C19)."""
    from expcore_onedata.sandbox.provider import SandboxProbe

    before = dict(fed_world.host.networks[FED_NETWORK])
    sbx = start(fed_world, "mounted", lease_key=KEY)
    assert set(fed_world.host.containers[sbx.names.sidecar].networks) == {FED_NETWORK}
    sbx.stop()
    assert set(fed_world.host.networks) == {FED_NETWORK}
    assert fed_world.host.networks[FED_NETWORK] == before
    assert not fed_world.host.containers
    assert not fed_world.host.mounts
    touched = [c for c in fed_world.host.calls if c[0] == "docker" and c[1] == "network" and FED_NETWORK in c[2][1:]]
    assert touched == [], "only a lookup of the federation network by name is allowed"
    assert SandboxProbe(fed_world.host, fed_world.s).resource_gone(KEY, {}) is True
