"""Helpers for the elastic channel's tests: a temp-dir site behind a fake ssh, a node's files, and a fake run."""

import hashlib
import io
import json
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from expcore.campaign.elastic import channel as ch
from expcore.campaign.elastic.exchange import MODULE
from expcore.campaign.elastic.exchange import main as exchange_main
from expcore.campaign.elastic.records import Lease
from expcore.campaign.elastic.transport import SSH_FAILED, Site, SshTransport
from expcore.campaign.elastic.worker import Outcome


@dataclass
class FakeSsh:
    """`subprocess.run` for an ssh argv `["fake-ssh", <site>, <remote command>]`: the remote command runs here, the
    exchange verb in this process and `cat` as a read. A site in `down` fails as ssh does (exit 255) and runs
    nothing; one in `lost_reply` runs the command and then fails, as a connection that drops after the remote side
    ran; one in `cut` has its reply cut in half; one in `pull_down` fails every pull as ssh does."""

    down: set[str] = field(default_factory=set)
    lost_reply: set[str] = field(default_factory=set)
    cut: set[str] = field(default_factory=set)
    pull_down: set[str] = field(default_factory=set)
    calls: list[tuple[str, list[str]]] = field(default_factory=list)

    def __call__(self, argv: list[str], *, check: bool = False, timeout: float | None = None, **kwargs: Any):
        assert argv[0] == "fake-ssh", argv
        assert len(argv) == 3, argv
        site, remote = argv[1], shlex.split(argv[2])
        self.calls.append((site, remote))
        if site in self.down:
            return subprocess.CompletedProcess(argv, SSH_FAILED, b"", b"")
        if remote[0] == "cat":
            return self._cat(argv, site, remote, kwargs["stdout"])
        assert remote[-2:] == ["-m", MODULE], remote
        out = io.StringIO()
        status = exchange_main(io.BytesIO(kwargs["input"]), out)
        reply = out.getvalue().encode("utf-8")
        if site in self.lost_reply:
            return subprocess.CompletedProcess(argv, SSH_FAILED, b"", b"")
        if site in self.cut:
            reply = reply[: len(reply) // 2]
        return subprocess.CompletedProcess(argv, status, reply, b"")

    def _cat(self, argv: list[str], site: str, remote: list[str], sink: Any) -> subprocess.CompletedProcess:
        assert remote[:2] == ["cat", "--"]
        try:
            data = Path(remote[2]).read_bytes()
        except OSError:
            return subprocess.CompletedProcess(argv, 1, None, None)
        if site in self.pull_down:
            return subprocess.CompletedProcess(argv, SSH_FAILED, None, None)
        sink.write(data)
        return subprocess.CompletedProcess(argv, 0, None, None)


def site_of(tmp_path: Path, name: str = "site-a") -> Site:
    """A site whose shared file system is a temp dir, reached through the fake ssh."""
    return Site(name=name, root=str(tmp_path / name / "channels"), ssh=("fake-ssh", name), python=("python3", "-B"))


def transport(ssh: FakeSsh) -> SshTransport:
    return SshTransport(run=ssh)


def channel_of(site: Site, label: str) -> Path:
    return ch.federation_dir(site.root, label)


def inbox_record(site: Site, label: str, lease_id: int) -> dict[str, Any] | None:
    inbox = channel_of(site, label) / ch.INBOX
    try:
        return json.loads((inbox / ch.lease_name(lease_id)).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def beat(site: Site, label: str, lease_id: int, counter: int) -> None:
    """The node's heartbeat for a lease."""
    outbox = channel_of(site, label) / ch.OUTBOX
    outbox.mkdir(parents=True, exist_ok=True)
    ch.write_json(
        outbox / ch.HEARTBEAT,
        {"format": ch.HEARTBEAT_FORMAT, "federation": label, "lease_id": lease_id, "counter": counter},
    )


def complete(
    site: Site,
    label: str,
    lease: Lease,
    *,
    state: str = "done",
    charged: int = 0,
    spend_micro_usd: int = 250_000,
    archive: bytes | None = None,
    sha256: str | None = None,
) -> str:
    """The node's archive and completion for a lease; the archive's SHA-256 (or the one the completion claims)."""
    directory = channel_of(site, label)
    ch.make_dirs(directory)
    data = archive if archive is not None else f"the store of lease {lease.lease_id}".encode()
    (directory / ch.ARCHIVES / ch.archive_name(lease.lease_id)).write_bytes(data)
    claimed = sha256 or hashlib.sha256(data).hexdigest()
    ch.write_json(
        directory / ch.OUTBOX / ch.completion_name(lease.lease_id),
        {
            "format": ch.COMPLETION_FORMAT,
            "federation": label,
            "lease_id": lease.lease_id,
            "run_key": lease.run_key,
            "archive_sha256": claimed,
            "state": state,
            "charged": charged,
            "spend_micro_usd": spend_micro_usd,
        },
    )
    return claimed


@dataclass
class FakeHandle:
    """A run that is still running for `beats` waits, then ends with `status` (calling `effect` first)."""

    beats: int = 0
    status: int = 0
    effect: Callable[[], None] | None = None
    waited: int = 0

    def wait(self, timeout: float) -> int | None:
        self.waited += 1
        if self.waited <= self.beats:
            return None
        if self.effect is not None:
            self.effect()
            self.effect = None
        return self.status


@dataclass
class FakeRunner:
    """Starts a FakeHandle per lease, as `make(study, lease_file)` says, and records what ran, and whether two ran at
    once."""

    make: Callable[[Path, Path], FakeHandle] = lambda study, lease_file: FakeHandle()
    started: list[int] = field(default_factory=list)
    running: int = 0
    overlapped: bool = False

    def start(self, study: Path, lease_file: Path, log: Path) -> Any:
        lease_id = json.loads(lease_file.read_text(encoding="utf-8"))["lease_id"]
        self.started.append(lease_id)
        if self.running:
            self.overlapped = True
        self.running += 1
        inner = self.make(study, lease_file)
        runner = self

        class Tracked:
            def wait(self, timeout: float) -> int | None:
                status = inner.wait(timeout)
                if status is not None:
                    runner.running -= 1
                return status

        (study / "out").mkdir(exist_ok=True)
        (study / "out" / "state.sqlite").write_bytes(b"a store")
        return Tracked()


def outcome_of(**fields: Any) -> Callable[[Path, Any], Outcome]:
    """A store reading that says the given things (a run done in one attempt, which no limit counts, by default)."""
    values = {
        "attempts": 1,
        "attempts_started": 1,
        "charged": 0,
        "state": "done",
        "spend_micro_usd": 500_000,
        "spend_known": True,
    }
    values.update(fields)
    return lambda study, lease: Outcome(**values)
