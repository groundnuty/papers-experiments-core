"""The federation's own CA, its services' web certificates, and the generated secrets.

Everything is made with the openssl command under a 077 umask and checked afterwards: a private key or a secret is a
regular file of mode 0600 in a directory of mode 0700, and nothing here prints a value. The CA certificate is the one
public file; the core points `[tls] ca_file` at it.
"""

import os
import secrets
import subprocess
import tempfile
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path

CA_DAYS = 30
LEAF_DAYS = 30
SECRET_MODE = 0o600
DIRECTORY_MODE = 0o700


class PkiError(RuntimeError):
    """Making or checking a certificate or a secret failed; the message names the file's role, never a value."""


@contextmanager
def private_umask():
    """Create files that no group or other can read, for the length of the block."""
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def openssl(*args: str) -> None:
    """Run the openssl command; a failure names the subcommand and never echoes its output, which can hold a key."""
    done = subprocess.run(["openssl", *args], capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise PkiError(f"openssl {args[0]} failed with status {done.returncode}")


# The CA's extensions, each declared once, in a configuration of their own with no `-addext`: on OpenSSL 1.1.1 (EL8's)
# `req -x509` applies the default configuration's `v3_ca` section and then adds what `-addext` names, so a CA made with
# `-addext basicConstraints=...` carried it twice and was no trust anchor (`openssl verify`: error 20). The same text is
# `expcore_onedata.sandbox.caconfig.CA_CONFIG`; this script imports nothing of the core.
CA_CONFIG = """\
[req]
distinguished_name = dn
prompt = no
default_md = sha256
x509_extensions = ca_extensions

[dn]
CN = ca

[ca_extensions]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,cRLSign
"""


@contextmanager
def private_file(directory: Path, text: str, suffix: str):
    """A new file of mode 0600 in `directory` holding `text`, removed when the block ends, however it ends."""
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".openssl-", suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        yield path
    finally:
        path.unlink(missing_ok=True)


def private_directory(path: Path) -> Path:
    """Make a directory of mode 0700 (and its parents), or tighten an existing one to it."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(DIRECTORY_MODE)
    return path


def write_secret(path: Path, value: str) -> Path:
    """Write a secret to a new file of mode 0600; refuse to replace one, so a rerun never changes a live credential."""
    if path.exists():
        raise PkiError(f"{path.name}: a secret file already exists and is never overwritten")
    with private_umask():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, SECRET_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
    return path


def ensure_secret(path: Path, length: int = 24) -> str:
    """Return the secret in `path`, generating it first when the file does not exist."""
    if not path.exists():
        write_secret(path, secrets.token_urlsafe(length))
    require_private(path)
    return path.read_text(encoding="utf-8").strip()


def require_private(path: Path) -> None:
    """Refuse a secret or key that is not a regular file of mode 0600 owned by the current user."""
    info = path.lstat()
    if not path.is_file() or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PkiError(f"{path.name}: must be a regular file of mode 0600 owned by the current user")


def make_ca(ca_pem: Path, ca_key: Path, name: str) -> None:
    """Make the federation's self-signed CA: its key in `ca_key` (0600), its certificate in `ca_pem` (0644)."""
    if ca_pem.exists() and ca_key.exists():
        require_private(ca_key)
        return
    with private_umask(), private_file(ca_key.parent, CA_CONFIG, ".cnf") as config:
        openssl(
            "req", "-x509", "-newkey", "rsa:3072", "-nodes", "-sha256", "-days", str(CA_DAYS),
            "-keyout", str(ca_key), "-out", str(ca_pem), "-subj", f"/CN={name}", "-config", str(config),
        )  # fmt: skip
    ca_pem.chmod(0o644)
    require_private(ca_key)


def subject_alt_names(domain: str, node: str, addresses: Iterable[str]) -> str:
    """The subjectAltName of a service: its domain, its node's name in it, and the addresses it is published on."""
    names = [f"DNS:{domain}", f"DNS:{node}.{domain}", *(f"IP:{address}" for address in addresses)]
    return ",".join(names)


def make_leaf(ca_pem: Path, ca_key: Path, key: Path, cert: Path, domain: str, sans: str, work: Path) -> None:
    """Make a service's key (0600) and its web certificate, signed by the CA, for the names in `sans`."""
    if key.exists() and cert.exists():
        require_private(key)
        return
    csr, ext = work / f"{domain}.csr", work / f"{domain}.ext"
    ext.write_text(
        f"subjectAltName={sans}\nbasicConstraints=CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth,clientAuth\n"
        "subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n",  # OpenSSL 3 adds both itself, 1.1.1 neither
        encoding="utf-8",
    )
    with private_umask():
        openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(csr), "-subj", f"/CN={domain}")
        openssl(
            "x509", "-req", "-in", str(csr), "-CA", str(ca_pem), "-CAkey", str(ca_key), "-sha256",
            "-set_serial", f"0x{secrets.token_hex(16)}", "-days", str(LEAF_DAYS), "-extfile", str(ext),
            "-out", str(cert),
        )  # fmt: skip
    csr.unlink()
    ext.unlink()
    cert.chmod(0o644)
    require_private(key)


def verify_leaf(ca_pem: Path, cert: Path, host: str) -> None:
    """Check that a certificate chains to the CA and names `host`, as a client that verifies would."""
    flag = "-verify_ip" if host.replace(".", "").isdigit() else "-verify_hostname"
    openssl("verify", "-CAfile", str(ca_pem), flag, host, str(cert))
