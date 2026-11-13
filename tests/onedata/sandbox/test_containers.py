"""The docker arguments of each container: read-only everywhere, images by id, no secret in an argument (O05, O13)."""

import dataclasses
import json
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
LOG_DIR = Path("/w/oneclient-log")  # the run's directory for the sidecar's oneclient log


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


def mounts(args):
    """The `--mount` specs of a docker command line."""
    return [v for f, v in pairs(args) if f == "--mount"]


def sidecar_args(s, names, **kw):
    """`docker run` of the sidecar for provider `p.test`, with the run's token file and log directory unless given."""
    given = {"token_file": TOKEN_FILE, "log_dir": LOG_DIR} | kw
    return C.sidecar_run_args(s, names, LABELS, Path("/w/mount"), s.oneclient.id, "p.test", **given)


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
    args = sidecar_args(s, names, token_file=token_file)
    assert ("--device", "/dev/fuse") in pairs(args)
    assert C.ENV_ACCESS_TOKEN not in [v for f, v in pairs(args) if f == "-e"]
    assert [a for a in args if C.ENV_ACCESS_TOKEN in a] == [C.SIDECAR_START]  # named only where the file is read
    assert f"type=bind,src={token_file},dst={C.SIDECAR_TOKEN_PATH},readonly" in mounts(args)
    assert not [v for f, v in pairs(args) if f == "-v" and str(token_file) in v]  # `-v` makes a directory of a gone one
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
    assert f"type=bind,src={gate_token},dst={C.GATE_TOKEN_PATH},readonly" in mounts(args)
    assert not [v for f, v in pairs(args) if f == "-v" and str(gate_token) in v]  # `-v` makes a directory of a gone one


def test_o05_the_gates_tls_keys_are_bound_with_mount_so_a_missing_directory_is_refused_not_made(s, names):
    """O05: the gate's TLS keys are a secret, bound read-only with `--mount`, which refuses a missing source. Its
    configuration, which holds none, stays a `-v` bind."""
    args = C.gate_run_args(s, names, LABELS, Path("/w"), Path("/pkg"), s.gate.id, token_file=GATE_TOKEN_FILE)
    assert "type=bind,src=/w/tls,dst=/etc/gate/tls,readonly" in mounts(args)
    assert not [v for f, v in pairs(args) if f == "-v" and "/w/tls" in v]
    assert "/w/gate.json:/etc/gate/gate.json:ro" in [v for f, v in pairs(args) if f == "-v"]


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
    args = sidecar_args(federated, names)
    assert [v for f, v in pairs(args) if f == "--network"] == [FED_NETWORK]
    assert args[args.index("-H") + 1] == FED_LOCAL
    assert "p.test" not in args
    plain = sidecar_args(s, names)
    assert [v for f, v in pairs(plain) if f == "--network"] == ["bridge"]
    assert plain[plain.index("-H") + 1] == "p.test"


@pytest.mark.parametrize("mode", list(DataPathMode))
def test_o05_the_federation_network_is_the_sidecars_alone_and_the_agent_never_joins_it(s, names, mode):
    """O05: the sidecar is on the federation network and the agent is not: it stays on its own internal network in
    every data path, so it reaches the federation through the mount and nothing else."""
    federated = dataclasses.replace(s, federation=FederationNetwork(FED_NETWORK, FED_LOCAL))
    sidecar = sidecar_args(federated, names)
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


def test_o05_the_sidecar_declares_oneclients_log_directory_and_binds_the_runs_own_directory_there(s, names):
    """O05: oneclient is told where to log (`--log-dir`, once, as its option takes one value) and the run's own
    owner-only directory is bound read-write at that path, so the log is read from the host and collecting it never
    needs `docker cp` from a running sidecar."""
    args = sidecar_args(s, names)
    command = args[args.index("-c") + 3 :]  # what the sidecar's shell hands to `env`
    assert command.count("--log-dir") == 1
    assert command[command.index("--log-dir") + 1] == C.SIDECAR_LOG_DIR
    assert command.index("--log-dir") < command.index(C.SIDECAR_MOUNT)
    assert f"type=bind,src={LOG_DIR},dst={C.SIDECAR_LOG_DIR}" in mounts(args)  # no `readonly`: it writes there
    assert [a for a in command if a.startswith("-l") or a.startswith("--l")] == ["--log-dir"]


@pytest.mark.parametrize(
    ("extra", "option"),
    [
        (("-l/var/lib/oc-log",), "-l"),
        (("-l", "/var/lib/oc-log"), "-l"),
        (("-fl/var/lib/oc-log",), "-l"),
        (("--log-dir", "/var/lib/oc-log"), "--log-dir"),
        (("--log-dir=/var/lib/oc-log",), "--log-dir"),
        (("--log-d=/var/lib/oc-log",), "--log-d"),  # an abbreviation oneclient accepts
        (("--log", "/var/lib/oc-log"), "--log"),  # one it refuses as ambiguous: refused here all the same
        (("-f", "--log-dir"), "--log-dir"),  # no value: oneclient would take the mountpoint
    ],
)
def test_o05_the_sidecars_command_refuses_a_log_directory_in_extra_args_in_every_form(s, names, extra, option):
    """O05: the sandbox owns oneclient's log directory, whatever `[sandbox.mount] extra_args` holds when the settings
    are built in code rather than loaded: the command is never built with a second `--log-dir`, which oneclient
    takes once and would fail to start on."""
    given = dataclasses.replace(s, mount=dataclasses.replace(s.mount, extra_args=extra))
    with pytest.raises(ValueError, match="log directory") as e:
        C.sidecar_command("p.test", given)
    assert f"({option})" in str(e.value)
    assert "oc-log" not in str(e.value)
    with pytest.raises(ValueError, match="log directory"):
        sidecar_args(given, names)


def test_o05_other_extra_args_follow_the_log_directory_the_sandbox_declares(s):
    """O05: the arguments the operator may give come after the sandbox's own, with the one `--log-dir`."""
    given = dataclasses.replace(s, mount=dataclasses.replace(s.mount, extra_args=("--provider-timeout=30", "-d")))
    command = C.sidecar_command("p.test", given)
    assert command.count("--log-dir") == 1
    assert command[command.index("--log-dir") + 1] == C.SIDECAR_LOG_DIR
    assert command[-3:-1] == ["--provider-timeout=30", "-d"]
    assert command[-1] == C.SIDECAR_MOUNT


@pytest.mark.parametrize("path", ["/w/a,b/token", '/w/a"b/token', "/w/a\nb/token"])
def test_o05_a_path_that_mount_would_read_as_syntax_is_refused_not_bound_as_something_else(s, names, path):
    """O05: `--mount` takes comma-separated fields: a path with a comma, a quote or a line break would end the field or
    open another, so the bind of a secret is refused."""
    with pytest.raises(ValueError, match="comma"):
        sidecar_args(s, names, token_file=Path(path))
    with pytest.raises(ValueError, match="comma"):
        C.gate_run_args(s, names, LABELS, Path("/w"), Path("/pkg"), s.gate.id, token_file=Path(path))


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
