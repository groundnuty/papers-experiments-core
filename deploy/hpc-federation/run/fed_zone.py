"""fed_zone.py: the zone-side helper of the run, in a step on the zone's node (python3.11 -I).

  fed_zone.py --workdir W [--evidence DIR] prepare-n SPACE_NAME N
      as the admin: create the space, mint N one-hour supportSpace invites (one per provider), an access token, and a
      read-only access token for the data gate ("gate_token": time and data.readonly caveats). One JSON object on
      stdout for pick.py (pipe it: it holds tokens). Status goes to stderr.
  fed_zone.py --workdir W [--evidence DIR] gate-token
      a fresh read-only gate token alone: {"gate_token": ...} on stdout.
  fed_zone.py --workdir W [--evidence DIR] online N [--wait SECONDS]
      exit 0 once N providers are online in the zone; the listing, as one JSON object, on stdout (no token).

The admin password (the zone panel's emergency passphrase) is read from its 0600 file and sent only as an HTTP
header. TLS: the Onedata test CA, for this node's name, connected on 127.0.0.1:8443.

With --evidence DIR, every HTTP call appends one line to DIR/http-zone.jsonl: its method, path, status, and the
response body's size and SHA-256 (never a header, never a body). Raw evidence, one file per step (run/README.md).
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import stat
import subprocess
import sys
import time
from typing import Any

API = "/api/v3/onezone"
LIFETIME = 3600
EVIDENCE_FILE: str | None = None


ERROR_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")


def answer_note(status: int, body: bytes | str) -> str:
    """A failed answer as a message may carry it: its status, the body's size and SHA-256, and the error's id when the
    body is Onedata's error object. Never the body: an error may echo an account, a path or a credential."""
    raw = body if isinstance(body, bytes) else str(body).encode("utf-8", "replace")
    note = f"HTTP {status}, {len(raw)} bytes, sha256 {hashlib.sha256(raw).hexdigest()[:16]}"
    try:
        error = json.loads(raw.decode("utf-8")).get("error")
        found = error.get("id") if isinstance(error, dict) else None
    except (ValueError, AttributeError):
        found = None
    if isinstance(found, str) and ERROR_ID.fullmatch(found):
        note += f", error id {found}"
    return note


def die(msg: str) -> None:
    print(f"fed-zone: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def registered_name(workdir: str) -> str:
    """The name the service is registered under, which its certificate holds: the work directory's `declared-name`
    when configure.sh wrote one (O77), else the node's own FQDN, as before."""
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


def say(msg: str) -> None:
    print(f"fed-zone: {msg}", file=sys.stderr, flush=True)


def note(method: str, path: str, status: int, body: bytes) -> None:
    """One HTTP call as data, appended to the evidence file (0600) when the step was given a directory for it."""
    if EVIDENCE_FILE is None:
        return
    line = {
        "method": method,
        "path": path,
        "status": status,
        "body_bytes": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }
    fd = os.open(EVIDENCE_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")


def private_dir(path: str) -> bool:
    st = os.lstat(path)
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700


def read_secret(path: str) -> str:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o600:
        die(f"{path} is not a 0600 file of yours")
    with open(path, encoding="ascii") as f:
        value = f.readline().strip()
    if not value:
        die(f"{path} is empty")
    return value


class LocalHTTPS(http.client.HTTPSConnection):
    def connect(self) -> None:
        sock = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class Zone:
    def __init__(self, workdir: str) -> None:
        rootfs = os.path.join(workdir, "rootfs")
        self.fqdn = registered_name(workdir)
        ca = os.path.join(rootfs, "volumes/persistence_backup/etc/oz_panel/OnedataTestWebServerCa.pem")
        self.ctx = ssl.create_default_context(cafile=ca)
        password = read_secret(os.path.join(rootfs, "root/.hpcop/passphrase"))
        self.auth = "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, str, str | None]:
        conn = LocalHTTPS(self.fqdn, 8443, timeout=60, context=self.ctx)
        headers = {"Authorization": self.auth}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body)
        try:
            conn.request(method, API + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            note(method, path, resp.status, raw)
            return resp.status, raw.decode("utf-8", "replace"), resp.getheader("Location")
        finally:
            conn.close()

    def json(self, method: str, path: str, body: dict[str, Any] | None = None) -> tuple[Any, str | None]:
        status, raw, location = self.call(method, path, body)
        if status not in (200, 201, 204):
            die(f"{method} {path}: {answer_note(status, raw)}")
        return (json.loads(raw) if raw.strip() else {}), location


def listing(zone: Zone) -> list:
    listed, _ = zone.json("GET", "/providers")
    rows = []
    for pid in listed.get("providers", []):
        info, _ = zone.json("GET", f"/providers/{pid}")
        rows.append({"name": info.get("name"), "providerId": pid, "online": info.get("online")})
    return rows


def online(zone: Zone, n: int, wait: float) -> int:
    end = time.monotonic() + wait
    start = time.monotonic()
    while True:
        rows = listing(zone)
        up = sum(1 for r in rows if r["online"] is True)
        if up >= n or time.monotonic() >= end:
            print(json.dumps({"providers": rows, "online": up, "waited_s": round(time.monotonic() - start, 1)}))
            return 0 if up >= n else 2
        time.sleep(5)


def create_space(zone: Zone, name: str) -> str:
    before, _ = zone.json("GET", "/user/spaces")
    _, location = zone.json("POST", "/user/spaces", {"name": name})
    sid = location.rstrip("/").rsplit("/", 1)[-1] if location else None
    if not sid:
        after, _ = zone.json("GET", "/user/spaces")
        new = [s for s in after.get("spaces", []) if s not in before.get("spaces", [])]
        if len(new) != 1:
            die(f"cannot tell the new space's id ({len(new)} new spaces)")
        sid = new[0]
    space, _ = zone.json("GET", f"/spaces/{sid}")
    if space.get("name") != name:
        die("the created space has another name")
    return sid


def prepare_n(zone: Zone, name: str, n: int) -> int:
    t0 = time.monotonic()
    user, _ = zone.json("GET", "/user")
    admin = user["userId"]
    sid = create_space(zone, name)
    say("space created")
    caveats = [{"type": "time", "validUntil": int(time.time()) + LIFETIME}]
    supports = []
    for _ in range(n):
        tok, _ = zone.json(
            "POST",
            "/user/tokens/temporary",
            {"type": {"inviteToken": {"inviteType": "supportSpace", "spaceId": sid}}, "caveats": caveats},
        )
        supports.append(tok["token"])
    access, _ = zone.json("POST", "/user/tokens/temporary", {"type": {"accessToken": {}}, "caveats": caveats})
    gate, _ = zone.json(
        "POST",
        "/user/tokens/temporary",
        {"type": {"accessToken": {}}, "caveats": [*caveats, {"type": "data.readonly"}]},
    )
    say(
        f"{n} supportSpace invites, an access token and a read-only gate token minted (valid {LIFETIME} s); "
        f"prepared in {time.monotonic() - t0:.1f} s"
    )
    out = {
        "space_id": sid,
        "tokens": supports,
        "users": [admin],
        "size": 1073741824,
        "access_token": access["token"],
        "gate_token": gate["token"],
    }
    print(json.dumps(out), flush=True)
    return 0


def gate_token(zone: Zone) -> int:
    caveats = [{"type": "time", "validUntil": int(time.time()) + LIFETIME}, {"type": "data.readonly"}]
    gate, _ = zone.json("POST", "/user/tokens/temporary", {"type": {"accessToken": {}}, "caveats": caveats})
    say(f"a read-only gate token minted (valid {LIFETIME} s)")
    print(json.dumps({"gate_token": gate["token"]}), flush=True)
    return 0


def options(argv: list) -> tuple[dict[str, str], list]:
    found: dict[str, str] = {}
    while argv[:1] and argv[0] in ("--workdir", "--evidence"):
        if len(argv) < 2:
            die(__doc__)
        found[argv[0][2:]] = argv[1]
        argv = argv[2:]
    return found, argv


def main(argv: list) -> int:
    global EVIDENCE_FILE
    opts, rest = options(argv)
    workdir = opts.get("workdir")
    if not workdir or not rest:
        die(__doc__)
    if not (os.path.basename(workdir).startswith("hpcop-oz.") and private_dir(workdir)):
        die(f"{workdir} is not a private hpcop-oz work directory")
    if "evidence" in opts:
        if not private_dir(opts["evidence"]):
            die("the evidence directory is not a private directory of yours")
        EVIDENCE_FILE = os.path.join(opts["evidence"], "http-zone.jsonl")
    zone = Zone(workdir)
    if rest[0] == "gate-token" and len(rest) == 1:
        return gate_token(zone)
    if rest[0] == "prepare-n" and len(rest) == 3:
        return prepare_n(zone, rest[1], int(rest[2]))
    if rest[0] == "online" and len(rest) in (2, 4):
        wait = float(rest[3]) if len(rest) == 4 and rest[2] == "--wait" else 0.0
        return online(zone, int(rest[1]), wait)
    die(__doc__)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
