"""A federation stand-in with a certificate no default trusts: a CA made in the test, and a TLS server on 127.0.0.1.

`make_federation_ca` makes, with the `openssl` command (as the gate tests make the run's CA), a self-signed CA and a
server certificate it signs for `IP:127.0.0.1`. `TlsServer` serves HTTPS on a free loopback port with that certificate
and answers every GET and POST with a small JSON document, counting what reached it. Nothing leaves the machine.
"""

import json
import shutil
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

HAS_OPENSSL = shutil.which("openssl") is not None
ANSWER = {"name": "tls-test-space", "ok": True}  # what the server answers; a space read takes `name` from it


def _openssl(*words: str) -> None:
    done = subprocess.run(["openssl", *words], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise RuntimeError(f"openssl {words[0]} failed: {done.stderr.strip()[:400]}")


def make_federation_ca(directory: Path, name: str = "federation-test-ca") -> SimpleNamespace:
    """Make a self-signed CA and a server certificate for 127.0.0.1 signed by it; return their paths.

    The CA holds no private key in `ca` (that file is what a deployment would point `ca_file` at); the server's key and
    certificate are `key` and `cert`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    ca_key, ca, key, csr, cert, ext = (
        directory / part for part in ("ca.key", "ca.pem", "server.key", "server.csr", "server.pem", "server.ext")
    )
    curve = ("-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes")
    _openssl(
        "req", "-x509", *curve, "-keyout", str(ca_key), "-out", str(ca), "-days", "2", "-subj", f"/CN={name}",
        "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    )  # fmt: skip
    _openssl("req", *curve, "-keyout", str(key), "-out", str(csr), "-subj", "/CN=127.0.0.1")
    ext.write_text(
        "subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n",
        encoding="utf-8",
    )
    _openssl(
        "x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key), "-CAcreateserial",
        "-out", str(cert), "-days", "2", "-extfile", str(ext),
    )  # fmt: skip
    return SimpleNamespace(ca=ca, key=key, cert=cert, directory=directory)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.seen.append((self.command, self.path))
        body = json.dumps(ANSWER).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _answer
    do_POST = _answer


class TlsServer(ThreadingHTTPServer):
    """HTTPS on 127.0.0.1 with the federation's certificate; `seen` lists each (method, path) that was answered.

    A client that refuses the certificate ends the handshake, which fails in `accept` and is dropped quietly: only
    answered requests are counted. Use it as a context manager; `url` is `https://127.0.0.1:<port>`.
    """

    daemon_threads = True

    def __init__(self, federation: SimpleNamespace) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.seen: list[tuple[str, str]] = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(federation.cert), str(federation.key))
        self.socket = context.wrap_socket(self.socket, server_side=True)
        self.url = f"https://127.0.0.1:{self.server_port}"

    def handle_error(self, request, client_address):
        pass  # a client that refused the certificate, or went away: not the test's concern here

    def __enter__(self) -> "TlsServer":
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc) -> None:
        self.shutdown()
        self.server_close()
