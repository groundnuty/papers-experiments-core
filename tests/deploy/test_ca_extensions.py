"""O92: the Compose federation's CA declares each extension once and is a trust anchor on any OpenSSL, whatever the
openssl command's default configuration adds.

`fedpki.make_ca` made its CA with `-addext` on top of the command's default configuration, which on OpenSSL 1.1.1 (an
EL8 HPC node's command) wrote basicConstraints twice: `openssl verify` said error 20 and Python's ssl store refused it.
A host with OpenSSL 3 merges the two quietly, so the check reads the made CA's extensions, and also runs the maker
under a default configuration that adds an extension of its own. Names are invented (`.test`); nothing leaves the
machine.
"""

import pytest
from ca_checks import (
    DEFAULT_CONFIG_THAT_ADDS_EXTENSIONS,
    HAS_OPENSSL,
    assert_one_of_each_expected_extension,
    handshake,
    openssl_verifies,
)

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the openssl command makes the CA")

DOMAIN = "svc.test"


@pytest.fixture
def ca_and_leaf(fed, tmp_path):
    """The CA `fedpki` makes and a service certificate it signs: CA, key of the CA, the leaf, its key."""
    pki = fed.pki
    ca, ca_key, key, cert = tmp_path / "ca.pem", tmp_path / "ca.key", tmp_path / "svc.key", tmp_path / "svc.pem"
    pki.make_ca(ca, ca_key, "test CA")
    pki.make_leaf(ca, ca_key, key, cert, DOMAIN, pki.subject_alt_names(DOMAIN, "node1", ["127.0.0.1"]), tmp_path)
    return ca, ca_key, cert, key


@pytest.mark.parametrize("default_config", ["the host's own", "one that adds extensions"])
def test_o92_a_made_ca_has_each_extension_once_whatever_the_default_config_adds(
    fed, default_config, tmp_path, monkeypatch
):
    """O92: the made CA's extensions are exactly those a CA always had (key identifiers, basicConstraints critical
    CA:TRUE, keyUsage critical keyCertSign and cRLSign), each once, and the default configuration adds none."""
    if default_config != "the host's own":
        config = tmp_path / "default.cnf"
        config.write_text(DEFAULT_CONFIG_THAT_ADDS_EXTENSIONS, encoding="utf-8")
        monkeypatch.setenv("OPENSSL_CONF", str(config))
    fed.pki.make_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "test CA")
    assert_one_of_each_expected_extension(tmp_path / "ca.pem")


def test_o92_a_made_ca_is_a_trust_anchor_for_the_leaf_it_signed(ca_and_leaf):
    """O92: Python's ssl store loads the CA and accepts its leaf's chain, and `openssl verify` accepts it."""
    ca, _, cert, key = ca_and_leaf
    handshake(ca, cert, key, DOMAIN)
    assert openssl_verifies(ca, cert)


def test_o92_a_made_ca_leaves_no_configuration_file_behind_and_its_key_is_private(ca_and_leaf, tmp_path):
    """O92: the made CA leaves nothing but its certificate and key, and the key is mode 0600."""
    _, ca_key, _, _ = ca_and_leaf
    assert not [path.name for path in tmp_path.iterdir() if path.suffix == ".cnf"]
    assert ca_key.stat().st_mode & 0o777 == 0o600


def test_o92_a_failed_ca_leaves_no_configuration_file_behind(fed, tmp_path):
    """O92: the configuration file the CA is made from is removed when openssl fails too."""
    (tmp_path / "ca.key").mkdir()  # openssl cannot write the key there
    with pytest.raises(fed.pki.PkiError):
        fed.pki.make_ca(tmp_path / "ca.pem", tmp_path / "ca.key", "test CA")
    assert not [path.name for path in tmp_path.iterdir() if path.suffix == ".cnf"]


def test_o92_the_script_and_the_core_declare_the_same_ca_extensions(fed):
    """O92: the deploy script carries a copy of the core's CA configuration (it imports nothing of the core), so the
    two must stay the same text."""
    from expcore_onedata.sandbox.caconfig import CA_CONFIG

    assert fed.pki.CA_CONFIG == CA_CONFIG
