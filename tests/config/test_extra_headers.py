"""B20: `extra_headers` in a model entry of `models.toml`: the request headers a front routes by. What it is refused
for, and that an entry that sets none keeps the digest it had in the campaign fingerprint (C06)."""

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


def test_x01_a_model_names_its_extra_headers_and_an_entry_that_names_none_has_none(tmp_path):
    """B20: the key is an optional table of header name to value; absent it is empty."""
    assert table(tmp_path).resolve("relay").extra_headers == {}
    spec = table(tmp_path, 'extra_headers = { "x-hpcburst-workload" = "batch-a", "X-Other" = "a b" }\n')
    assert spec.resolve("relay").extra_headers == {"x-hpcburst-workload": "batch-a", "X-Other": "a b"}


@pytest.mark.parametrize(
    "name",
    ["authorization", "Proxy-Authorization", "COOKIE", "content-type", "Content-Length", "host", "x-stainless-os"],
)
def test_x01_a_name_the_client_sets_itself_is_refused_in_any_case(tmp_path, name):
    """B20: no way to send a credential, to override the body's type or length, or the host."""
    with pytest.raises(ConfigError, match=r"extra_headers .* the client sets itself"):
        table(tmp_path, f'extra_headers = {{ "{name}" = "x" }}\n')


@pytest.mark.parametrize("name", ["X-API-Key", "api-key", "x-auth-token", "X-Client-Secret", "x-password"])
def test_x01_a_name_that_is_a_credentials_header_is_refused(tmp_path, name):
    """B20: a key never rides in a header of the entry, whatever its value looks like (the key goes in key_env or
    key_file and is sent in Authorization by the adapter)."""
    with pytest.raises(ConfigError, match="names a credential's header"):
        table(tmp_path, f'extra_headers = {{ "{name}" = "not-checked" }}\n')


def test_x01_the_session_header_is_not_a_name_in_any_case(tmp_path):
    """B20: the attempt's id rides in the session header; a second value for it would silently win or lose."""
    with pytest.raises(ConfigError, match=r"extra_headers .* the model's session_header"):
        table(tmp_path, 'session_header = "X-Session-Id"\nextra_headers = { "x-session-ID" = "fixed" }\n')


@pytest.mark.parametrize(
    ("entry", "fragment"),
    [
        ('"Bad Header" = "x"', "must be an HTTP header name"),
        ('"X:Id" = "x"', "must be an HTTP header name"),
        ('"" = "x"', "must be an HTTP header name"),
        ('"x-a" = ""', "must not be empty"),
        ('"x-a" = "line\\nbreak"', "printable ASCII"),
        ('"x-a" = "tab\\there"', "printable ASCII"),
        ('"x-a" = "caf\\u00e9"', "printable ASCII"),
        ('"x-a" = " padded"', "space at either end"),
        ('"x-a" = "' + "v" * 257 + '"', "at most 256"),
        ('"x-a" = 7', "must be text"),
        ('"x-a" = "Bearer abc"', "looks like a credential"),
        ('"x-a" = "basic dXNlcjpwdw=="', "looks like a credential"),
        ('"x-a" = "1", "X-A" = "2"', "twice"),
    ],
)
def test_x01_a_bad_name_or_value_is_refused_and_the_message_never_holds_the_value(tmp_path, entry, fragment):
    """B20: a name is an HTTP token; a value is 1 to 256 printable ASCII characters with no line break (so none can
    split the request); and what is said names the key, never the value, which may one day be identifying."""
    with pytest.raises(ConfigError, match=fragment) as caught:
        table(tmp_path, f"extra_headers = {{ {entry} }}\n")
    assert "abc" not in str(caught.value)
    assert "dXNlcjpwdw" not in str(caught.value)


def test_x01_the_key_must_be_a_table(tmp_path):
    """B20: a list or a string is no table of headers."""
    with pytest.raises(ConfigError, match="extra_headers"):
        table(tmp_path, 'extra_headers = ["x-a"]\n')


def test_x01_only_an_openai_compat_model_takes_extra_headers():
    """B20: the Messages API and the stub send nothing of the kind, so the key is refused there, at load."""
    for backend, extra in (("anthropic", {"key_env": "TEST_VENDOR_KEY"}), ("stub", {})):
        with pytest.raises(ConfigError, match="extra_headers applies to openai_compat"):
            ModelSpec(
                key="quillmark",
                backend=backend,
                api_id="vendor-large",
                status="frozen",
                extra_headers={"x-hpcburst-workload": "batch-a"},
                **extra,
            )


def test_x01_a_model_that_sets_no_extra_headers_keeps_the_digest_it_had_in_the_fingerprint():
    """B20, C06: the digest of an entry without the key is byte for byte the one it had before the key existed (the
    literal is the digest of this entry on the commit before), so a campaign started before it resumes. An entry that
    sets it has another, whatever the order of its table: the header decides what the front does with the run."""
    spec = ModelSpec(
        key="gamma-relay",
        backend="openai_compat",
        api_id="gamma-relay",
        status="frozen",
        base_url="https://llm.example.invalid/v1",
        free=True,
    )
    assert model_digest(spec) == "675005d213c3f888d5e23a42967d5709cf0e0f3d5845470e6264877807031aca"
    routed = ModelSpec(**{**spec.__dict__, "extra_headers": {"x-a": "1", "x-b": "2"}})
    reordered = ModelSpec(**{**spec.__dict__, "extra_headers": {"x-b": "2", "x-a": "1"}})
    other = ModelSpec(**{**spec.__dict__, "extra_headers": {"x-a": "1", "x-b": "3"}})
    assert model_digest(routed) != model_digest(spec)
    assert model_digest(routed) == model_digest(reordered)
    assert model_digest(other) not in (model_digest(routed), model_digest(spec))
