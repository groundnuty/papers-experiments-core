"""fake_couchbase.py: a loopback stand-in for Couchbase 4.5's REST, for the offline tests of the Couchbase start.

Its access rules are the image's, decoded from the image's ns_server beams (tools/dbgi.py on the Abst chunk):
- until an administrator exists (ns_config_auth.erl:60, "provisioned"), every caller, with or without
  credentials, is the administrator (menelaus_roles.erl:229-235): onepanel's own memoryQuota POST, which sends
  the administrator it is about to create, goes through;
- once one exists, a request with other credentials is a 401, and an anonymous one gets only bucket_sasl on each
  password-less bucket (menelaus_roles.erl:229-235, ns_config_auth.erl:226-228), which reads [pools] and nothing
  else (preconfigured_roles); a request it does not allow is a 401 (menelaus_auth.erl:321-336).
Endpoints (permissions from menelaus_web.erl's get_action): GET /pools and /pools/default ([pools] read; /pools
lists the default pool only once provisioned), GET /pools/default/buckets ([pools] read), GET /settings/web
([settings] read: {"port", "username"}, menelaus_web.erl:2373), POST /pools/default (memoryQuota),
/node/controller/setupServices (services=kv; 400 once provisioned), /settings/stats, /settings/web (username,
password, port=SAME: creates the administrator; 400 before the services are set, so a test sees cluster-init's
order) and /pools/default/buckets (name, ramQuotaMB; needs the memory quota; the bucket has no password, as
onepanel creates it). FAKE_CB_ANON_ADMIN=1 breaks the rules on purpose: an anonymous caller stays the
administrator after one exists, for the guard's negative test.
It opens its port only after DELAY seconds, so the polling of its clients is exercised, and serves for LIFETIME
seconds. Every request is logged on stdout ("request <method> <path>"), so a test can tell that none arrived.
FAKE_CB_LISTEN_FD=N: file descriptor N is a TCP socket its parent bound to 127.0.0.1:PORT and keeps bound, not
listening, until this process has it. After DELAY it listens on that socket instead of binding PORT itself, so
the port is never free between the parent's choice and the listen: a port that was only looked up and closed can
be handed to another process of the node in that gap (an EADDRINUSE here). The parent closes its own copy once
this process has started, so the listener ends with this process.
Usage: fake_couchbase.py PORT DELAY LIFETIME [USER:PASSWORD (start already provisioned)]
"""

import base64
import binascii
import http.server
import json
import os
import socket
import sys
import threading
import time
import urllib.parse

state = {"creds": None, "services": None, "quota": None, "buckets": []}
ANON_ADMIN = os.environ.get("FAKE_CB_ANON_ADMIN") == "1"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def identity(self):
        """'admin', 'anonymous', or None (credentials that are not the administrator's)."""
        if state["creds"] is None:
            return "admin"
        h = self.headers.get("Authorization")
        if h is None:
            return "admin" if ANON_ADMIN else "anonymous"
        if not h.startswith("Basic "):
            return None
        try:
            creds = base64.b64decode(h[6:], validate=True).decode()
        except (binascii.Error, UnicodeDecodeError):
            return None
        return "admin" if creds == state["creds"] else None

    def allowed(self, permission):
        who = self.identity()
        return who == "admin" or (who == "anonymous" and permission == "pools_read" and bool(state["buckets"]))

    def reply(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        if body is not None:
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_one_request(self):
        super().handle_one_request()
        if getattr(self, "command", None):
            print("request %s %s" % (self.command, self.path), flush=True)
            self.command = None

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        form = dict(urllib.parse.parse_qsl(body))
        if not self.allowed("admin"):
            return self.reply(401)
        if self.path == "/pools/default":
            if not form.get("memoryQuota", "").isdigit():
                return self.reply(400, ["memoryQuota"])
            state["quota"] = int(form["memoryQuota"])
            return self.reply(200)
        if self.path == "/settings/stats":
            return self.reply(200)
        if self.path == "/node/controller/setupServices":
            if state["creds"] is not None or form.get("services") != "kv":
                return self.reply(400, ["services"])
            state["services"] = "kv"
            return self.reply(200)
        if self.path in ("/pools/default/buckets", "/pools/default/buckets/"):  # couchbase-cli posts to the second
            if state["quota"] is None or not form.get("name") or not form.get("ramQuotaMB", "").isdigit():
                return self.reply(400, ["bucket"])
            state["buckets"].append(form["name"])
            return self.reply(202)
        if self.path != "/settings/web":
            return self.reply(404)
        if state["services"] is None:  # as cluster-init does it: services first
            return self.reply(400, ["services"])
        if form.get("port") != "SAME" or not form.get("username") or not form.get("password"):
            return self.reply(400, ["form"])
        state["creds"] = "%s:%s" % (form["username"], form["password"])
        self.reply(200)

    def do_GET(self):
        if self.path in ("/pools", "/pools/default", "/pools/default/buckets"):
            if not self.allowed("pools_read"):
                return self.reply(401)
            if self.path == "/pools":
                pools = [{"name": "default", "uri": "/pools/default"}] if state["creds"] else []
                return self.reply(200, {"pools": pools, "isAdminCreds": self.identity() == "admin"})
            if self.path == "/pools/default":
                return self.reply(200, {"name": "default"})
            return self.reply(200, [{"name": b, "authType": "sasl", "saslPassword": ""} for b in state["buckets"]])
        if self.path == "/settings/web":
            if not self.allowed("settings_read"):
                return self.reply(401)
            user = state["creds"].split(":", 1)[0] if state["creds"] else ""
            return self.reply(200, {"port": self.server.server_port, "username": user})
        self.reply(404)


def open_server(port):
    """The server on 127.0.0.1:port: on the socket the parent holds bound for it (FAKE_CB_LISTEN_FD), else a new one."""
    fd = os.environ.get("FAKE_CB_LISTEN_FD")
    if fd is None:
        return http.server.HTTPServer(("127.0.0.1", port), Handler)
    held = socket.socket(fileno=int(fd))
    if held.getsockname() != ("127.0.0.1", port):
        sys.exit("fake_couchbase: FAKE_CB_LISTEN_FD is bound to %s, not to 127.0.0.1:%d" % (held.getsockname(), port))
    server = http.server.HTTPServer(held.getsockname(), Handler, bind_and_activate=False)
    server.socket.close()  # the server made its own, which it would have bound
    server.socket = held
    server.server_port = port  # server_bind sets it; the handler of GET /settings/web reads it
    server.server_activate()  # listen()
    return server


def main():
    port, delay, lifetime = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    if len(sys.argv) > 4:
        state["creds"] = sys.argv[4]
        state["services"] = "kv"
    time.sleep(delay)
    server = open_server(port)
    threading.Timer(lifetime, server.shutdown).start()
    print("listening", flush=True)
    server.serve_forever()
    server.server_close()


if __name__ == "__main__":
    main()
