"""The docker arguments of each container: read-only everywhere, images by id, no secret in an argument (O05, O13)."""

from pathlib import Path

import pytest

from expcore_onedata.sandbox import containers as C
from expcore_onedata.sandbox.api import DataPathMode
from expcore_onedata.tls import TlsSettings

LABELS = ["--label", "org.test.sandbox=1"]


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


def test_o05_the_sidecar_is_the_only_container_with_fuse_and_the_token_goes_by_name(s, names):
    """O05: The sidecar is the only container with fuse and the token goes by name."""
    args = C.sidecar_run_args(s, names, LABELS, Path("/w/mount"), s.oneclient.id, "p.test")
    assert ("--device", "/dev/fuse") in pairs(args)
    assert ("-e", C.ENV_ACCESS_TOKEN) in pairs(args)  # the name only: the value travels in the environment
    assert "ro,allow_other" in args
    assert s.oneclient.id in args
    assert "type=bind,src=/w/mount,dst=/mnt/oneclient,bind-propagation=rshared" in args


def test_o05_the_gate_takes_the_token_by_name_and_it_is_the_name_the_gate_reads(s, names):
    """O05: the gate holds the token: it is named with -e and its value travels in the docker client's environment."""
    from expcore_onedata.sandbox import gate

    args = C.gate_run_args(s, names, LABELS, Path("/w"), Path("/pkg"), s.gate.id)
    assert ("-e", C.ENV_GATE_TOKEN) in pairs(args)
    assert C.ENV_GATE_TOKEN == gate.TOKEN_ENV
    assert not [a for a in args if "=" in a and a.startswith(C.ENV_GATE_TOKEN)]  # never NAME=value


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
    assert credential_like == [C.ENV_TOKEN]  # the placeholder alone (O21)
    assert env[C.ENV_TOKEN] == s.placeholder_token


@pytest.mark.parametrize("mode", list(DataPathMode))
def test_o21_only_a_rest_agent_gets_the_placeholder_token_and_only_by_name(s, names, mode):
    """O21: on a REST data path the agent's ONEDATA_TOKEN is the configured placeholder, so the published client
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
    args = C.gate_run_args(s, names, LABELS, Path("/w"), Path("/pkg"), s.gate.id)
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


def test_the_oneclient_log_directory_is_the_last_one_given():
    assert C.effective_log_dir(["-f"]) == C.ONECLIENT_LOG_DIR
    assert C.effective_log_dir(["--log-dir", "/a", "--log-dir=/b"]) == "/b"
    assert C.effective_log_dir(["-l", "/c"]) == "/c"


def test_o21_the_certifi_question_answers_with_the_interpreters_bundle_or_none_and_is_read_back_exactly():
    """O21: the probe the sandbox runs in the agent image, run here with this interpreter, prints the path certifi
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


def test_o21_the_certifi_question_runs_as_the_agent_without_a_network_and_its_bundle_is_extended_once(s):
    """O21: the one-shot runs the image's interpreter as the agent's user with no network; the certifi bundle joins the
    declared trust paths after them, once."""
    args = C.certifi_probe_args(s, "sbx-once-1", LABELS, s.agent.id)
    assert ("--network", "none") in pairs(args)
    assert ("--user", f"{s.agent_uid}:{s.agent_uid}") in pairs(args)
    assert ("--entrypoint", s.python) in pairs(args)
    assert args[:2] == ["run", "--rm"]
    assert args[-3:] == [s.agent.id, "-c", C.CERTIFI_PROBE]
    assert C.trust_targets(("/etc/a.pem",), "/lib/cacert.pem") == ("/etc/a.pem", "/lib/cacert.pem")
    assert C.trust_targets(("/lib/cacert.pem",), "/lib/cacert.pem") == ("/lib/cacert.pem",)
    assert C.trust_targets(("/etc/a.pem",), None) == ("/etc/a.pem",)
