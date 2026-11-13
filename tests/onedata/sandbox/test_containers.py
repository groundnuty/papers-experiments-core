"""The docker arguments of each container: read-only everywhere, images by id, no secret in an argument (O05, O13)."""

import dataclasses
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sandbox_support import FED_LOCAL, FED_NETWORK, FED_ZONE

from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox.api import DataPathMode, Endpoint
from expcore_onedata.sandbox.settings import FederationNetwork
from expcore_onedata.tls import TlsSettings

LABELS = ["--label", "org.test.sandbox=1"]
TOKEN_FILE = Path("/w/secrets/oneclient-token")  # where a run keeps the mount's token for its sidecar
GATE_TOKEN_FILE = Path("/w/secrets/gate-token")  # and the REST path's for its gate


@pytest.fixture
def s(world):
    return world.s


@pytest.fixture
def names(s):
    return C.Names.make(s.name_prefix, "k1-0123abcd")


def agent(s, names, mode, **kw):
    defaults = {
        "image_id": s.agent.id,
        "mode": mode,
        "work": Path("/w/work"),
        "share": None,
        "gate_ip": None,
        "gate_hosts": [],
        "ca": None,
        "trust_bundles": {},
        "zone_host": None,
    }
    return C.agent_run_args(s, names, LABELS, **(defaults | kw))


def pairs(args):
    """Flag, value pairs of a docker command line (for flags that take a value)."""
    return [(a, args[i + 1]) for i, a in enumerate(args[:-1]) if a.startswith("-")]


def test_names_are_derived_from_the_prefix_and_the_sandbox_id(s, names):
    assert names.agent == f"{s.name_prefix}-k1-0123abcd"
    assert names.gate == names.agent + "-gate"
    assert names.net == names.agent + "-net"
    assert names.sidecar == names.agent + "-oneclient"


@pytest.mark.parametrize("mode", list(DataPathMode))
def test_o05_the_agent_is_read_only_without_capabilities_in_every_data_path(s, names, mode):
    """O05: the same flags in every condition; the data path is read-only whatever it is."""
    extra = {
        DataPathMode.REST: {
            "gate_ip": "10.1.0.2",
            "gate_hosts": ["h.test"],
            "ca": Path("/w/ca.pem"),
            "zone_host": "z.test",
        },
        DataPathMode.MOUNT: {"share": Path("/w/mount")},
        DataPathMode.NONE: {},
    }[mode]
    args, _ = agent(s, names, mode, **extra)
    assert "--read-only" in args
    assert ("--cap-drop", "ALL") in pairs(args)
    assert ("--security-opt", "no-new-privileges") in pairs(args)
    assert "--cap-add" not in args
    assert "--privileged" not in args
    assert "--device" not in args
    assert args[-3:] == [s.agent.id, "sleep", "infinity"]  # by id, never by tag (O13)


def test_o05_the_mount_is_bound_into_the_agent_read_only(s, names):
    """O05: The mount is bound into the agent read only."""
    args, env = agent(s, names, DataPathMode.MOUNT, share=Path("/w/mount"))
    spec = dict(pairs(args))["--mount"]
    assert spec == f"type=bind,src=/w/mount,dst={s.mount.path},readonly,bind-propagation=rslave"
    assert env == {}


def test_o05_the_sidecar_is_the_only_container_with_fuse_and_takes_its_token_from_a_read_only_file(s, names):
    """O05: the sidecar is the only container with FUSE. Its token is never given to docker: not as an argument and
    not as an environment variable (`-e NAME`, whose value docker keeps in the container's configuration, where
    `docker inspect` shows it to anyone with access to the daemon). It is a file bound into the sidecar read-only,
    which the sidecar's shell reads into oneclient's environment as it starts oneclient."""
    token_file = Path("/w/secrets/oneclient-token")
    args = C.sidecar_run_args(s, names, LABELS, Path("/w/mount"), s.oneclient.id, "p.test", token_file=token_file)
    assert ("--device", "/dev/fuse") in pairs(args)
    assert C.ENV_ACCESS_TOKEN not in [v for f, v in pairs(args) if f == "-e"]
    assert [a for a in args if C.ENV_ACCESS_TOKEN in a] == [C.SIDECAR_START]  # named only where the file is read
    assert f"{token_file}:{C.SIDECAR_TOKEN_PATH}:ro" in [v for f, v in pairs(args) if f == "-v"]
    assert dict(pairs(args))["--entrypoint"] == "sh"
    script = args[args.index("-c") + 1]
    assert f'{C.ENV_ACCESS_TOKEN}="$(cat {C.SIDECAR_TOKEN_PATH})"' in script  # read from the file, in the container
    assert script.endswith('exec env "$@"')  # then oneclient, as `sidecar_command` says
    assert "ro,allow_other" in args
    assert s.oneclient.id in args
    assert "type=bind,src=/w/mount,dst=/mnt/oneclient,bind-propagation=rshared" in args


def test_o05_the_sidecars_shell_hands_the_files_token_to_oneclient_and_refuses_an_empty_one(tmp_path):
    """O05: run by a real shell, the sidecar's start script puts the token file's value, and nothing else, into the
    environment of the program `env` starts, with every argument passed through as given (spaces included), and
    exits without starting it when the file is empty or missing. The one line before it is the config record (O36)."""
    token = tmp_path / "token"
    script = C.SIDECAR_START.replace(C.SIDECAR_TOKEN_PATH, str(token))
    show = ["sh", "-c", f'printf "%s|%s|%s" "${C.ENV_ACCESS_TOKEN}" "$1" "$2"', "show", "a b", "-u"]

    def run():
        return subprocess.run(
            ["sh", "-c", script, C.SIDECAR_SHELL_NAME, *show], capture_output=True, text=True, check=False, timeout=30
        )

    token.write_text("tok-invented-0123", encoding="utf-8")
    done = run()
    assert done.returncode == 0
    assert done.stdout.splitlines()[-1].endswith("tok-invented-0123|a b|-u")
    assert (
        "tok-invented-0123" not in done.stdout.split("\n", 1)[0]
    )  # the record line is written before the token is read
    token.write_text("", encoding="utf-8")
    assert run().returncode == 97
    token.unlink()
    assert run().returncode == 97


def test_o05_the_gate_takes_its_token_from_a_read_only_file_and_docker_is_given_no_environment_for_it(s, names):
    """O05: the gate holds the token, from a file bound into its container alone, read-only, at the path the gate's
    configuration names; docker is given no environment variable at all (`-e NAME` would keep the value in the
    container's configuration, where `docker inspect` shows it)."""
    gate_token = Path("/w/run-secrets/gate-token")
    args = C.gate_run_args(s, names, LABELS, Path("/w"), Path("/pkg"), s.gate.id, token_file=gate_token)
    assert "-e" not in args
    assert f"{gate_token}:{C.GATE_TOKEN_PATH}:ro" in [v for f, v in pairs(args) if f == "-v"]


STORAGES = (("0a1b2c3d4e5f", "/srv/invented/one"), ("6a7b8c9d0e1f", "/srv/invented/two"))  # invented, in file order
CHECK_FILE = "space-invented/checks/direct.bin"


def with_io_mode(s, mode, **mount):
    """The settings with the mount in `mode`; a direct one maps the invented storages and names a check file (O40)."""
    direct = {"storages": STORAGES, "direct_check_file": CHECK_FILE} if mode == "direct" else {}
    return dataclasses.replace(s, mount=dataclasses.replace(s.mount, io_mode=mode, **(direct | mount)))


def test_o36_the_default_world_mounts_with_proxied_io(s):
    """O36: the sidecar's oneclient carries `--force-proxy-io`, from the table's `io_mode = "proxy"`."""
    assert s.mount.io_mode == "proxy"
    assert C.sidecar_command("p.test", s).count("--force-proxy-io") == 1


@pytest.mark.parametrize(
    ("mode", "flag", "other"),
    [("proxy", "--force-proxy-io", "--force-direct-io"), ("direct", "--force-direct-io", "--force-proxy-io")],
)
def test_o36_the_sidecar_is_always_told_how_to_reach_storage_by_exactly_one_force_option(s, names, mode, flag, other):
    """O36: whichever mode is declared, oneclient is started with its own force option, once, and never the other:
    what it would otherwise detect at its first read is declared up front."""
    declared = with_io_mode(s, mode)
    command = C.sidecar_command("p.test", declared)
    assert command.count(flag) == 1
    assert other not in command
    args = C.sidecar_run_args(
        declared, names, LABELS, Path("/w/mount"), s.oneclient.id, "p.test", token_file=TOKEN_FILE
    )
    assert args[args.index("-c") + 1 :].count(flag) == 1


@pytest.mark.parametrize("mode", [None, "", "auto", "x"])
def test_o36_a_mount_with_no_known_io_mode_never_gets_an_argv_that_leaves_it_to_oneclient(s, mode):
    """O36: settings built without the loader's checks fail at the command, not into oneclient's own detection."""
    with pytest.raises(ValueError, match="io_mode"):
        C.sidecar_command("p.test", with_io_mode(s, mode))


def test_o36_the_options_the_sandbox_sets_are_the_commands_own(s):
    """O36: what the metadata records as the sandbox's options are exactly the ones in the command, and none of the
    study's extra arguments is among them."""
    declared = with_io_mode(s, "direct", extra_args=("--provider-timeout", "30"))
    options = C.sidecar_options(declared)
    command = C.sidecar_command("p.test", declared)
    assert options == ["-o", "ro,allow_other", "--force-direct-io", "--config", "/dev/null", "--disable-log-buffering"]
    at = command.index(options[0])
    assert command[at : at + len(options)] == options
    assert command[-3:-1] == ["--provider-timeout", "30"]
    assert command[-1] == C.SIDECAR_MOUNT


def unset_names(command):
    """The environment variables `env` removes in a sidecar command: each `-u NAME` before the program."""
    program = command.index(C.ONECLIENT_BIN)
    return [command[i + 1] for i, word in enumerate(command[:program]) if word == "-u"]


def test_o36_oneclients_other_inputs_are_closed_in_the_command(s):
    """O36: nothing but argv and the token sets an option of the sidecar's oneclient: `env -u` removes the three
    variables that could (the image's own skip-verification flag, `ONECLIENT_OVERRIDE` and `ONECLIENT_CONFIG`), and
    `--config /dev/null` means no configuration file is parsed. The token's own variable is not removed."""
    command = C.sidecar_command("p.test", s)
    assert unset_names(command) == ["ONECLIENT_INSECURE", "ONECLIENT_OVERRIDE", "ONECLIENT_CONFIG"]
    assert C.ENV_ACCESS_TOKEN not in unset_names(command)
    at = command.index("--config")
    assert command[at : at + 2] == ["--config", "/dev/null"]
    assert command.count("--config") == 1
    assert command.index(C.ONECLIENT_BIN) < at < command.index(C.SIDECAR_MOUNT)


def test_o36_the_token_still_reaches_oneclient_through_its_own_variable_and_the_file(s, names):
    """O36: closing the environment keeps the one path the token takes (O05): the file bound into the sidecar alone,
    read into `ONECLIENT_ACCESS_TOKEN` by the entrypoint, which `env -u` leaves alone."""
    args = C.sidecar_run_args(s, names, LABELS, Path("/w/mount"), s.oneclient.id, "p.test", token_file=TOKEN_FILE)
    assert f"{TOKEN_FILE}:{C.SIDECAR_TOKEN_PATH}:ro" in [v for f, v in pairs(args) if f == "-v"]
    script = args[args.index("-c") + 1]
    assert f'{C.ENV_ACCESS_TOKEN}="$(cat {C.SIDECAR_TOKEN_PATH})"' in script
    assert f"export {C.ENV_ACCESS_TOKEN}" in script
    assert C.ENV_ACCESS_TOKEN not in unset_names(args)
    assert "-e" not in args  # and docker is given no environment for it


def test_o36_the_command_refuses_settings_that_would_give_oneclient_two_force_options_or_a_second_config(s):
    """O36: settings reach the command by `dataclasses.replace` and by direct assignment alike; the command is where the
    exactly-one invariant is held, whatever built the settings. A mount whose fields were set past its own check
    (`object.__setattr__`, as a copy of a frozen object may be) still raises here and never yields an argv."""
    for extra in (("--force-direct-io",), ("--force-p",), ("--config", "/other.conf"), ("ONECLIENT_CONFIG=/other",)):
        mount = dataclasses.replace(s.mount)
        object.__setattr__(mount, "extra_args", extra)
        broken = dataclasses.replace(s, mount=mount)
        with pytest.raises(ValueError, match="extra_args"):
            C.sidecar_command("p.test", broken)
        with pytest.raises(ValueError, match="extra_args"):
            C.sidecar_options(broken)
    mount = dataclasses.replace(s.mount)
    object.__setattr__(mount, "io_mode", "auto")
    with pytest.raises(ValueError, match="io_mode"):
        C.sidecar_command("p.test", dataclasses.replace(s, mount=mount))


def test_o36_a_direct_mount_has_one_force_option_and_the_closed_inputs_of_a_proxy_one(s):
    """O36: a direct mount's command has exactly one force option, the config file and the closed environment, as for
    proxy."""
    declared = with_io_mode(s, "direct")
    command = C.sidecar_command("p.test", declared)
    assert command.count("--force-direct-io") == 1
    assert "--force-proxy-io" not in command
    assert unset_names(command) == ["ONECLIENT_INSECURE", "ONECLIENT_OVERRIDE", "ONECLIENT_CONFIG"]


def test_o40_a_direct_mount_names_each_storage_to_oneclient_at_its_place_in_the_sidecar(s):
    """O40: one `--override <storage id>:mountPoint:<path in the sidecar>` per mapped storage, in the file's order,
    each at a path of its position (never the host's), after the sandbox's options and before the study's arguments,
    so the POSIX helper reads each storage where the sidecar has it and never scans the sidecar's mounts for it."""
    declared = with_io_mode(s, "direct", extra_args=("--provider-timeout", "30"))
    command = C.sidecar_command("p.test", declared)
    overrides = [command[i + 1] for i, word in enumerate(command) if word == "--override"]
    assert overrides == [
        f"0a1b2c3d4e5f:mountPoint:{C.DIRECT_STORAGE_ROOT}/0",
        f"6a7b8c9d0e1f:mountPoint:{C.DIRECT_STORAGE_ROOT}/1",
    ]
    assert command.index("--disable-log-buffering") < command.index("--override")
    assert command.index("--override") < command.index("--provider-timeout")
    assert not any("/srv/invented" in word for word in command)  # the host's path is the bind's alone


def test_o40_o12_the_options_a_record_keeps_hold_no_storage_id_or_path(s):
    """O40, O12: `sidecar_options` is what a run's metadata records; a storage id and a host path are configured
    values, so they are in the overrides and the binds alone."""
    options = C.sidecar_options(with_io_mode(s, "direct"))
    text = " ".join(options)
    for storage_id, path in STORAGES:
        assert storage_id not in text
        assert path not in text
    assert "--override" not in options


def test_o40_each_storage_is_bound_read_only_into_the_sidecar_at_its_place(s, names):
    """O40: each mapped storage's host directory is a `--mount` bind, read-only, into the sidecar, at the path its
    override names; `--mount` refuses a source that is not there, where `-v` would create it."""
    args = C.sidecar_run_args(
        with_io_mode(s, "direct"), names, LABELS, Path("/w/mount"), s.oneclient.id, "p.test", token_file=TOKEN_FILE
    )
    binds = [v for f, v in pairs(args) if f == "--mount" and "/srv/invented" in v]
    assert binds == [
        f"type=bind,src=/srv/invented/one,dst={C.DIRECT_STORAGE_ROOT}/0,readonly",
        f"type=bind,src=/srv/invented/two,dst={C.DIRECT_STORAGE_ROOT}/1,readonly",
    ]
    assert not any("/srv/invented" in v for f, v in pairs(args) if f == "-v")


def test_o40_a_proxy_mount_binds_no_storage_and_passes_no_override_or_log_switch(s, names):
    """O40: proxy reads through the provider: the sidecar gets no storage, and oneclient no override and no log
    switch of the core's."""
    command = C.sidecar_command("p.test", s)
    assert "--override" not in command
    assert "--disable-log-buffering" not in command
    args = C.sidecar_run_args(s, names, LABELS, Path("/w/mount"), s.oneclient.id, "p.test", token_file=TOKEN_FILE)
    assert not any(C.DIRECT_STORAGE_ROOT in word for word in args)


def test_o40_the_agent_never_gets_a_storage_in_a_direct_mount(s, names):
    """O40: the providers' storage is the sidecar's alone: the agent's container is given the mount's share and its
    inputs, and no argument of it names a storage's host directory or its place in the sidecar."""
    declared = with_io_mode(s, "direct")
    args, env = agent(declared, names, DataPathMode.MOUNT, share=Path("/w/mount"))
    text = " ".join(args) + json.dumps(env)
    assert "/srv/invented" not in text
    assert C.DIRECT_STORAGE_ROOT not in text
    assert "0a1b2c3d4e5f" not in text


def test_o40_the_command_refuses_a_direct_mount_whose_mapping_was_removed_past_its_check(s):
    """O40: a mount whose fields were set past its own check (`object.__setattr__`) still raises at the command, so a
    direct oneclient never starts with no storage, and a proxy one never with one."""
    mount = dataclasses.replace(with_io_mode(s, "direct").mount)
    object.__setattr__(mount, "storages", ())
    with pytest.raises(ValueError, match="storages"):
        C.sidecar_command("p.test", dataclasses.replace(s, mount=mount))
    proxy = dataclasses.replace(s.mount)
    object.__setattr__(proxy, "storages", STORAGES)
    with pytest.raises(ValueError, match="storages"):
        C.sidecar_run_args(
            dataclasses.replace(s, mount=proxy), C.Names.make("sbx", "k"), LABELS, Path("/w/mount"), "img", "p.test",
            token_file=TOKEN_FILE,
        )  # fmt: skip


HELP = """General options:
  -h [ --help ] (=0)                    Show this help and exit.
  -c [ --config ] <path> (={path})
                                        Specify path to config file.
  --ignore-env                          Ignore options from environment variables.
"""
needs_tools = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("sh", "sed", "sha256sum", "cut", "head")),
    reason="the record is made by the sidecar's shell with these tools",
)


def run_record(tmp_path, help_text):
    """The sidecar's record step, run by a real shell with a stand-in for oneclient whose `--help` prints
    `help_text`."""
    binary = tmp_path / "oneclient"
    binary.write_text(f"#!/bin/sh\ncat <<'END'\n{help_text}END\n", encoding="utf-8")
    binary.chmod(0o755)
    script = C.SIDECAR_RECORD.replace(C.ONECLIENT_BIN, str(binary))
    done = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=False, timeout=30)
    assert done.returncode == 0, done.stderr
    return done.stdout


@needs_tools
def test_o36_the_sidecar_hashes_the_image_config_file_the_binary_names_and_the_record_reads_it(tmp_path):
    """O36: the file the image's oneclient would parse by default is found by asking the binary (`--help` names it), and
    its SHA-256 is printed before oneclient starts; `image_config_record` reads that line. The file is not parsed:
    this is only what the record shows."""
    conf = tmp_path / "ones3.conf"
    conf.write_bytes(b"# an invented configuration file\nforce_direct_io = true\n")
    out = run_record(tmp_path, HELP.format(path=conf))
    assert out == f"expcore-oneclient-config sha256={hashlib.sha256(conf.read_bytes()).hexdigest()} path={conf}\n"
    assert C.image_config_record(out) == {"path": str(conf), "sha256": hashlib.sha256(conf.read_bytes()).hexdigest()}


@needs_tools
def test_o36_a_file_the_sidecar_cannot_hash_is_recorded_unread_with_its_path_when_the_binary_named_one(tmp_path):
    """O36: an absent file (oneclient then runs with no options from it) is `unread`, with the path the binary named;
    a help that names none is `unread` with no path. Neither stops the start."""
    out = run_record(tmp_path, HELP.format(path=tmp_path / "absent.conf"))
    assert C.image_config_record(out) == {"path": str(tmp_path / "absent.conf"), "sha256": "unread"}
    nameless = run_record(tmp_path, "General options:\n  -h [ --help ]  Show this help.\n")
    assert C.image_config_record(nameless) == {"path": None, "sha256": "unread"}


@pytest.mark.parametrize(
    "logs",
    [
        None,
        "",
        "Oneclient has been successfully mounted in '/mnt/oneclient'.\n",
        "expcore-oneclient-config sha256=nothex path=/etc/oneclient.conf\n",
    ],
)
def test_o36_a_log_without_a_usable_record_gives_unread(logs):
    """O36: the sandbox's start never fails on the record: no output, no line or a line that is not a SHA-256 is
    `unread` (and a path that is not one absolute path of the image is dropped)."""
    assert C.image_config_record(logs)["sha256"] == "unread"
    assert C.image_config_record("expcore-oneclient-config sha256=" + "a" * 64 + " path=../../host/x\n") == {
        "path": None,
        "sha256": "a" * 64,
    }


def test_the_sidecar_command_sets_tls_from_the_settings_alone(s):
    assert "--insecure" not in C.sidecar_command("p.test", s)
    insecure = s.__class__(**{**s.__dict__, "tls": TlsSettings(insecure=True)})
    command = C.sidecar_command("p.test", insecure)
    assert command.count("--insecure") == 1
    assert command[:2] == ["-u", "ONECLIENT_INSECURE"]  # the image's own skip-verification flag is removed


def test_the_rest_agent_trusts_only_the_runs_ca_and_resolves_the_hosts_to_the_gate(s, names):
    bundle = Path("/w/tls/trust-0.pem")
    args, env = agent(
        s, names, DataPathMode.REST, gate_ip="10.1.0.2", gate_hosts=["z.test", "p.test"], ca=Path("/w/ca.pem"),
        trust_bundles={"/usr/lib/bundle.pem": bundle}, zone_host="z.test",
    )  # fmt: skip
    values = [v for f, v in pairs(args) if f == "--add-host"]
    assert values == ["z.test:10.1.0.2", "p.test:10.1.0.2"]
    assert f"{bundle}:/usr/lib/bundle.pem:ro" in [v for f, v in pairs(args) if f == "-v"]
    assert env["SSL_CERT_FILE"] == C.CA_PATH
    names_given = {v for f, v in pairs(args) if f == "-e"}
    assert names_given == set(env)
    credential_like = [
        n for n in names_given if any(w in n.upper() for w in ("TOKEN", "SECRET", "PASSWORD", "KEY", "AUTH"))
    ]
    assert credential_like == [C.ENV_TOKEN]  # the placeholder alone (O23)
    assert env[C.ENV_TOKEN] == s.placeholder_token


@pytest.mark.parametrize("mode", list(DataPathMode))
def test_o23_only_a_rest_agent_gets_the_placeholder_token_and_only_by_name(s, names, mode):
    """O23: on a REST data path the agent's ONEDATA_TOKEN is the configured placeholder, so the published client
    starts; it is named with -e, its value in the docker client's environment like every other. A mount or no data path
    gets no token variable at all."""
    extra = {
        DataPathMode.REST: {
            "gate_ip": "10.1.0.2",
            "gate_hosts": ["z.test"],
            "ca": Path("/w/ca.pem"),
            "zone_host": "z.test",
        },
        DataPathMode.MOUNT: {"share": Path("/w/mount")},
        DataPathMode.NONE: {},
    }[mode]
    args, env = agent(s, names, mode, **extra)
    if mode is DataPathMode.REST:
        assert C.ENV_TOKEN == "ONEDATA_TOKEN"  # the name the published client reads
        assert env[C.ENV_TOKEN] == s.placeholder_token == "placeholder"
        assert ("-e", C.ENV_TOKEN) in pairs(args)
    else:
        assert C.ENV_TOKEN not in env
    assert not [a for a in args if a.startswith(f"{C.ENV_TOKEN}=")]  # never NAME=value


def test_a_rest_agent_without_a_gate_is_refused(s, names):
    with pytest.raises(ValueError, match="gate"):
        agent(s, names, DataPathMode.REST)
    with pytest.raises(ValueError, match="mount"):
        agent(s, names, DataPathMode.MOUNT)


def test_o05_the_gate_runs_read_only_with_one_capability_and_its_files_mounted_read_only(s, names):
    """O05: The gate runs read only with one capability and its files mounted read only."""
    args = C.gate_run_args(s, names, LABELS, Path("/w"), Path("/pkg"), s.gate.id, token_file=GATE_TOKEN_FILE)
    assert "--read-only" in args
    assert ("--cap-add", "NET_BIND_SERVICE") in pairs(args)
    assert ("--cap-drop", "ALL") in pairs(args)
    mounts = [v for f, v in pairs(args) if f == "-v"]
    assert f"/pkg:{C.GATE_DIR}:ro" in mounts
    assert "/w/gate.json:/etc/gate/gate.json:ro" in mounts
    assert args[-2:] == [f"{C.GATE_DIR}/gate.py", "/etc/gate/gate.json"]
    assert s.gate.id in args


def test_the_limits_come_from_the_settings(s, names):
    args, _ = agent(s, names, DataPathMode.NONE)
    flags = dict(pairs(args))
    assert flags["--cpus"] == str(s.limits.cpus)
    assert flags["--memory"] == str(s.limits.memory_bytes)
    assert flags["--pids-limit"] == str(s.limits.pids)
    assert flags["--ulimit"] == f"nofile={s.limits.nofile}:{s.limits.nofile}"
    assert flags["--user"] == f"{s.agent_uid}:{s.agent_uid}"


def test_o03_with_a_federation_network_the_sidecar_starts_on_it_and_dials_the_local_providers_name(s, names):
    """O03: with `[sandbox.federation]` the sidecar starts on the federation's own network, never also on the
    default bridge, and dials the local provider by its name there, not the host of the URL the host reaches.
    Without the table it starts as it always has: on the default bridge, dialling the URL's host."""
    federated = dataclasses.replace(s, federation=FederationNetwork(FED_NETWORK, FED_LOCAL))
    args = C.sidecar_run_args(
        federated, names, LABELS, Path("/w/mount"), s.oneclient.id, "p.test", token_file=TOKEN_FILE
    )
    assert [v for f, v in pairs(args) if f == "--network"] == [FED_NETWORK]
    assert args[args.index("-H") + 1] == FED_LOCAL
    assert "p.test" not in args
    plain = C.sidecar_run_args(s, names, LABELS, Path("/w/mount"), s.oneclient.id, "p.test", token_file=TOKEN_FILE)
    assert [v for f, v in pairs(plain) if f == "--network"] == ["bridge"]
    assert plain[plain.index("-H") + 1] == "p.test"


@pytest.mark.parametrize("mode", list(DataPathMode))
def test_o05_the_federation_network_is_the_sidecars_alone_and_the_agent_never_joins_it(s, names, mode):
    """O05: the sidecar is on the federation network and the agent is not: it stays on its own internal network in
    every data path, so it reaches the federation through the mount and nothing else."""
    federated = dataclasses.replace(s, federation=FederationNetwork(FED_NETWORK, FED_LOCAL))
    sidecar = C.sidecar_run_args(
        federated, names, LABELS, Path("/w/mount"), s.oneclient.id, "p.test", token_file=TOKEN_FILE
    )
    assert dict(pairs(sidecar))["--network"] == FED_NETWORK
    extra = {
        DataPathMode.REST: {"gate_ip": "10.1.0.2", "gate_hosts": ["h.test"], "ca": Path("/w/ca.pem"), "zone_host": "z"},
        DataPathMode.MOUNT: {"share": Path("/w/mount")},
        DataPathMode.NONE: {},
    }[mode]
    args, _ = C.agent_run_args(
        federated, names, LABELS, image_id=s.agent.id, mode=mode, work=Path("/w/work"),
        **({"share": None, "gate_ip": None, "gate_hosts": [], "ca": None, "trust_bundles": {}, "zone_host": None}
           | extra),
    )  # fmt: skip
    assert [v for f, v in pairs(args) if f == "--network"] == [names.net]
    assert not [a for a in args if FED_NETWORK in a]


# The hosts of a Compose federation's platform.toml: both published on the host's loopback (#203).
LOOPBACK_ZONE, LOOPBACK_PROVIDER = Endpoint("127.0.0.1", 9443), Endpoint("127.0.0.1", 9444)


def test_o03_with_a_federation_network_the_gate_starts_on_it_and_dials_the_zone_and_provider_by_their_names(s, names):
    """O30 (#203): with `[sandbox.federation]` the gate starts on the federation's own network, never also on the
    default bridge, and serves and dials the zone and the local provider by their names there, on the network's port
    443: never the host's loopback, where the platform file publishes them and where a container reaches only itself.
    The sandbox's internal network is joined afterwards (`docker network connect`), as before."""
    federated = dataclasses.replace(s, federation=FederationNetwork(FED_NETWORK, FED_LOCAL, FED_ZONE))
    args = C.gate_run_args(federated, names, LABELS, Path("/w"), Path("/pkg"), s.gate.id, token_file=GATE_TOKEN_FILE)
    assert [v for f, v in pairs(args) if f == "--network"] == [FED_NETWORK]
    hosts = C.gate_upstreams(federated, LOOPBACK_ZONE, LOOPBACK_PROVIDER)
    assert hosts == {
        FED_ZONE: {"role": "onezone", "addr": FED_ZONE, "port": 443, "tls": True},
        FED_LOCAL: {"role": "oneprovider", "addr": FED_LOCAL, "port": 443, "tls": True},
    }
    assert "127.0.0.1" not in json.dumps(hosts)


def test_o03_without_the_table_the_gate_is_on_the_default_bridge_and_dials_the_urls_hosts_as_before(s, names):
    """O30: without `[sandbox.federation]` the gate starts as it always has: on docker's default bridge, serving and
    dialling the hosts and ports of the zone's and the local provider's URLs."""
    args = C.gate_run_args(s, names, LABELS, Path("/w"), Path("/pkg"), s.gate.id, token_file=GATE_TOKEN_FILE)
    assert [v for f, v in pairs(args) if f == "--network"] == ["bridge"]
    zone, provider = Endpoint("zone.test", 443), Endpoint("alpha.test", 8443, tls=True)
    assert C.gate_upstreams(s, zone, provider) == {
        "zone.test": {"role": "onezone", "addr": "zone.test", "port": 443, "tls": True},
        "alpha.test": {"role": "oneprovider", "addr": "alpha.test", "port": 8443, "tls": True},
    }


def test_o03_a_federation_table_without_the_zones_name_gives_the_gate_no_upstreams(s):
    """O30: the load refuses a REST level whose table has no zone; a table built without one in code is refused too,
    rather than letting the zone fall back to the platform file's URL, which no container on the network reaches."""
    federated = dataclasses.replace(s, federation=FederationNetwork(FED_NETWORK, FED_LOCAL))
    with pytest.raises(ValueError, match="zone"):
        C.gate_upstreams(federated, LOOPBACK_ZONE, LOOPBACK_PROVIDER)


def test_o05_on_the_federation_network_the_gate_joins_it_and_the_rest_agent_never_does(s, names):
    """O30 (#203): the REST agent stays on its own internal network, whose one peer is the gate; the gate alone is on
    the federation's network. The agent resolves the names the gate serves to the gate's address, so it reaches the
    federation through the gate and nothing else."""
    federated = dataclasses.replace(s, federation=FederationNetwork(FED_NETWORK, FED_LOCAL, FED_ZONE))
    hosts = C.gate_upstreams(federated, LOOPBACK_ZONE, LOOPBACK_PROVIDER)
    args, env = agent(
        federated, names, DataPathMode.REST, gate_ip="10.1.0.2", gate_hosts=list(hosts), ca=Path("/w/ca.pem"),
        zone_host=FED_ZONE,
    )  # fmt: skip
    assert [v for f, v in pairs(args) if f == "--network"] == [names.net]
    assert not [a for a in args if FED_NETWORK in a]
    assert sorted(v for f, v in pairs(args) if f == "--add-host") == sorted(f"{h}:10.1.0.2" for h in hosts)
    assert env[C.ENV_ZONE_HOST] == FED_ZONE


def test_the_oneclient_log_directory_is_the_last_one_given():
    assert C.effective_log_dir(["-f"]) == C.ONECLIENT_LOG_DIR
    assert C.effective_log_dir(["--log-dir", "/a", "--log-dir=/b"]) == "/b"
    assert C.effective_log_dir(["-l", "/c"]) == "/c"


def test_o23_the_certifi_question_answers_with_the_interpreters_bundle_or_none_and_is_read_back_exactly():
    """O23: the probe the sandbox runs in the agent image, run here with this interpreter, prints the path certifi
    gives, which `certifi_bundle` reads back; an interpreter without certifi prints `certifi:none`, read as None."""
    import subprocess
    import sys

    import certifi

    found = subprocess.run([sys.executable, "-c", C.CERTIFI_PROBE], capture_output=True, text=True, check=True)
    assert C.certifi_bundle(found.stdout) == certifi.where()
    hidden = "import sys\nsys.modules['certifi'] = None\n" + C.CERTIFI_PROBE  # an import of it raises ImportError
    absent = subprocess.run([sys.executable, "-c", hidden], capture_output=True, text=True, check=True)
    assert absent.stdout == "certifi:none\n"
    assert C.certifi_bundle(absent.stdout) is None


def test_o23_the_certifi_question_runs_as_the_agent_without_a_network_and_its_bundle_is_extended_once(s):
    """O23: the one-shot runs the image's interpreter as the agent's code runs, as the agent's user in /work, the first
    directory of its import path, and with no network; the certifi bundle joins the declared trust paths after them,
    once."""
    args = C.certifi_probe_args(s, "sbx-once-1", LABELS, s.agent.id)
    assert ("--network", "none") in pairs(args)
    assert ("--user", f"{s.agent_uid}:{s.agent_uid}") in pairs(args)
    assert ("--workdir", "/work") in pairs(args)
    assert ("--entrypoint", s.python) in pairs(args)
    assert args[:2] == ["run", "--rm"]
    assert args[-3:] == [s.agent.id, "-c", C.CERTIFI_PROBE]
    assert C.trust_targets(("/etc/a.pem",), "/lib/cacert.pem") == ("/etc/a.pem", "/lib/cacert.pem")
    assert C.trust_targets(("/lib/cacert.pem",), "/lib/cacert.pem") == ("/lib/cacert.pem",)
    assert C.trust_targets(("/etc/a.pem",), None) == ("/etc/a.pem",)
