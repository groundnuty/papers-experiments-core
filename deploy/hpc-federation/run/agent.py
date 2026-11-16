"""agent.py: runs inside the agent's cell (setuid Apptainer, --containall --cleanenv, the image's python3) on the
sandbox node S, which hosts no Onedata service. It holds no token. Prints one JSON object.

  agent.py GATE_PORT PROVIDER_FQDN SPACE_NAME SPACE_ID ROOT_ID FILE_ID SHA256 ZONE_FQDN P1_FQDN P2_FQDN P3_FQDN

1. The data path, through the gate (127.0.0.1:GATE_PORT, TLS for PROVIDER_FQDN by the run's CA in /cell/ca.pem), by the
   routes the gate's policy lists (gatepolicy.py): a lookup of the space root's id by the space's name, a listing of
   that root, the file's content (SHA-256 read, beside the expected one), a write (refused), and a request that carries
   a client token of its own on a listed route, beside the same request without one: the gate must strip the client's
   token and use its own, so both answer alike.
2. Direct to the provider without a token: refused.
3. Reachability by TCP connect only (no payload), per role: the zone and every provider on their public ports (8443,
   6665) and on the ports that are loopback-only on their nodes (Couchbase 8091 and 11210, onepanel 9443, the default
   epmd 4369), and one internet host on 443. A Couchbase login with the image's default administrator is tried only
   where its port is reachable, and an epmd port that is reachable is noted.
4. What the cell holds: no token in its environment, nothing in /cell but the CA, this script and the hosts list.

Every probe records the status and, for an answer with a body, its size and SHA-256. The output holds the raw facts;
whether they pass is decided later, from this file, by run/evidence.py. Python 3.8 (the image's).
"""

import base64
import hashlib
import http.client
import json
import os
import socket
import ssl
import sys
import time
from urllib.parse import quote

API = "/api/v3/oneprovider"
ROLES = ("Z", *("P%d" % n for n in (1, 2, 3)))
PORTS = (8443, 6665, 9443, 8091, 11210, 4369)
INTERNET = ("pypi.org", 443)


class Via(http.client.HTTPSConnection):
    """HTTPS to host:port by name (Host header and SNI), but connected to a fixed address (the gate on loopback)."""

    def __init__(self, host, port, addr, **kw):
        super().__init__(host, port, **kw)
        self._addr = addr

    def connect(self):
        sock = socket.create_connection((self._addr, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def call(conn, method, path, body=None, headers=None):
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.read()
    except Exception as e:  # recorded, never raised: the cell reports what happened
        return "error: %s" % type(e).__name__, b""
    finally:
        conn.close()


def shape(status, body):
    return {"status": status, "body_bytes": len(body), "body_sha256": hashlib.sha256(body).hexdigest()}


def tcp(host, port, timeout=3.0):
    t = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout):
            return {"connect": "open", "ms": round((time.monotonic() - t) * 1000)}
    except socket.timeout:
        return {"connect": "timeout"}
    except ConnectionRefusedError:
        return {"connect": "refused"}
    except OSError as e:
        return {"connect": "error: %s" % (e.strerror or type(e).__name__)}


def json_field(body, key):
    try:
        value = json.loads(body.decode("utf-8")).get(key)
    except (ValueError, AttributeError):
        return None
    return value if isinstance(value, str) else None


def data_path(out, gate, name, root_id, file_id, want):
    """The probes through the gate, each by a route the policy lists, and the one write it must refuse."""
    status, body = call(gate(), "POST", "%s/lookup-file-id/%s" % (API, quote(name, safe="")))
    found = json_field(body, "fileId") if status == 200 else None
    out["gate_lookup"] = dict(shape(status, body), file_id=found or "", file_id_expected=root_id)
    root = found or root_id  # the listing goes on from the id the lookup gave; its own failure stays recorded above
    status, body = call(gate(), "GET", "%s/data/%s/children?limit=10" % (API, root))
    try:
        entries = len(json.loads(body.decode("utf-8")).get("children", [])) if status == 200 else 0
    except (ValueError, AttributeError):
        entries = 0
    out["gate_listing"] = dict(shape(status, body), entries=entries)
    started = time.monotonic()
    status, body = call(gate(), "GET", "%s/data/%s/content" % (API, file_id))
    read = dict(shape(status, body), sha256_expected=want, s=round(time.monotonic() - started, 3))
    read["bytes"] = len(body)
    read["sha256_read"] = hashlib.sha256(body).hexdigest() if status == 200 else None
    out["gate_read"] = read
    status, body = call(
        gate(), "PUT", "%s/data/%s/content" % (API, file_id), b"x", {"Content-Type": "application/octet-stream"}
    )
    out["gate_write_refused"] = shape(status, body)
    probe, pbody = call(gate(), "GET", "%s/data/%s" % (API, root), headers={"X-Auth-Token": "not-a-token"})
    control, cbody = call(gate(), "GET", "%s/data/%s" % (API, root))
    out["gate_strips_client_token"] = {
        "probe_status": probe,
        "probe_body_sha256": hashlib.sha256(pbody).hexdigest(),
        "control_status": control,
        "control_body_sha256": hashlib.sha256(cbody).hexdigest(),
    }


def direct(out, provider, space_id, file_id):
    def conn():
        return http.client.HTTPSConnection(provider, 8443, timeout=30, context=ssl._create_unverified_context())

    space, sbody = call(conn(), "GET", "%s/spaces/%s" % (API, space_id))
    content, cbody = call(conn(), "GET", "%s/data/%s/content" % (API, file_id))
    out["direct_no_token"] = {
        "space_status": space,
        "space_body_sha256": hashlib.sha256(sbody).hexdigest(),
        "content_status": content,
        "content_body_sha256": hashlib.sha256(cbody).hexdigest(),
    }


def reachability(out, hosts):
    reach = {}
    for role, host in zip(ROLES, hosts):
        reach[role] = {str(port): tcp(host, port) for port in PORTS}
    out["reach"] = reach
    out["internet_443"] = tcp(*INTERNET)
    logins = {}
    for role, host in zip(ROLES, hosts):
        if reach[role]["8091"]["connect"] == "open":
            auth = "Basic " + base64.b64encode(b"admin:password").decode()
            status, _ = call(
                http.client.HTTPConnection(host, 8091, timeout=10),
                "GET",
                "/pools/default",
                headers={"Authorization": auth},
            )
            logins[role] = status
    out["couchbase_login_attempts"] = logins
    out["epmd_open_roles"] = [role for role in ROLES if reach[role]["4369"]["connect"] == "open"]


def main(argv):
    if len(argv) != 11:
        sys.exit(__doc__)
    port, provider, name, space_id, root_id, file_id, want = (
        int(argv[0]),
        argv[1],
        argv[2],
        argv[3],
        argv[4],
        argv[5],
        argv[6],
    )
    hosts = argv[7:]
    out = {}
    ctx = ssl.create_default_context(cafile="/cell/ca.pem")

    def gate():
        return Via(provider, port, "127.0.0.1", timeout=60, context=ctx)

    data_path(out, gate, name, root_id, file_id, want)
    direct(out, provider, space_id, file_id)
    reachability(out, hosts)
    out["env_keys"] = sorted(os.environ)
    out["env_token_like"] = [k for k, v in os.environ.items() if "TOKEN" in k.upper() or len(v) > 100]
    out["cell_files"] = sorted(os.listdir("/cell"))
    out["uid"] = os.getuid()
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
