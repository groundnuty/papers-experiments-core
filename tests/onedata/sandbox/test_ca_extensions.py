"""O92: the CA a run's gate trusts declares each extension once and is a trust anchor on any OpenSSL, whatever the
openssl command's default configuration adds.

The Apptainer sandbox makes its run CA with `DockerHost.make_ca`, and the tests' own TLS server with
`make_federation_ca`. On OpenSSL 1.1.1 (an EL8 HPC node's command) a CA made through `-addext` carried basicConstraints
twice and could not be a trust anchor. A host with OpenSSL 3 merges the two quietly, so the check here reads the made
CA's extensions, and runs the makers under a default configuration that adds extensions of its own. Host names are
invented (`.test`); nothing leaves the machine.
"""

from pathlib import Path

import pytest
from ca_checks import (
    DEFAULT_CONFIG_THAT_ADDS_EXTENSIONS,
    HAS_OPENSSL,
    assert_leaf_names_its_key_and_its_issuers_key,
    assert_one_of_each_expected_extension,
    handshake,
    openssl_verifies,
)
from tls_support import make_federation_ca

from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.hostops import DockerHost

pytestmark = pytest.mark.skipif(not HAS_OPENSSL, reason="the openssl command makes the CA")


def sandbox_ca(directory: Path) -> tuple[Path, Path, Path, str]:
    """The run CA the sandbox makes, and the leaf it signs for `zone.test`: CA, leaf, leaf key, the name it carries."""
    DockerHost().make_ca(directory, ["zone.test"])
    return directory / "ca.pem", directory / "zone.test.pem", directory / "zone.test.key", "zone.test"


def tls_server_ca(directory: Path) -> tuple[Path, Path, Path, str]:
    """The test server's CA and its leaf for 127.0.0.1."""
    made = make_federation_ca(directory)
    return made.ca, made.cert, made.key, "127.0.0.1"


MAKERS = pytest.mark.parametrize("maker", [sandbox_ca, tls_server_ca], ids=["sandbox", "tests-tls-server"])


@MAKERS
@pytest.mark.parametrize("default_config", ["the host's own", "one that adds extensions"])
def test_o92_a_made_ca_has_each_extension_once_whatever_the_default_config_adds(
    maker, default_config, tmp_path, monkeypatch
):
    """O92: the made CA's extensions are exactly those a CA always had (key identifiers, basicConstraints critical
    CA:TRUE, keyUsage critical keyCertSign and cRLSign), each once, and the default configuration adds none."""
    if default_config != "the host's own":
        config = tmp_path / "default.cnf"
        config.write_text(DEFAULT_CONFIG_THAT_ADDS_EXTENSIONS, encoding="utf-8")
        monkeypatch.setenv("OPENSSL_CONF", str(config))
    ca, *_ = maker(tmp_path / "pki")
    assert_one_of_each_expected_extension(ca)


@MAKERS
def test_o92_a_made_ca_is_a_trust_anchor_for_the_leaf_it_signed(maker, tmp_path):
    """O92: Python's ssl store loads the CA and accepts its leaf's chain even in strict mode, `openssl verify`
    accepts it, and the leaf carries the key identifiers a strict client needs (1.1.1 adds none itself)."""
    ca, cert, key, host = maker(tmp_path / "pki")
    handshake(ca, cert, key, host)
    assert openssl_verifies(ca, cert)
    assert_leaf_names_its_key_and_its_issuers_key(cert)


def test_o92_a_failed_ca_leaves_no_configuration_file_behind(tmp_path):
    """O92: the configuration file the CA is made from is removed when openssl fails too."""
    directory = tmp_path / "pki"
    directory.mkdir()
    (directory / "ca.key").mkdir()  # openssl cannot write the key there
    with pytest.raises(SandboxError):
        DockerHost().make_ca(directory, ["zone.test"])
    assert not [path.name for path in directory.iterdir() if path.suffix == ".cnf"]


def test_o92_a_made_ca_leaves_no_configuration_file_behind_and_its_key_is_private(tmp_path):
    """O92: nothing but the certificates, their requests and extension files stays, and the CA's key is mode 0600."""
    sandbox_ca(tmp_path / "pki")
    names = sorted(path.name for path in (tmp_path / "pki").iterdir())
    assert not [name for name in names if name.endswith(".cnf")], names
    assert (tmp_path / "pki" / "ca.key").stat().st_mode & 0o777 == 0o600
