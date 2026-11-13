"""Sandbox tests against a real docker daemon, skipped unless EXPCORE_LIVE_DOCKER=1.

    EXPCORE_LIVE_DOCKER=1 uv run pytest tests/live/test_sandbox.py
    EXPCORE_LIVE_DOCKER=1 EXPCORE_LIVE_PLATFORM=/path/to/platform.toml uv run pytest tests/live/test_sandbox.py

The first checks need only a docker daemon. The ones that start a sandbox need `EXPCORE_LIVE_PLATFORM` as well: its
`[sandbox]` table names the images, the address pool, the work directory and the levels of the data path, and the
data-path levels mint a read-only token for a fixed user (`[principals.data_path]`). They start a sandbox for each
configured level, run a line of code in it, prove it refuses a write, stop it, and require that the lease key's
probe then answers True. Nothing here writes to a space; without the variables nothing is constructed or contacted.
With a platform file, a read-only input is started too: the agent runs and reads its script, every change to it fails
with EROFS, and /work stays writable.

The TLS checks need a platform file whose `[tls]` is not the default, because its federation's certificate chains to a
CA no default trusts (a self-signed one): with each of the three modes in turn, oneclient must fail to mount by default
and mount with `ca_file` or `insecure_tls`, and the gate's upstream must refuse the zone by default and reach it with
either. With `insecure_tls` in the file, the `ca_file` cases skip.

The listing and byte-count checks (O21, O22) need a level of `[sandbox.data_paths]` whose listing is complete, and
`EXPCORE_LIVE_LISTING_PATH` naming a provisioned directory of more than 1000 entries by its path in the space: through
the gate the agent's one listing of it must come back whole as the last page, and the collection's count must equal the
bytes the agent's code received. A mount level's count must be unknown after its collection.

The published-client check (O23) needs a REST level and an agent image with `onedatarestfsspec` installed (it skips
without): with only the placeholder token and the default trust, the client must list the directory of
`[sandbox] content_check_file` and read that file whole through the gate.
"""

import dataclasses
import json
import os
import time

import pytest

if os.environ.get("EXPCORE_LIVE_DOCKER") != "1":
    pytest.skip("EXPCORE_LIVE_DOCKER is not set; these tests use a real docker daemon", allow_module_level=True)

from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxError, SandboxSpec
from expcore_onedata.sandbox.hostops import DockerHost
from expcore_onedata.sandbox.provider import SandboxProbe
from expcore_onedata.sandbox.sandbox import DockerSandbox

PLATFORM = os.environ.get("EXPCORE_LIVE_PLATFORM")
CEILING_S = 300.0
needs_platform = pytest.mark.skipif(not PLATFORM, reason="EXPCORE_LIVE_PLATFORM is not set")


def test_docker_answers_and_the_host_facts_hold_no_name():
    facts = DockerHost().host_facts()
    assert facts["docker_server"]
    assert set(facts) == {"kernel", "docker_server"}


@pytest.fixture(scope="module")
def config():
    from expcore_onedata.config import load_platform

    loaded = load_platform(PLATFORM)
    if loaded.sandbox is None:
        pytest.skip("the platform file has no [sandbox] table")
    return loaded


@pytest.fixture(scope="module")
def host(config):
    return DockerHost(sudo_command=config.sandbox.sudo)


def configured_levels() -> list[str]:
    """The levels of the platform file's `[sandbox.data_paths]`, read at collection so each one is a test of its own."""
    if not PLATFORM:
        return []
    from expcore_onedata.config import load_platform

    loaded = load_platform(PLATFORM, require_secrets=False)
    return sorted(loaded.sandbox.data_paths) if loaded.sandbox is not None else []


LEVELS = configured_levels()


def levels(config):
    return sorted(config.sandbox.data_paths)


def spec_for(config, level, run_key, lease_key=None):
    from expcore_onedata.client import OnezoneClient
    from expcore_onedata.tokens import TokenIssuer, data_path_spec

    mode = config.sandbox.mode_of(level)
    if mode is DataPathMode.NONE:
        return SandboxSpec(run_key=run_key, mode=mode, lease_key=lease_key)
    principal = config.principals.data_path
    if principal is None:
        pytest.skip("[principals.data_path] is needed to mint a data-path token")
    client = OnezoneClient(config)
    try:
        token = TokenIssuer(client, config).mint(
            data_path_spec(config.space.id, mode.interface or "rest"), principal, ceiling_s=CEILING_S
        )
    finally:
        client.close()
    return SandboxSpec(
        run_key=run_key,
        mode=mode,
        token=token,
        zone=Endpoint.from_url(config.zone.url),
        provider=Endpoint.from_url(config.local_provider.url),
        lease_key=lease_key,
        listing=config.sandbox.listing_of(level) or "paged",
    )


@needs_platform
@pytest.mark.parametrize("level", LEVELS)
def test_each_configured_data_path_starts_runs_code_refuses_a_write_and_is_gone_after_stop(config, host, level):
    """C19: one test per level of the data path, so a level that cannot run here (no principal to mint its token)
    skips itself and never the others; each proves its own teardown with the lease key's probe."""
    probe = SandboxProbe(host, config.sandbox)
    key = "live-check"  # a label for the probe; no lease is taken
    sandbox = DockerSandbox.start(host, config.sandbox, spec_for(config, level, f"live-{int(time.time())}", key))
    try:
        assert sandbox.metadata()["read_only_verified"]["root"] is True
        result = sandbox.exec("print(6 * 7)", 60)
        assert result.stdout.strip() == "42"
        assert result.kill is None
    finally:
        sandbox.stop()
    assert sandbox.stopped
    assert probe.resource_gone(key, {}) is True


@needs_platform
def test_a_write_to_the_agents_root_is_refused_in_a_started_sandbox(config, host):
    level = levels(config)[0]
    sandbox = DockerSandbox.start(host, config.sandbox, spec_for(config, level, f"live-ro-{int(time.time())}"))
    try:
        result = sandbox.exec(
            "import errno\ntry:\n    open('/probe', 'w')\nexcept OSError as e:\n    print(e.errno == errno.EROFS)", 60
        )
        assert result.stdout.strip() == "True"
    finally:
        sandbox.stop()


NO_CREDENTIAL = """
import os, pathlib
needle = {needle!r}.encode()
placeholder = {placeholder!r}  # a REST agent's ONEDATA_TOKEN holds the placeholder, which is no credential (O23)
hits = []
for name, value in os.environ.items():
    if (name, value) == placeholder:
        continue
    if any(w in name.upper() for w in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")):
        hits.append("an environment variable named like a credential")
for pid in filter(str.isdigit, os.listdir("/proc")):
    for what in ("environ", "cmdline"):
        try:
            data = pathlib.Path("/proc/" + pid + "/" + what).read_bytes()
        except OSError:
            continue
        if needle in data:
            hits.append("the " + what + " of a process")
for top in ("/work", "/tmp", "/etc", "/home", "/root", "/run", "/var", "/opt", "/srv", "/mnt"):
    for here, _, files in os.walk(top, onerror=lambda e: None):
        for name in files:
            try:
                path = pathlib.Path(here, name)
                if path.stat().st_size < (1 << 22) and needle in path.read_bytes():
                    hits.append("a file")
            except OSError:
                continue
print("clean" if not hits else sorted(set(hits)))
"""


@needs_platform
@pytest.mark.parametrize("level", LEVELS)
def test_no_credential_is_visible_inside_the_agent_on_the_data_path_of_this_level(config, host, level):
    """O05: the agent's code cannot read the data-path token: not in its environment, its files or its processes. The
    one variable named like a token it may hold is a REST agent's ONEDATA_TOKEN, and only with the placeholder (O23)."""
    from expcore_onedata.sandbox.containers import ENV_TOKEN

    spec = spec_for(config, level, f"live-cred-{int(time.time())}")
    sandbox = DockerSandbox.start(host, config.sandbox, spec)
    try:
        needle = spec.token.reveal() if spec.token is not None else "no-token-for-this-level"
        placeholder = (ENV_TOKEN, config.sandbox.placeholder_token) if spec.mode is DataPathMode.REST else None
        result = sandbox.exec(NO_CREDENTIAL.format(needle=needle, placeholder=placeholder), 120)
        assert result.stdout.strip() == "clean"
    finally:
        sandbox.stop()


@needs_platform
@pytest.mark.parametrize("level", LEVELS)
def test_o05_docker_inspect_of_every_container_of_a_running_sandbox_shows_no_token(config, host, level):
    """O05: anyone with access to the docker daemon reads a container's configuration with `docker inspect`; on every
    container of a running sandbox of this level (the agent, and the gate or the sidecar its data path starts) it shows
    no token: each holder takes its token from a file bound into it alone (issue 137)."""
    spec = spec_for(config, level, f"live-inspect-{int(time.time())}")
    sandbox = DockerSandbox.start(host, config.sandbox, spec)
    try:
        names = (sandbox.names.agent, sandbox.names.gate, sandbox.names.sidecar)
        shown = {name: host.docker("inspect", name, check=False, timeout=30) for name in names}
        running = [name for name, answer in shown.items() if answer.returncode == 0]
        assert sandbox.names.agent in running
        secret = spec.token.reveal() if spec.token is not None else None
        assert secret is None or not [name for name in running if secret in shown[name].stdout]
    finally:
        sandbox.stop()


# The agent's read of the zone through the gate: a call the gate passes, whose answer's status says whether the gate's
# upstream connection was made (any answer of the federation) or refused (the gate's 502).
ZONE_CALL = """
import os, urllib.error, urllib.request
url = "https://" + os.environ["ONEDATA_ONEZONE_HOST"] + "/api/v3/onezone/tokens/infer_access_token_scope"
request = urllib.request.Request(url, data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
try:
    print(urllib.request.urlopen(request, timeout=60).status)
except urllib.error.HTTPError as error:
    print(error.code)
"""
TLS_MODES = ("verify-default", "ca_file", "insecure_tls", "ca_file-of-another-ca")
REFUSED_MODES = ("verify-default", "ca_file-of-another-ca")  # a ca_file of a CA the federation does not chain to fails


def tls_for(config, label, tmp_path):
    """The TLS setting of one mode, against the federation of the platform file. The file must declare a mode other
    than the default, which says its certificate is one no default trusts, so the default can be shown to fail. The
    unrelated CA is made here (it needs the openssl command)."""
    import hashlib
    import shutil

    from tls_support import make_federation_ca

    from expcore_onedata.tls import TlsSettings

    if config.tls.mode == "verify":
        pytest.skip("the platform file's [tls] is the default: its federation's certificate is trusted as it is")
    if label == "ca_file" and config.tls.ca_file is None:
        pytest.skip("the platform file's [tls] names no ca_file")
    if label == "ca_file-of-another-ca":
        if shutil.which("openssl") is None:
            pytest.skip("the openssl command makes the unrelated CA")
        other = make_federation_ca(tmp_path / "unrelated", name="unrelated-test-ca").ca
        return TlsSettings(ca_file=other, ca_sha256=hashlib.sha256(other.read_bytes()).hexdigest())
    return {"verify-default": TlsSettings(), "ca_file": config.tls, "insecure_tls": TlsSettings(insecure=True)}[label]


def level_of(config, mode):
    """A configured level of the data path that is this mode, or a skip."""
    found = [level for level in levels(config) if config.sandbox.mode_of(level) is mode]
    if not found:
        pytest.skip(f"[sandbox.data_paths] maps no level to {mode.value}")
    return found[0]


@needs_platform
@pytest.mark.parametrize("label", TLS_MODES)
def test_o05_oneclient_in_the_sidecar_mounts_as_the_tls_setting_says(config, host, label, tmp_path):
    """O05: against the federation of the platform file, whose certificate no default trusts, the sidecar's oneclient
    fails to mount by default and with a ca_file of an unrelated CA, and mounts with the federation's CA bundle (the
    run's read-only copy, named by SSL_CERT_FILE) or with insecure_tls."""
    settings = dataclasses.replace(config.sandbox, tls=tls_for(config, label, tmp_path))
    level = level_of(config, DataPathMode.MOUNT)
    spec = spec_for(config, level, f"live-tls-mount-{int(time.time())}")
    if label in REFUSED_MODES:
        with pytest.raises(SandboxError):
            DockerSandbox.start(host, settings, spec)
        return
    sandbox = DockerSandbox.start(host, settings, spec)
    try:
        assert sandbox.metadata()["read_only_verified"] == {"root": True, "mount": True}
        assert sandbox.metadata()["tls"] == settings.tls.record()
    finally:
        sandbox.stop()


@needs_platform
@pytest.mark.parametrize("label", TLS_MODES)
def test_o05_the_gates_upstream_reaches_the_zone_as_the_tls_setting_says(config, host, label, tmp_path):
    """O05: against the federation of the platform file, whose certificate no default trusts, the gate refuses the
    zone's certificate by default and with a ca_file of an unrelated CA (the agent's call is answered 502), and reaches
    the zone with the federation's CA bundle (the run's read-only copy) or with insecure_tls."""
    settings = dataclasses.replace(config.sandbox, tls=tls_for(config, label, tmp_path))
    level = level_of(config, DataPathMode.REST)
    sandbox = DockerSandbox.start(host, settings, spec_for(config, level, f"live-tls-rest-{int(time.time())}"))
    try:
        status = int(sandbox.exec(ZONE_CALL, 120).stdout.strip())
    finally:
        sandbox.stop()
    if label in REFUSED_MODES:
        assert status == 502
    else:
        assert status < 500


LISTING_PATH = os.environ.get("EXPCORE_LIVE_LISTING_PATH")  # a provisioned directory of more than 1000 entries
# The agent's own read of a directory through the gate, as the official client lists: one request for a page of 1000.
LISTING_CALL = """
import json, urllib.request
base = "https://" + {provider!r} + "/api/v3/oneprovider"
lookup = urllib.request.Request(base + "/lookup-file-id/" + {space!r} + "/" + {path!r}, data=b"", method="POST")
found = urllib.request.urlopen(lookup, timeout=120).read()
listing = urllib.request.urlopen(base + "/data/" + json.loads(found)["fileId"] + "/children?limit=1000", timeout=600)
body = listing.read()
page = json.loads(body)
names = [entry.get("name") for entry in page["children"]]
print(json.dumps({{"lookup": len(found), "listing": len(body), "entries": len(names), "unique": len(set(names)),
                  "last": page["isLast"], "next": page["nextPageToken"]}}))
"""

# What the agent's code does with a read-only input in a real container: runs its script, then tries every change.
INPUT_USE = """
import errno, json, os, subprocess
script = os.path.join({target!r}, "analyse.sh")
said = {{"ran": subprocess.run([script], capture_output=True, text=True, check=False).stdout.strip()}}


def attempt(what, act):
    try:
        act()
    except OSError as error:
        said[what] = errno.errorcode.get(error.errno, error.errno)
    else:
        said[what] = "done"


def create(path):
    with open(path, "x") as handle:
        handle.write("written")


attempt("read", lambda: open(script, "rb").read())
attempt("write", lambda: open(script, "w").close())
attempt("beside", lambda: create(os.path.join({target!r}, "beside.txt")))
attempt("rename", lambda: os.rename(script, script + ".moved"))
attempt("chmod", lambda: os.chmod(script, 0o777))
attempt("delete", lambda: os.unlink(script))
attempt("work", lambda: create("/work/written-by-the-agent.txt"))
print(json.dumps(said, sort_keys=True))
"""


@needs_platform
def test_o21_o22_a_complete_level_serves_a_large_directory_whole_and_counts_the_bytes_it_served(config, host, tmp_path):
    """O21, O22: through the gate of a level whose listing is complete, one listing of a directory of more than 1000
    entries comes back whole as the last page, with each name once; after the collection stopped the gate, its count
    is known and equals the bytes the agent's code received: the listing and the lookup, and no file content. Its
    tables list the one directory, with the listing's bytes, and no file."""
    import json

    if not LISTING_PATH:
        pytest.skip("EXPCORE_LIVE_LISTING_PATH is not set; it names a directory of more than 1000 entries in the space")
    found = [level for level in levels(config) if config.sandbox.listing_of(level) == "complete"]
    if not found:
        pytest.skip("[sandbox.data_paths] gives no level a complete listing")
    spec = spec_for(config, found[0], f"live-listing-{int(time.time())}")
    sandbox = DockerSandbox.start(host, config.sandbox, spec)
    try:
        code = LISTING_CALL.format(provider=spec.provider.host, space=config.space.name, path=LISTING_PATH.strip("/"))
        said = json.loads(sandbox.exec(code, 900).stdout.strip())
        sandbox.collect(tmp_path / "evidence")
        counted = sandbox.served_bytes()
    finally:
        sandbox.stop()
    assert said["entries"] > 1000
    assert said["unique"] == said["entries"]
    assert (said["last"], said["next"]) == (True, None)
    assert counted["unknown"] == []
    assert (counted["listing"], counted["listing_bytes"], counted["other_bytes"]) == (
        "complete",
        said["listing"],
        said["lookup"],
    )
    assert counted["content_bytes"] == 0
    assert (counted["files"], counted["files_total"]) == ({}, 0)  # no file's content was read
    assert [row["body_bytes"] for row in counted["listings"].values()] == [said["listing"]]  # one directory listed


@needs_platform
def test_o23_the_published_client_lists_and_reads_through_the_gate_with_the_placeholder(config, host):
    """O23: in a REST level's agent, the published client (`onedatarestfsspec`, which the agent image must have; the
    test skips otherwise) starts with the placeholder token, verifies the gate against the image's certifi bundle that
    the sandbox extended with the run's CA, lists the directory of `[sandbox] content_check_file` and reads that file
    whole, as many bytes as its size. The code it sends is the one the in-process test runs
    (`tests/onedata/sandbox/test_published_client.py`)."""
    import json

    from sandbox_support import PUBLISHED_CLIENT

    level = level_of(config, DataPathMode.REST)
    check = config.sandbox.content_check_file
    sandbox = DockerSandbox.start(host, config.sandbox, spec_for(config, level, f"live-client-{int(time.time())}"))
    try:
        code = PUBLISHED_CLIENT.format(space=config.space.name, path=check)
        said = json.loads(sandbox.exec(code, 300).stdout.strip())
        certifi = sandbox.metadata()["certifi"]
    finally:
        sandbox.stop()
    if said["client"] is None:
        pytest.skip("the agent image does not have the published client, onedatarestfsspec")
    assert certifi == "run_ca_added"
    assert said["token"] == config.sandbox.placeholder_token
    assert f"{config.space.name}/{check}" in said["listing"]
    assert said["read"] == said["size"] > 0


@needs_platform
def test_o22_a_mount_levels_count_is_unknown_after_its_collection(config, host, tmp_path):
    """O22: a mount's reads go through oneclient, which no gate sees: its count is unknown, never zero."""
    level = level_of(config, DataPathMode.MOUNT)
    sandbox = DockerSandbox.start(host, config.sandbox, spec_for(config, level, f"live-mount-count-{int(time.time())}"))
    try:
        sandbox.exec(f"import os\nprint(len(os.listdir({config.sandbox.mount.path!r})))", 120)
        sandbox.collect(tmp_path / "evidence")
        counted = sandbox.served_bytes()
    finally:
        sandbox.stop()
    assert counted["unknown"] == ["mount_not_through_gate"]
    assert counted["served_bytes"] is None
    assert counted["files"] is None


@needs_platform
def test_o20_a_read_only_input_is_read_and_run_but_never_changed_and_work_stays_writable(config, host, tmp_path):
    """O20: in a real container the agent runs the input's script and reads it, while writing, renaming, chmod-ing and
    deleting it and creating a file beside it each fail with EROFS, and its own working directory stays writable; the
    start's own probe proved as much, and the metadata names the input by its hash."""
    from expcore_onedata.sandbox.api import ReadOnlyInput

    supplied = tmp_path / "supplied"
    supplied.mkdir()
    (supplied / "analyse.sh").write_text("#!/bin/sh\necho analysed\n", encoding="utf-8")
    (supplied / "analyse.sh").chmod(0o755)
    target = "/opt/supplied"
    spec = spec_for(config, levels(config)[0], f"live-input-{int(time.time())}")
    spec = dataclasses.replace(spec, inputs=(ReadOnlyInput(supplied, target),))
    sandbox = DockerSandbox.start(host, config.sandbox, spec)
    try:
        meta = sandbox.metadata()
        assert meta["read_only_verified"]["inputs"] is True
        assert [recorded["path"] for recorded in meta["read_only_inputs"]] == [target]
        said = json.loads(sandbox.exec(INPUT_USE.format(target=target), 60).stdout)
    finally:
        sandbox.stop()
    refused = dict.fromkeys(("write", "beside", "rename", "chmod", "delete"), "EROFS")
    assert said == {"ran": "analysed", "read": "done", **refused, "work": "done"}


@needs_platform
def test_o39_a_harness_step_the_containers_timeout_killed_is_timed_out_and_a_step_that_kills_itself_is_not(
    config, host
):
    """O39: in a real container a harness step (`record=False`) that sleeps past its limit is ended by the container's
    own `timeout -s KILL` and reads as `timed_out` with the `container_timeout` bound, and one that kills itself at once
    exits 137 too but is no timeout. The fake host cannot show which exit code the real `timeout` gives or how long
    `docker exec` takes to start; this does."""
    sandbox = DockerSandbox.start(
        host, config.sandbox, spec_for(config, levels(config)[0], f"live-bound-{int(time.time())}")
    )
    try:
        itself = sandbox.exec("import os\nos.kill(os.getpid(), 9)", 30, record=False)
        hung = sandbox.exec("import time\ntime.sleep(3600)", 2.0, record=False)
        steps = sandbox.metadata()["harness_steps"]
    finally:
        sandbox.stop()
    assert (itself.exit_code, itself.timed_out, itself.bound) == (137, False, None)
    assert hung.exit_code in (137, 124)
    assert (hung.timed_out, hung.bound) == (True, "container_timeout")
    assert [(step["timed_out"], step["bound"]) for step in steps] == [(False, None), (True, "container_timeout")]
