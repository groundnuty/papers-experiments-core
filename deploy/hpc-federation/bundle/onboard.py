"""Support one space on this node's provider, and map its users to the job user.

Run it with python3.11 -I, in a step of the provider's job; stdin is one JSON object:
  {"space_id": <id>, "token": <support token>, "users": [<onedata user ids>], "size": <bytes>}
The onepanel passphrase is read from the launch's 0600 file and sent only as an HTTP header; the support
token arrives on stdin. Neither reaches a command line. TLS is verified against the Onedata test CA for
this node's name, connected on 127.0.0.1. Exits 1 if support, the default credentials or any user
mapping fails, so a caller never mistakes a partial onboarding for success.
Usage: python3.11 -I onboard.py [--workdir W]
"""

import base64
import glob
import http.client
import json
import os
import re
import socket
import ssl
import stat
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

TIMEOUT = 120


def die(msg: str) -> None:
    print(f"onboard: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def registered_name(workdir: str) -> str:
    """The name the service is registered under, which its certificate holds: the work directory's `declared-name`
    when configure.sh wrote one (O73), else the node's own FQDN, as before."""
    path = os.path.join(workdir, "declared-name")
    if not os.path.lexists(path):
        return subprocess.run(["hostname", "-f"], capture_output=True, text=True, check=True).stdout.strip()
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        die(f"{path} is not a regular file of yours")
    with open(path, encoding="ascii") as handle:
        value = handle.readline().strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+", value):
        die(f"{path} holds no fully qualified name")
    return value


def private_dir(path: str) -> bool:
    st = os.lstat(path)
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700


def find_workdir(explicit: Optional[str]) -> str:
    if explicit:
        if not (os.path.basename(explicit).startswith("hpcop-op.") and private_dir(explicit)):
            die(f"{explicit} is not a private hpcop-op work directory")
        return explicit
    tmp = os.environ.get("TMPDIR")
    if not tmp:
        die("TMPDIR is not set: run in a step of the provider's job, or pass --workdir")
    found = [d for d in glob.glob(os.path.join(tmp, "hpcop-op.??????")) if private_dir(d)]
    if len(found) != 1:
        die(f"expected one private hpcop-op work directory under {tmp}, found {len(found)}")
    return found[0]


def read_secret(path: str) -> str:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
        die(f"{path} is not a 0600 file of yours")
    with open(path, encoding="utf-8") as f:
        value = f.readline().strip()
    if not value:
        die(f"{path} is empty")
    return value


class LocalHTTPS(http.client.HTTPSConnection):
    """HTTPS to 127.0.0.1, with the certificate checked for the node's own name."""

    def connect(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class Panel:
    def __init__(self, fqdn: str, ca: str, passphrase: str) -> None:
        self.fqdn = fqdn
        self.ctx = ssl.create_default_context(cafile=ca)
        self.auth = "Basic " + base64.b64encode(f"onepanel:{passphrase}".encode()).decode()

    def req(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Tuple[int, str]:
        conn = LocalHTTPS(self.fqdn, 9443, timeout=TIMEOUT, context=self.ctx)
        headers = {"Authorization": self.auth, "Content-Type": "application/json"}
        try:
            conn.request(
                method,
                "/api/v3/onepanel/provider" + path,
                body=None if body is None else json.dumps(body),
                headers=headers,
            )
            resp = conn.getresponse()
            return resp.status, resp.read().decode("utf-8", "replace")
        finally:
            conn.close()


def main(argv: List[str]) -> int:
    explicit = None
    if argv[:1] == ["--workdir"] and len(argv) == 2:
        explicit = argv[1]
    elif argv:
        die("usage: onboard.py [--workdir W]")
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict) or not all(k in payload for k in ("space_id", "token")):
        die("stdin must be a JSON object with space_id and token")
    workdir = find_workdir(explicit)
    rootfs = os.path.join(workdir, "rootfs")
    fqdn = registered_name(workdir)
    panel = Panel(
        fqdn,
        os.path.join(rootfs, "volumes/persistence_backup/etc/op_panel/OnedataTestWebServerCa.pem"),
        read_secret(os.path.join(rootfs, "root/.hpcop/passphrase")),
    )
    status, body = panel.req("GET", "/storages")
    ids = json.loads(body).get("ids", []) if status == 200 else []
    if not ids:
        die(f"no storage on this provider (GET /storages: HTTP {status})")
    sid, sp, uid, gid = ids[0], payload["space_id"], os.getuid(), os.getgid()
    failures = []
    status, _ = panel.req("GET", f"/spaces/{sp}")
    if status == 200:
        support = "already supported"
    else:
        status, body = panel.req(
            "POST", "/spaces", {"token": payload["token"], "size": payload.get("size", 20000000000), "storageId": sid}
        )
        support = f"support HTTP {status}"
        if status != 201:
            failures.append(f"support: HTTP {status} {body[:200]}")
    status, body = panel.req(
        "PUT",
        f"/storages/{sid}/luma/local_feed/storage_access/posix_compatible/default_credentials/{sp}",
        {"uid": uid, "gid": gid},
    )
    if status not in (200, 201, 204):
        failures.append(f"default credentials: HTTP {status} {body[:200]}")
    path = f"/storages/{sid}/luma/local_feed/storage_access/all/onedata_user_to_credentials"
    users = payload.get("users", [])
    mapped = 0
    for user in users:
        status, _ = panel.req(
            "POST",
            path,
            {
                "onedataUser": {"mappingScheme": "onedataUser", "onedataUserId": user},
                "storageUser": {"storageCredentials": {"type": "posix", "uid": str(uid)}, "displayUid": uid},
            },
        )
        if status not in (200, 201, 204):
            status, body = panel.req("GET", f"{path}/{user}")
            if not (status == 200 and f'"uid":"{uid}"' in body.replace(" ", "")):
                failures.append(f"user {user}: not mapped to uid {uid}")
                continue
        mapped += 1
    print(f"{support}; users mapped {mapped}/{len(users)}")
    for failure in failures:
        print(f"onboard: FAILED {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
