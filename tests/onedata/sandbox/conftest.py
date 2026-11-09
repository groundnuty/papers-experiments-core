"""Fixtures for the sandbox tests: a platform document with the sandbox table, an in-memory host, and a leak scan."""

import json
import logging
from types import SimpleNamespace

import pytest
from fake_host import FakeHost
from platform_support import write_platform
from sandbox_support import add_sandbox, configured_sandbox_values

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.sandbox import reap, teardown
from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.registry import Registry
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.tokencheck import TokenRefused


@pytest.fixture
def config(tmp_path, fake) -> PlatformConfig:
    """The platform document of this directory has the sandbox table; it replaces the parent's `config`."""
    return load_platform(write_platform(tmp_path, add_sandbox))


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


def _written_logs_and_records(root):
    """The files the sandbox code writes as logs and records, wherever a test put them: the teardown logs and the
    reaper's records. (The gate's request log is not among them: it holds the paths the agent asked for, its own.)"""
    teardown_logs = (p for p in root.rglob("*.jsonl") if p.parent.name == teardown.TEARDOWN_LOG_DIR)
    return sorted({*teardown_logs, *root.rglob(reap.REAPED)})


@pytest.fixture(autouse=True)
def _no_sandbox_output_holds_a_configured_value(monkeypatch, tmp_path):
    """O12: record every message of an exception the sandbox code builds, every line it logs, the metadata of every
    sandbox, every step of its teardown log, and the teardown logs and reaper records it wrote to disk
    (`_written_logs_and_records`), and fail the test if one holds a value of the `[sandbox]` table (an
    image, a pool, a path, a login, a level). They name keys, roles and positions instead, and a tool's stderr by its
    size and SHA-256."""
    found: list[tuple[str, str]] = []

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
        found.append(("the metadata of a run", json.dumps(out, default=str)))
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
    assert not leaks, f"the output of the sandbox code holds configured values: {leaks}"
