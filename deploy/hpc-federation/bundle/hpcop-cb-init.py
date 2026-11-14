"""hpcop-cb-init.py: the Couchbase guard, and the waits for our own listeners.

Runs inside the container. Couchbase 4.5 lets any REST caller act as its administrator until one exists
(/diag/eval runs Erlang). onepanel creates it, as the image intends: its deployment starts Couchbase, waits
until 8091 accepts a connection, sleeps couchbase_after_init_delay (prep lowers it from 4 s to 1 s), then
runs couchbase-cli cluster-init with this launch's random administrator, read from our 0600 overlay
(service_couchbase.erl:208-269), then bucket-create. Until then the port shim keeps 127.0.0.1:8091 to this
job's uid: loopback, and the owner check on every connection.
An earlier revision set the administrator here, before onepanel. That broke the deployment: cluster-init's
first request carries no valid credentials (`auth=(None, None)`, node.py:338, cluster_manager.py:310-314), so
a Couchbase that already has an administrator answers 401 and cluster-init exits 1 (live run 2).
Live run 3: couchbase-cli 4.5.1 refuses a password over 24 characters only by printing an error, and
cluster-init exited 0 without creating the administrator (node.py:26, 434-437), so Couchbase had none for its
whole life. With a password of 25 to 42 characters bucket-create would have worked too, and the deployment
would have gone on with no administrator. node-init.sh now generates 24; this guard fails the start, whatever
the cause, when Couchbase has no administrator once onepanel's Couchbase setup is done, or another one.

--watch SECRETS TIMEOUT PORT START_PID (hpcop-start.sh runs it in the background, before the stock launcher,
  with its own PID, which the exec into the stock start script keeps):
  looks at /proc/net/tcp and tcp6 every 5 ms until a listener of this uid holds 127.0.0.1:PORT, then asks every
  10 ms, without credentials, GET /pools (cluster-init's own first call) and, while that shows an unprovisioned
  node (200, no pool listed), GET /pools/default/buckets. Then:
  - a bucket while there is no administrator: onepanel's setup has run (cluster-init, then bucket-create) and
    left Couchbase without one: FAILED, at the next round of questions after bucket-create;
  - an administrator (401, or a pool listed): GET /settings/web must answer 200 to this launch's administrator
    and name it, and 401 without credentials: passed, otherwise FAILED. That endpoint needs {[settings], read},
    which an anonymous caller has only while Couchbase has no administrator (cb_guard in lib.sh has the
    sources); GET /pools/default would not do, as the password-less onedata bucket lets anyone read [pools];
  - our listener gone before an administrator, or none within TIMEOUT seconds, or an error of the guard
    itself: FAILED.
  It prints into run.log the window, from the first look that found our listener, with an upper bound from the
  port opening (the start of the last look that did not), and its verdict: "hpcop: Couchbase guard passed", or
  "hpcop: Couchbase guard FAILED: <cause>", after which it sends SIGTERM to START_PID if that is still its
  parent: the stock start script, whose trap stops every component. finish*.sh fails the start on FAILED, and
  when the deployment ends without "passed". It changes nothing in Couchbase, and never connects before our
  listener holds the port (the shim checks each connect as well). START_PID 0 (offline tests): signal nothing.
  Exit status 0 when passed, 2 when FAILED.
--wait-listener PORT TIMEOUT:
  exit 0 once a listener of this uid holds 127.0.0.1:PORT, 1 after TIMEOUT seconds: the launcher's wait for
  onepanel's 9443 before its first request.
The credentials come from 0600 files; nothing secret is on a command line or in the environment.
Python 3.8 (the image's).
"""
import base64
import http.client
import json
import os
import signal
import socket
import struct
import sys
import time

HOST = "127.0.0.1"
ASK = 0.01           # between REST questions, once our listener is there
LOOK = 0.005         # between looks at the socket tables
TCP_LISTEN, TCP_SYN_RECV = "0A", "03"
PASSED = "Couchbase guard passed"      # lib.sh: CB_GUARD_PASSED, CB_GUARD_FAILED
FAILED = "Couchbase guard FAILED"


def table_address(packed):
    """An address as /proc/net/tcp{,6} prints it: each 32-bit word in host order, in hex."""
    return "".join("%08X" % w for w in struct.unpack("=%dI" % (len(packed) // 4), packed))


# The addresses a listener can hold so that it takes 127.0.0.1 and nobody else can listen there:
# 127.0.0.1 or 0.0.0.0, as IPv4 sockets (tcp) or v4-mapped IPv6 ones (tcp6). Not [::]: it may be v6-only.
HOLDS = {
    "tcp": {table_address(socket.inet_aton(HOST)), table_address(bytes(4))},
    "tcp6": {table_address(socket.inet_pton(socket.AF_INET6, "::ffff:" + HOST)),
             table_address(socket.inet_pton(socket.AF_INET6, "::ffff:0.0.0.0"))},
}


def own_listener(port):
    """True if a listener of this uid holds 127.0.0.1:port. Reads only the listening part of each table:
    the kernel prints every listener before any connection."""
    me = os.getuid()
    for table, addresses in HOLDS.items():
        try:
            with open("/proc/net/" + table, encoding="ascii") as f:
                next(f, None)   # the header
                for line in f:
                    fields = line.split()
                    if len(fields) < 8:
                        continue
                    if fields[3] not in (TCP_LISTEN, TCP_SYN_RECV):
                        break
                    address, _, hexport = fields[1].partition(":")
                    if (fields[3] == TCP_LISTEN and int(hexport, 16) == port and address.upper() in addresses
                            and fields[7] == str(me)):
                        return True
        except (OSError, ValueError):
            continue
    return False


def wait_listener(port, timeout):
    end = time.monotonic() + timeout
    while not own_listener(port):
        if time.monotonic() > end:
            sys.exit("hpcop: no listener of this uid holds %s:%d after %.0f s" % (HOST, port, timeout))
        time.sleep(0.05)
    print("hpcop: a listener of this uid holds %s:%d" % (HOST, port), flush=True)


def read_secret(path):
    with open(path, encoding="ascii") as f:
        value = f.readline().strip()
    if not value:
        raise ValueError("%s is empty" % path)
    return value


def request(port, path, auth=None):
    """GET path: (status, the JSON body, or None when it is not JSON)."""
    headers = {"Host": "%s:%d" % (HOST, port)}
    if auth:
        # One line whatever the length: b64encode never breaks lines (couchbase-cli's encodestring does).
        headers["Authorization"] = "Basic " + base64.b64encode(("%s:%s" % auth).encode()).decode()
    conn = http.client.HTTPConnection(HOST, port, timeout=10)
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
    finally:
        conn.close()
    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        data = None
    return resp.status, data


def pool_count(data):
    pools = data.get("pools") if isinstance(data, dict) else None
    return len(pools) if isinstance(pools, list) else None


def say(message):
    print("hpcop: " + message, flush=True)


def fail(start_pid, cause):
    """The verdict first (finish*.sh reads it from run.log), then stop the service through the start script."""
    say("%s: %s; stopping the service" % (FAILED, cause))
    if start_pid <= 0:
        say("no start script to signal (START_PID %d)" % start_pid)
    elif os.getppid() != start_pid:
        say("the start script (pid %d) is no longer this guard's parent: not signalled; finish*.sh stops the "
            "service" % start_pid)
    else:
        try:
            os.kill(start_pid, signal.SIGTERM)
            say("SIGTERM sent to the start script (pid %d); its trap stops every component" % start_pid)
        except OSError as e:
            say("could not signal the start script (pid %d): %s; finish*.sh stops the service" % (start_pid, e))
    return 2


def wait_admin(port, timeout, start, start_pid):
    """From the first look that finds our listener to the first sign of an administrator, or a FAILED verdict.
    Returns (None, times) once Couchbase looks provisioned, or (exit status, None) after a verdict."""
    absent = None    # start of the last look that did not find our listener: the port opened after it
    while True:
        look = time.monotonic()
        if own_listener(port):
            break
        if look - start > timeout:
            return fail(start_pid, "no listener of this uid held %s:%d within %.0f s" % (HOST, port, timeout)), None
        absent = look
        time.sleep(LOOK)
    seen = time.monotonic()
    while True:
        now = time.monotonic()
        if now - start > timeout:
            return fail(start_pid, "Couchbase on %s:%d still had no administrator %d ms after it was first seen "
                        "listening (the guard waits %.0f s)" % (HOST, port, (now - seen) * 1000, timeout)), None
        if not own_listener(port):
            return fail(start_pid, "Couchbase stopped listening on %s:%d %d ms after it was first seen, before it "
                        "had an administrator" % (HOST, port, (now - seen) * 1000)), None
        try:
            status, data = request(port, "/pools")
            if status == 200 and pool_count(data) == 0:   # unprovisioned: no administrator yet
                bstatus, buckets = request(port, "/pools/default/buckets")
                if bstatus == 200 and isinstance(buckets, list) and buckets:
                    names = ", ".join(str(b.get("name", "?")) if isinstance(b, dict) else "?" for b in buckets)
                    return fail(start_pid, "Couchbase on %s:%d has a bucket (%s) but no administrator, %d ms after "
                                "it was first seen listening: onepanel's cluster-init did not create one "
                                "(couchbase-cli 4.5.1 skips it, with exit status 0, for a password over 24 "
                                "characters)" % (HOST, port, names, (time.monotonic() - seen) * 1000)), None
                time.sleep(ASK)
                continue
        except (OSError, http.client.HTTPException):
            time.sleep(ASK)   # not serving yet, or the shim refused the connect: ask again
            continue
        if status == 401 or (status == 200 and pool_count(data)):
            return None, (absent, seen, time.monotonic(), status)
        time.sleep(ASK)       # not ready (5xx) or not Couchbase's answer: ask again


def watch(secrets, timeout, port, start_pid):
    start = time.monotonic()
    ours = (read_secret(secrets + "/couchbase-user"), read_secret(secrets + "/couchbase-password"))
    verdict, times = wait_admin(port, timeout, start, start_pid)
    if verdict is not None:
        return verdict
    absent, seen, done, status = times
    while True:
        now = time.monotonic()
        if now - start > timeout:
            return fail(start_pid, "Couchbase on %s:%d gave no clear answer to GET /settings/web within %.0f s"
                        % (HOST, port, timeout))
        try:
            mine, settings = request(port, "/settings/web", ours)
            anonymous, _ = request(port, "/settings/web")
        except (OSError, http.client.HTTPException):
            time.sleep(ASK)
            continue
        if mine == 401:
            return fail(start_pid, "Couchbase on %s:%d has an administrator that is not this launch's (GET "
                        "/settings/web with ours: HTTP 401)" % (HOST, port))
        if mine == 200:
            who = settings.get("username") if isinstance(settings, dict) else None
            if who != ours[0]:
                return fail(start_pid, "Couchbase on %s:%d names %r as its administrator, not this launch's"
                            % (HOST, port, who))
            if anonymous == 200:
                return fail(start_pid, "Couchbase on %s:%d answers an anonymous GET /settings/web with HTTP 200: "
                            "anyone can act as its administrator" % (HOST, port))
            if anonymous == 401:
                break
        time.sleep(ASK)       # 5xx or another transient answer: ask again
    if absent is None:
        opened = "it was already listening when this watcher started, so the time it opened is not known"
    else:
        opened = "at most %d ms after it opened" % ((done - absent) * 1000)
    say("Couchbase administrator set %d ms after %s:%d was first seen listening, %s (looked every %d ms, then "
        "asked every %d ms); it is this launch's (an anonymous GET /pools then answered HTTP %d%s)"
        % ((done - seen) * 1000, HOST, port, opened, LOOK * 1000, ASK * 1000, status,
           "" if status == 401 else ": the password-less onedata bucket lets anyone read [pools]"))
    say("%s: this launch's administrator answers GET /settings/web, and an anonymous one is refused (401)" % PASSED)
    return 0


def guarded_watch(args):
    start_pid = int(args[3])
    try:
        return watch(args[0], float(args[1]), int(args[2]), start_pid)
    except Exception as e:     # fail closed: an error of the guard is a FAILED verdict, never a silent pass
        return fail(start_pid, "the guard itself failed: %s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--wait-listener":
        wait_listener(int(sys.argv[2]), float(sys.argv[3]))
    elif len(sys.argv) == 6 and sys.argv[1] == "--watch":
        sys.exit(guarded_watch(sys.argv[2:]))
    else:
        sys.exit("usage: hpcop-cb-init.py --watch SECRETS TIMEOUT PORT START_PID | --wait-listener PORT TIMEOUT")
