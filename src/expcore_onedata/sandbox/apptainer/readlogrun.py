# Provenance: new
"""The read-log channel on the Apptainer backend: its collector as a helper, its binds, its canary and its log (O91).

A study's canonical scripts run in the agent's code tool, inside a cell, and log every read they make to a collector
the harness runs outside it (`expcore_onedata.readlog`). On this backend, when `[sandbox.apptainer]` declares
`readlog_collector_sha256` (the pin of the core's own collector file), a run has:

* three directories of the run's, each 0700: `readlog/` (the collector's copy, its log and its two streams, never bound
  into a cell), `readlog-sock/` (empty, the collector's socket directory) and `readlog-client/` (the client module's
  copy alone). The collector's and the client's copies are hashed against their sources, and only the copies are used.
  The socket's path (`<work_root>/<run id>/readlog-sock/sock`) must fit a Unix socket's 107 bytes: a longer one refuses
  the start before anything exists (`check_socket_path`), as a collector that cannot bind it would only end unready;
* the collector, a helper of the driver's uid (role `readlog`, `helpers.start`), started before any cell, told the run's
  nonce and the expected sender uid: every cell runs as the driver's own uid (`facts.py`, `same_uid`), so that is
  `os.getuid()`;
* in EVERY cell (`binds`, so in each `apptainer exec` of `cell_argv`, the start's canaries and probes included), the
  socket directory bound read-only at `paths.SOCKET_DIR` and the client module's directory bound read-only at
  `paths.CLIENT_DIR`. A Unix datagram socket takes `sendto` on a read-only bind (the egress shim depends on the same),
  and nothing else of the run's directories is bound: the log, the collector and its copy are out of every cell's reach.
  `cell_argv` is the one builder of the execs that run the agent's code, and of the harness's own cell programs; the
  harness's one-shot execs of the image (the certifi probe, the trust-bundle reads, the collection's copy helper) run
  programs of the harness alone, never the agent's code, and carry no channel binds, as they carry no egress shim;
* a canary, sent through the real client from inside a cell before the agent's first cell (`CANARY_PROGRAM`), so the
  log's first record is the harness's own: a start whose cell cannot send it fails;
* at the collection, the collector stopped on SIGTERM (it drains what is queued and writes its end line), its log read
  once, copied into the evidence and scanned for the data-path token like every file of evidence (`collect.py`), and
  exposed as `read_log()`: bytes, or None when it could not be read, which the instrument reads as an UNKNOWN log.
  A collector that was killed wrote no end line, so its log is unknown as well, and its death is infrastructure
  (`infra.HELPERS`). A collector that ended BEFORE the harness stopped it (told to end by something else, so it wrote a
  clean end line), or whose stop was not clean (it needed the kill, or could not be shown gone), has a log that may
  read intact with the reads it took and none after: that log is withheld (`read_log()` None, no copy), so the run's
  log is unknown, never a clean one (`Channel.read`; `readlog_stop` says how the stop went);
* the collector's and the client's hashes, the placement and the sender uid, once, in `infrastructure_facts` as the
  readlog's own (`readlog.facts`, through the one writer, O89).

A platform that declares no pin has none of this: no helper, no directory, no bind, `read_log()` None, and its record is
what it was. The targets themselves are the harness's own on every platform (`settings.OWN_TARGETS`, `inputs.TAKEN`).
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from expcore_onedata.readlog import facts, files, paths, reader
from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.apptainer import argv as AV
from expcore_onedata.sandbox.apptainer import helpers, layout
from expcore_onedata.sandbox.apptainer.settings import Pinned
from expcore_onedata.sandbox.hostops import describe_stderr

if TYPE_CHECKING:
    from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox

log = logging.getLogger(__name__)

ROLE = "readlog"  # the helper's role: `infra.HELPERS`, `teardown`, the run's record
READY = "readlog collector ready"  # what the collector prints on stdout once it listens
DIR, SOCK_DIR, CLIENT_DIR = "readlog", "readlog-sock", "readlog-client"  # in the run's directory, each 0700
COLLECTOR_COPY = "readlog-collector.py"  # the run's copy of the collector, in `readlog/`
LOG_NAME = "readlog.jsonl"  # the collector's log, in `readlog/`
COPY = "read-log.jsonl"  # the evidence copy the collection writes beside the egress log: `result["read_log"]`
STOP_KEY = "readlog_stop"  # in the run's metadata: how the collector's stop went (`helpers.stop`)
PLACEMENT = "apptainer_helper"  # `readlog.facts.PLACEMENTS`
CANARY_S = 60.0  # the canary cell's limit, as the egress canary's
CANARY_SAID = "readlog canary sent"
CANARY_PROGRAM = f"""import importlib.util as u
spec = u.spec_from_file_location({paths.CLIENT_MODULE!r}, {paths.CLIENT!r})
client = u.module_from_spec(spec)
spec.loader.exec_module(client)
client.log_canary()
print({CANARY_SAID!r})
"""


class Channel:
    """The read-log channel of one Apptainer sandbox (the module's docstring). `enabled` is the platform's choice: the
    nonce and the expected sender uid exist either way, so a study can build its instrument without asking."""

    def __init__(self, box: ApptainerSandbox) -> None:
        self.box = box
        self.nonce = secrets.token_hex(16)
        self.uid = os.getuid()  # every cell runs as the driver's own uid
        self._client_sha256: str | None = None
        self._stopped_cleanly = False  # the collector ran until the harness's own stop, which ended it on SIGTERM

    @property
    def enabled(self) -> bool:
        app = self.box.app
        return app is not None and app.readlog_collector_sha256 is not None

    # ------------------------------------------------------------------------------------------------ start
    def check_pin(self) -> None:
        """Before anything exists: this core's collector file is the one the platform pins, or `StartRefused`."""
        if not self.enabled:
            return
        try:
            found = files.sha256_of(files.collector_file())
        except OSError:
            raise StartRefused("the read-log collector of this core could not be read") from None
        if found != self.box.app.readlog_collector_sha256:
            raise StartRefused(
                "[sandbox.apptainer] readlog_collector_sha256 is not the read-log collector of this core: "
                "its SHA-256 differs"
            )

    def check_socket_path(self) -> None:
        """Before anything exists: the collector's socket path fits a Unix socket's address, or `StartRefused`
        (`layout.check_socket_path`)."""
        if self.enabled:
            layout.check_socket_path(self.box.dir / SOCK_DIR / paths.SOCKET_NAME, "read-log")

    def prepare(self) -> None:
        """The three directories and the two copies, each shown to have its hash (`layout.copy_pinned`)."""
        if not self.enabled:
            return
        run = self.box.dir
        for name in (DIR, SOCK_DIR, CLIENT_DIR):
            (run / name).mkdir(mode=0o700)
        pin = Pinned(files.collector_file(), self.box.app.readlog_collector_sha256)
        layout.copy_pinned(pin, run / DIR / COLLECTOR_COPY, "the read-log collector")
        client = files.client_file()
        self._client_sha256 = files.sha256_of(client)
        layout.copy_pinned(Pinned(client, self._client_sha256), run / CLIENT_DIR / paths.CLIENT_NAME,
                           "the read-log client")  # fmt: skip

    def start(self) -> None:
        """The collector, before any cell, and the run's fact of what ran and where."""
        if not self.enabled:
            return
        box, run = self.box, self.box.dir
        argv = [box._host_python(), "-I", "-B", str(run / DIR / COLLECTOR_COPY), str(run / SOCK_DIR),
                str(run / DIR / LOG_NAME), self.nonce, str(self.uid)]  # fmt: skip
        helpers.start(box.node, ROLE, argv, run / DIR, READY, noted=box._note_helper, env={AV.RUN_TAG: box.sid},
                      sleep=box._sleep, clock=box._clock)  # fmt: skip
        value = facts.fact(collector_sha256=box.app.readlog_collector_sha256, client_sha256=self._client_sha256,
                           placement=PLACEMENT, expected_peer_uid=self.uid)  # fmt: skip
        box.record_infrastructure_fact(facts.KEY, value, owner=facts.OWNER)

    def binds(self) -> list[AV.Bind]:
        """What every cell binds, read-only: the socket directory and the client module's directory at the channel's
        fixed targets. Nothing else of the channel is bound."""
        if not self.enabled:
            return []
        run = self.box.dir
        return [AV.Bind(run / SOCK_DIR, paths.SOCKET_DIR), AV.Bind(run / CLIENT_DIR, paths.CLIENT_DIR)]

    def canary(self) -> None:
        """The harness's canary through the real client, from inside a cell, so it is the log's first record: a cell
        that cannot send it (no socket, no client, a collector that is gone) fails the start."""
        if not self.enabled:
            return
        done = self.box._cells.run(CANARY_PROGRAM, CANARY_S, record=False)
        lines = done.stdout.strip().splitlines()
        if done.exit_code != 0 or (lines[:1] != [CANARY_SAID]):
            log.error("the read-log canary said %r (%s)", lines[:1], describe_stderr(done.stderr))
            raise SandboxError("the read-log canary could not be sent from inside a cell")

    # ------------------------------------------------------------------------------------------------ collection
    def stop(self) -> None:
        """Stop the collector on SIGTERM so it drains and writes its end line (the harness's own stop: its state now
        stands as its last of the run, so the stop is no death). What the stop was is recorded: whether the collector
        still ran when the harness came to stop it (`ran_until_stop`), and how it ended; a stop that failed holds the
        error. Only a collector that ran until this stop and ended on SIGTERM is a clean one (`read`)."""
        box = self.box
        helper = box._helpers.get(ROLE)
        if helper is None or STOP_KEY in box._meta:
            return
        box.infra.stopping(ROLE)
        ran = box.node.alive(helper.process.pid, helper.process.start)  # True only while it ran until this stop
        try:
            note = helpers.stop(box.node, helper, sleep=box._sleep, clock=box._clock)
        except SandboxError as error:
            box._meta[STOP_KEY] = {"error": str(error), "ran_until_stop": ran}
            return
        box._meta[STOP_KEY] = {**note, "ran_until_stop": ran}
        self._stopped_cleanly = ran is True and note["ended_on_term"] is True

    def read(self) -> bytes | None:
        """The collector's log as the instrument is to read it, or None: the log is withheld, so it reads as missing
        and so unknown, when it would read INTACT although its collector did not run until the harness's own stop or
        that stop was not clean (the collector was told to end by something else and wrote a clean end line, needed
        the kill, or could not be shown gone): a lost collector is never a clean log, however complete its bytes
        look. A log that already reads unknown (a killed collector's has no end line) is handed on as it is, with
        its own reasons, so what was read before the loss can be looked at."""
        data = self._read_file()
        if data is None or self._stopped_cleanly:
            return data
        if reader.read_log(data, self.nonce, expected_peer_uid=self.uid).intact:
            log.error("the read-log collector did not end cleanly under the harness's stop: its log is withheld")
            return None
        return data

    def _read_file(self) -> bytes | None:
        """The collector's log, read once from the file the stopped collector left; None when it cannot be read as a
        regular file. At most one byte past the size the reader accepts is read, so a log that is too large reads as
        too large and never fills the driver's memory."""
        path: Path = self.box.dir / DIR / LOG_NAME
        limit = paths.MAX_LOG_BYTES + paths.END_RESERVE_BYTES + 1
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    return None
                return handle.read(limit)
        except OSError:
            return None


__all__ = ["CANARY_PROGRAM", "CANARY_SAID", "COPY", "ROLE", "Channel"]
