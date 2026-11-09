# Provenance: new
"""The one TLS setting of a federation, `[tls]` of platform.toml, which every client of the federation takes (O05, O12).

Four clients reach a federation, and each takes its certificate check from this one setting, so no two can differ:
- the platform layer's REST client (`client.OnezoneClient`);
- the data gate's upstream connection, the agent's REST data path in the sandbox (`sandbox/gate.py`);
- oneclient in the mount sidecar (`sandbox/containers.py`);
- the MCP tool server (`toolserver.py`).

```toml
[tls]                           # optional; without it, verification is on, each client against its own default CAs
ca_file = "federation-ca.pem"   # a CA bundle the federation's certificate chains to, relative to this file:
                                #   verification stays on, against this bundle
insecure_tls = true             # or: verification off, for a disposable test federation only; never with ca_file
```

The bundle is read, checked to hold certificates and no private key, and hashed when the file is loaded. A record and
the campaign fingerprint name the setting by `record()`: `{"mode": "verify"}`, `{"mode": "ca_file", "ca_sha256": ...}`
or `{"mode": "insecure"}`, never by the bundle's path or by anything of its contents beyond that hash.

No client reads the configured file. `bundle()` reads it once, refuses it unless its bytes have the hash the record
holds, and returns those bytes; a client uses them and never the path, which anyone who can write the file could swap
after the check. The REST client loads them into its context (`cadata`). A run that hands them to another process
writes them once, with `write_private_copy`, to a read-only file of its own in a directory of its own, and gives that
process the copy: the sandbox's gate and sidecar get it bind-mounted read-only, the tool server gets its path. So what
every client trusts is the bytes that were hashed, and a bundle swapped after the check is never trusted.
"""

import hashlib
import os
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from expcore.config.loading import ConfigError, Section

VERIFY, CA_FILE, INSECURE = "verify", "ca_file", "insecure"
# The settings a client process reads for its CA bundle: Python's OpenSSL and httpx, requests, curl. A process given the
# bundle (the tool server) gets all three, so whichever its HTTP client reads names the same file.
CA_BUNDLE_ENV = ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
WARNING = (
    "warning: TLS verification is off for every client of the federation ([tls] insecure_tls), which is for a "
    "disposable test federation only"
)
_UNSAFE_PATH = (":", ",")  # characters docker's mount syntax splits on: a bundle mounted by such a path would break
CA_COPY_NAME = "federation-ca.pem"  # the run's own read-only copy of the verified bytes, in the run's directory
CA_COPY_MODE = 0o444


@dataclass(frozen=True)
class TlsSettings:
    """How every client of the federation checks its certificate: `verify` (the default), `ca_file` or `insecure`."""

    ca_file: Path | None = None  # resolved; None unless the mode is ca_file
    ca_sha256: str | None = None  # of the bundle as loaded
    insecure: bool = False

    def __post_init__(self) -> None:
        if (self.ca_file is None) != (self.ca_sha256 is None):
            raise ValueError("a CA bundle and its hash come together")
        if self.insecure and self.ca_file is not None:
            raise ValueError("verification is either against a CA bundle or off, not both")

    @property
    def mode(self) -> str:
        """`verify`, `ca_file` or `insecure`."""
        return INSECURE if self.insecure else CA_FILE if self.ca_file is not None else VERIFY

    def record(self) -> dict[str, Any]:
        """The setting as a record and the fingerprint hold it: the mode, and with ca_file the bundle's SHA-256."""
        return {"mode": self.mode, "ca_sha256": self.ca_sha256} if self.ca_file is not None else {"mode": self.mode}

    def bundle(self) -> bytes:
        """The CA bundle's bytes, read once and returned only when they have the hash the record holds; ConfigError
        when they do not. A client uses these bytes, never the configured path. The message names the key only."""
        if self.ca_file is None:
            raise ValueError("the TLS setting has no CA bundle")
        try:
            content = self.ca_file.read_bytes()
        except OSError as error:
            raise ConfigError(f"[tls] ca_file: the CA bundle can no longer be read ({type(error).__name__})") from None
        if hashlib.sha256(content).hexdigest() != self.ca_sha256:
            raise ConfigError("[tls] ca_file: the CA bundle changed since platform.toml was loaded; load it again")
        return content

    def client_verify(self) -> ssl.SSLContext | bool:
        """What a Python HTTP client takes as its `verify`: True (its own default CAs), a context that trusts the
        verified bytes of the bundle alone, loaded from memory, or False (no verification)."""
        if self.insecure:
            return False
        if self.ca_file is None:
            return True
        return ssl.create_default_context(cadata=_pem_text(self.bundle()))


def _pem_text(content: bytes) -> str:
    """The bundle as the text `cadata` takes; ValueError when it is not ASCII, as PEM is."""
    return content.decode("ascii")


def write_private_copy(content: bytes, directory: Path) -> Path:
    """Write verified bundle bytes once to `directory`, a directory of the run's own, as a new read-only file (mode
    0444) that the run owns; return its path. An existing file there is refused, so nothing is written over."""
    target = directory / CA_COPY_NAME
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), CA_COPY_MODE)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    os.chmod(target, CA_COPY_MODE)  # whatever the umask took away, the copy is readable by every container's user
    return target


def _ca_file(section: Section, base: Path) -> TlsSettings:
    """Read `ca_file`: a readable PEM bundle of certificates and nothing else, by a path docker can mount."""
    path = (base / section.string("ca_file")).resolve()
    if any(mark in str(path) for mark in _UNSAFE_PATH):
        raise section.fail("ca_file", "must be a path without ':' or ',', which docker's mount syntax splits on")
    try:
        content = path.read_bytes()
    except OSError as error:
        raise section.fail("ca_file", f"cannot be read ({type(error).__name__})") from None
    if b"PRIVATE KEY" in content:
        raise section.fail("ca_file", "holds a private key; it must hold the CA's certificates only")
    try:  # loaded from the bytes, as the REST client loads them
        loaded = ssl.create_default_context(cadata=_pem_text(content)).cert_store_stats()["x509"]
    except (ssl.SSLError, ValueError):
        loaded = 0
    if not loaded:
        raise section.fail("ca_file", "is not a PEM bundle of certificates")
    return TlsSettings(ca_file=path, ca_sha256=hashlib.sha256(content).hexdigest())


def load_tls(section: Section, base: Path) -> TlsSettings:
    """Read the optional `[tls]` table; raise ConfigError naming the file and the key, never the bundle's path."""
    section.reject_unknown(("ca_file", "insecure_tls"))
    insecure = section.data.get("insecure_tls", False)
    if not isinstance(insecure, bool):
        raise section.fail("insecure_tls", "must be true or false")
    if insecure and "ca_file" in section.data:
        raise section.fail(
            "insecure_tls",
            "cannot be true beside ca_file: verify against the bundle or turn verification off, not both",
        )
    if "ca_file" in section.data:
        return _ca_file(section, base)
    return TlsSettings(insecure=insecure)


def moved(section: Section, key: str) -> ConfigError:
    """The refusal of a client's own TLS key of earlier versions, which could disagree with `[tls]`."""
    return section.fail(
        key,
        "is no longer read: one [tls] table sets the certificate check of every client of the federation "
        "(ca_file, or insecure_tls = true)",
    )
