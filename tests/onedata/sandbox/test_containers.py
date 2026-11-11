"""The docker arguments of each container: read-only everywhere, images by id, no secret in an argument (O05, O13)."""

import dataclasses
import subprocess
from pathlib import Path

import pytest
from sandbox_support import FED_LOCAL, FED_NETWORK

from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox.api import DataPathMode
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
    exits without starting it when the file is empty or missing."""
    token = tmp_path / "token"
    script = C.SIDECAR_START.replace(C.SIDECAR_TOKEN_PATH, str(token))
    show = ["sh", "-c", f'printf "%s|%s|%s" "${C.ENV_ACCESS_TOKEN}" "$1" "$2"', "show", "a b", "-u"]

    def run():
        return subprocess.run(
            ["sh", "-c", script, C.SIDECAR_SHELL_NAME, *show], capture_output=True, text=True, check=False, timeout=30
        )

    token.write_text("tok-invented-0123", encoding="utf-8")
    done = run()
    assert (done.returncode, done.stdout) == (0, "tok-invented-0123|a b|-u")
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
    assert not [n for n in names_given if any(w in n.upper() for w in ("TOKEN", "SECRET", "PASSWORD", "KEY", "AUTH"))]


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


def test_the_oneclient_log_directory_is_the_last_one_given():
    assert C.effective_log_dir(["-f"]) == C.ONECLIENT_LOG_DIR
    assert C.effective_log_dir(["--log-dir", "/a", "--log-dir=/b"]) == "/b"
    assert C.effective_log_dir(["-l", "/c"]) == "/c"
