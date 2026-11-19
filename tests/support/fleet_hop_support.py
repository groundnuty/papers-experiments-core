"""A fake `ssh` on PATH for the tests of the fleet hop (deploy/hpc-federation/fleethop.py): it opens a local listener
instead of a tunnel, logs what it was given, and can die, never listen, or answer a request with a status. Every host,
path and port the tests give it is invented; nothing leaves this host."""

import json
import os
import socket
import sys
import time
from pathlib import Path

LOGIN, SOCKET, KEY, KNOWN = "ZQhost.invalid", "/ZQ/front.sock", "/ZQ/hop-key", "/ZQ/known-hosts"
FAKE_SSH = f"""#!{sys.executable}
import json, os, socket, sys, time
argv = sys.argv[1:]
state = os.environ["FAKE_SSH_STATE"]
n = int(open(state).read()) if os.path.exists(state) else 0
open(state, "w").write(str(n + 1))
plan = json.loads(os.environ["FAKE_SSH_PLAN"])
mode = plan[min(n, len(plan) - 1)]
port = int(argv[argv.index("-L") + 1].split(":")[1])
with open(os.environ["FAKE_SSH_LOG"], "a") as log:
    log.write(json.dumps({{"n": n, "pid": os.getpid(), "argv": argv, "t": time.monotonic(), "mode": mode}}) + "\\n")
print("fake ssh was given", argv, file=sys.stderr)  # what a real ssh says: the host and the paths
print("fake ssh says", argv)
if mode == "die":
    sys.exit(255)
if mode == "nolisten":
    time.sleep(300)
server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", port))
server.listen()
if mode == "listen_then_die":
    time.sleep(float(os.environ.get("FAKE_SSH_LIFE", "0.5")))
    sys.exit(255)
held, answered = [], 0
life = float(os.environ.get("FAKE_SSH_LIFE", "0.5"))
born = time.monotonic()
while True:
    client, _ = server.accept()
    if mode == "silent":  # a tunnel whose far end is dead: it accepts, and nothing ever comes back
        held.append(client)
        continue
    data = client.recv(4096) if mode in ("http401", "answer_then_silent", "answer_then_die") else b""
    if data and mode == "answer_then_silent" and answered >= int(os.environ.get("FAKE_SSH_ANSWERS", "1")):
        held.append(client)
        continue
    if data:
        answered += 1
        client.sendall(b"HTTP/1.1 401 Unauthorized\\r\\nContent-Length: 0\\r\\nConnection: close\\r\\n\\r\\n")
    client.close()
    if mode == "answer_then_die" and data:
        time.sleep(life)
        sys.exit(255)
"""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def accepts(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
    except OSError:
        return False
    return True


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def settled(condition, seconds=10.0) -> bool:
    """Tell whether `condition()` becomes true within `seconds`."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(0.05)
    return False


class Ssh:
    """A fake `ssh` first on PATH, its plan (what each start does, the last repeating), and what it logged."""

    def __init__(self, tmp_path: Path, monkeypatch, *plan: str, life: float = 0.5, answers: int = 1):
        self.directory = tmp_path / "fakebin"
        self.directory.mkdir()
        script = self.directory / "ssh"
        script.write_text(FAKE_SSH)
        script.chmod(0o755)
        self.log, self.state = tmp_path / "ssh.log", tmp_path / "ssh.state"
        monkeypatch.setenv("PATH", f"{self.directory}{os.pathsep}{os.environ['PATH']}")
        monkeypatch.setenv("FAKE_SSH_PLAN", json.dumps(list(plan or ["stay"])))
        monkeypatch.setenv("FAKE_SSH_LOG", str(self.log))
        monkeypatch.setenv("FAKE_SSH_STATE", str(self.state))
        monkeypatch.setenv("FAKE_SSH_LIFE", str(life))
        monkeypatch.setenv("FAKE_SSH_ANSWERS", str(answers))
        self.port = free_port()

    def hop(self, **changes) -> dict:
        return {"login": LOGIN, "socket": SOCKET, "key": KEY, "known_hosts": KNOWN, "local_port": self.port, **changes}

    def started(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


def keeper(hop_module, hop, **timing):
    fast = {"wait_s": 5.0, "settle_s": 0.3, "min_interval_s": 0.3, "max_interval_s": 1.0, "poll_s": 0.05,
            "probe_s": 0.4, "probe_timeout_s": 0.4}  # fmt: skip
    return hop_module.HopKeeper(hop, **{**fast, **timing})
