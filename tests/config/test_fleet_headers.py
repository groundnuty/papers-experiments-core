"""B20 and B20: `session_header` and `deployment_header` in a model entry of `models.toml`: how they are declared, what
they are refused for, and that an entry that sets neither keeps the digest it had in the campaign fingerprint (C06)."""

import textwrap

import pytest

from expcore.config.loading import ConfigError
from expcore.config.models import ModelSpec, load_models
from expcore.study.pipeline import model_digest

RELAY = """
[models.relay]
backend = "openai_compat"
api_id = "vendor/relay-large"
base_url = "http://127.0.0.1:29440/v1"
status = "frozen"
"""


def table(tmp_path, extra: str = ""):
    path = tmp_path / "models.toml"
    path.write_text(textwrap.dedent(RELAY) + textwrap.dedent(extra), encoding="utf-8")
    return load_models(path)


def test_f01_a_model_names_its_two_headers_and_an_entry_that_names_none_has_none(tmp_path):
    """B20: both keys are optional; absent they are None."""
    assert table(tmp_path).resolve("relay").session_header is None
    assert table(tmp_path).resolve("relay").deployment_header is None
    spec = table(tmp_path, 'session_header = "X-Session-Id"\ndeployment_header = "hpcburst-deployment-id"\n')
    assert spec.resolve("relay").session_header == "X-Session-Id"
    assert spec.resolve("relay").deployment_header == "hpcburst-deployment-id"


@pytest.mark.parametrize(
    ("line", "fragment"),
    [
        ('session_header = "Bad Header"', "session_header"),
        ('session_header = "X:Id"', "session_header"),
        ('session_header = ""', "session_header"),
        ('deployment_header = "x\\ny"', "deployment_header"),
        ('session_header = "Authorization"', "session_header .* the client sets itself"),
        ('session_header = "OpenAI-Project"', "session_header .* the client sets itself"),
        ('session_header = "openai-organization"', "session_header .* the client sets itself"),
        ('deployment_header = "content-length"', "deployment_header .* the client sets itself"),
        ("session_header = 7", "session_header"),
    ],
)
def test_f01_a_header_name_that_is_no_token_or_would_override_the_client_is_refused(tmp_path, line, fragment):
    """B20: a header name is an HTTP token, never one the client sets itself (the key rides in Authorization)."""
    with pytest.raises(ConfigError, match=fragment):
        table(tmp_path, line + "\n")


def test_f01_only_an_openai_compat_model_takes_the_headers(tmp_path):
    """B20: the Messages API adapter reads no deployment and sends no session, so the keys are refused there."""
    for key in ("session_header", "deployment_header"):
        with pytest.raises(ConfigError, match=f"{key} applies to openai_compat models only"):
            ModelSpec(
                key="quillmark",
                backend="anthropic",
                api_id="vendor-large",
                status="frozen",
                key_env="TEST_VENDOR_KEY",
                **{key: "X-Thing"},
            )


def test_f01_a_model_that_sets_neither_header_keeps_the_digest_it_had_in_the_fingerprint():
    """B20, C06: the digest of an entry without the two keys is byte for byte the one it had before they existed
    (the literal is the digest of this entry on the commit before), so a campaign started before them resumes; an
    entry that sets either has another, since the header decides what the front sees and what a record says."""
    spec = ModelSpec(
        key="gamma-relay",
        backend="openai_compat",
        api_id="gamma-relay",
        status="frozen",
        base_url="https://llm.example.invalid/v1",
        free=True,
    )
    assert model_digest(spec) == "675005d213c3f888d5e23a42967d5709cf0e0f3d5845470e6264877807031aca"
    sessioned = ModelSpec(**{**spec.__dict__, "session_header": "X-Session-Id"})
    deployed = ModelSpec(**{**spec.__dict__, "deployment_header": "x-deployment"})
    assert len({model_digest(spec), model_digest(sessioned), model_digest(deployed)}) == 3
