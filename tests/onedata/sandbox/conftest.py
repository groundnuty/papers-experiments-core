"""Fixtures for the sandbox tests: a platform document with the sandbox table, an in-memory host, and a leak scan."""

from types import SimpleNamespace

import pytest
from fake_host import FakeHost
from platform_support import write_platform
from sandbox_support import add_sandbox, configured_sandbox_values

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.sandbox.api import SandboxError, StartRefused
from expcore_onedata.sandbox.registry import Registry
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


@pytest.fixture(autouse=True)
def _no_sandbox_exception_holds_a_configured_value(monkeypatch, tmp_path):
    """O12: record the message of every exception the sandbox code builds, and fail the test if one holds a value of the
    `[sandbox]` table (an image, a pool, a path, a login, a level). Messages name keys, roles and positions instead."""
    messages: list[str] = []

    def recording(original):
        def init(self, *args, **kwargs):
            original(self, *args, **kwargs)
            messages.append(str(self))

        return init

    for exception in SANDBOX_EXCEPTIONS:
        monkeypatch.setattr(exception, "__init__", recording(exception.__init__))
    yield
    values = configured_sandbox_values(tmp_path)
    leaks = {text: found for text in messages if (found := [v for v in values if v.lower() in text.lower()])}
    assert not leaks, f"a message of the sandbox holds configured values: {leaks}"
