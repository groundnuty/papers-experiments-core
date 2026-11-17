"""O70: a service of the HPC bundle registers under the name its layout declares, not its node's hostname, and its cell
is given a hosts file that resolves every peer's declared name (the node's own /etc/hosts is replaced, since an
unresolved peer crashes a provider's lookup of it); no onepanel or Onedata setting of the service holds the node's name
as the domain it registers or its certificate is made for.

Run in plain bash on this host, over the stand-ins of tests/deploy/hpcnode.py (hostname, apptainer, curl, ss). Every
name is invented: `.test` for the layout's, `example.invalid` for a node's, and the documentation range for addresses.
"""

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("hpcop_node_tests", HERE / "hpcnode.py")
node = sys.modules.get("hpcop_node_tests") or importlib.util.module_from_spec(spec)
sys.modules["hpcop_node_tests"] = node
spec.loader.exec_module(node)

NODE_FQDN = "provider-a.example.invalid"  # what the hostname stand-in says: the node's own name
NODE_SHORT = "provider-a"
ZONE_NAME, ALPHA, BETA = "zone.layout.test", "alpha.layout.test", "beta.layout.test"
ADDRESSES = {ZONE_NAME: "192.0.2.10", ALPHA: "192.0.2.11", BETA: "192.0.2.12"}
HOSTS = [f"--host={name}={address}" for name, address in ADDRESSES.items()]
ADMIN = "admin@example.org"


LISTENER = 'tcp LISTEN 0 4096 127.0.0.1:9100 0.0.0.0:* users:(("beam.smp",pid=7,fd=9)) uid:1000 ino:1 sk:1'


@pytest.fixture
def env(tmp_path):
    holder = tmp_path / "tmpfs"
    holder.mkdir(mode=0o700)
    bin_dir = node.install_fakes(tmp_path / "bin")
    (bin_dir / "ss").write_text(f"#!/bin/bash\nprintf '%s\\n' '{LISTENER}'\n", encoding="utf-8")  # the listeners
    (bin_dir / "ss").chmod(0o755)
    return node.node_env(
        holder,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        FAKE_CURL_LOG=str(tmp_path / "curl-log"),
        FAKE_SERVICE_ARGS=str(tmp_path / "service-args"),
        FAKE_LOG=str(tmp_path / "fake-log"),
        FAKE_CURL_STATE=str(tmp_path / "curl-state"),
    )


def configure(env, kind, *args):
    work = node.make_work(Path(env["TMPDIR"]), kind=kind, secrets=False)
    (work / "rootfs" / "etc").mkdir()
    secrets = node.secrets_of(work)
    done = node.script("configure.sh", kind, "--workdir", str(work), *args, env=env)
    return work, secrets, done


def peers_of(skip):
    return [address for name, address in ADDRESSES.items() if name != skip]


def provider_args(own=ALPHA, zone=ZONE_NAME):
    return ["alpha", zone, ADMIN, *peers_of(own), f"--declared={own}", *HOSTS]


def zone_args():
    return ["zone", *peers_of(ZONE_NAME), f"--declared={ZONE_NAME}", *HOSTS]


def test_o70_a_provider_registers_its_declared_name_and_the_zones_and_certifies_for_its_own(env):
    """O70: `oneprovider.domain` and the generated certificate's domain are the provider's declared name, the zone it
    registers with is the zone's declared name, and the node's own FQDN is the cluster's node name only."""
    work, secrets, done = configure(env, "op", *provider_args())
    assert done.returncode == 0, done.stderr
    config = (secrets / "config.yaml").read_text(encoding="utf-8")
    assert re.search(rf'^  domain: "{ALPHA}"$', config, re.M)
    assert re.search(rf'^  domainName: "{ZONE_NAME}"$', config, re.M)  # onezone.domainName of the provider's config
    assert f'hostname: "{NODE_SHORT}"' in config  # the cluster's own node keeps the node's name
    assert not re.search(rf'^  (domain|domainName): "{re.escape(NODE_FQDN)}"', config, re.M)
    assert f"ONEPANEL_GENERATED_CERT_DOMAIN={ALPHA}\n" in (secrets / "env").read_text(encoding="utf-8")
    assert (work / "declared-name").read_text(encoding="utf-8") == f"{ALPHA}\n"
    assert f"registered as {ALPHA}" in done.stderr


def test_o70_the_zone_registers_its_declared_name_and_certifies_for_it(env):
    """O70: `onezone.domainName` and the generated certificate's domain are the zone's declared name."""
    _, secrets, done = configure(env, "oz", *zone_args())
    assert done.returncode == 0, done.stderr
    config = (secrets / "config.yaml").read_text(encoding="utf-8")
    assert re.search(rf'^  domainName: "{ZONE_NAME}"$', config, re.M)
    assert NODE_FQDN not in config.replace(f'"{NODE_SHORT}"', "")
    assert f"ONEPANEL_GENERATED_CERT_DOMAIN={ZONE_NAME}\n" in (secrets / "env").read_text(encoding="utf-8")


@pytest.mark.parametrize("kind", ["op", "oz"])
def test_o70_the_hosts_file_lists_the_node_and_every_peers_declared_name_with_its_address(env, kind):
    """O70: the cell's hosts file has the loopback, the node's own names on its own address (the cluster keeps them),
    and every service's declared name, the service's own and each peer's, with its address."""
    own = ALPHA if kind == "op" else ZONE_NAME
    args = provider_args() if kind == "op" else zone_args()
    work, _, done = configure(env, kind, *args)
    assert done.returncode == 0, done.stderr
    lines = (work / "hosts").read_text(encoding="utf-8").splitlines()
    entries = [line.split() for line in lines if line and not line.startswith("#")]
    assert ["127.0.0.1", "localhost"] in entries
    assert [ADDRESSES[own], NODE_FQDN, NODE_SHORT, own] in entries
    for name, address in ADDRESSES.items():
        if name != own:
            assert [address, name] in entries
    assert oct((work / "hosts").stat().st_mode & 0o777) == "0o644"
    listed = (work / "rootfs" / "etc" / "hpcop-peers").read_text(encoding="utf-8").splitlines()
    peers = [line for line in listed if line and not line.startswith("#")]
    assert sorted(peers) == sorted(peers_of(own))  # the shim moves the same peers' 443 as the hosts file names


def test_o70_without_a_declared_name_a_service_registers_the_nodes_own_as_before(env):
    """O70: no `--declared` means the node's FQDN registers and certifies, and no hosts file is made."""
    work, secrets, done = configure(env, "op", "alpha", ZONE_NAME, ADMIN, "192.0.2.10")
    assert done.returncode == 0, done.stderr
    assert re.search(rf'^  domain: "{re.escape(NODE_FQDN)}"$', (secrets / "config.yaml").read_text(), re.M)
    assert f"ONEPANEL_GENERATED_CERT_DOMAIN={NODE_FQDN}\n" in (secrets / "env").read_text(encoding="utf-8")
    assert not (work / "hosts").exists()
    assert not (work / "declared-name").exists()


@pytest.mark.parametrize(
    ("args", "why"),
    [
        # a peer whose address no --host names: its declared name would not resolve
        (["alpha", ZONE_NAME, ADMIN, "192.0.2.10", "192.0.2.99", f"--declared={ALPHA}", *HOSTS], "no declared name"),
        # a declared service that is no peer: the shim would not move its 443
        (["alpha", ZONE_NAME, ADMIN, "192.0.2.10", f"--declared={ALPHA}", *HOSTS], "no listed peer"),
        (["alpha", ZONE_NAME, ADMIN, *peers_of(ALPHA), f"--declared={ALPHA}", *HOSTS[:2]], "no declared name"),
        (["alpha", ZONE_NAME, ADMIN, *peers_of(ALPHA), *HOSTS], "need --declared"),
        (["alpha", ZONE_NAME, ADMIN, *peers_of(ALPHA), "--declared=gamma.layout.test", *HOSTS], "own declared name"),
        (["alpha", ZONE_NAME, ADMIN, *peers_of(ALPHA), f"--declared={ALPHA}", *HOSTS, HOSTS[0]], "twice"),
        (
            [
                "alpha",
                ZONE_NAME,
                ADMIN,
                *peers_of(ALPHA),
                f"--declared={ALPHA}",
                *HOSTS,
                "--host=zz.layout.test=192.0.2.10",
            ],
            "under two names",
        ),
        (
            ["alpha", ZONE_NAME, ADMIN, *peers_of(ALPHA), f"--declared={ALPHA}", f"--declared={BETA}", *HOSTS],
            "more than one",
        ),
        (
            ["alpha", ZONE_NAME, ADMIN, *peers_of(ALPHA), f"--declared={ALPHA}", "--host=nonsense", *HOSTS],
            "NAME=ADDRESS",
        ),
        (
            ["alpha", ZONE_NAME, ADMIN, *peers_of(ALPHA), f"--declared={ALPHA}", "--host=x.layout.test=not-an-ip"],
            "invalid ip",
        ),
        (
            ["alpha", "other.layout.test", ADMIN, *peers_of(ALPHA), f"--declared={ALPHA}", *HOSTS],
            "zone's declared name",
        ),
    ],
)
def test_o70_a_service_whose_peers_names_are_not_all_listed_is_refused_before_anything_is_written(env, args, why):
    """O70: a peer with no declared name, a declared name that is no peer, a repeated name or address, a malformed
    entry or a zone name nothing lists stops the script before it writes a configuration, a hosts file or a name."""
    work, secrets, done = configure(env, "op", *args)
    assert done.returncode != 0
    assert why in done.stderr
    assert not (secrets / "config.yaml").exists()
    assert not (work / "hosts").exists()
    assert not (work / "declared-name").exists()


# --- the service's own start, and what it dials about itself ---


class Kept:
    """A kept work directory of one kind that registered under a declared name, restarted over stand-ins."""

    def __init__(self, tmp_path, env, processes, kind):
        self.kind, self.processes = kind, processes
        holder = Path(env["TMPDIR"])
        self.work = node.make_work(holder, kind=kind)
        node.put(self.work / "stop-requested", "keep\n")
        node.put(self.work / "keep-until", f"{int(time.time()) + 3600}\n")
        self.own = ALPHA if kind == "op" else ZONE_NAME
        identity = {
            "kind": kind,
            "domain": self.own,
            **({"providerId": "pid-1"} if kind == "op" else {"adminUserId": "a-1"}),
        }
        node.put(self.work / "identity.json", json.dumps(identity) + "\n")
        self.bundle = tmp_path / "bundle"
        shutil.copytree(node.BUNDLE, self.bundle)
        (self.bundle / "check-defaults.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        secrets = node.secrets_of(self.work)
        node.put(secrets / "couchbase-user", "cb0123456789ab\n")
        node.put(secrets / "couchbase-password", "Aa0" * 8 + "\n")
        node.put(secrets / "passphrase", "Pass1234" * 6 + "\n")
        panel = f"{kind}_panel"
        rootfs = self.work / "rootfs"
        (rootfs / "etc").mkdir()
        (rootfs / "etc" / panel).symlink_to(f"/volumes/persistence/etc/{panel}")
        ca = rootfs / "volumes" / "persistence_backup" / "etc" / panel
        ca.mkdir(parents=True)
        (ca / "OnedataTestWebServerCa.pem").write_text("ca\n", encoding="utf-8")
        (rootfs / "etc" / "hpcop-erlang-env").write_text("ERL_EPMD_PORT=21500\n", encoding="utf-8")
        (rootfs / "root" / "service-ready.lock").touch()
        node.put(self.work / "hosts", "# written by configure.sh\n127.0.0.1 localhost\n", 0o644)
        node.put(self.work / "declared-name", self.own + "\n", 0o644)
        Path(env["FAKE_CURL_STATE"]).write_text(
            json.dumps(
                {
                    "creds": ["cb0123456789ab", "Aa0" * 8],
                    "providerId": "pid-1",
                    "domain": self.own,
                    "adminUserId": "a-1",
                }
            ),
            encoding="utf-8",
        )
        self.env = {**env, "HPCOP_STOP_SECONDS": "30"}

    def start(self):
        proc = subprocess.Popen(
            ["bash", str(self.bundle / "restart.sh"), self.kind, "--workdir", str(self.work)],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True,
        )  # fmt: skip
        self.processes.append(proc)
        deadline = time.monotonic() + 60
        marker, record = self.work / "service-verified", self.work / "service.pid"
        while not (record.exists() and marker.exists() and marker.read_bytes() == record.read_bytes()):
            assert proc.poll() is None, proc.communicate()[0]
            assert time.monotonic() < deadline, "timed out waiting for the ready marker"
            time.sleep(0.1)
        return proc

    def stop(self):
        argv = [
            "bash",
            str(self.bundle / "teardown.sh"),
            self.kind,
            "--workdir",
            str(self.work),
            "--keep",
            "--timeout",
            "30",
        ]
        subprocess.run(argv, capture_output=True, text=True, env=self.env, timeout=90, check=False)


@pytest.fixture
def processes():
    started = []
    yield started
    node.kill_all(started)


@node.needs_pidfd
@pytest.mark.parametrize("kind", ["op", "oz"])
def test_o70_the_service_starts_with_our_hosts_file_in_place_of_the_nodes_and_dials_itself_by_its_declared_name(
    tmp_path, env, processes, kind
):
    """O70: a service whose work directory holds a hosts file is started with the site's bind paths off and that file
    bound read-only over /etc/hosts (a restart reads it from the work directory, not from configure.sh's arguments), and
    its own readiness calls name its declared name and pin it to the loopback, never the node's FQDN."""
    kept = Kept(tmp_path, env, processes, kind)
    kept.start()
    try:
        words = Path(env["FAKE_SERVICE_ARGS"]).read_text(encoding="utf-8").split("\n")
        at = words.index("--no-mount")
        assert words[at : at + 4] == ["--no-mount", "bind-paths", "--bind", f"{kept.work}/hosts:/etc/hosts:ro"]
        calls = Path(env["FAKE_CURL_LOG"]).read_text(encoding="utf-8")
        assert f"--resolve {kept.own}:8443:127.0.0.1" in calls
        assert NODE_FQDN not in calls
        urls = re.findall(r"https?://\S+", calls)  # Onedata's (the declared name) and Couchbase's (the loopback)
        assert urls
        assert all(url.startswith((f"https://{kept.own}:", "http://127.0.0.1:")) for url in urls)
    finally:
        kept.stop()


@node.needs_pidfd
def test_o70_a_service_without_a_hosts_file_keeps_the_sites_binds_as_before(tmp_path, env, processes):
    """O70: no hosts file in the work directory, no `--no-mount`, and the node's own name is what it dials."""
    kept = Kept(tmp_path, env, processes, "oz")
    (kept.work / "hosts").unlink()
    (kept.work / "declared-name").unlink()
    node.put(kept.work / "identity.json", json.dumps({"kind": "oz", "domain": NODE_FQDN, "adminUserId": "a-1"}) + "\n")
    Path(env["FAKE_CURL_STATE"]).write_text(
        json.dumps(
            {"creds": ["cb0123456789ab", "Aa0" * 8], "providerId": "p", "domain": NODE_FQDN, "adminUserId": "a-1"}
        ),
        encoding="utf-8",
    )
    kept.start()
    try:
        assert "--no-mount" not in Path(env["FAKE_SERVICE_ARGS"]).read_text(encoding="utf-8")
        assert f"--resolve {NODE_FQDN}:8443:127.0.0.1" in Path(env["FAKE_CURL_LOG"]).read_text(encoding="utf-8")
    finally:
        kept.stop()


def test_o70_the_registration_token_is_minted_from_the_zone_by_its_declared_name(tmp_path, env):
    """O70: the zone's own call for a provider's token names the zone's declared name, pinned to the loopback of the
    zone's node, and the node's FQDN is nowhere in it."""
    work = node.make_work(Path(env["TMPDIR"]), kind="oz")
    (work / "rootfs" / "root" / "service-ready.lock").touch()
    ca = tmp_path / "ca.pem"
    ca.write_text("ca\n", encoding="utf-8")
    node.put(node.secrets_of(work) / "passphrase", "Pass1234" * 6 + "\n")
    node.put(work / "declared-name", ZONE_NAME + "\n", 0o644)
    Path(env["FAKE_CURL_STATE"]).write_text(
        json.dumps({"creds": ["admin", "Pass1234" * 6], "adminUserId": "a-1", "domain": ZONE_NAME}), encoding="utf-8"
    )
    done = node.script("mint-registration-token.sh", "alpha", "--cacert", str(ca), "--workdir", str(work), env=env)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "minted-registration-token"
    calls = Path(env["FAKE_CURL_LOG"]).read_text(encoding="utf-8")
    assert f"--resolve {ZONE_NAME}:8443:127.0.0.1" in calls
    assert f"https://{ZONE_NAME}:8443/api/v3/onezone/user/tokens/named" in calls
    assert NODE_FQDN not in calls


def test_o70_no_script_that_dials_the_service_takes_the_name_from_the_node_alone():
    """O70: the scripts that call the service they run beside take the name from `service_name`, which reads the work
    directory's declared name and falls back to the node's, never from `node_fqdn` directly."""
    for name in ("finish.sh", "finish-zone.sh", "mint-registration-token.sh", "zone.sbatch"):
        text = (node.BUNDLE / name).read_text(encoding="utf-8")
        assert "service_name" in text, name
        assert not re.search(r"\$\(node_fqdn\)", text), name


# --- the first run's helpers dial the name the service registered under ---

HELPERS = ("bundle/onboard.py", "run/fed_zone.py", "run/fed_provider.py")


def _helper(path):
    """One of the first run's node-side helpers, loaded by its path (they run as scripts, and have no package)."""
    file = node.BUNDLE.parent / path
    loaded = importlib.util.spec_from_file_location("helper_" + file.stem, file)
    module = importlib.util.module_from_spec(loaded)
    loaded.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", HELPERS)
def test_o70_a_helper_dials_the_declared_name_a_service_registered_under(tmp_path, path):
    """O70: onboard.py and the first run's zone and provider helpers verify the service's certificate for the name in
    the work directory's `declared-name` when the service registered under a declared name, and for the node's own
    (`hostname -f`) as before when it did not: the certificate holds the registered name alone."""
    helper = _helper(path)
    work = tmp_path / "hpcop-op.Ab12Cd"
    work.mkdir(mode=0o700)
    helper.subprocess.run = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=NODE_FQDN + "\n", stderr="")
    assert helper.registered_name(str(work)) == NODE_FQDN
    (work / "declared-name").write_text(ALPHA + "\n", encoding="ascii")
    assert helper.registered_name(str(work)) == ALPHA
    (work / "declared-name").write_text("not a name\n", encoding="ascii")
    with pytest.raises(SystemExit):
        helper.registered_name(str(work))
