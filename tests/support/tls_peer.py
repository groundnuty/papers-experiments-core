"""A real TLS service on the loopback, for the tests of a client that reaches a service by name (O73).

`TlsPeer(directory, name)` listens on 127.0.0.1 at a free port with a certificate that names `name` alone, signed by a
CA of its own (`ca`). It answers every request with an empty JSON object, or with the `answer` a test sets (sent in the
pieces of `chunks`, when it sets them), and records what it was sent: the server name the client's handshake asked for
(`sni`), and each request's method, path and `Host` header (`requests`). The names are invented (`.test` resolves
nowhere), so a client that connects at all has been given the address another way.
"""

import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from expcore_onedata.sandbox.hostops import DockerHost


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args: Any) -> None:
        pass

    def _answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        self.server.peer.requests.append(  # type: ignore[attr-defined]
            {"method": self.command, "path": self.path, "host": self.headers.get("Host")}
        )
        peer = self.server.peer  # type: ignore[attr-defined]
        status, body = peer.answer
        self.send_response(status)
        self.send_header("Content-Type", peer.content_type)
        if peer.chunks:  # a chunked answer: each piece goes alone, so a name can be split across them
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for piece in peer.chunks:
                self.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
                self.wfile.flush()
                time.sleep(0.2)
            self.wfile.write(b"0\r\n\r\n")
            return
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_PUT = do_DELETE = _answer


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:  # a client that refuses the certificate
        pass


class TlsPeer:
    """The service: a certificate for `name`, a loopback port, and a record of what its clients sent."""

    def __init__(self, directory: Path, name: str) -> None:
        DockerHost().make_ca(directory, [name])
        self.name = name
        self.ca = directory / "ca.pem"
        self.sni: list[str | None] = []
        self.requests: list[dict[str, Any]] = []
        self.answer: tuple[int, bytes] = (200, b"{}")  # what every request is answered with: a test sets it
        self.content_type = "application/json"  # a file's content is answered as bytes: a test sets it
        # when set, the answer's body is sent in these pieces (Transfer-Encoding: chunked)
        self.chunks: list[bytes] = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(directory / f"{name}.pem", directory / f"{name}.key")
        context.sni_callback = self._remember
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.peer = self  # type: ignore[attr-defined]
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.port: int = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def _remember(self, _socket: ssl.SSLSocket, server_name: str | None, _context: ssl.SSLContext) -> None:
        self.sni.append(server_name)

    @property
    def url(self) -> str:
        """The URL a client is configured with: the declared name and the port, which resolve to nothing."""
        return f"https://{self.name}:{self.port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=10)

    def __enter__(self) -> "TlsPeer":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
