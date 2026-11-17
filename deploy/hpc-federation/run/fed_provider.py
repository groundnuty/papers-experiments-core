"""fed_provider.py: the provider-side helper of the run, in a step on the provider's node (python3.11 -I).

  fed_provider.py --workdir W --bundle B [--evidence DIR] support
      stdin: pick.py support I. Support the space with this provider's posix storage (onboard.py) and wait until
      op_worker serves its root.
  fed_provider.py --workdir W [--evidence DIR] write
      stdin: pick.py access. Create one 1 MiB file of random bytes in the space root, read it back here; print the
      space's id, the root's id, the file's id, its size, its SHA-256 and the SHA-256 of what was read back.
  fed_provider.py --workdir W [--evidence DIR] read FILE_ID SHA256
      stdin: pick.py access. Read the file through this provider (its blocks come from the writer over the providers'
      link); print the SHA-256 read beside the expected one, and the file's distribution as this provider reports it.

Each prints one JSON object (no secret). Exit 1 on any failure. The access token is sent only as X-Auth-Token.
TLS: the Onedata test CA, for this node's name, on 127.0.0.1:8443.

With --evidence DIR, every HTTP call appends one line to DIR/http-<provider>.jsonl (the work directory's name keeps
the file apart per node): method, path, status, and the response body's size and SHA-256. No header, no body.
"""

from __future__ import annotations

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

API = "/api/v3/oneprovider"
SIZE = 1 << 20
EVIDENCE_FILE: str | None = None


ERROR_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")


def answer_note(status, body) -> str:
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


ONBOARD_SUMMARY = re.compile(r"(already supported|support HTTP [0-9]{3}); users mapped [0-9]+/[0-9]+")


def onboard_errors(text):
    """onboard.py's stderr as data, never as text: a failed answer's body follows its status there, may span lines,
    and no prefix proves a line is onboard's own. The statuses it names, its line count, size and SHA-256 prefix."""
    raw = text.encode("utf-8", "replace")
    return {
        "lines": len(text.splitlines()),
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest()[:16],
        "http_statuses": [int(code) for code in re.findall(r"\bHTTP ([0-9]{3})\b", text)],
    }


def onboard_summary(text):
    """onboard.py's one line on stdout, kept only in its own format."""
    first = text.strip().splitlines()[0] if text.strip() else ""
    size = len(text.encode("utf-8", "replace"))
    return first if ONBOARD_SUMMARY.fullmatch(first) else "(not onboard's summary line: %d bytes)" % size


def die(msg, result=None):
    if result is not None:
        print(json.dumps(result))
    print(f"fed-provider: ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def registered_name(workdir):
    """The name the service is registered under, which its certificate holds: the work directory's `declared-name`
    when configure.sh wrote one (O70), else the node's own FQDN, as before."""
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


def note(method, path, status, body):
    """One HTTP call as data, appended to the evidence file (0600)."""
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


class LocalHTTPS(http.client.HTTPSConnection):
    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class Worker:
    def __init__(self, fqdn, ca, token):
        self.fqdn, self.token = fqdn, token
        self.ctx = ssl.create_default_context(cafile=ca)

    def call(self, method, path, body=None, headers=None):
        conn = LocalHTTPS(self.fqdn, 8443, timeout=120, context=self.ctx)
        try:
            conn.request(method, API + path, body=body, headers={"X-Auth-Token": self.token, **(headers or {})})
            resp = conn.getresponse()
            data = resp.read()
            note(method, path.split("?", 1)[0], resp.status, data)
            return resp.status, data
        finally:
            conn.close()


def space_root(worker, sid, wait=600):
    t = time.monotonic()
    last = None
    while time.monotonic() - t < wait:
        status, body = worker.call("GET", f"/spaces/{sid}")
        last = status
        if status == 200 and json.loads(body).get("fileId"):
            return json.loads(body)["fileId"], round(time.monotonic() - t, 2), last
        time.sleep(2)
    return None, round(time.monotonic() - t, 2), last


def support(workdir, bundle, payload, result):
    t0 = time.monotonic()
    onboard = os.path.join(bundle, "onboard.py")
    done = subprocess.run(
        [sys.executable, "-I", onboard, "--workdir", workdir],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=600,
    )
    result.update(
        {
            "onboard_rc": done.returncode,
            "onboard_out": onboard_summary(done.stdout),
            "support_s": round(time.monotonic() - t0, 2),
        }
    )
    if done.stderr.strip():
        result["onboard_err"] = onboard_errors(done.stderr)
    if done.returncode != 0:
        die("onboard.py failed", result)
    print(json.dumps(result))
    return 0


def write(worker, root, result):
    data = os.urandom(SIZE)
    want = hashlib.sha256(data).hexdigest()
    name = f"run-{int(time.time())}.bin"
    started = time.monotonic()
    status, body = worker.call("POST", f"/data/{root}/children?name={name}&type=REG")
    if status != 201:
        die(f"create file: {answer_note(status, body)}", result)
    fid = json.loads(body)["fileId"]
    status, body = worker.call("PUT", f"/data/{fid}/content", data, {"Content-Type": "application/octet-stream"})
    if status not in (200, 201, 204):
        die(f"write content: {answer_note(status, body)}", result)
    status, back = worker.call("GET", f"/data/{fid}/content")
    got = hashlib.sha256(back).hexdigest() if status == 200 else None
    result.update(
        {
            "root_id": root,
            "file_id": fid,
            "name": name,
            "bytes": SIZE,
            "sha256": want,
            "sha256_read_back": got,
            "read_back_status": status,
            "write_read_s": round(time.monotonic() - started, 3),
        }
    )
    print(json.dumps(result))
    return 0 if got == want else 1


def read(worker, fid, want, result):
    began = time.monotonic()
    got, status, tries = None, None, 0
    while time.monotonic() - began < 300:
        tries += 1
        status, back = worker.call("GET", f"/data/{fid}/content")
        if status == 200 and len(back) == SIZE:
            got = hashlib.sha256(back).hexdigest()
            break
        time.sleep(2)
    result.update(
        {
            "file_id": fid,
            "read_status": status,
            "tries": tries,
            "read_s": round(time.monotonic() - began, 3),
            "sha256_read": got,
            "sha256_expected": want,
        }
    )
    dstatus, dist = worker.call("GET", f"/data/{fid}/distribution")
    result["distribution_status"] = dstatus
    if dstatus == 200:
        result["distribution"] = json.loads(dist)
    print(json.dumps(result))
    return 0 if got == want else 1


def options(argv) -> tuple[dict[str, str], list]:
    found: dict[str, str] = {}
    while argv[:1] and argv[0] in ("--workdir", "--bundle", "--evidence"):
        if len(argv) < 2:
            die(__doc__)
        found[argv[0][2:]] = argv[1]
        argv = argv[2:]
    return found, argv


def main(argv):
    global EVIDENCE_FILE
    opts, rest = options(argv)
    workdir = opts.get("workdir")
    if not workdir or not rest:
        die(__doc__)
    st = os.lstat(workdir)
    if not (
        os.path.basename(workdir).startswith("hpcop-op.")
        and stat.S_ISDIR(st.st_mode)
        and st.st_uid == os.getuid()
        and stat.S_IMODE(st.st_mode) == 0o700
    ):
        die(f"{workdir} is not a private hpcop-op work directory")
    if "evidence" in opts:
        edir = os.lstat(opts["evidence"])
        if not (stat.S_ISDIR(edir.st_mode) and edir.st_uid == os.getuid() and stat.S_IMODE(edir.st_mode) == 0o700):
            die("the evidence directory is not a private directory of yours")
        EVIDENCE_FILE = os.path.join(opts["evidence"], "http-%s.jsonl" % os.path.basename(workdir).split(".", 1)[1])
    rootfs = os.path.join(workdir, "rootfs")
    fqdn = registered_name(workdir)
    ca = os.path.join(rootfs, "volumes/persistence_backup/etc/op_panel/OnedataTestWebServerCa.pem")
    payload = json.loads(sys.stdin.readline())
    sid = payload["space_id"]
    result = {"space_id": sid}
    if rest == ["support"]:
        if "bundle" not in opts:
            die(__doc__)
        return support(workdir, opts["bundle"], payload, result)
    worker = Worker(fqdn, ca, payload["access_token"])
    root, waited, last = space_root(worker, sid)
    result["space_root_wait_s"] = waited
    if not root:
        die(f"op_worker never served the space root (last HTTP {last})", result)
    if rest == ["write"]:
        return write(worker, root, result)
    if rest[0] == "read" and len(rest) == 3:
        return read(worker, rest[1], rest[2], result)
    die(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
