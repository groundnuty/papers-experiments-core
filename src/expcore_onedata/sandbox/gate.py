# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/sandbox/gate.py, blob 81e0330d90ea3d9a21ed27a9a9a09e9a78cafab0
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   merged: icsa-2027-experiments @ 6fc798cf, icsa/sandbox/gate.py:846-888 (a listing merged from its pages upstream)
#   changes: see the history of this file from its import commit
"""Policy gate: the only network peer the sandbox has (the sandbox network is internal).

Runs in its own container, as root with all capabilities but NET_BIND_SERVICE
dropped, attached to the sandbox's internal network and to a bridge to reach
the upstream. Configuration: JSON at the path in argv[1]:

  {"run_key": "...", "verify_ssl": true, "log": "/var/gate/requests.jsonl",
   "upstream_ca_file": "/etc/gate/federation-ca.pem", "tls_dir": "/etc/gate/tls",
   "listing": "paged|complete",
   "hosts": {"<name>": {"role": "onezone|oneprovider", "addr": "<upstream host>",
                        "port": 443, "tls": true}},
   "tcp_forwards": [{"listen": 8000, "host": "...", "port": 8000}]}

`listing` is the data path's listing mode (gatepolicy, O21), `paged` when absent; a gate given another refuses to
start. In `complete` the gate answers a listing with all of the directory's entries as one page, which it merges from
the pages it reads upstream (`Handler._complete_listing`): each page passes the response boundary as any answer that is
no file's content does, the merged answer is checked once more before any byte of it goes, and a listing it cannot
complete within its bounds, or that gives an entry twice, is refused rather than passed on in part. The answer is the
directory's whole listing only if no other client changed the directory while it was read: an entry removed meanwhile
can be missing unseen (gatepolicy, "A known limit").

For each host name the sandbox resolves (via --add-host) to the gate, the gate
terminates TLS with a certificate for that name (signed by the run's CA, which
only the sandbox trusts), applies gatepolicy, and forwards to the real upstream,
checking its certificate as the platform's [tls] says: against the image's CAs
by default, against `upstream_ca_file` alone (the platform's CA bundle, mounted
read-only), or not at all when `verify_ssl` is false. `tcp_forwards` are plain TCP
pass-through for the knowledge plane. Every request is logged as one JSON line.

The gate holds the data-path token (from its environment, never from the configuration file or an argument) and adds it
to every request it forwards; an `X-Auth-Token`, `Authorization` or `Cookie` header the client sent is dropped, and a
token-scope inference carries the gate's token in its body. So the agent's code holds no credential at all (O05). Every
answer passes the response boundary (`gateboundary.py`) on its way back, so none hands the token to the agent: a redirect
or an encoded body becomes a 502 and is never followed or passed on, a header holding the token in any form makes the
answer a 502, a credential header without it is withheld, and a body in which the token appears is cut before any byte
of it goes. The boundary detects; it never rewrites an answer.

This file runs alone in the gate container with the standard library only; it imports nothing of the platform layer
(tests/onedata/sandbox/test_gate.py checks that). It passes only the reads that gatepolicy lists, so the data path
through the gate is read-only whatever the client asks (O05). A request it refuses is answered 4xx and logged as a
decision, never as an error; an error the gate makes itself is logged with origin `gate`, so a run can tell it from the
platform's (O16).

Connections are kept alive on both sides: each request is still decided by gatepolicy on its own, and logged on its
own, whatever came before it on the connection; a refusal or an error closes the client's connection. Each client
connection is served by its own thread and owns its upstream connections: nothing is shared between threads but the
TLS context and the log.

Every line the handler of a client connection logs carries that connection's `conn` (unique in this process), and a request's line carries
`req_headers`: the few request headers that say what client it was (REQ_HEADERS_LOGGED), never any other. A response's line says what its body was
(`body`): `content` (the platform's 200 or 206 to a read of a file's content), `listing` (its 200 to a listing, or the merged answer), `other` (any other
answer of the platform's, passed on) or `gate` (the gate's own answer: a refusal, an error); the per-run count of the bytes served reads it (gatelog, O22). A 206 of a file's content
also carries the platform's `Content-Range` (`content_range`), which says where in the file its bytes begin, for the count per file. A request's `host` is the role the
gate serves that host in (`onezone`, `oneprovider`), or `not_configured`, never the host's configured name (O12). When a connection ends after a response without a
request following, one `conn_close` event line says how (see `Handler._peer_gone`), with the kernel's counters of that connection at that moment: a response's
`body_bytes` counts what the gate's socket accepted, which says nothing of what the client read, and the close is what shows it. When the gate itself is stopped
(SIGTERM or SIGINT: `serve`; or the server closed: `GateServer.server_close`), every connection still open gets that line with `ended` "teardown", before the process
exits (`teardown`), and then one `teardown_complete` line once every such line is written; a gate that is killed (SIGKILL, `docker rm -f`) cannot write
either, and a log without `teardown_complete` says so. SIGUSR1 is the harness's mark of where the agent's run begins: one `run_start` line naming every request
begun and not yet logged (`mark_run_start`), so the count of the bytes served keeps the harness's own requests apart (O22).

Errors and retries, identical on every path: an upstream attempt that fails before any byte of
an answer arrives (connect, handshake, send, or the peer closing first) is retried once, on a new connection, before
anything reaches the client; nothing is retried after an answer began, and a read timeout is never retried. Every
failure is logged with its origin, `upstream` (the platform) or `gate` (the gate itself), and with what the client saw
(`5xx` or `reset`), so a run can tell an error the gate made from one the platform made.
"""

from __future__ import annotations

import contextlib
import errno
import http.client
import importlib.util
import io
import itertools
import json
import os
import select
import signal
import socket
import ssl
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def _load_sibling(name: str):
    """A sibling module (`gatepolicy.py`, `gateboundary.py`), loaded by its path: the gate runs alone in its container,
    where this directory is mounted and nothing is installed, so there is no package to import it from."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # a dataclass looks its module up by name while it is being built
    spec.loader.exec_module(module)
    return module


gatepolicy = _load_sibling("gatepolicy")
gateboundary = _load_sibling("gateboundary")  # every answer passes it before it reaches the agent (O05)

CFG: dict = {}
_LOCK = threading.Lock()
_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}
RUN_HEADER = "X-Run-Key"
TOKEN_ENV = (
    "GATE_ACCESS_TOKEN"  # the data-path token, in the gate's environment: the agent's code never holds one (O05)
)
AUTH_HEADER = "X-Auth-Token"
CLIENT_CREDENTIALS = {AUTH_HEADER.lower(), "authorization", "cookie"}  # whatever the agent sends of these is dropped
UPSTREAM_READ_BYTES = 1 << 20  # one read of an upstream body; a token split between two reads is still found
# How many answers the gate buffers at once (O05): an answer that is no file content is read whole, up to
# gateboundary.BUFFER_CAP (4 MiB), and checked before it goes. At most 16 such buffers are held, 64 MiB, and a check
# holds about four times its buffer at its peak (the raw bytes, a decoded layer, the next, a projection): 256 MiB. A
# complete listing (O21) reads each page in one of these slots, and one listing at a time is merged (_LISTING_SLOT),
# into at most gatepolicy.COMPLETE_LISTING_BYTES (64 MiB) of entries and tokens: its pieces, the merged answer and the
# check of it, about 6 x 64 = 384 MiB at the peak, and a 16-byte digest of each entry and token in a set, about 100
# bytes each with the set's own, 22 MB for the 220,000 entries 64 MiB holds. Together about 660 MiB, under the gate's
# memory limit (sandbox.limits.gate_memory_bytes, 1 GiB by default). An answer that must be buffered, or a listing to
# merge, waits for a slot: the wait is the agent's own concurrency, on the agent's time.
BUFFER_SLOTS = 16
_BUFFER_SLOTS = threading.BoundedSemaphore(BUFFER_SLOTS)
_LISTING_SLOT = threading.BoundedSemaphore(1)
# A buffered answer is written to the agent within this, or its connection is closed (the agent stopped reading): a
# client that holds its socket open without reading cannot keep a buffer slot, so it cannot stall the others.
CLIENT_WRITE_TIMEOUT_S = 60


def _log(rec: dict) -> None:
    rec["t"] = time.time()
    try:
        with _LOCK, open(CFG["log"], "a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError as e:  # never take the request down, but never be silent
        print("GATE LOG FAILED", e, file=sys.stderr, flush=True)


_LIVE: dict[
    str, Handler
] = {}  # the client connections being served, by `conn`: added when a handler is set up, removed when it finishes
_LIVE_LOCK = threading.Lock()
TEARDOWN_BUDGET_S = 1.0  # the most the gate waits for its teardown lines when it stops
_CONN_IDS = itertools.count(
    1
)  # next() is atomic: one id per client connection, unique in this process (its pid is in the id, for logs of several)
REQ_HEADERS_LOGGED = (
    "user-agent",
    "accept",
    "accept-encoding",
    "connection",
)  # a fixed allowlist: headers that carry no content, nothing else is ever logged
REQ_HEADER_MAX = 512
SIOCOUTQNSD = 0x894B  # ioctl: bytes the socket has queued and not yet sent (termios has SIOCOUTQ as TIOCOUTQ, not this)


def _logged_headers(headers: dict) -> dict:
    """The allowlisted request headers the request carried (names lower-case, as `headers` has them), and no others: a token, a cookie or any other header never
    reaches the log, since this is the only place a header is read for it. A value is cut at REQ_HEADER_MAX characters."""
    return {k: str(headers[k])[:REQ_HEADER_MAX] for k in REQ_HEADERS_LOGGED if k in headers}


def _tcp_facts(sock) -> dict:
    """The kernel's view of a TCP connection now: its state (`tcp_state`, 7 = CLOSE: a reset arrived, 8 = CLOSE_WAIT: a FIN), the bytes of the send queue not yet
    acknowledged (`outq_unacked`, SIOCOUTQ) and not yet sent (`outq_unsent`, SIOCOUTQNSD), and TCP_INFO's `bytes_acked`, `bytes_sent` and `bytes_retrans` (retransmissions
    are counted in `bytes_sent`). Each is None when the kernel does not give it: this never raises, and a field it cannot read is not a zero. The TCP_INFO offsets are
    those of `struct tcp_info`, which only grows at its end; a shorter buffer leaves the later fields None."""
    out = dict.fromkeys(("tcp_state", "outq_unacked", "outq_unsent", "bytes_acked", "bytes_sent", "bytes_retrans"))
    try:
        buf = sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 256)
    except (OSError, AttributeError, ValueError):
        buf = b""
    if buf:
        out["tcp_state"] = buf[0]
        for key, off in (("bytes_acked", 120), ("bytes_sent", 200), ("bytes_retrans", 208)):
            if len(buf) >= off + 8:
                out[key] = struct.unpack_from("=Q", buf, off)[0]
    try:
        import fcntl
        import termios

        for key, req in (("outq_unacked", termios.TIOCOUTQ), ("outq_unsent", SIOCOUTQNSD)):
            with contextlib.suppress(OSError, ValueError, AttributeError, struct.error):
                out[key] = struct.unpack("=i", fcntl.ioctl(sock.fileno(), req, b"\0\0\0\0"))[0]
    except ImportError:
        pass
    return out


# The gate must never give up before the client would. A client's effective timeouts can be 8 s to connect
# (TCP and TLS) and 4 h to read; behind the gate the client's 8 s covers only
# the hop to the gate, and it then waits up to 4 h for the answer. So upstream (Onezone and Oneprovider alike) the gate
# allows 60 s to connect and hand-shake, plus one transparent retry, and 4 h to read. An earlier gate with an 8 s
# bound returned a 502 when a TLS handshake to the zone ran out of it under load.
CONNECT_TIMEOUT = 60  # gate config `upstream_connect_timeout_s` overrides
READ_TIMEOUT = 14400  # not below the client's read timeout: the gate must never fail first
LOCAL_ERRNOS = {
    errno.EMFILE,
    errno.ENFILE,
    errno.ENOMEM,
    errno.ENOBUFS,
    errno.ENOSPC,
    errno.EDQUOT,
    errno.EROFS,
    errno.EADDRNOTAVAIL,
}  # the last: the gate's own ephemeral ports ran out


HANDSHAKE_TIMEOUT_S = 30  # a client's TLS handshake, in its own thread
IDLE_TIMEOUT_S = 300  # a client connection with no request for this long is closed (threads are capped by --pids-limit)
BODY_TIMEOUT_S = 60  # a request body must arrive within this; the allowed calls send a few KB at most
MAX_BODY = 1 << 20  # larger request bodies are refused unread (the allowed calls are lookups)
_CTX: dict[bool, ssl.SSLContext] = {}


def _upstream_ctx(verify: bool) -> ssl.SSLContext:
    """One client TLS context per process and verification mode: building one reloads the CA bundle (25 ms under load)."""
    with _LOCK:
        if verify not in _CTX:
            ctx = ssl.create_default_context(cafile=CFG.get("upstream_ca_file"))  # None: the image's system CAs
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            _CTX[verify] = ctx
        return _CTX[verify]


def _upstream_conn(h: dict) -> http.client.HTTPConnection:
    if h.get("tls", True):
        conn = http.client.HTTPSConnection(
            h["addr"],
            h.get("port", 443),
            context=_upstream_ctx(CFG.get("verify_ssl", True)),
            timeout=CFG.get("upstream_connect_timeout_s", CONNECT_TIMEOUT),
        )
    else:
        conn = http.client.HTTPConnection(
            h["addr"], h.get("port", 80), timeout=CFG.get("upstream_connect_timeout_s", CONNECT_TIMEOUT)
        )
    conn.connect()
    conn.sock.settimeout(READ_TIMEOUT)  # short connect, then a read timeout that outlasts the client's
    return conn


def _went_stale(conn: http.client.HTTPConnection) -> bool:
    """A kept connection with nothing owed to us: readable now means the peer closed it (FIN, RST or close_notify)."""
    if conn.sock is None:
        return True
    p = select.poll()
    p.register(conn.sock, select.POLLIN)
    return bool(p.poll(0))


class _CountingRaw(io.RawIOBase):
    """The socket below the response's buffer: every byte that arrives is counted as it arrives, so an answer that
    began and then failed (a partial status line, then a reset) is never taken for "no answer"."""

    def __init__(self, raw, seen: dict):
        super().__init__()
        self._raw, self._seen = raw, seen

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = self._raw.readinto(b)
        self._seen["bytes"] += n or 0
        return n

    def close(self) -> None:
        self._raw.close()
        super().close()


class _ProbedRaw(io.RawIOBase):
    """The socket below the request reader's buffer, with a switch: while `probing`, a read answers "nothing yet" (None, as a non-blocking socket would) without
    touching the socket, so `BufferedReader.peek` then shows what the buffer already holds and never reads. That is how the gate sees a request that is already
    in the handler's buffer, which neither `select` nor a peek at the kernel's queue can see."""

    def __init__(self, raw):
        super().__init__()
        self._raw, self.probing = raw, False

    def readable(self) -> bool:
        return True

    def readinto(self, b):
        return None if self.probing else self._raw.readinto(b)

    def close(self) -> None:
        self._raw.close()
        super().close()


class _LastLine:
    """Keeps the last line http.client read, to tell whether the header block reached its terminating blank line."""

    def __init__(self, fp, seen: dict):
        self._fp, self._seen = fp, seen

    def readline(self, *a):
        line = self._fp.readline(*a)
        self._seen["last_line"] = line
        return line

    def __getattr__(self, name):
        return getattr(self._fp, name)


class _CountingResponse(http.client.HTTPResponse):
    def __init__(self, sock, debuglevel=0, method=None, url=None, *, seen: dict):
        super().__init__(sock, debuglevel, method=method, url=url)
        unbuffered = sock.makefile("rb", buffering=0)  # its own reference to the socket, taken before the old one goes
        self.fp.close()
        self.fp = _LastLine(io.BufferedReader(_CountingRaw(unbuffered, seen)), seen)


class _Framing(Exception):
    """An upstream response whose head or framing cannot be trusted: never retried, answered with 502."""


def origin_of(e: BaseException) -> str:
    """Whose failure a failed upstream call is. The gate's: its own timer ran out (a direct client would have had at
    least as long), a local resource ran out (descriptors, memory, disk), or anything that is not a transport
    error (a bug). The platform's: the connection refused, reset or lost, a failed TLS handshake, a name that does not
    resolve, a cut or malformed answer."""
    if isinstance(e, TimeoutError):
        return "gate"
    if isinstance(e, OSError) and e.errno in LOCAL_ERRNOS:
        return "gate"
    if isinstance(e, (_Framing, http.client.HTTPException, ConnectionError, ssl.SSLError, socket.gaierror, OSError)):
        return "upstream"
    return "gate"


class _Upstream(Exception):
    """A failed upstream call; `origin` says whether the platform or the gate itself failed it (`origin_of`)."""

    def __init__(self, cause: BaseException):
        super().__init__(repr(cause))
        self.cause = cause
        self.origin = origin_of(cause)


_LOST = (
    ConnectionError,
    http.client.CannotSendRequest,
    http.client.RemoteDisconnected,
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
)


def _repeatable(e: BaseException, phase: str, answer_bytes: int) -> bool:
    """May this failed attempt be made once more? Never for a failure of the gate's own resources. Connecting (TCP,
    TLS handshake): any other OSError, a timeout included, since nothing was sent. Exchanging: only a connection lost
    (a TLS EOF included) with no byte of an answer received. A timeout while waiting for an answer is never repeatable:
    the request may still be running upstream, and a timed-out request is never sent twice."""
    if isinstance(e, OSError) and e.errno in LOCAL_ERRNOS:
        return False
    if phase == "connect":
        return isinstance(e, OSError)
    if answer_bytes or isinstance(e, TimeoutError):
        return False
    return isinstance(e, _LOST)


def _checked(resp: http.client.HTTPResponse, seen: dict) -> http.client.HTTPResponse:
    if seen.get("last_line") not in (b"\r\n", b"\n"):
        raise _Framing("the upstream response ended inside its header block")
    te = resp.getheader("Transfer-Encoding")
    cl = resp.getheader("Content-Length")
    if te is not None and cl is not None:
        raise _Framing("the upstream response has both Transfer-Encoding and Content-Length")
    if cl is not None and not resp.chunked and resp.length is None and resp.status not in (204, 304):
        raise _Framing(f"the upstream response has an invalid Content-Length {cl!r}")
    return resp


TLS_RECORD = 16384  # the most plaintext one TLS record carries: a client reads a record whole or not at all
# a complete listing's answer (O21): the pages' entries in one list, which is the last page and points to none
_MERGED_HEAD, _MERGED_JOIN, _MERGED_TAIL = b'{"children": [', b", ", b'], "isLast": true, "nextPageToken": null}'
_MERGED_HEADERS = [("Content-Type", "application/json")]


def _body_kind(tag: str | None, status: int) -> str:
    """What a body of the platform's that the gate passes on is, for its line (`body`) and the count of the bytes served
    (gatelog, O22): a file's content, a listing, or anything else."""
    if tag == "content" and status in (200, 206):
        return "content"
    if tag == "children" and status == 200:
        return "listing"
    return "other"


class _CountingWriter:
    """The client's side of a response, counted: what each write handed to the socket, and where the headers ended.

    `socketserver`'s writer is one `sendall` per write, which raises after sending part of the buffer without saying how
    much: a write that failed would be counted as nothing, and a log that holds less than the client received (a cancelled
    transfer, a reset) breaks amendment 22. So the progress is taken from the socket's own `send`, which returns what went out,
    and a failed write counts what it sent in `n` and the rest of itself in `failed`: what did not go out, so not confirmed.
    `total` is the body's length when the response declared one (its Content-Length), for the log's `body_bytes_unconfirmed`.
    Over TLS a `send` is one `SSL_write`, which raises with an unknown number of its records already out, so a TLS
    socket is written one record (16 KiB) at a time: the client cannot read a record that is not whole, so the count is
    never below what the client received. Without a socket to write through (`sock` None) a failed write is all `failed`."""

    def __init__(self, raw, sock=None):
        self.raw, self.sock, self.n, self.mark, self.failed, self.total = raw, sock, 0, None, 0, None
        self.step = TLS_RECORD if isinstance(sock, ssl.SSLSocket) else None

    def reset(self) -> None:
        self.n, self.mark, self.failed, self.total = 0, None, 0, None

    def write(self, b):
        if self.sock is None:
            try:
                r = self.raw.write(b)
            except BaseException:
                self.failed += len(b)
                raise
            self.n += len(b)
            return r
        view = memoryview(b).cast("B")
        total, sent = len(view), 0
        try:
            while sent < total:
                k = self.sock.send(view[sent : sent + self.step] if self.step else view[sent:])
                if not k:
                    raise ConnectionError("the client's socket took no bytes")
                sent += k
        except BaseException:
            self.n += sent
            self.failed += total - sent
            raise
        self.n += total
        return total

    def __getattr__(self, name):
        return getattr(self.raw, name)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "gate"
    disable_nagle_algorithm = (
        True  # headers and body are separate writes: keep-alive would stall on Nagle + delayed ACK
    )

    def log_message(self, *a):
        pass

    def setup(self) -> None:
        self._conn = (
            f"{os.getpid()}-{next(_CONN_IDS)}"  # first: whatever this handler logs, from here on, is this connection's
        )
        self._timed_out = False
        self._last_logged = 0  # the number of the last request line this connection wrote
        self._close_logged = False
        # the TLS handshake runs here, in this connection's own thread: done in accept() it would serialise every
        # client's handshake behind the slowest one, and a client that never finishes it would stall the gate
        if isinstance(self.request, ssl.SSLSocket):
            self.request.settimeout(HANDSHAKE_TIMEOUT_S)
            self.request.do_handshake()
        super().setup()
        self.wfile = self._w = _CountingWriter(self.wfile, self.connection)
        self._probe = _ProbedRaw(
            self.rfile.detach()
        )  # the buffer socketserver made, nothing read yet, rebuilt over a raw that can be probed
        self.rfile = io.BufferedReader(self._probe, io.DEFAULT_BUFFER_SIZE)  # the size `makefile` gives it
        self._pool: dict[tuple, http.client.HTTPConnection] = {}  # this client connection's upstream connections
        self._served = 0  # requests so far on this client connection
        self._response_started = False
        with _LIVE_LOCK:  # last: a handler whose setup failed was never live
            _LIVE[self._conn] = self

    def finish(self) -> None:
        with _LIVE_LOCK:
            _LIVE.pop(self._conn, None)
        for conn in self._pool.values():
            conn.close()
        self._pool.clear()
        super().finish()

    def handle_one_request(self) -> None:
        self.connection.settimeout(IDLE_TIMEOUT_S)  # only while waiting for a request: `_handle` lifts it
        self.raw_requestline = b""  # set by the parser once a request line has arrived
        self._w.reset()
        self._timed_out = False
        after = self._served
        if self._peer_gone():
            return
        super().handle_one_request()
        if after and not self.raw_requestline and not self._timed_out:
            # the TLS read came back empty after a response: the connection ended with no request, and the raw peek had taken the first bytes (a close_notify) for one
            self._conn_close("tls_close", after)

    def _input_waiting(self) -> bool:
        """Bytes already taken off the socket and not yet used: decrypted by TLS (`pending`) or in the request reader's buffer. The kernel's queue does not show them,
        so a request that is already here, pipelined behind the one just answered, would otherwise read as a quiet socket. Reads nothing."""
        try:
            if getattr(self.connection, "pending", lambda: 0)():
                return True
        except (OSError, ValueError):
            pass
        self._probe.probing = True
        try:
            return bool(self.rfile.peek(0))
        finally:
            self._probe.probing = False

    def _conn_close(self, ended: str, after: int, **extra) -> None:
        """Log how this connection ended after `after` responses, with the kernel's counters of it (`_tcp_facts`) taken now."""
        self._close_logged = True
        _log(
            {
                "event": "conn_close",
                "conn": self._conn,
                "after_request": after,
                "ended": ended,
                **_tcp_facts(self.connection),
                **extra,
            }
        )

    def _teardown_line(self) -> None:
        """The gate is stopping and this connection is open: its close line, `ended` "teardown", unless it has written one. `after_request` is the last request line
        it wrote; `in_request` says a request was read and not yet answered (its line will never be written), which is then what is outstanding."""
        if self._close_logged:
            return
        after = self._last_logged
        self._conn_close("teardown", after, in_request=self._served > after)

    def _peer_gone(self) -> bool:
        """After a response and before the next request: has the client gone? If so the line `conn_close` is logged and True is returned (the connection ends here).

        Only a connection that has served a response is looked at, and only before the TLS layer reads anything: through TLS, the next `readline` returns b'' both for a
        clean close and for a reset, and the counters change once it has read. A request already in hand (`_input_waiting`) is a request. Otherwise the gate waits for the
        socket to become readable, for at most IDLE_TIMEOUT_S as before (`ended` "idle_timeout": the connection closes, as it always did, and now says so), and then
        peeks at one byte of the kernel's queue without taking it: a byte is a request or a TLS alert (the caller goes on, and a TLS read that then returns nothing is
        `tls_close`), b'' is the client's FIN (`eof`), a reset is `reset`, any other error `error`. Nothing is sent and nothing consumed: what the client sees is unchanged."""
        after = self._served
        if not after or self._input_waiting():
            return False
        p = select.poll()
        p.register(self.connection, select.POLLIN)
        if not p.poll(IDLE_TIMEOUT_S * 1000):
            self._conn_close("idle_timeout", after)
            self.close_connection = True
            return True
        extra: dict = {}
        try:
            data = socket.socket.recv(
                self.connection, 1, socket.MSG_PEEK | socket.MSG_DONTWAIT
            )  # the socket's own recv, below the TLS layer
        except ConnectionResetError:
            ended = "reset"
        except (BlockingIOError, TimeoutError):
            return False  # readable and then not: let the ordinary read wait as it always did
        except OSError as e:
            ended, extra = "error", {"error": repr(e)[:300]}
        else:
            if data:
                return False
            ended = "eof"
        self._conn_close(ended, after, **extra)
        self.close_connection = True
        return True

    def log_error(self, fmt, *args) -> None:
        """http.server reports a timed-out request read here and nowhere else. A request whose line arrived but whose
        headers did not is logged as the client's; a connection idle between requests closes without a record."""
        msg = fmt % args if args else fmt
        if msg.startswith("Request timed out"):
            self._timed_out = True
        if msg.startswith("Request timed out") and self.raw_requestline:
            _log(
                {
                    "event": "request_error",
                    "conn": self._conn,
                    "status": None,
                    "reason": "request headers not received in time",
                    "error_origin": "client",
                    "client_saw": "close",
                    "request_line": self.raw_requestline[:200].decode("latin-1", "replace").strip(),
                }
            )

    def send_response_only(self, code, message=None) -> None:
        """A response's head begins, the final one after a `100 Continue` included: nothing is body until its headers
        are all written (`end_headers`), so a head whose write fails is never counted as body bytes (O22)."""
        self._w.mark = None
        super().send_response_only(code, message)

    def send_header(self, keyword, value) -> None:
        super().send_header(keyword, value)
        if keyword.lower() == "content-length":
            with contextlib.suppress(ValueError):
                self._w.total = int(value)  # the body's length, as the client is told it (the last one sent)

    def end_headers(self) -> None:
        super().end_headers()
        self._w.mark = self._w.n  # what follows is the body
        self._response_started = True

    def _log(self, rec: dict) -> None:
        """A request's line, with the body bytes its response handed to the client's socket (amendment 17(c): the record that stands in for Traefik's), on every
        response: a denial's, the gate's own error, a short or cut transfer.

        `body_bytes`: the bytes of this response's body that the gate's socket accepted, counting the partial progress of a write that failed
        (see `_CountingWriter`); never below what the client received. `body_bytes_unconfirmed`, present only when a write failed: the rest of that body's
        length that was not confirmed sent, the length declared (Content-Length) minus `body_bytes`, so that the two add up to the body's full length. A body
        with no declared length (chunked, or ended by the close) has no total to subtract from: it is the rest of the write that failed. A failed write of the
        headers is the body not sent at all."""
        w = self._w
        rec.setdefault("conn", self._conn)
        body = 0 if w.mark is None else w.n - w.mark
        rec["body_bytes"] = body
        if w.failed:
            in_body = w.failed if w.mark is not None else 0  # a header write's rest is not body
            total = w.total if w.total is not None else body + in_body
            rec["body_bytes_unconfirmed"] = max(total - body, in_body)
        _log(rec)
        # Only now, with the line written: a teardown that reads this connection between the response and its line
        # sees the request still outstanding (`in_request`), so the count is unknown rather than a zero (O22).
        if "conn_request" in rec:
            self._last_logged = rec["conn_request"]

    def _begin_request(self) -> bool:
        """Count a request this connection is about to answer, before anything is written; False, with the connection
        to close unanswered, once the server's stop has begun. The stop sets its flag before it reads the connections,
        so a request is either counted in time for the teardown to see it outstanding, or answered with nothing at all:
        no answer can go out unseen between the teardown's reading and its `teardown_complete` line (O22)."""
        self._served += 1
        stopping = getattr(self.server, "stopping", None)
        if stopping is not None and stopping.is_set():
            self.close_connection = True
            return False
        return True

    def send_error(self, code, message=None, explain=None) -> None:
        """Errors the request parser answers before `_handle` runs (a malformed request line or version, an unknown
        method, an oversized line): logged like every other request, as the client's own. `_handle` never calls this.
        Counted as a request like any other, so a stop that comes while it is answered sees it outstanding."""
        if not self._begin_request():
            return
        rec = {
            "conn_request": self._served,
            "event": "request_error",
            "status": code,
            "reason": message or "",
            "error_origin": "client",
            "client_saw": "5xx" if code >= 500 else "4xx",
            "request_line": (getattr(self, "raw_requestline", b"") or b"")[:200].decode("latin-1", "replace").strip(),
            "body": "gate",
        }
        try:
            super().send_error(code, message, explain)
        finally:
            self._log(rec)

    def _answer(self, rec: dict, code: int, obj: dict) -> None:
        """The gate's own answer, and then its line, so the line has the answer's body bytes; logged whatever the client did meanwhile."""
        rec["body"] = "gate"
        try:
            self._send_json(code, obj)
        finally:
            self._log(rec)

    def _send_json(self, code: int, obj: dict, *, close: bool = True) -> int:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        self.wfile.write(body)
        return len(body)

    # ------------------------------------------------------------ upstream, one request at a time
    def _drop(self, hcfg: dict) -> None:
        conn = self._pool.pop((hcfg["addr"], hcfg.get("port", 443)), None)
        if conn is not None:
            conn.close()

    def _read_body(self, n: int) -> tuple[bytes | None, int, str | None]:
        """(body, 0, None) or (None, status, reason): the whole body by one deadline, however slowly it trickles."""
        if not n:
            return None, 0, None
        deadline = time.monotonic() + BODY_TIMEOUT_S
        parts, got = [], 0
        while got < n:
            left = deadline - time.monotonic()
            if left <= 0:
                return None, 408, f"request body not received within {BODY_TIMEOUT_S} s"
            self.connection.settimeout(left)
            try:
                b = self.rfile.read1(min(n - got, 1 << 16))
            except TimeoutError:
                return None, 408, f"request body not received within {BODY_TIMEOUT_S} s"
            except OSError as e:
                return None, 400, f"request body cut short: {e!r}"
            if not b:
                return None, 400, "request body cut short"
            parts.append(b)
            got += len(b)
        return b"".join(parts), 0, None

    def _upstream(self, hcfg: dict, method: str, target: str, body, headers: dict, rec: dict):
        """(response) of one upstream request on this client connection's kept connection to that host.

        A kept connection found closed is replaced before anything is sent (`upstream_stale`). An attempt that then fails
        with no byte of an answer received is made once more on a new connection (`upstream_retries`), whether its
        connection was new or kept; that is the only retry, it happens before any byte reaches the client, and it is the
        same for every request. An answer begun (an interim 100 included), a read timeout, or a second failure is raised
        as an upstream failure."""
        key = (hcfg["addr"], hcfg.get("port", 443))
        conn = self._pool.get(key)
        if conn is not None and _went_stale(conn):
            self._drop(hcfg)
            rec["upstream_stale"] = rec.get("upstream_stale", 0) + 1
            conn = None
        for attempt in (1, 2):
            seen = {"bytes": 0}
            rec["upstream_conn"] = "reused" if conn is not None else "new"
            phase = "connect"
            try:
                if conn is None:
                    conn = self._pool[key] = _upstream_conn(hcfg)
                phase = "exchange"
                conn.response_class = lambda sock, *a, seen=seen, **kw: _CountingResponse(sock, *a, seen=seen, **kw)
                conn.request(method, target, body=body, headers=headers)
                return _checked(conn.getresponse(), seen)
            except _Framing as e:
                self._drop(hcfg)
                raise _Upstream(e) from e
            except Exception as e:
                self._pool.pop(key, None) if conn is None else self._drop(hcfg)
                if attempt == 2 or not _repeatable(e, phase, seen["bytes"]):
                    raise _Upstream(e) from e
                rec["upstream_retries"] = rec.get("upstream_retries", 0) + 1
                rec["upstream_retry_cause"] = repr(e)[:200]
                conn = None
        raise AssertionError("unreachable")

    def _settle(self, hcfg: dict, resp) -> None:
        """After a response is read to its end: the connection goes back to the pool unless the peer is closing it."""
        if resp.will_close:
            self._drop(hcfg)

    def _abort_client(self) -> None:
        """End the client's connection with a reset: a body already begun must not look complete."""
        self.close_connection = True
        try:
            # linger 0 makes close() a reset; closing here comes before socketserver's shutdown, whose FIN would look clean
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            self.connection.close()
        except OSError:
            pass

    def _handle(self) -> None:
        self.connection.settimeout(BODY_TIMEOUT_S)  # the head has arrived; the body gets a bounded time
        if not self._begin_request():
            return
        self._response_started = False
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        hcfg = CFG["hosts"].get(host)
        url = urlsplit(self.path)
        headers = {k.lower(): v for k, v in self.headers.items()}
        rec = {
            "host": hcfg["role"] if hcfg is not None else "not_configured",  # the role it is served in, never its name
            "method": self.command,
            "path": url.path,
            "query": url.query,
            "range": headers.get("range"),
            "conn": self._conn,
            "conn_request": self._served,
            "req_headers": _logged_headers(headers),
        }
        n = headers.get("content-length") or "0"
        if "transfer-encoding" in headers or not n.isdecimal():
            # only a declared length frames a body here: anything else could leave bytes to be read as the next request
            rec.update(decision="deny", reason="request body framing not allowed", status=400)
            self._answer(rec, 400, {"error": {"id": "badRequest", "details": "request body framing not allowed"}})
            return
        if int(n) > MAX_BODY:
            rec.update(decision="deny", reason="request body too large", status=413)
            self._answer(rec, 413, {"error": {"id": "badRequest", "details": "request body too large"}})
            return
        body, code, why = self._read_body(int(n))
        if why is not None:
            rec.update(decision="deny", reason=why, status=code)
            try:
                self._answer(rec, rec["status"], {"error": {"id": "badRequest", "details": why}})
            except OSError:
                self.close_connection = True
            return
        self.connection.settimeout(None)  # the upstream's own timeouts bound the rest
        if hcfg is None:
            rec.update(decision="deny", reason="host not allowed", status=403)
            self._answer(rec, 403, {"error": {"id": "forbidden", "details": "host not allowed"}})
            return
        listing = CFG.get("listing", gatepolicy.PAGED)  # a listing's limit: O05; its mode: O21
        dec = gatepolicy.decide(hcfg["role"], self.command, url.path, url.query, body, listing=listing)
        rec.update(decision="allow" if dec.allow else "deny", reason=dec.reason, tag=dec.tag)
        if not dec.allow:
            rec["status"] = dec.status
            self._answer(
                rec,
                dec.status,
                {"error": {"id": "forbidden" if dec.status == 403 else "badRequest", "details": dec.reason}},
            )
            return
        if dec.tag == "scope" and CFG.get("token"):
            body = self._with_gate_token(body)
            if body is None:
                rec.update(decision="deny", reason="a scope request body must be a JSON object", status=400)
                self._answer(rec, 400, {"error": {"id": "badRequest", "details": rec["reason"]}})
                return
        try:
            if dec.merge_pages:
                self._complete_listing(hcfg, url, headers, body, rec)
            else:
                self._forward(hcfg, url, headers, body, rec)
        except (ConnectionError, ssl.SSLError) as e:
            # unwrapped connection and TLS errors come only from the client's socket (upstream ones arrive as
            # _Upstream): the client went away, for instance while the headers were written; its own doing, not an error
            self._drop(hcfg)
            rec.update(client_aborted=True, error=repr(e)[:300])
            rec.setdefault("status", None)
            self._log(rec)
            self.close_connection = True
        except Exception as e:
            self._drop(hcfg)  # its state is unknown: it is never reused
            origin = e.origin if isinstance(e, _Upstream) else "gate"
            rec.update(status=502, error=repr(e.cause if isinstance(e, _Upstream) else e)[:500], error_origin=origin)
            if self._response_started:  # a 502 cannot follow a response already begun: its body must not look complete
                rec["client_saw"] = "reset"
                self._log(rec)
                self._abort_client()
                return
            rec["client_saw"] = "5xx"
            details = str(e)
            if gateboundary.holds_token(details.encode(errors="replace"), CFG.get("token")):  # an upstream's echo
                details = gateboundary.CREDENTIAL_DETAILS
            with contextlib.suppress(OSError):
                self._answer(rec, 502, {"error": {"id": "badGateway", "details": details}})

    def _fwd_headers(self, headers: dict) -> dict:
        # the run attribution is the gate's: whatever the agent sent under that name is dropped, then ours is added
        out = {
            k: v
            for k, v in headers.items()
            if k not in _HOP and k != RUN_HEADER.lower() and k not in CLIENT_CREDENTIALS and k != "accept-encoding"
        }
        out["Host"] = headers.get("host", "")
        out["Accept-Encoding"] = "identity"  # a body the boundary can read: never compressed, whatever the agent asked
        if CFG.get("run_key"):
            out[RUN_HEADER] = CFG["run_key"]
        if CFG.get("token"):  # the credential is the gate's: the agent holds none, and any it sent is gone (O05)
            out[AUTH_HEADER] = CFG["token"]
        return out

    @staticmethod
    def _with_gate_token(body: bytes | None) -> bytes | None:
        """The body of a token-scope inference with the gate's token in it, whatever the client put there; None when
        the body is not a JSON object. The call names the token it asks about in its body, and the agent has none."""
        try:
            document = json.loads(body or b"{}")
        except ValueError:
            return None
        if not isinstance(document, dict):
            return None
        return json.dumps({**document, "token": CFG["token"]}).encode()

    def _refuse_answer(self, hcfg, resp, rec, *, error: str, details: str, refusal: str | None = None) -> None:
        """An answer the gate does not pass on (O05): the upstream connection goes unread, and the agent gets a 502 with
        a fixed message before any byte of the answer. `refusal` names the boundary's refusal in the line
        (`gate_refusal`): a redirect, an answer the gate cannot look into, or one that holds its token."""
        self._drop(hcfg)
        rec.update(status=502, upstream_status=resp.status, error=error, error_origin="upstream", client_saw="5xx")
        if refusal is not None:
            rec["gate_refusal"] = refusal
        with contextlib.suppress(OSError):  # a client gone meanwhile: the line is logged all the same
            self._answer(rec, 502, {"error": {"id": "badGateway", "details": details}})

    def _forward(self, hcfg, url, headers, body, rec) -> None:
        target = url.path + ("?" + url.query if url.query else "")
        self._pass_on(hcfg, self._upstream(hcfg, self.command, target, body, self._fwd_headers(headers), rec), rec)

    def _screen(self, hcfg, resp, rec) -> list[tuple[str, str]] | None:
        """The boundary's checks of an answer's status and headers (O05): the headers that may go to the agent, or None
        when the answer was refused (answered with a 502 and logged). A redirect and an encoded body are refused, and so
        is a header that holds the token; a credential header without it is withheld."""
        if gateboundary.is_redirect(resp.status):  # its Location or its body may hold the token: never followed
            error, details = "the upstream answered with a redirect", gateboundary.REDIRECT_DETAILS
            self._refuse_answer(hcfg, resp, rec, error=error, details=details, refusal=gateboundary.REDIRECT_REFUSED)
            return None
        if not gateboundary.readable_encoding(resp.getheader("Content-Encoding"), resp.getheader("Transfer-Encoding")):
            error, details = "the upstream answered with an encoded body", gateboundary.ENCODED_DETAILS
            self._refuse_answer(hcfg, resp, rec, error=error, details=details, refusal=gateboundary.ENCODED_RESPONSE)
            return None
        token = CFG.get("token")
        passed, withheld = [], []
        for k, v in resp.getheaders():
            if k.lower() in _HOP:
                continue
            if gateboundary.holds_token(f"{k}: {v}".encode("latin-1", "replace"), token):  # no part of it goes
                error, details = (
                    "the upstream's answer held the gate's credential in a header",
                    gateboundary.CREDENTIAL_DETAILS,
                )
                self._refuse_answer(
                    hcfg, resp, rec, error=error, details=details, refusal=gateboundary.CREDENTIAL_IN_HEADER
                )
                return None
            if k.lower() in gateboundary.CREDENTIAL_HEADERS:
                withheld.append(k.lower())  # a credential of the upstream's, never the agent's
            else:
                passed.append((k, v))
        if withheld:
            rec["withheld_headers"] = sorted(set(withheld))  # their names only
        return passed

    def _pass_on(self, hcfg, resp, rec) -> None:
        """The platform's answer, through the boundary, to the agent as it came: a file's content streamed, anything else
        read whole and checked first."""
        passed = self._screen(hcfg, resp, rec)
        if passed is None:
            return
        token, kind = CFG.get("token"), _body_kind(rec.get("tag"), resp.status)
        if kind == "content" and resp.status == 206:  # where in the file its bytes begin, as the platform says (O22)
            rec["content_range"] = (resp.getheader("Content-Range") or "")[:REQ_HEADER_MAX] or None
        # the length http.client frames the body by (None: chunked, or until EOF), never the raw header; the boundary
        # never rewrites a body, so it stays true
        declared = None if resp.chunked else resp.length
        if gateboundary.streams(resp.status, rec.get("tag"), resp.getheader("Content-Type")):
            rec["body"] = kind
            declared = self._send_head(resp, passed, declared)
            sent, failure, failure_origin = self._stream(resp, rec, token)
        else:  # no file's content: whole, and checked before any byte goes, in one of the shared buffer slots
            with self._buffer_slot(rec):
                body = self._buffered(hcfg, resp, rec, declared, token)
                if body is None:
                    return  # refused: answered and logged
                declared, sent, failure, failure_origin = self._send_whole(resp, passed, body, rec, kind)
                del body  # the slot is free once its buffer is gone
        self._finish(hcfg, resp, rec, sent, declared, failure, failure_origin)

    @contextlib.contextmanager
    def _buffer_slot(self, rec):
        """One of the shared buffer slots, held for the block; a wait for it is recorded (`buffer_wait_s`), summed over
        the pages of a complete listing."""
        waited = time.monotonic()
        with _BUFFER_SLOTS:
            if (wait := time.monotonic() - waited) > 0.001:  # the agent's own concurrency: other answers held the slots
                rec["buffer_wait_s"] = round(rec.get("buffer_wait_s", 0) + wait, 3)
            yield

    def _send_whole(self, resp, passed, body: bytes, rec, kind: str) -> tuple[int | None, int, str | None, str]:
        """A checked body and its head, written within CLIENT_WRITE_TIMEOUT_S: (length framed, bytes sent, failure, its
        origin). A client that stops reading frees its slot: its connection is closed (`client_stalled`)."""
        self.connection.settimeout(CLIENT_WRITE_TIMEOUT_S)
        rec["body"] = kind
        try:
            declared = self._send_head(resp, passed, len(body))
            sent, failure, failure_origin = self._write_whole(body, rec)
        except TimeoutError:  # the head did not go: the agent stopped reading
            rec["client_stalled"] = rec["client_aborted"] = True
            self.close_connection = True
            declared, sent, failure, failure_origin = len(body), 0, None, "upstream"
        finally:
            self.connection.settimeout(None)
        return declared, sent, failure, failure_origin

    def _complete_listing(self, hcfg, url, headers, body, rec) -> None:
        """A listing in the complete mode (O21): every page of the directory, read upstream in the server's order and
        answered as one page, or nothing of it.

        The gate asks for pages of `gatepolicy.MAX_LIST_LIMIT` with the agent's other options (`complete_listing_request`)
        and goes on while a page says it is not the last (`gatepolicy.ListingWalk`). The first page's answer that is not
        a 200 (the directory does not exist, say) is passed on as the paged mode passes it. After that, a page that is
        not a 200 or that the listing cannot go on from (an entry it gave before among them) is the platform's failure
        (a 502 of origin `upstream`), and a listing whose entries and tokens pass `COMPLETE_LISTING_BYTES`, or that
        passes `COMPLETE_LISTING_PAGES`, is refused for the gate's integrity (`listing_too_large`). Each page passes the
        boundary as any answer that is no file's content does, read whole in a buffer slot and checked, and the merged
        answer is checked once more. The answer is the directory's whole listing when the directory did not change while
        it was read; an entry removed meanwhile can be missing from it unseen (`gatepolicy`, "A known limit")."""
        pairs, body = gatepolicy.complete_listing_request(url.query, body)
        fwd, token = self._fwd_headers(headers), CFG.get("token")
        pieces: list[bytes] = []
        size, entries, walk, after = len(_MERGED_HEAD) + len(_MERGED_TAIL), 0, gatepolicy.ListingWalk(), None
        rec.update(complete_listing=True, pages=0)
        waited = time.monotonic()
        with _LISTING_SLOT:
            if (wait := time.monotonic() - waited) > 0.001:  # another complete listing was being merged
                rec["listing_wait_s"] = round(wait, 3)
            while True:
                target = gatepolicy.page_target(url.path, pairs, after)
                resp = self._upstream(hcfg, self.command, target, body, fwd, rec)
                if resp.status != 200 and not rec["pages"]:
                    self._pass_on(hcfg, resp, rec)  # the platform's answer to the agent's own listing
                    return
                if resp.status != 200:
                    raise _Upstream(_Framing(f"page {rec['pages'] + 1} of a complete listing answered {resp.status}"))
                if self._screen(hcfg, resp, rec) is None:
                    return
                with self._buffer_slot(rec):
                    data = self._buffered(hcfg, resp, rec, None if resp.chunked else resp.length, token)
                    if data is None:
                        return  # refused: answered and logged
                    self._settle(hcfg, resp)
                    try:
                        children, after = walk.page(data)
                    except ValueError as e:
                        raise _Upstream(_Framing(str(e))) from e
                    piece = json.dumps(children).encode()[1:-1]  # the entries without the brackets: ASCII JSON
                rec["pages"] += 1
                entries += len(children)
                if piece:
                    size += len(piece) + (len(_MERGED_JOIN) if pieces else 0)
                    pieces.append(piece)
                held = size + walk.token_bytes  # the tokens count too: empty pages with long tokens are not free
                bound = "bytes" if held > gatepolicy.COMPLETE_LISTING_BYTES else None
                if bound is None and after is not None and rec["pages"] >= gatepolicy.COMPLETE_LISTING_PAGES:
                    bound = "pages"
                if bound is not None:
                    rec["listing_bound"] = bound
                    error, details = "a complete listing is past the gate's bound", gateboundary.LISTING_DETAILS
                    self._refuse_answer(
                        hcfg, resp, rec, error=error, details=details, refusal=gateboundary.LISTING_TOO_LARGE
                    )
                    return
                if after is None:
                    break
            merged = _MERGED_HEAD + _MERGED_JOIN.join(pieces) + _MERGED_TAIL
            del pieces
            rec["entries"] = entries
            if gateboundary.holds_token(merged, token):  # the pages were checked; their joins are checked here
                error, details = "the merged listing held the gate's credential", gateboundary.CREDENTIAL_DETAILS
                self._refuse_answer(
                    hcfg, resp, rec, error=error, details=details, refusal=gateboundary.CREDENTIAL_IN_RESPONSE
                )
                return
            declared, sent, failure, failure_origin = self._send_whole(resp, _MERGED_HEADERS, merged, rec, "listing")
            del merged
        self._finish(hcfg, resp, rec, sent, declared, failure, failure_origin)

    def _buffered(self, hcfg, resp, rec, declared, token) -> bytes | None:
        """An answer that is no file's content, read whole (up to `gateboundary.BUFFER_CAP`) and checked with the full
        detector before any byte of it goes (O05): the body, or None when it was refused with a 502 (over the cap,
        `oversized_response`; holding the token, `credential_in_response`). The answers that echo a request, an error or
        a file's metadata, are small. A read that fails is the upstream's error, answered as one (`_handle`)."""
        body = bytearray()
        while True:
            try:  # never more than one byte over the cap, so a refused answer is not held whole
                chunk = resp.read(min(UPSTREAM_READ_BYTES, gateboundary.BUFFER_CAP + 1 - len(body)))
            except (OSError, http.client.HTTPException) as e:
                raise _Upstream(e) from e
            if not chunk:
                break
            body += chunk
            if len(body) > gateboundary.BUFFER_CAP:
                error, details = "the upstream's answer is larger than the gate buffers", gateboundary.OVERSIZED_DETAILS
                self._refuse_answer(
                    hcfg, resp, rec, error=error, details=details, refusal=gateboundary.OVERSIZED_RESPONSE
                )
                return None
        if declared is not None and len(body) != declared:  # http.client does not raise on an early EOF here
            raise _Upstream(http.client.IncompleteRead(bytes(body[:0]), declared - len(body)))
        body = bytes(body)  # one copy of it from here on
        if gateboundary.holds_token(body, token):
            error, details = (
                "the upstream's answer held the gate's credential in its body",
                gateboundary.CREDENTIAL_DETAILS,
            )
            self._refuse_answer(
                hcfg, resp, rec, error=error, details=details, refusal=gateboundary.CREDENTIAL_IN_RESPONSE
            )
            return None
        return body

    def _send_head(self, resp, passed, declared) -> int | None:
        """The status and the headers that passed, framed by the body's length when it is known; the length framed."""
        self.send_response(resp.status)
        for k, v in passed:
            self.send_header(k, v)
        if resp.status in (204, 304):
            declared = 0  # no body by definition, nothing to frame
        elif declared is not None:
            self.send_header(
                "Content-Length", str(declared)
            )  # a framed body: this connection can carry the next request
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        return declared

    def _write_whole(self, body: bytes, rec) -> tuple[int, str | None, str]:
        """A checked body, in one write: (bytes sent, failure, its origin)."""
        try:
            self.wfile.write(body)
        except OSError as e:
            if e.errno in LOCAL_ERRNOS:  # the gate's own resources failed the write, not the client
                return 0, repr(e), "gate"
            rec["client_aborted"] = True
            if isinstance(e, TimeoutError):  # the agent stopped reading: its connection goes, its slot is freed
                rec["client_stalled"] = True
                self.close_connection = True
            return 0, None, "upstream"
        return len(body), None, "upstream"

    def _stream(self, resp, rec, token) -> tuple[int, str | None, str]:
        """A file's content, streamed through the body watch (the boundary's second layer): (bytes sent, failure, its
        origin). The token is minted for the run after the study's data was provisioned, and the data path is
        read-only, so a file's content cannot hold it; the watch is defence in depth, with the bound `held_back`
        gives it."""
        sent, failure, failure_origin = 0, None, "upstream"
        watch = gateboundary.BodyWatch(token)  # holds back what could begin the token until the next read shows it
        while True:
            try:
                chunk = resp.read(UPSTREAM_READ_BYTES)
            except (OSError, http.client.HTTPException) as e:
                failure, failure_origin = repr(e), origin_of(e)  # the gate's own read timeout is the gate's
                break
            out = watch.feed(chunk) if chunk else watch.flush()  # at the end, what was held back
            if out is None:  # the token, in some form: nothing more of this answer reaches the agent (O05)
                failure = "the upstream's answer held the gate's credential in its body"
                rec["cut_reason"] = rec["gate_refusal"] = gateboundary.CREDENTIAL_IN_RESPONSE
                break
            try:
                self.wfile.write(out)
            except OSError as e:
                if e.errno in LOCAL_ERRNOS:  # the gate's own resources failed the write, not the client
                    failure, failure_origin = repr(e), "gate"
                else:
                    rec["client_aborted"] = True
                break
            sent += len(out)
            if not chunk:
                break
        return sent, failure, failure_origin

    def _finish(self, hcfg, resp, rec, sent, declared, failure, failure_origin) -> None:
        """The end of an answer begun: a transfer cut is marked with the bytes the client never got (O16), and the
        line is logged."""
        if failure is None and not rec.get("client_aborted") and declared is not None and sent != declared:
            failure = f"upstream sent {sent} of {declared} bytes"  # http.client does not raise on an early EOF here
        if failure or rec.get("client_aborted"):
            rec["cut"] = (
                True  # a transfer the gate did not finish (O16): the bytes the client never got are on the line
            )
            if declared is not None:
                rec["unread_bytes"] = max(declared - sent, 0)
            self._drop(hcfg)
            self._abort_client()
        else:
            self._settle(hcfg, resp)
        if failure:
            rec.update(
                status=502,
                upstream_status=resp.status,
                bytes=sent,
                error=failure,
                error_origin=failure_origin,
                client_saw="reset",
            )
        else:
            rec.update(status=resp.status, bytes=sent)
            if resp.status >= 500:
                rec.update(error_origin="upstream", client_saw="5xx")  # the platform's own 5xx, passed on as it was
        self._log(rec)

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = do_TRACE = do_CONNECT = _handle


TEARDOWN_COMPLETE = "teardown_complete"  # the log's last word on a gate that stopped with every close line written


def _teardown_lines() -> None:
    with _LIVE_LOCK:
        live = list(_LIVE.values())
    failed = 0
    for h in live:
        try:
            h._teardown_line()
        except Exception as e:
            failed += 1
            print("GATE TEARDOWN LINE FAILED", repr(e)[:200], file=sys.stderr, flush=True)
    if not failed:  # the positive mark the count of the bytes served needs (O22): without it, a line may be missing
        _log({"event": TEARDOWN_COMPLETE, "connections": len(live)})


def teardown() -> None:
    """The gate is stopping: a `conn_close` line (`ended` "teardown") for every client connection still open, before the process exits, and then one
    `teardown_complete` line, written only when every close line was. Never raises, and never waits more than TEARDOWN_BUDGET_S: the lines are written by a
    thread this waits for that long, so a log that is stuck cannot hold the gate up. A gate killed before it gets here (SIGKILL, its memory limit) leaves no
    `teardown_complete` line, and the count of the bytes it served is then unknown (gatelog, O22)."""
    try:
        t = threading.Thread(target=_teardown_lines, daemon=True)
        t.start()
        t.join(TEARDOWN_BUDGET_S)
        if t.is_alive():
            print("GATE TEARDOWN INCOMPLETE", file=sys.stderr, flush=True)
    except Exception as e:
        print("GATE TEARDOWN FAILED", repr(e)[:200], file=sys.stderr, flush=True)


RUN_START = "run_start"  # the harness's mark: the agent's run begins after it (gatelog, O22)


def mark_run_start() -> None:
    """The harness has ended its own steps and the agent's loop is about to start (SIGUSR1, `_on_run_start`): one
    `run_start` line. Every open connection is read first, and a request one has begun and not yet logged is named in
    `before` by its `conn` and `conn_request`, so its line, which comes after the mark, still counts as before it; a
    request begun after that reading is the run's. Never raises: a mark that could not be written is missing from the
    log, and the run's count of the bytes served is then unknown (gatelog, O22)."""
    try:
        with _LIVE_LOCK:
            live = list(_LIVE.values())
        before = [[h._conn, n] for h in live for n in range(h._last_logged + 1, h._served + 1)]
        _log({"event": RUN_START, "connections": len(live), "before": before})
    except Exception as e:
        print("GATE RUN START FAILED", repr(e)[:200], file=sys.stderr, flush=True)


def _on_run_start(signum, frame) -> None:
    """SIGUSR1. The mark is written by a thread of its own: the main thread may hold the log's lock when it comes."""
    threading.Thread(target=mark_run_start, daemon=True).start()


class GateServer(ThreadingHTTPServer):
    """One thread per client connection; a deep accept queue for bursts of new connections (the default is 5; gate
    config `listen_backlog` overrides). Closing it is the gate's stop: the connections still open get their teardown lines first (`teardown`)."""

    request_queue_size = 256
    daemon_threads = True

    def __init__(self, *args, **kwargs) -> None:
        # Set when this server's stop begins, before its teardown reads the connections (`Handler._begin_request`).
        self.stopping = threading.Event()
        super().__init__(*args, **kwargs)

    def server_close(self) -> None:
        self.stopping.set()  # first: a request counted after this is closed unanswered, one counted before is seen
        try:
            teardown()
        finally:
            super().server_close()

    def handle_error(self, request, client_address) -> None:
        """A connection that failed outside a request (a TLS handshake, a thread that could not start): logged, never
        silent. Its origin is the gate unless the client broke off its own handshake."""
        import traceback

        e = sys.exc_info()[1]
        client = isinstance(e, (ssl.SSLError, ConnectionError, TimeoutError, socket.timeout))
        _log(
            {
                "event": "connection_error",
                "error": repr(e)[:300],
                "error_origin": "client" if client else "gate",
                "client_saw": "reset",
                "trace": traceback.format_exc(limit=3)[-600:],
            }
        )


def _tls_context() -> ssl.SSLContext:
    ctxs = {}
    for name in CFG["hosts"]:
        c = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        c.load_cert_chain(os.path.join(CFG["tls_dir"], f"{name}.pem"), os.path.join(CFG["tls_dir"], f"{name}.key"))
        ctxs[name] = c
    first = next(iter(ctxs.values()))

    def pick(sock, server_name, _ctx):
        sock.context = ctxs.get((server_name or "").lower(), first)

    first.sni_callback = pick
    return first


def _pipe(a, b, sides: tuple[str, str], state: dict, listen: int) -> None:
    """Copy a -> b for the knowledge-plane forward. The first failure of a connection pair is logged once: a local
    resource error is the gate's, otherwise the side that failed decides (the platform's side, or the client's own);
    the other direction then fails only because this one shut both down, and is not logged."""
    failed = None
    try:
        while True:
            try:
                d = a.recv(65536)
            except OSError as e:
                failed = (sides[0], e)
                break
            if not d:
                break
            try:
                b.sendall(d)
            except OSError as e:
                failed = (sides[1], e)
                break
    finally:
        with _LOCK:
            first = not state["closing"]
            state["closing"] = True
        if failed and first:
            side, e = failed
            origin = "gate" if e.errno in LOCAL_ERRNOS else ("upstream" if side == "upstream" else "client")
            _log(
                {
                    "event": "tcp_forward_error",
                    "listen": listen,
                    "side": side,
                    "error": repr(e)[:300],
                    "error_origin": origin,
                    "client_saw": None if origin == "client" else "reset",
                }
            )
        for s_ in (a, b):
            with contextlib.suppress(OSError):
                s_.shutdown(socket.SHUT_RDWR)


def _tcp_forward(listen: int, host: str, port: int) -> None:
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", listen))
    srv.listen(64)

    def serve():
        while True:  # nothing that happens to one connection may end the accept loop
            try:
                c, _ = srv.accept()
            except OSError as e:
                _log(
                    {
                        "event": "tcp_forward_error",
                        "listen": listen,
                        "error": repr(e)[:300],
                        "error_origin": "gate",
                        "client_saw": None,
                    }
                )
                time.sleep(0.1)
                continue
            u = None
            try:
                u = socket.create_connection(
                    (host, port), timeout=CFG.get("upstream_connect_timeout_s", CONNECT_TIMEOUT)
                )
                u.settimeout(None)  # the connect bound only: reading waits as long as the client does
                state = {"closing": False}
                threading.Thread(target=_pipe, args=(c, u, ("client", "upstream"), state, listen), daemon=True).start()
                threading.Thread(target=_pipe, args=(u, c, ("upstream", "client"), state, listen), daemon=True).start()
            except Exception as e:
                _log(
                    {
                        "event": "tcp_forward_error",
                        "listen": listen,
                        "error": repr(e)[:300],
                        "error_origin": origin_of(e) if isinstance(e, OSError) else "gate",
                        "client_saw": "reset",
                    }
                )
                for s_ in (c, u):
                    if s_ is not None:
                        with contextlib.suppress(OSError):
                            s_.shutdown(socket.SHUT_RDWR)
                        s_.close()

    threading.Thread(target=serve, daemon=True).start()


def serve(httpd) -> None:
    """Serve until SIGTERM or SIGINT (a container's PID 1 ignores SIGTERM unless it has a handler), then stop: close the server, which writes the teardown lines.
    The handler only asks a thread to end the loop: `shutdown` called from the thread that runs the loop would wait for itself."""

    def stop(signum, frame):
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main() -> None:
    with open(sys.argv[1]) as handle:
        CFG.update(json.load(handle))
    CFG["token"] = os.environ.pop(TOKEN_ENV, "")  # out of the environment as soon as it is read
    if not CFG["token"]:
        raise SystemExit("the gate has no token to add to the requests it forwards")
    if CFG.get("listing", gatepolicy.PAGED) not in gatepolicy.LISTINGS:  # O21: never a mode it would guess at
        raise SystemExit("the gate's listing mode is not one it knows")
    for f in CFG.get("tcp_forwards", []):
        _tcp_forward(f["listen"], f["host"], f["port"])
    GateServer.request_queue_size = int(CFG.get("listen_backlog", GateServer.request_queue_size))
    httpd = GateServer((CFG.get("listen_host", "0.0.0.0"), int(CFG.get("listen_port", 443))), Handler)
    httpd.socket = _tls_context().wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    # a gate that cannot log does not start, and one that started leaves a log, so a missing log is lost evidence, not zero errors
    with open(CFG["log"], "a"):
        pass
    # before "ready": unhandled, SIGUSR1 ends a process, and the container's PID 1 ignores it, so no mark is written (O22)
    signal.signal(signal.SIGUSR1, _on_run_start)
    print("gate ready", flush=True)
    serve(httpd)


if __name__ == "__main__":
    main()
