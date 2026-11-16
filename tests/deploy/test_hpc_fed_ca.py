"""The zone's CA as an HPC federation's root keeps it (deploy/hpc-federation/hpcca.py, DESIGN.md section 5): only the
PEM certificate blocks of what `zone-export.sh ca` printed, in their order. The image's test CA file holds the CA's
private key before its certificate (seen live: every `up` failed in the core's phase, which refuses a key in
`ca_file`); a key block of any kind is left out, and an export with no certificate is refused."""

import base64

import pytest
from hpc_fed_support import hpc_on_path


@pytest.fixture(scope="module")
def ca():
    with hpc_on_path():
        import hpcca

        yield hpcca


def block(kind: str, body: str, headers: str = "") -> str:
    """One PEM block; the markers are built here, so no file of the repository holds a key's literal header."""
    return f"-----BEGIN {kind}-----\n{headers}{body}\n-----END {kind}-----\n"


def body(text: bytes) -> str:
    """A base64 body in 64-column lines, as a PEM writer folds it (the bytes stand in for DER)."""
    encoded = base64.b64encode(text).decode("ascii")
    return "\n".join(encoded[i : i + 64] for i in range(0, len(encoded), 64))


CERT = block("CERTIFICATE", body(b"a stand-in for the test CA's certificate, long enough to fold onto two lines"))
OTHER = block("CERTIFICATE", body(b"a stand-in for an intermediate certificate"))
RSA_KEY = block("RSA PRIVATE KEY", body(b"a stand-in for the test CA's key"))
ENCRYPTED = block("ENCRYPTED PRIVATE KEY", body(b"a stand-in for an encrypted key"))
LEGACY_HEADERS = "Proc-Type: 4,ENCRYPTED\nDEK-Info: AES-128-CBC,00\n\n"  # OpenSSL's legacy encrypted form
OLD_ENCRYPTED = block("RSA PRIVATE KEY", body(b"a stand-in for a legacy encrypted key"), LEGACY_HEADERS)


def test_a_key_block_before_the_certificate_leaves_the_certificate_alone(ca):
    """O57: the image's CA file as seen live, its key first and its certificate after: the certificate is kept whole,
    and nothing of the key."""
    kept = ca.ca_certificates(RSA_KEY + "\n" + CERT)
    assert kept == CERT
    assert "PRIVATE KEY" not in kept


@pytest.mark.parametrize("key", [RSA_KEY, ENCRYPTED, OLD_ENCRYPTED, block("EC PRIVATE KEY", body(b"x" * 32))])
def test_an_export_of_a_key_alone_is_refused(ca, key):
    """O57: a key block of any kind, encrypted or not, with no certificate beside it is refused, naming no content."""
    with pytest.raises(ca.CaExportError, match="holds no certificate"):
        ca.ca_certificates(key)


def test_every_kind_of_key_and_any_other_block_is_left_out(ca):
    """O57: only CERTIFICATE blocks are kept; a PKCS#8 key, an encrypted one, an EC key and parameters are not."""
    text = "".join(
        [ENCRYPTED, CERT, block("PRIVATE KEY", body(b"k" * 40)), block("EC PARAMETERS", body(b"p" * 9)), OLD_ENCRYPTED]
    )
    assert ca.ca_certificates(text) == CERT


def test_a_chain_of_certificates_is_kept_in_its_order(ca):
    """O57: two certificates are both kept, in the order the export printed them, with the text between them gone."""
    kept = ca.ca_certificates("subject=CN = test\n" + OTHER + RSA_KEY + CERT)
    assert kept == OTHER + CERT


@pytest.mark.parametrize(
    "text",
    [
        "-----BEGIN CERTIFICATE-----\nTUlJQg==\n",  # never closed
        "-----BEGIN CERTIFICATE-----\nTUlJQg==\n" + CERT,  # opens again before it closes
        block("CERTIFICATE", "not base64 at all!"),
        block("CERTIFICATE", ""),
        "",
    ],
)
def test_an_export_that_is_not_whole_pem_is_refused(ca, text):
    """O57: a block left open, a certificate whose body is not base64, or nothing at all: refused, never written."""
    with pytest.raises(ca.CaExportError):
        ca.ca_certificates(text)
