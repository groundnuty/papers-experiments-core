"""Fixtures for the sandbox tests: a platform document with the sandbox table, an in-memory host, and a leak scan."""

import json
import logging
import os
from types import SimpleNamespace

import pytest
from fake_host import FakeHost
from platform_support import BASELINE, CREDENTIALS_DIR, configured_values, write_platform
from sandbox_support import SLOT_USERS, add_sandbox, configured_sandbox_values

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.sandbox import reap, teardown
from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.tokencheck import TokenRefused
from expcore_onedata.users import Slots, load_slots


@pytest.fixture
def config(tmp_path, fake) -> PlatformConfig:
    """The platform document of this directory has the sandbox table; it replaces the parent's `config`."""
    return load_platform(write_platform(tmp_path, add_sandbox))


@pytest.fixture
def slots(config, fake) -> Slots:
    """The slots of the platform document, their users known to the fake federation with view and read, and each user's
    credential file beside the others."""
    directory = config.file.parent / CREDENTIALS_DIR
    for login, (password, user_id) in SLOT_USERS.items():
        file = directory / f"{login}.pass"
        file.write_text(password + "\n", encoding="utf-8")
        os.chmod(file, 0o600)
        fake.add_user(login, password, user_id)
        fake.grant(user_id, BASELINE)
    return load_slots(config.file, config)


@pytest.fixture
def world(tmp_path):
    """A host that knows the configured images, the platform configuration and a registry of its own."""
    config = load_platform(write_platform(tmp_path, add_sandbox), require_secrets=False)
    s = config.sandbox
    host = FakeHost()
    for ref in (s.agent, s.gate, s.oneclient):
        host.add_image(ref.name, ref.id)
    host.oneclient_images.add(s.oneclient.id)
    return SimpleNamespace(host=host, config=config, s=s, registry=Registry(), tmp_path=tmp_path)


SANDBOX_EXCEPTIONS = (SandboxError, StartRefused, InfraError, ConfigError, TokenRefused)


class _Logged(logging.Handler):
    """Keeps each line the platform layer logs, with the traceback it carries, as a log file would hold it."""

    def __init__(self, sink: list[tuple[str, str]]) -> None:
        super().__init__(logging.DEBUG)
        self.sink = sink
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.sink.append(("a log line", self.format(record)))


GATE_REQUEST_FIELDS = ("path", "query", "range", "request_line", "req_headers")  # what the agent asked for


def _gate_log_leaks(root, values: list[str]) -> list[tuple[str, str]]:
    """The fields of the gate's request logs that hold a configured value: a substring of any field the gate writes
    itself, and the exact value of a field the agent's request chose (an API path is the agent's, and a configured
    mount path such as `/data` is a substring of many), as `(where, value)`."""
    leaks = []
    for written in sorted(p for p in root.rglob("*requests.jsonl") if p.is_file()):
        for number, line in enumerate(written.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            try:
                record = json.loads(line) if line.strip() else {}
            except ValueError:  # a line that is no JSON (a test's damaged log) is scanned as it is
                record = {"unparsed_line": line}
            for key, field in record.items():
                if key in GATE_REQUEST_FIELDS:
                    asked = [str(v) for v in field.values()] if isinstance(field, dict) else [str(field)]
                    hits = [v for v in values if v in asked]
                else:
                    text = json.dumps(field).lower()
                    hits = [v for v in values if v.lower() in text]
                leaks += [(f"the gate log {written.name}, line {number}, field {key}", v) for v in hits]
    return leaks


SERVED_TABLES = ("files", "listings")  # the tables of the bytes served (O22), keyed by ids the agent's requests named


def _agent_chosen(meta: dict) -> tuple[dict, list[str]]:
    """The metadata without what the agent's requests chose, and those values: the file and directory ids that key the
    tables of the bytes served and the paths that name them, the agent's own as the gate log's request fields are. Each
    is matched whole, as `_gate_log_leaks` matches those (a configured mount path such as `/data` is a substring of
    every API path); everything else in a row is scanned as the rest of the metadata is."""
    served = meta.get("served_bytes")
    if not isinstance(served, dict):
        return meta, []
    asked: list[str] = []
    tables = {}
    for name in SERVED_TABLES:
        rows = served.get(name)
        if isinstance(rows, dict):
            asked += [str(v) for row_id, row in rows.items() for v in (row_id, row.get("path"))]
            tables[name] = [{k: v for k, v in row.items() if k != "path"} for row in rows.values()]
    return meta | {"served_bytes": served | tables}, asked


def _written_logs_and_records(root):
    """The files the sandbox code writes as logs and records, wherever a test put them: the teardown logs and the
    reaper's records. (The gate's request log is not among them: it holds the paths the agent asked for, its own.)"""
    teardown_logs = (p for p in root.rglob("*.jsonl") if p.parent.name == teardown.TEARDOWN_LOG_DIR)
    return sorted({*teardown_logs, *root.rglob(reap.REAPED)})


@pytest.fixture(autouse=True)
def _no_sandbox_output_holds_a_configured_value(monkeypatch, tmp_path):
    """O12: record every message of an exception the sandbox code builds, every line it logs, the metadata of every
    sandbox, every step of its teardown log, the teardown logs and reaper records it wrote to disk
    (`_written_logs_and_records`) and the gate's request logs (`_gate_log_leaks`, which also looks for the platform's
    hosts), and fail the test if one holds a value of the `[sandbox]` table (an
    image, a pool, a path, a login, a level). They name keys, roles and positions instead, and a tool's stderr by its
    size and SHA-256."""
    found: list[tuple[str, str]] = []
    asked: list[str] = []  # the values in the metadata the agent's requests chose (`_agent_chosen`)

    def recording(original):
        def init(self, *args, **kwargs):
            original(self, *args, **kwargs)
            found.append(("an error message", str(self)))

        return init

    for exception in SANDBOX_EXCEPTIONS:
        monkeypatch.setattr(exception, "__init__", recording(exception.__init__))

    metadata = DockerSandbox.metadata

    def recording_metadata(self):
        out = metadata(self)
        rest, chosen = _agent_chosen(out)
        found.append(("the metadata of a run", json.dumps(rest, default=str)))
        asked.extend(chosen)
        return out

    monkeypatch.setattr(DockerSandbox, "metadata", recording_metadata)
    step = teardown._StepLog.__call__

    def recording_step(self, name, **fields):
        record = step(self, name, **fields)
        found.append(("a teardown log record", json.dumps(record, default=str)))
        return record

    monkeypatch.setattr(teardown._StepLog, "__call__", recording_step)
    logger, handler = logging.getLogger("expcore_onedata"), _Logged(found)
    level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    yield
    logger.removeHandler(handler)
    logger.setLevel(level)
    for written in _written_logs_and_records(tmp_path):
        found.append((f"the file {written.name}", written.read_text(encoding="utf-8", errors="replace")))
    values = [
        v for v in configured_sandbox_values(tmp_path) if not v.startswith("sha256:")
    ]  # ids: recorded by design (O13)
    leaks = {
        f"{what}: {text[:200]}": hit
        for what, text in found
        if (hit := [v for v in values if v.lower() in text.lower()])
    }
    for where, value in _gate_log_leaks(tmp_path, values + configured_values(tmp_path)):  # the hosts it serves too
        leaks.setdefault(where, []).append(value)
    for value in values + configured_values(tmp_path):
        if value in asked:
            leaks.setdefault("an id or path of the bytes served in a run's metadata", []).append(value)
    assert not leaks, f"the output of the sandbox code holds configured values: {leaks}"
