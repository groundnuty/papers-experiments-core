"""The Compose file of a declared layout, generated, never written by hand.

    uv run python deploy/compose-federation/federation.py --layout PATH compose

Every command of federation.py generates the file from its layout and hands it to `docker compose` with this directory
as the project directory, so `./start.sh` resolves here. What keeps the federation to itself is part of the generator,
not of any layout: one project name, only the two pinned images and never a pull, every published port on the host's
loopback, every bind source below the root (or `./start.sh`) and never created by the daemon, no privileged container,
and a label on every container naming the root it binds. `compose.yaml` beside this file is the default layout's
output, committed for review; a test keeps it equal to what the generator makes.
"""

import json
import re
from typing import Any

from fedlayout import IMAGES, NODE, ROOT_MARKER
from fedspec import LOOPBACK, ZONE, Service, Spec

ROOT = "${EXPCORE_FED_ROOT:?set by federation.py}"  # no default: a file run by hand binds no root by accident
ROOT_LABEL = "org.expcore.fed.root"  # every container names the root it binds: commands refuse another root's
ZONE_IMAGE, PROVIDER_IMAGE = IMAGES
SAFE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*")


def bind(source: str, target: str, *, read_only: bool = False) -> dict[str, Any]:
    """A bind mount whose source the daemon never creates: a missing source is an error."""
    mount: dict[str, Any] = {"type": "bind", "source": source, "target": target}
    if read_only:
        mount["read_only"] = True
    mount["bind"] = {"create_host_path": False}
    return mount


def cluster(domain: str) -> str:
    """The cluster section both panels read: one node, which is manager, worker and database."""
    return (
        "cluster:\n"
        f'  domainName: "{domain}"\n'
        "  nodes:\n"
        "    n1:\n"
        f'      hostname: "{NODE}"\n'
        "  managers:\n"
        '    mainNode: "n1"\n'
        '    nodes: ["n1"]\n'
        "  workers:\n"
        '    nodes: ["n1"]\n'
        "  databases:\n"
        "    serverQuota: 256\n"
        "    bucketQuota: 256\n"
        '    nodes: ["n1"]\n'
    )


def zone_config(spec: Spec) -> str:
    return (
        cluster(spec.zone.domain)
        + "onezone:\n"
        + f'  name: "{spec.project}-zone"\n'
        + f'  domainName: "{spec.zone.domain}"\n'
        + "  letsEncryptEnabled: false\n"
    )


def provider_config(spec: Spec, service: Service) -> str:
    return (
        cluster(service.domain)
        + "  storages:\n"
        + "    posix:\n"
        + '      type: "posix"\n'
        + '      mountPoint: "/volumes/storage"\n'
        + "oneprovider:\n"
        + "  geoLatitude: 0.0\n"
        + "  geoLongitude: 0.0\n"
        + "  register: true\n"
        + f'  name: "{service.name}"\n'
        + f'  adminEmail: "admin@{service.domain}"\n'
        + '  tokenProvisionMethod: "fromFile"\n'
        + '  tokenFile: "/expcore/registration-token"\n'
        + "  letsEncryptEnabled: false\n"
        + "  subdomainDelegation: false\n"
        + f'  domain: "{service.domain}"\n'
        + "onezone:\n"
        + f'  domainName: "{spec.zone.domain}"\n'
    )


def service_model(spec: Spec, service: Service) -> dict[str, Any]:
    """One Onedata service: the stock image under start.sh, its panel config, its binds and its one loopback port."""
    zone = service.name == ZONE
    name = service.name
    volumes = [bind(f"{ROOT}/{name}/persistence", "/volumes/persistence")]
    if not zone:
        volumes += [
            bind(f"{ROOT}/{name}/storage", "/volumes/storage"),
            bind(f"{ROOT}/secrets/{name}.regtoken", "/expcore/registration-token", read_only=True),
        ]
    volumes += [
        bind(f"{ROOT}/ca/federation-ca.pem", "/expcore/federation-ca.pem", read_only=True),
        bind("./start.sh", "/expcore/start.sh", read_only=True),
    ]
    config = ("ONEZONE_CONFIG", zone_config(spec)) if zone else ("ONEPROVIDER_CONFIG", provider_config(spec, service))
    return {
        "image": ZONE_IMAGE if zone else PROVIDER_IMAGE,
        "pull_policy": "never",
        "restart": "no",
        "entrypoint": ["/bin/bash", "/expcore/start.sh"],
        "command": ["/root/onezone.sh" if zone else "/root/oneprovider.sh"],
        "hostname": NODE,
        "domainname": service.domain,
        "stop_grace_period": "120s",
        "mem_limit": service.memory_limit,
        "labels": {ROOT_LABEL: ROOT},
        "healthcheck": {
            "test": ["CMD", "test", "-f", "/root/service-ready.lock"],
            "interval": "10s",
            "timeout": "5s",
            "retries": 3,
            "start_period": "900s",
        },
        "env_file": [{"path": f"{ROOT}/secrets/{name}.env", "required": False}],
        "environment": {"ONEPANEL_BATCH_MODE": "true", "ONEPANEL_LOG_LEVEL": "info", config[0]: config[1]},
        "volumes": volumes,
        "ports": [{"target": 443, "host_ip": LOOPBACK, "published": str(service.port), "protocol": "tcp"}],
        "networks": {"fed": {"aliases": [service.domain]}},
    }


def janitor_model(spec: Spec) -> dict[str, Any]:
    """Down's helper: removes what the services wrote as root, and only in a root holding this project's mark."""
    marker = f"/fed/{ROOT_MARKER}"
    script = (
        f'test "$$(cat {marker} 2>/dev/null)" = {spec.project} || {{ echo "not this federation root" >&2; exit 3; }}; '
        "find /fed -mindepth 1 -delete"
    )
    return {
        "image": PROVIDER_IMAGE,
        "pull_policy": "never",
        "profiles": ["janitor"],
        "network_mode": "none",
        "labels": {ROOT_LABEL: ROOT},
        "entrypoint": ["sh", "-c", script],
        "volumes": [bind(ROOT, "/fed")],
    }


def model(spec: Spec) -> dict[str, Any]:
    """The Compose model of a layout: its services, the janitor and the one network."""
    services = {service.name: service_model(spec, service) for service in spec.services}
    services["janitor"] = janitor_model(spec)
    network = {"driver": "bridge", "labels": {ROOT_LABEL: ROOT}}  # names its root, as every container does
    return {"name": spec.project, "services": services, "networks": {"fed": network}}


# -- YAML ------------------------------------------------------------------------------------------------------------
# Every string is written as a JSON string, which YAML reads as a double-quoted scalar, and every multi-line string as
# a literal block; nothing is left to YAML's guessing of types.


def _key(key: str) -> str:
    return key if SAFE_KEY.fullmatch(key) else json.dumps(key)


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"no YAML form for {type(value).__name__}")


def _entry(head: str, value: Any, depth: int) -> list[str]:
    """`head` followed by `value`: inline when it is a scalar or a list of scalars, below it otherwise."""
    pad = "  " * (depth + 1)
    if isinstance(value, str) and "\n" in value:
        if not value.endswith("\n") or value.endswith("\n\n"):
            raise ValueError("a multi-line value must end with exactly one newline")
        return [f"{head} |", *(pad + line if line else "" for line in value[:-1].split("\n"))]
    if isinstance(value, list) and all(not isinstance(v, dict | list) for v in value):
        return [f"{head} [{', '.join(_scalar(v) for v in value)}]"]
    if isinstance(value, dict | list):
        return [head, *_lines(value, depth + 1)] if value else [f"{head} {{}}"]
    return [f"{head} {_scalar(value)}"]


def _lines(value: dict[str, Any] | list[Any], depth: int) -> list[str]:
    pad = "  " * depth
    out: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            out += _entry(f"{pad}{_key(key)}:", item, depth)
        return out
    for item in value:
        if isinstance(item, dict):
            first, *rest = _lines(item, depth + 1)
            out += [f"{pad}- {first.lstrip()}", *rest]
        else:
            out += _entry(f"{pad}-", item, depth)
    return out


def render(spec: Spec) -> str:
    """The Compose file of a layout, as YAML, headed by where it came from."""
    header = [
        f"# The Compose file of the federation layout {spec.source} (project {spec.project}), made by fedcompose.py.",
        "#",
        "# Never edit it, and never run it with docker compose alone: federation.py generates it from the layout at",
        "# every command, makes the CA, the certificates and the secrets these services read, and bootstraps the",
        "# federation. To see what a layout generates: federation.py --layout PATH compose",
    ]
    return "\n".join([*header, *_lines(model(spec), 0)]) + "\n"
