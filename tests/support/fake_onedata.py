# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/mock_onedata.py, blob 7a9a660be9cfb87c3bd24a914c26331d75cc004a
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""A local mock of the Onedata REST surface the official fsspec client uses.

Test infrastructure, NOT Onedata. It implements exactly what
onedatarestfsspec 25.0.0 / onedatafilerestclient 25.0.0 call:

  POST /api/v3/onezone/tokens/infer_access_token_scope
  POST /api/v3/oneprovider/lookup-file-id/{space}@{id}[/{path}]
  GET  /api/v3/oneprovider/data/{id}                 (attributes; JSON body lists them)
  GET  /api/v3/oneprovider/data/{id}/children        (limit, token; nextPageToken, isLast)
  GET  /api/v3/oneprovider/data/{id}/content         (Range -> 206 + Content-Range)

Page size as measured on the real federation (#10): default 1000, `max_limit` 10000, and a larger
`limit` is REJECTED with HTTP 400 `badValueNotInRange` (not clamped). Every request is recorded in
`.requests` so tests can assert what actually went over the wire. Files are
bytes or pathlib.Path (read lazily with seek, so large files stay on disk).
"""

from __future__ import annotations

import binascii
import dataclasses
import json
import pathlib
import re
import threading
import time
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


@dataclasses.dataclass
class Request:
    method: str
    path: str
    query: dict
    range: str | None
    token: str | None
    body: bytes = b""
    run_headers: tuple = ()  # every X-dataset-192 header value received


class MockOnedata:
    SPACE_ID = "spmock0001"
    PROVIDER_ID = "prmock0001"

    def __init__(
        self,
        files: Mapping[str, bytes | pathlib.Path],
        *,
        space_name: str = "dataset-176",
        zone_host: str = "onezone.mock",
        provider_host: str = "provider.mock",
        max_limit: int | None = 10000,
        token: str = "mock-read-only-token",
        provider_version: str = "25.0.0",
    ):
        self.files = {k.strip("/"): v for k, v in files.items()}
        self.space_name, self.zone_host, self.provider_host = space_name, zone_host, provider_host
        self.max_limit, self.token, self.provider_version = max_limit, token, provider_version
        self._index()
        self.content_delay_s = 0.0  # tests: delay the headers of every content response
        self.requests: list[Request] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self.port = 0

    # -- tree ---------------------------------------------------------------
    def _index(self) -> None:
        """Directory index, built once (the file set is static): O(1) lookups at 15k+ entries."""
        self._kids: dict[str, set[str]] = {"": set()}
        for k in self.files:
            parts = k.split("/")
            for i in range(len(parts)):
                d = "/".join(parts[:i])
                self._kids.setdefault(d, set()).add(parts[i])
        self._sorted = {d: sorted(v) for d, v in self._kids.items()}

    def _is_dir(self, path: str) -> bool:
        return path in self._kids

    def _children(self, path: str) -> list[str]:
        return self._sorted.get(path, [])

    def _id(self, path: str) -> str:
        return self.SPACE_ID if path == "" else "f" + binascii.hexlify(path.encode()).decode()

    def _path(self, fid: str) -> str | None:
        if fid == self.SPACE_ID:
            return ""
        if fid.startswith("f"):
            return binascii.unhexlify(fid[1:]).decode()
        return None

    def _size(self, path: str) -> int:
        v = self.files[path]
        return v.stat().st_size if isinstance(v, pathlib.Path) else len(v)

    def _attrs(self, path: str, keys: list[str] | None) -> dict:
        d = self._is_dir(path)
        a = {
            "fileId": self._id(path),
            "name": path.rsplit("/", 1)[-1] if path else self.space_name,
            "type": "DIR" if d else "REG",
            "size": 0 if d else self._size(path),
            "mtime": 0,
            "posixPermissions": "644",
            "path": "/" + self.space_name + ("/" + path if path else ""),
        }
        return {k: a.get(k) for k in keys} if keys else a

    # -- server -------------------------------------------------------------
    def start(self, host: str = "0.0.0.0") -> MockOnedata:
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def _err(self, code, eid, details=""):
                self._json(code, {"error": {"id": eid, "details": details}})

            def _go(self):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                tok = self.headers.get("X-Auth-Token")
                with outer._lock:
                    outer.requests.append(
                        Request(
                            self.command,
                            u.path,
                            q,
                            self.headers.get("Range"),
                            tok,
                            body,
                            tuple(self.headers.get_all("X-dataset-192") or ()),
                        )
                    )
                if self.command == "POST" and u.path.endswith("/onezone/tokens/infer_access_token_scope"):
                    sent = json.loads(body or b"{}").get("token")
                    if sent != outer.token:
                        return self._err(401, "unauthorized")
                    return self._json(200, outer._scope())
                if tok != outer.token:
                    return self._err(401, "unauthorized")
                m = re.match(r"/api/v3/oneprovider/lookup-file-id/([^/]+)(?:/(.*))?$", u.path)
                if self.command == "POST" and m:
                    p = (m.group(2) or "").strip("/")
                    if p and p not in outer.files and not outer._is_dir(p):
                        return self._err(404, "posix", "enoent")
                    return self._json(200, {"fileId": outer._id(p)})
                m = re.match(r"/api/v3/oneprovider/data/([^/]+)(/children|/content)?$", u.path)
                if self.command == "GET" and m:
                    path = outer._path(m.group(1))
                    if path is None or not (outer._is_dir(path) or path in outer.files):
                        return self._err(404, "posix", "enoent")
                    keys = json.loads(body).get("attributes") if body else None
                    if m.group(2) is None:
                        return self._json(200, outer._attrs(path, keys))
                    if m.group(2) == "/children":
                        return self._children(path, q, keys)
                    return self._content(path)
                self._err(404, "notFound", u.path)

            def _children(self, path, q, keys):
                names = outer._children(path)
                limit = int(q.get("limit", 1000))
                if outer.max_limit is not None and limit > outer.max_limit:
                    return self._err(400, "badValueNotInRange", f"limit must be <= {outer.max_limit}")
                start = int(q["token"]) if q.get("token") else 0
                page = names[start : start + limit]
                nxt = start + len(page)
                last = nxt >= len(names)
                pre = path + "/" if path else ""
                self._json(
                    200,
                    {
                        "children": [outer._attrs(pre + n, keys) for n in page],
                        "isLast": last,
                        "nextPageToken": None if last else str(nxt),
                    },
                )

            def _content(self, path):
                v = outer.files.get(path)
                if v is None:
                    return self._err(400, "posix", "eisdir")
                size = outer._size(path)
                if outer.content_delay_s:
                    time.sleep(outer.content_delay_s)
                rng = self.headers.get("Range")
                lo, hi, code = 0, size - 1, 200
                if rng:
                    mm = re.match(r"bytes=(\d+)-(\d*)$", rng)
                    if not mm:
                        return self._err(400, "badValueRange")
                    lo = int(mm.group(1))
                    hi = min(int(mm.group(2)) if mm.group(2) else size - 1, size - 1)
                    if lo >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    code = 206
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream")
                if code == 206:
                    self.send_header("Content-Range", f"bytes {lo}-{hi}/{size}")
                self.send_header("Content-Length", str(hi - lo + 1))
                self.end_headers()
                if isinstance(v, pathlib.Path):
                    with open(v, "rb") as f:
                        f.seek(lo)
                        left = hi - lo + 1
                        while left > 0:
                            b = f.read(min(1 << 20, left))
                            if not b:
                                break
                            self.wfile.write(b)
                            left -= len(b)
                else:
                    self.wfile.write(v[lo : hi + 1])

            do_GET = do_POST = do_PUT = do_DELETE = _go

        self._httpd = ThreadingHTTPServer((host, 0), H)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    def _scope(self) -> dict:
        return {
            "validUntil": 0,
            "dataAccessScope": {
                "readonly": True,
                "spaces": {
                    self.SPACE_ID: {"name": self.space_name, "supports": {self.PROVIDER_ID: {"readonly": False}}}
                },
                "providers": {
                    self.PROVIDER_ID: {
                        "name": "mock-provider",
                        "domain": self.provider_host,
                        "version": self.provider_version,
                        "online": True,
                    }
                },
            },
        }
