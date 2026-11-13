# Provenance: new
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/sandbox/sandbox.py:345-470 (the docker run arguments)
#   changes: arguments are built by pure functions from the settings, so the read-only flags can be tested
"""The `docker run` arguments of a sandbox's containers, built by pure functions from the settings.

The data path is read-only at the mount (O05): the agent's root filesystem is read-only, every capability is dropped,
no new privilege is possible, and the oneclient mount is bound into the agent read-only. These functions are where those
flags are written, and a test reads them back. Images are given by resolved id, so a tag that moves after the pin check
cannot change what runs (O13). A secret never appears in an argument, and no token reaches docker at all: each is a
file bound read-only into the one container that holds it, the sidecar's read by its shell into oneclient's environment
(`sidecar_run_args`) and the gate's by the gate (`gate_run_args`), so `docker inspect` shows no token. Docker is given
an environment variable (`-e NAME`, whose value it keeps in the container's configuration) for the agent's settings
alone.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox.api import DataPathMode, Endpoint
from expcore_onedata.sandbox.settings import SandboxSettings
from expcore_onedata.sandbox.teardown import SIDECAR_MOUNT, SIDECAR_SUFFIX

AGENT_OOM_SCORE_ADJ = 1000  # the host's out-of-memory killers take the agent's processes before anything else
GATE_DIR = "/opt/gate"  # where the gate's two files are mounted, read-only, in the gate container
CA_PATH = "/etc/sandbox/ca.pem"  # the run's certificate authority in the agent
# Where the run's own read-only copy of the platform's [tls] CA bundle (`tls.write_private_copy`, never the configured
# file) is bind-mounted read-only for the federation's two clients in containers: the gate's upstream connection and
# oneclient. The agent never gets it: it trusts the run's CA, the gate's, alone.
GATE_CA_PATH = "/etc/gate/federation-ca.pem"
SIDECAR_CA_PATH = "/etc/expcore/federation-ca.pem"
SIDECAR_TOKEN_PATH = "/run/expcore/oneclient-token"  # the mount's token, bound into the sidecar alone, read-only
GATE_TOKEN_PATH = "/etc/gate/token"  # the REST path's token, bound into the gate alone, read-only; gate.json names it
ENV_SSL_CERT_FILE = "SSL_CERT_FILE"  # OpenSSL's setting of the CA file its default verify paths load
ONECLIENT_BIN = "/opt/oneclient/bin/oneclient"  # the published oneclient image's binary
ONECLIENT_LOG_DIR = "/tmp/oneclient/0"  # where it writes its log unless told otherwise
# The published REST client's settings (its environment), not names of any federation.
ENV_ZONE_HOST = "ONEDATA_ONEZONE_HOST"
# The client refuses to start without a token, so a REST agent's holds the configured placeholder (O23), never a
# credential: the gate drops it with any other token the agent sends and adds its own.
ENV_TOKEN = "ONEDATA_TOKEN"
ENV_VERIFY_SSL = "ONEDATA_VERIFY_SSL"
ENV_ACCESS_TOKEN = "ONECLIENT_ACCESS_TOKEN"
# The sidecar's entrypoint: read the token file into oneclient's variable, refuse an empty one, then become `env` with
# the arguments of `sidecar_command`. The value lives in oneclient's own environment, never in docker's configuration.
SIDECAR_START = (
    f'{ENV_ACCESS_TOKEN}="$(cat {SIDECAR_TOKEN_PATH})" && [ -n "${ENV_ACCESS_TOKEN}" ] || exit 97; '
    f'export {ENV_ACCESS_TOKEN}; exec env "$@"'
)
SIDECAR_SHELL_NAME = "oneclient-sidecar"  # the shell's $0, before the arguments it hands to `env`
ENV_CA_BUNDLES = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
DEFAULT_BRIDGE = "bridge"  # docker's default network: the sidecar's and the gate's without [sandbox.federation]
FEDERATION_PORT = 443  # where the gate dials the zone and the provider on [sandbox.federation]'s network
# The published client sends through `requests.Session.send`, which merges no environment setting, so it verifies the
# gate against the certifi bundle of the agent's image and never reads ENV_CA_BUNDLES (O23). On a REST data path the
# sandbox asks the image's interpreter where that bundle is and extends it with the run's CA, as a trust path.
CERTIFI_PROBE = (
    "try:\n"
    "    import certifi\n"
    "except ImportError:\n"
    "    print('certifi:none')\n"
    "else:\n"
    "    print('certifi:' + certifi.where())\n"
)
CERTIFI_NONE = "none"
CERTIFI_PATH = re.compile(r"/[A-Za-z0-9._+/-]{1,4095}")  # an absolute path `-v SOURCE:PATH:ro` can mount over


def certifi_probe_args(s: SandboxSettings, name: str, labels: Sequence[str], image_id: str) -> list[str]:
    """`docker run` of the one-shot container that asks the agent image's interpreter where its certifi bundle is: as
    the agent's code runs (`cells.py`), the same interpreter as the agent's user in /work, the first directory of its
    import path; with no network, removed when it ends, and labelled like every container of the sandbox."""
    uid = s.agent_uid
    return [
        *("run", "--rm", "--name", name, *labels, "--network", "none", "--user", f"{uid}:{uid}", "--workdir", "/work"),
        *("--entrypoint", s.python, image_id, "-c", CERTIFI_PROBE),
    ]


def certifi_bundle(stdout: str) -> str | None:
    """The path of the image's certifi bundle from the probe's output, or None when the image has no certifi. Any other
    output, more than the one line or a path the agent's mount could not be put on, raises `ValueError`: no answer."""
    lines = stdout.splitlines()
    if len(lines) != 1 or not lines[0].startswith("certifi:"):
        raise ValueError("the probe did not answer with one line")
    found = lines[0].removeprefix("certifi:")
    if found == CERTIFI_NONE:
        return None
    if not CERTIFI_PATH.fullmatch(found) or {".", ".."} & set(found.split("/")):
        raise ValueError("the probe's path is not one absolute path without dot segments")
    return found


def trust_targets(declared: Sequence[str], certifi: str | None) -> tuple[str, ...]:
    """The paths in the agent image whose CA bundles get the run's CA: the declared `trust_paths`, then the certifi
    bundle, once, when the image has one."""
    return (*declared, *([certifi] if certifi is not None and certifi not in declared else []))


@dataclass(frozen=True)
class Names:
    """The names of a sandbox's containers and network, all derived from the prefix and the sandbox id."""

    sid: str
    agent: str
    net: str
    gate: str
    sidecar: str

    @classmethod
    def make(cls, prefix: str, sid: str) -> Names:
        agent = f"{prefix}-{sid}"
        return cls(sid, agent, agent + "-net", agent + "-gate", agent + SIDECAR_SUFFIX)


def sidecar_command(provider_host: str, settings: SandboxSettings) -> list[str]:
    """The arguments the sidecar's shell hands to `env` once it has read the token file (`sidecar_run_args`).

    The published image sets an environment flag that makes the client skip TLS verification, so the default entrypoint
    would run unverified whatever is passed. `env -u` removes the flag, and the platform's `[tls]` alone decides the
    check: by default oneclient verifies against the image's CAs; with `ca_file`, `env` sets `SSL_CERT_FILE` to the
    run's copy that `sidecar_run_args` mounts read-only, the CA file OpenSSL's default verify paths load (oneclient has
    no option of its own for a CA, and `tests/live/test_sandbox.py` checks the image honours it); `--insecure` is passed
    only with `insecure_tls`. `-f` keeps the client in the foreground. The mount is read-only (`-o ro`).
    """
    mount = settings.mount
    if mount is None:
        raise ValueError("a mount data path needs [sandbox.mount]")
    io = {"direct": ["--force-direct-io"], "proxy": ["--force-proxy-io"]}.get(mount.io_mode or "", [])
    trust = [f"{ENV_SSL_CERT_FILE}={SIDECAR_CA_PATH}"] if settings.tls.ca_file is not None else []
    return [
        *("-u", "ONECLIENT_INSECURE", *trust, ONECLIENT_BIN, "-f", "-H", provider_host),
        *("-o", "ro,allow_other", *io),
        *(["--insecure"] if settings.tls.insecure else []),
        *mount.extra_args,
        SIDECAR_MOUNT,
    ]


def sidecar_route(settings: SandboxSettings, provider_host: str) -> tuple[str, str]:
    """The network the sidecar starts on and the provider host it dials there (O03).

    With `[sandbox.federation]`, the federation's own network and the local provider's name on it: a federation whose
    providers the host reaches at addresses no container can use (a Compose project's ports on the host's loopback) is
    reached there, by the name its certificate holds. Without it, docker's default bridge and the host of the local
    provider's URL, which a federation with public addresses needs. The agent joins neither (`agent_run_args`)."""
    if settings.federation is None:
        return DEFAULT_BRIDGE, provider_host
    return settings.federation.network, settings.federation.local_provider


def gate_network(settings: SandboxSettings) -> str:
    """The network the REST data path's gate starts on, before it is connected to the sandbox's internal one: the
    federation's own with `[sandbox.federation]`, as the sidecar's (`sidecar_route`), else docker's default bridge."""
    return DEFAULT_BRIDGE if settings.federation is None else settings.federation.network


def gate_upstreams(settings: SandboxSettings, zone: Endpoint, provider: Endpoint) -> dict[str, dict[str, Any]]:
    """The hosts the gate serves and dials, by the name the agent resolves to the gate (O03, O05): the zone and the
    agent's pinned local provider, nothing else.

    Each name is also the one the gate's certificate for it is issued for, the one it matches a request's `Host` and
    SNI against, and, as `addr`, the one it dials upstream, verifying the certificate there as the platform's `[tls]`
    says. With `[sandbox.federation]` these are the zone's and the local provider's names on the federation's network,
    on its port 443 (#203): the URLs of `[zone]` and `[providers]` may be the host's published loopback ports, which a
    container reaches only itself at. Without it they are the hosts and ports of those URLs, as always."""
    federation = settings.federation
    if federation is None:
        named = (
            ("onezone", zone.host, zone.port, zone.tls),
            ("oneprovider", provider.host, provider.port, provider.tls),
        )
    elif federation.zone is None:
        raise ValueError("a REST data path on [sandbox.federation] needs the zone's name on the federation's network")
    else:
        named = (
            ("onezone", federation.zone, FEDERATION_PORT, True),
            ("oneprovider", federation.local_provider, FEDERATION_PORT, True),
        )
    return {name: {"role": role, "addr": name, "port": port, "tls": tls} for role, name, port, tls in named}


def effective_log_dir(args: Sequence[str], default: str = ONECLIENT_LOG_DIR) -> str:
    """The log directory oneclient was started with: the last --log-dir / -l in its arguments, else the default."""
    out = default
    for i, a in enumerate(args):
        if a in ("--log-dir", "-l") and i + 1 < len(args):
            out = args[i + 1]
        elif a.startswith("--log-dir="):
            out = a.split("=", 1)[1]
    return out


def _ca_mount(s: SandboxSettings, ca_copy: Path | None, destination: str) -> tuple[str, ...]:
    """The read-only mount of the run's copy of the CA bundle, which the platform's `ca_file` requires; none without."""
    if s.tls.ca_file is None:
        return ()
    if ca_copy is None:
        raise ValueError("the platform's ca_file needs the run's own copy of the verified bundle (ca_copy)")
    return ("-v", f"{ca_copy}:{destination}:ro")


def gate_run_args(
    s: SandboxSettings,
    names: Names,
    labels: Sequence[str],
    sdir: Path,
    gate_files: Path,
    image_id: str,
    *,
    token_file: Path,
    ca_copy: Path | None = None,
) -> list[str]:
    """`docker run` for the gate: read-only root, one capability (to bind its port), its files mounted read-only.

    It starts on `gate_network`'s network, the federation's own with `[sandbox.federation]`, and the sandbox then
    connects it to the sandbox's internal network, where the agent reaches it; the agent never joins the federation's.
    It runs as root with every other capability dropped, so it reads only files root owns or anyone may read: the run
    gives it its TLS keys and its token file as root's, owner-only (`DockerSandbox._prepare_gate`, `_write_token_file`).

    The gate holds the data-path token and adds it to the requests it forwards (O05). The token never reaches docker:
    `token_file`, the run's file of it, is bound read-only at `GATE_TOKEN_PATH` in this container alone, and the gate
    reads it at its start (its configuration names the path). An environment variable given to `docker run` would be
    kept in the container's configuration, where `docker inspect` shows it. With the platform's `ca_file`, `ca_copy`,
    the run's own copy of the verified bundle, is mounted read-only at `GATE_CA_PATH`, which the gate's configuration
    names as its upstream CA file."""
    return [
        *("run", "-d", "--name", names.gate, "--network", gate_network(s), *labels),
        *("--cap-drop", "ALL", "--cap-add", "NET_BIND_SERVICE", "--security-opt", "no-new-privileges"),
        *("--read-only", "--user", "0", "--pids-limit", "512", "--memory", str(s.limits.gate_memory_bytes)),
        *("-v", f"{sdir / 'tls'}:/etc/gate/tls:ro", "-v", f"{sdir / 'gate.json'}:/etc/gate/gate.json:ro"),
        *_ca_mount(s, ca_copy, GATE_CA_PATH),
        *("-v", f"{token_file}:{GATE_TOKEN_PATH}:ro"),
        *("-v", f"{gate_files}:{GATE_DIR}:ro", "-v", f"{sdir / 'gatelog'}:/var/gate"),
        *("--entrypoint", "python", image_id, f"{GATE_DIR}/gate.py", "/etc/gate/gate.json"),
    ]


def sidecar_run_args(
    s: SandboxSettings,
    names: Names,
    labels: Sequence[str],
    share: Path,
    image_id: str,
    provider_host: str,
    *,
    token_file: Path,
    ca_copy: Path | None = None,
) -> list[str]:
    """`docker run` for the mount sidecar: it owns the mount and the token; the agent gets neither, nor FUSE. With the
    platform's `ca_file`, `ca_copy`, the run's own copy of the verified bundle, is mounted read-only at
    `SIDECAR_CA_PATH` (see `sidecar_command`). Its network and the host it dials are `sidecar_route`'s: the
    federation's own network when `[sandbox.federation]` names one, and never a second network beside it.

    The token never reaches docker (O05): `token_file`, the run's file of it, is bound read-only at
    `SIDECAR_TOKEN_PATH` in this container alone, and the entrypoint, `sh -c SIDECAR_START`, reads it into oneclient's
    environment before it becomes `env` with `sidecar_command`'s arguments. An environment variable given to `docker
    run` (`-e NAME`) would be kept in the container's configuration, where `docker inspect` shows it."""
    lim = s.limits
    network, dialled = sidecar_route(s, provider_host)
    return [
        *("run", "-d", "--name", names.sidecar, "--network", network, *labels),
        *("--cpus", str(lim.cpus), "--memory", str(lim.memory_bytes), "--memory-swap", str(lim.memory_bytes)),
        *("--pids-limit", str(lim.pids), "--cap-add", "SYS_ADMIN", "--device", "/dev/fuse"),
        *("--security-opt", "apparmor:unconfined", "--tmpfs", "/root/.local:size=1g"),
        *_ca_mount(s, ca_copy, SIDECAR_CA_PATH),
        *("-v", f"{token_file}:{SIDECAR_TOKEN_PATH}:ro"),
        *("--mount", f"type=bind,src={share},dst={SIDECAR_MOUNT},bind-propagation=rshared"),
        *("--entrypoint", "sh", image_id, "-c", SIDECAR_START, SIDECAR_SHELL_NAME, *sidecar_command(dialled, s)),
    ]


def agent_run_args(
    s: SandboxSettings,
    names: Names,
    labels: Sequence[str],
    *,
    image_id: str,
    mode: DataPathMode,
    work: Path,
    share: Path | None,
    gate_ip: str | None,
    gate_hosts: Sequence[str],
    ca: Path | None,
    trust_bundles: Mapping[str, Path],
    zone_host: str | None,
    inputs: Sequence[tuple[Path, str]] = (),
) -> tuple[list[str], dict[str, str]]:
    """`docker run` for the agent and the environment names it takes: read-only root, no capability, no new privilege.

    In REST mode the agent resolves the zone and the provider to the gate and trusts the run's CA. It holds no token
    and no other credential, in its environment or in a file: the gate adds the token to the requests it forwards, and
    the sidecar owns the mount's (O05). Its `ONEDATA_TOKEN` is the configured placeholder, which only lets the published
    client start (O23); the gate drops it. In mount mode the read-only mount is bound in with `readonly`. With no data
    path it has neither. Each read-only input, the run's own copy and the path the agent sees it at, is bound in with
    `readonly` too, in every data path (O20).
    """
    lim, uid = s.limits, s.agent_uid
    args = [
        *("run", "-d", "--init", "--oom-score-adj", str(AGENT_OOM_SCORE_ADJ), "--name", names.agent),
        *("--network", names.net, *labels, "--user", f"{uid}:{uid}", "--read-only"),
        *("--cap-drop", "ALL", "--security-opt", "no-new-privileges"),
        *("--cpus", str(lim.cpus), "--memory", str(lim.memory_bytes), "--memory-swap", str(lim.memory_bytes)),
        *("--pids-limit", str(lim.pids), "--ulimit", f"nofile={lim.nofile}:{lim.nofile}"),
        *("--tmpfs", f"/tmp:size={lim.tmp_bytes},mode=1777", "--shm-size", str(lim.shm_bytes)),
        *("-v", f"{work}:/work"),
    ]
    env: dict[str, str] = {}
    if mode is DataPathMode.REST:
        if not (gate_ip and ca and zone_host):
            raise ValueError("a REST data path needs the gate's address, the run's CA and the zone's host")
        args += ["-v", f"{ca}:{CA_PATH}:ro"]
        for path, bundle in trust_bundles.items():
            args += ["-v", f"{bundle}:{path}:ro"]
        for name in gate_hosts:
            args += ["--add-host", f"{name}:{gate_ip}"]
        env = dict.fromkeys(ENV_CA_BUNDLES, CA_PATH) | {ENV_ZONE_HOST: zone_host, ENV_VERIFY_SSL: "true"}
        env[ENV_TOKEN] = s.placeholder_token  # not a credential: the client's start needs a value (O23)
    elif mode is DataPathMode.MOUNT:
        if share is None or s.mount is None:
            raise ValueError("a mount data path needs the shared mount point and [sandbox.mount]")
        args += ["--mount", f"type=bind,src={share},dst={s.mount.path},readonly,bind-propagation=rslave"]
    for copy, target in inputs:
        args += ["--mount", f"type=bind,src={copy},dst={target},readonly"]
    # settings and the placeholder, never a credential; their values travel in the docker client's environment
    for name in env:
        args += ["-e", name]
    return [*args, image_id, "sleep", "infinity"], env
