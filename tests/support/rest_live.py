"""What the live test of the REST data path on the Compose federation (tests/live/test_rest_path.py) needs, apart, so
the offline tests check it.

- `rest_sandbox_table`: the `[sandbox]` table of one REST level on the federation, which `mount_live.derive_platform`
  writes into a copy of the federation's own platform.toml: its `[sandbox.federation]` puts the data gate on the
  federation's network, dialling the zone and the local provider by their domains there (#203).
- `read_cell`: the code the agent runs. With the standard library alone, through the gate: the zone's token-scope
  inference, then the file's id by its path and its content, whose SHA-256 it prints. One line of JSON, never the
  bytes it read, an address or a name.

Every name here is invented or public: the image name is the published one.
"""

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from mount_live import absent

from expcore_onedata.client import API_PROVIDER, AUTH_HEADER
from expcore_onedata.config import PlatformConfig

REST_LEVEL = "gated"  # the invented level of the data_path factor that maps to the REST data path
LEASE_KEY = "live-rest"  # labels the sandbox's containers, network and mounts, so the probe can show them gone
LABEL_NAMESPACE = "org.expcore.live"


def rest_sandbox_table(
    *,
    work_root: Path,
    images: Mapping[str, tuple[str, str]],
    pool: str,
    network: str,
    zone_address: str,
    local: str,
    local_address: str,
    content_check_file: str,
) -> dict[str, Any]:
    """The `[sandbox]` table of one REST level on the federation: `images` maps agent and gate to (name, id), and
    `content_check_file` is the file a REST level requires the pre-campaign check to read (the one the test reads)."""
    return {
        "work_root": str(work_root),
        "name_prefix": "fedrest",
        "label_namespace": LABEL_NAMESPACE,
        "agent_uid": 10001,
        "python": "python",
        "sudo": ["sudo", "-n"],
        "images": {role: {"name": name, "id": image_id} for role, (name, image_id) in images.items()},
        "network": {"pools": [pool], "prefix": 28, "max_attempts": 64},
        "limits": {
            "cpus": 1.0,
            "memory_bytes": 2 << 30,
            "pids": 256,
            "nofile": 1024,
            "tmp_bytes": 256 << 20,
            "shm_bytes": 64 << 20,
            "work_disk_bytes": 256 << 20,
        },
        "data_paths": {REST_LEVEL: "rest"},
        "content_check_file": content_check_file,
        "federation": {"network": network, "zone": zone_address, "providers": {local: local_address}},
    }


def expected_digest(config: PlatformConfig, path: str, *, transport: httpx.BaseTransport | None = None) -> dict:
    """The file's size and SHA-256, read from the host through the local provider's URL as the admin, never through
    the gate: what the agent's read through the gate must match. `{"absent": True}` for a file the space lacks."""
    base = config.local_provider.url + API_PROVIDER
    headers = {AUTH_HEADER: config.admin.read()}
    with httpx.Client(timeout=300, verify=config.tls.client_verify(), transport=transport) as http:
        found = http.post(f"{base}/lookup-file-id/{quote(config.space.name)}/{quote(path.strip('/'))}", headers=headers)
        if absent(found):
            return {"absent": True, "status": found.status_code}
        found.raise_for_status()
        content = http.get(f"{base}/data/{found.json()['fileId']}/content", headers=headers)
        content.raise_for_status()
    return {"size": len(content.content), "sha256": hashlib.sha256(content.content).hexdigest()}


READ = """
import hashlib, json, os, ssl, urllib.error, urllib.request
from urllib.parse import quote
provider, space, path = {provider!r}, {space!r}, {path!r}
zone = os.environ["ONEDATA_ONEZONE_HOST"]  # the zone's name the gate serves, as the published client reads it
context = ssl.create_default_context()  # SSL_CERT_FILE: the run's CA, which signs the gate's certificates alone


def call(method, url, body=None):
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, context=context, timeout=120) as answer:
            return answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except Exception as error:
        return type(error).__name__, b""


out = {{}}
status, body = call("POST", "https://" + zone + "/api/v3/onezone/tokens/infer_access_token_scope", b"{{}}")
out["zone_status"] = status
out["zone_names_the_provider"] = provider.encode() in body
base = "https://" + provider + "/api/v3/oneprovider"
status, body = call("POST", base + "/lookup-file-id/" + quote(space) + "/" + quote(path.strip("/")))
out["lookup_status"] = status
if status == 200:
    status, body = call("GET", base + "/data/" + json.loads(body)["fileId"] + "/content")
    out["content_status"] = status
    if status == 200:
        out.update(size=len(body), sha256=hashlib.sha256(body).hexdigest())
print(json.dumps(out))
"""


def read_cell(provider: str, space: str, path: str) -> str:
    """Read a file through the gate, as the agent can: the zone's token-scope inference (the gate puts its own token in
    the body), the file's id by its path in the space, and its content, at the local provider's name the gate serves.
    Prints the statuses, whether the zone's answer names the provider by that name, and the content's size and
    SHA-256."""
    return READ.format(provider=provider, space=space, path=path)
