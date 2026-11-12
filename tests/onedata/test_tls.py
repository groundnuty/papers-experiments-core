"""The one TLS setting of a federation, `[tls]` of platform.toml: loaded once, recorded by mode and hash, used by all.

The federation stand-in is a TLS server on 127.0.0.1 whose certificate chains to a CA made in the test, which no
default trusts (`tls_support`). The data gate's and the mount sidecar's halves are in `sandbox/test_tls_paths.py`, the
tool server's in `test_toolserver.py`.
"""

import hashlib
import json

import pytest
from campaign_support import make_fingerprint, make_manifest, make_schema
from sandbox_support import add_sandbox
from tls_support import ANSWER, HAS_OPENSSL, TlsServer, make_federation_ca, swap_after_check

from expcore.campaign.healthgate import read_health
from expcore.campaign.resume import FingerprintMismatch, open_campaign
from expcore.config.loading import ConfigError
from expcore_onedata.client import OnezoneClient, PlatformError
from expcore_onedata.config import load_platform
from expcore_onedata.health import PlatformHealth
from expcore_onedata.testing import NOW, write_platform
from expcore_onedata.tls import WARNING
from expcore_onedata.tokens import TokenIssuer

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the openssl command makes the federation's test CA")

CA_NAME = "federation-ca.pem"  # the configured value of ca_file, relative to platform.toml


@pytest.fixture(scope="module")
def federation(tmp_path_factory):
    return make_federation_ca(tmp_path_factory.mktemp("federation"))


@pytest.fixture(scope="module")
def stranger(tmp_path_factory):
    """Another CA, which the federation's certificate does not chain to."""
    return make_federation_ca(tmp_path_factory.mktemp("stranger"), name="unrelated-test-ca")


def with_tls(tls, *, zone_url=None, beta_url=None):
    """An edit of the platform document that sets `[tls]` (and, when given, the zone's and provider beta's URLs)."""

    def edit(data):
        if tls is not None:
            data["tls"] = tls
        if zone_url is not None:
            data["zone"]["url"] = zone_url
        if beta_url is not None:
            data["providers"]["beta"]["url"] = beta_url

    return edit


def load(tmp_path, tls=None, *, ca=None, zone_url=None, beta_url=None, require_secrets=True):
    """Load the default platform document with `[tls]` set, the CA bundle (when given) copied beside it."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    if ca is not None:
        (tmp_path / CA_NAME).write_bytes(ca.read_bytes())
    edit = with_tls(tls, zone_url=zone_url, beta_url=beta_url)
    return load_platform(write_platform(tmp_path, edit), require_secrets=require_secrets)


# ---------------------------------------------------------------------------------------------------- the setting


def test_o12_without_a_tls_table_every_client_verifies_and_the_record_says_so(tmp_path, fake):
    """O12: no [tls] table is verification on, each client against its own default CAs, recorded as `verify`."""
    config = load(tmp_path)
    assert config.tls.record() == {"mode": "verify"}
    assert config.tls.ca_file is None
    assert config.tls.insecure is False
    assert config.fingerprint() == {"tls": {"mode": "verify"}}


def test_o12_a_ca_file_is_recorded_by_the_sha256_of_the_bundle_and_never_by_its_path_or_contents(
    tmp_path, fake, federation
):
    """O12: the record and the fingerprint hold the mode and the bundle's SHA-256, never its path or a line of it."""
    config = load(tmp_path, {"ca_file": CA_NAME}, ca=federation.ca)
    digest = hashlib.sha256(federation.ca.read_bytes()).hexdigest()
    assert config.tls.record() == {"mode": "ca_file", "ca_sha256": digest}
    assert config.fingerprint() == {"tls": {"mode": "ca_file", "ca_sha256": digest}}
    assert config.tls.ca_file == (tmp_path / CA_NAME).resolve()
    text = json.dumps([config.tls.record(), config.fingerprint()])
    assert CA_NAME not in text
    assert str(tmp_path) not in text
    assert not any(line in text for line in federation.ca.read_text().splitlines() if "-----" not in line)


def test_o12_insecure_tls_is_recorded_as_insecure(tmp_path, fake):
    """O12: `insecure_tls = true` turns verification off for every client, and the record says `insecure`."""
    config = load(tmp_path, {"insecure_tls": True})
    assert config.tls.insecure is True
    assert config.tls.record() == {"mode": "insecure"}
    assert load(tmp_path, {"insecure_tls": False}).tls.record() == {"mode": "verify"}


def test_o12_the_sandbox_settings_carry_the_platforms_one_tls_setting(tmp_path, fake, federation):
    """O12: the sandbox (the data gate and the sidecar) takes the same setting the file declares, not one of its own."""

    def edit(data):
        add_sandbox(data)
        data["tls"] = {"ca_file": CA_NAME}

    (tmp_path / CA_NAME).write_bytes(federation.ca.read_bytes())
    config = load_platform(write_platform(tmp_path, edit), require_secrets=False)
    assert config.sandbox.tls == config.tls
    assert config.sandbox.tls.record()["mode"] == "ca_file"


WRONG = {
    "both modes": ({"ca_file": CA_NAME, "insecure_tls": True}, "insecure_tls", "not both"),
    "a missing bundle": ({"ca_file": "absent-ca.pem"}, "ca_file", "cannot be read"),
    "not a boolean": ({"insecure_tls": "yes"}, "insecure_tls", "true or false"),
    "an unknown key": ({"verify": False}, "verify", "not a known key"),
}


@pytest.mark.parametrize("label", WRONG)
def test_o12_a_wrong_tls_table_is_refused_naming_the_key_and_never_the_path(tmp_path, fake, federation, label):
    """O12: each mistake fails at load, with the file and the key named and the bundle's path never echoed."""
    tls, key, words = WRONG[label]
    with pytest.raises(ConfigError, match=words) as caught:
        load(tmp_path, tls, ca=federation.ca)
    message = str(caught.value)
    assert f"'{key}'" in message
    assert "[tls]" in message
    assert CA_NAME not in message
    assert "absent-ca.pem" not in message


@pytest.mark.parametrize(
    ("content", "words"),
    [
        (b"not a certificate\n", "PEM bundle of certificates"),
        (b"", "PEM bundle of certificates"),
        (None, "private key"),  # the CA's key appended to its certificate
    ],
)
def test_o12_a_ca_file_that_is_not_a_bundle_of_certificates_alone_is_refused(
    tmp_path, fake, federation, content, words
):
    """O12: the bundle must parse as certificates and hold no private key, which would be mounted into containers."""
    bundle = tmp_path / CA_NAME
    if content is None:
        content = federation.ca.read_bytes() + (federation.directory / "ca.key").read_bytes()
    bundle.write_bytes(content)
    with pytest.raises(ConfigError, match=words) as caught:
        load_platform(write_platform(tmp_path, with_tls({"ca_file": CA_NAME})))
    assert "'ca_file'" in str(caught.value)
    assert CA_NAME not in str(caught.value)


@pytest.mark.parametrize(
    ("edit", "where"),
    [
        (lambda d: d["toolserver"].update(insecure_tls=True), "toolserver: key 'insecure_tls'"),
        (lambda d: (add_sandbox(d), d["sandbox"]["mount"].update(tls="insecure")), "[sandbox.mount]: key 'tls'"),
    ],
)
def test_o12_the_per_client_tls_keys_of_earlier_versions_are_refused_with_a_pointer_to_tls(tmp_path, fake, edit, where):
    """O12: one setting for all four clients: a client's own key, which could disagree with it, is refused at load."""
    with pytest.raises(ConfigError, match=r"\[tls\]") as caught:
        load_platform(write_platform(tmp_path, edit), require_secrets=False)
    assert where in str(caught.value)


def test_o12_a_bundle_that_changed_since_the_load_is_refused_before_a_client_uses_it(
    tmp_path, fake, federation, stranger
):
    """O12: a client gets the bundle's bytes only while they are the ones the record names; a replaced bundle is
    refused."""
    config = load(tmp_path, {"ca_file": CA_NAME}, ca=federation.ca)
    assert config.tls.bundle() == federation.ca.read_bytes()
    (tmp_path / CA_NAME).write_bytes(stranger.ca.read_bytes())
    with pytest.raises(ConfigError, match="changed since") as caught:
        config.tls.bundle()
    assert CA_NAME not in str(caught.value)
    (tmp_path / CA_NAME).unlink()
    with pytest.raises(ConfigError, match="can no longer be read"):
        config.tls.bundle()


# ---------------------------------------------------------------------------------------------------- the REST client

ADMIN = "admin-test-0123"  # an invented admin token


@pytest.mark.parametrize(
    ("tls", "bundle", "answered"),
    [
        (None, None, False),  # the default: the client's own CAs, which do not hold the federation's
        ({"ca_file": CA_NAME}, "federation", True),
        ({"ca_file": CA_NAME}, "stranger", False),  # verification stays on: another CA's bundle does not do
        ({"insecure_tls": True}, None, True),
    ],
    ids=["verify-default", "ca_file", "ca_file-of-another-ca", "insecure_tls"],
)
def test_o05_the_rest_client_checks_the_federations_certificate_as_the_tls_setting_says(
    tmp_path, fake, federation, stranger, tls, bundle, answered
):
    """O05: against a certificate no default trusts, the REST client fails by default and with another CA's bundle,
    and reaches the server with the federation's CA as ca_file or with insecure_tls; a failure names no address."""
    ca = {"federation": federation.ca, "stranger": stranger.ca, None: None}[bundle]
    with TlsServer(federation) as server:
        config = load(tmp_path, tls, ca=ca, zone_url=server.url)
        client = OnezoneClient(config, admin_token=ADMIN)  # as a run builds it: no transport injected
        try:
            if answered:
                assert client.get("/spaces/sp-test") == ANSWER
                assert server.seen == [("GET", "/api/v3/onezone/spaces/sp-test")]
            else:
                with pytest.raises(PlatformError) as caught:
                    client.get("/spaces/sp-test")
                assert caught.value.transport is True  # no answer came: the handshake was refused
                assert "127.0.0.1" not in str(caught.value)
                assert server.seen == []
        finally:
            client.close()


def test_o05_the_rest_client_trusts_the_verified_bytes_and_never_a_bundle_swapped_after_the_check(
    tmp_path, fake, monkeypatch, federation, stranger
):
    """O05: the bundle swapped for another CA's right after its check is not what the REST client trusts: it reaches
    the federation, whose CA was hashed, and refuses a server of the swapped-in CA; a client built after the swap is
    refused, since the bundle no longer has the recorded hash."""
    with TlsServer(federation) as genuine, TlsServer(stranger) as impostor:
        config = load(tmp_path, {"ca_file": CA_NAME}, ca=federation.ca, zone_url=genuine.url, beta_url=impostor.url)
        checks = swap_after_check(monkeypatch, tmp_path / CA_NAME, stranger.ca.read_bytes())
        client = OnezoneClient(config, admin_token=ADMIN)
        try:
            assert (tmp_path / CA_NAME).read_bytes() == stranger.ca.read_bytes()  # swapped once checked
            assert client.get("/spaces/sp-test") == ANSWER
            with pytest.raises(PlatformError) as caught:
                client.get("/spaces", provider="beta")
            assert caught.value.transport is True
            assert impostor.seen == []
        finally:
            client.close()
        with pytest.raises(ConfigError, match="changed since"):
            OnezoneClient(config, admin_token=ADMIN)
        assert len(checks) == 2


# ---------------------------------------------------------------------------------------------------- health


def health_reading(tmp_path, fake, tls, *, healthy=True):
    """One reading of the platform's health check over a configuration with this `[tls]`."""

    def edit(data):
        if tls is not None:
            data["tls"] = tls
        if not healthy:
            data["support"]["providers"] = ["alpha", "beta"]  # beta is declared but not reported: unhealthy

    config = load_platform(write_platform(tmp_path, edit))
    client = OnezoneClient(config, http=fake.client_http())
    return read_health(
        PlatformHealth(config, client, TokenIssuer(client, config, clock=lambda: NOW), clock=lambda: NOW)
    )


@pytest.mark.parametrize("healthy", [True, False], ids=["healthy", "unhealthy"])
def test_o05_health_warns_while_insecure_tls_is_on_and_never_otherwise(tmp_path, fake, federation, healthy):
    """O05: turning verification off is the one opt-out of the certificate check, so every health reading says so
    while it is on, whatever its verdict; the default and ca_file read as before."""
    (tmp_path / CA_NAME).write_bytes(federation.ca.read_bytes())
    plain = health_reading(tmp_path, fake, None, healthy=healthy)
    assert plain.ok is healthy
    assert health_reading(tmp_path, fake, {"ca_file": CA_NAME}, healthy=healthy) == plain
    insecure = health_reading(tmp_path, fake, {"insecure_tls": True}, healthy=healthy)
    assert insecure.ok is healthy
    assert insecure.detail == f"{plain.detail}; {WARNING}"
    assert "insecure_tls" in WARNING
    assert "disposable test federation" in WARNING
    assert "warning" not in plain.detail


# ---------------------------------------------------------------------------------------------------- the campaign


def test_c06_a_campaign_does_not_resume_under_another_tls_setting_of_its_platform(tmp_path, fake, federation, stranger):
    """C06: through the platform's half of the fingerprint, a campaign started under one TLS setting is refused a
    resume under another mode or under another CA bundle at the same path; the same setting resumes."""
    schema, manifest = make_schema(), make_manifest(make_schema())
    configs = {
        "ca_file": load(tmp_path / "a", {"ca_file": CA_NAME}, ca=federation.ca),
        "another bundle": load(tmp_path / "b", {"ca_file": CA_NAME}, ca=stranger.ca),
        "insecure_tls": load(tmp_path / "c", {"insecure_tls": True}),
        "verify-default": load(tmp_path / "d"),
    }

    def resume(config):
        fingerprint = make_fingerprint(schema, platform=config.fingerprint())
        store, _ = open_campaign(tmp_path / "campaign.db", schema, manifest, fingerprint, now=NOW, max_attempts=3)
        store.close()

    resume(configs["ca_file"])
    for label in ("another bundle", "insecure_tls", "verify-default"):
        with pytest.raises(FingerprintMismatch) as caught:
            resume(configs[label])
        assert all(line.startswith("platform.tls") for line in caught.value.differences), label
        assert CA_NAME not in str(caught.value)
    resume(configs["ca_file"])
