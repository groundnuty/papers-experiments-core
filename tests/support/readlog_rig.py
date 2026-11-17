"""A real read-log collector process on a real Unix datagram socket, for the tests of `expcore_onedata.readlog`.

`Rig` starts `collector.py` as the harness does (`python3 -I collector.py <dir> <log> <nonce> <uid> [<max>]`), waits for
its ready line, and stops it through its own `Popen` handle (never by name). `load_client()` imports the client module
from its file as a cell's script does, with `SOCKET` pointed at the rig's socket (the only way a test may change it).
"""

import importlib.util
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from readlog_logs import NONCE

from expcore_onedata.readlog import files

READY = b"readlog collector ready"


def unix_sockets_allowed() -> bool:
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM).close()
    except OSError:
        return False
    return True


needs_unix = pytest.mark.skipif(not unix_sockets_allowed(), reason="this host does not permit Unix sockets (a sandbox)")


def short_dir() -> Path:
    """A private directory with a short path: a Unix socket's path is at most 107 bytes."""
    path = Path(tempfile.mkdtemp(prefix="rl"))
    if len(str(path / "s" / "sock")) > 100:
        shutil.rmtree(path, ignore_errors=True)
        pytest.skip(f"the temporary directory's path is too long for a Unix socket: {path}")
    return path


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_client(socket_path: str | os.PathLike[str] | None = None):
    """The client module, loaded from its file as a script in a cell loads it; `SOCKET` set when a path is given."""
    module = load_module(files.client_file(), "expcore_readlog_client")
    if socket_path is not None:
        module.SOCKET = str(socket_path)
    return module


def load_collector():
    return load_module(files.collector_file(), "expcore_readlog_collector_under_test")


class Rig:
    """One collector process: its socket directory, its log and its Popen handle."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.dir = root / "s"
        self.log = root / "log.jsonl"
        self.proc: subprocess.Popen[bytes] | None = None

    @property
    def sock(self) -> str:
        return str(self.dir / "sock")

    def make_dir(self, mode: int = 0o700) -> None:
        self.dir.mkdir(exist_ok=True)
        self.dir.chmod(mode)

    def command(self, uid: int, *, nonce: str = NONCE, max_bytes: int | None = None) -> list[str]:
        command = [sys.executable, "-I", str(files.collector_file()), str(self.dir), str(self.log), nonce, str(uid)]
        return command + ([str(max_bytes)] if max_bytes is not None else [])

    def start(self, uid: int | None = None, *, mode: int | None = None, **options) -> "Rig":
        """Start the collector (expecting `uid`, by default this process's own) and wait for its ready line."""
        uid = os.getuid() if uid is None else uid
        self.make_dir(mode if mode is not None else (0o700 if uid == os.getuid() else 0o755))
        self.proc = subprocess.Popen(
            self.command(uid, **options), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        ready, _, _ = select.select([self.proc.stdout], [], [], 30)
        line = self.proc.stdout.readline() if ready else b""
        if READY not in line:
            self.proc.kill()
            _, err = self.proc.communicate()
            raise AssertionError(f"the collector did not become ready: {line!r} {err!r}")
        return self

    def stop(self) -> int:
        """SIGTERM, as the harness stops it, and wait for the exit status."""
        assert self.proc is not None
        self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout=30)

    def kill(self) -> None:
        assert self.proc is not None
        self.proc.kill()
        self.proc.wait(timeout=30)

    def close(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)
        if self.proc is not None:
            for stream in (self.proc.stdout, self.proc.stderr):
                if stream is not None:
                    stream.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def data(self) -> bytes:
        return self.log.read_bytes()

    def wait_for_lines(self, count: int, timeout: float = 10.0) -> None:
        """Wait until the log holds at least `count` lines (the collector has processed what was sent)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.log.exists() and self.log.read_bytes().count(b"\n") >= count:
                return
            time.sleep(0.02)
        raise AssertionError(f"the log holds fewer than {count} lines after {timeout} s")


def raw_sender(path: str) -> socket.socket:
    """A plain datagram socket connected to the collector's socket: sends whatever bytes a test wants."""
    sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sender.connect(path)
    return sender
