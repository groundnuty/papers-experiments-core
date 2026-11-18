"""Checks on a CA that one of the core's makers produced: its extensions, and whether it works as a trust anchor.

A maker that leaves the `openssl` command's own default configuration to add extensions breaks on OpenSSL 1.1.1: its
`req -x509` applies the default `x509_extensions` section and then `-addext` on top, so basicConstraints is written
twice, and a CA like that is no trust anchor (`openssl verify` says error 20, Python's ssl store does not load it). Here
the made certificate is read through whatever `openssl` the host has, and a default configuration that adds an extension
of its own stands in for that duplicate on an OpenSSL 3 host, which merges the two quietly.
"""

import shutil
import ssl
import subprocess
from pathlib import Path

HAS_OPENSSL = shutil.which("openssl") is not None

# Every extension a made CA carries, by the name `openssl x509 -text` prints, with whether it is critical and its value
# line. The two key identifiers are hashes of the key, so their values are not compared.
EXPECTED_CA_EXTENSIONS = [
    ("X509v3 Authority Key Identifier", False, None),
    ("X509v3 Basic Constraints", True, ("CA:TRUE",)),
    ("X509v3 Key Usage", True, ("Certificate Sign, CRL Sign",)),
    ("X509v3 Subject Key Identifier", False, None),
]
KEY_IDENTIFIERS = ("X509v3 Authority Key Identifier", "X509v3 Subject Key Identifier")

# A configuration like a distribution's default: `req` applies a section holding basicConstraints, as OpenSSL 1.1.1's
# does, plus a comment that no CA of the core's carries.
DEFAULT_CONFIG_THAT_ADDS_EXTENSIONS = """\
[req]
distinguished_name = dn
x509_extensions = from_the_default_config
[dn]
CN = nobody
[from_the_default_config]
basicConstraints = critical,CA:TRUE
nsComment = added by the default configuration
"""


def extensions_of(cert: Path) -> list[tuple[str, bool, tuple[str, ...] | None]]:
    """The extensions of a certificate in the order `openssl x509 -text` prints them: name, critical, value lines."""
    done = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-noout", "-text"], capture_output=True, text=True, check=True
    )
    lines = done.stdout.splitlines()
    after = lines[next(n for n, line in enumerate(lines) if line.strip() == "X509v3 extensions:") + 1 :]
    margin = len(after[0]) - len(after[0].lstrip())
    found: list[tuple[str, bool, list[str]]] = []
    for line in after:
        indent = len(line) - len(line.lstrip())
        if indent < margin:
            break
        if indent == margin:
            name, _, flag = line.strip().partition(":")
            found.append((name, flag.strip() == "critical", []))
        else:
            found[-1][2].append(line.strip())
    return [(name, critical, None if name in KEY_IDENTIFIERS else tuple(values)) for name, critical, values in found]


def assert_one_of_each_expected_extension(ca: Path) -> None:
    """Fail on a duplicate extension (what 1.1.1 made) and on any difference from the extensions a CA always had."""
    found = extensions_of(ca)
    names = [name for name, _, _ in found]
    assert len(names) == len(set(names)), f"an extension is written twice: {names}"
    assert sorted(found) == sorted(EXPECTED_CA_EXTENSIONS), f"the CA's extensions are {found}"


def assert_leaf_names_its_key_and_its_issuers_key(cert: Path) -> None:
    """Fail unless a leaf has a subject and an authority key identifier, each once: OpenSSL 3's `x509 -CA` adds them
    itself and 1.1.1's does not, and a strict client (Python 3.13's default) refuses a chain whose leaf has no
    authority key identifier."""
    names = [name for name, _, _ in extensions_of(cert)]
    for wanted in KEY_IDENTIFIERS:
        assert names.count(wanted) == 1, f"{wanted} appears {names.count(wanted)} times in {names}"


def handshake(ca: Path, cert: Path, key: Path, host: str) -> None:
    """Run a TLS handshake in memory between a client that trusts only `ca` and a server presenting `cert`.

    It raises `ssl.SSLCertVerificationError` when the client does not accept the chain for `host`."""
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(str(cert), str(key))
    client_context = ssl.create_default_context(cafile=str(ca))
    # Strict as Python 3.13's default is; 3.12's is not, and a run's client may be either.
    client_context.verify_flags |= ssl.VERIFY_X509_STRICT
    assert len(client_context.get_ca_certs()) == 1, "Python's store did not load the CA"
    to_client, from_client, to_server, from_server = (ssl.MemoryBIO() for _ in range(4))
    client = client_context.wrap_bio(to_client, from_client, server_hostname=host)
    server = server_context.wrap_bio(to_server, from_server, server_side=True)
    pending = [client, server]
    for _ in range(10):
        pending = [end for end in pending if not _finished(end)]
        to_server.write(from_client.read())  # what each end said goes to the other
        to_client.write(from_server.read())
        if not pending:
            return
    raise AssertionError("the handshake did not finish")


def _finished(end: ssl.SSLObject) -> bool:
    try:
        end.do_handshake()
    except ssl.SSLWantReadError:
        return False
    return True


def openssl_verifies(ca: Path, cert: Path) -> bool:
    """Whether the `openssl verify` command accepts `cert` with `ca` as the only trust anchor."""
    done = subprocess.run(["openssl", "verify", "-CAfile", str(ca), str(cert)], capture_output=True, text=True)
    return done.returncode == 0
