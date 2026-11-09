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
or `{"mode": "insecure"}`, never by the bundle's path or by anything of its contents beyond that hash. A client is
handed the bundle by `bundle()`, which hashes it again and refuses one that changed since the load, so the hash a record
holds is the bundle the clients used. A container gets it bind-mounted read-only; nothing copies it.
"""

import hashlib
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

    def bundle(self) -> Path:
        """The CA bundle's path, once it is shown to be the bundle that was loaded; ConfigError when it is not.

        The message names the key, never the path."""
        if self.ca_file is None:
            raise ValueError("the TLS setting has no CA bundle")
        try:
            digest = hashlib.sha256(self.ca_file.read_bytes()).hexdigest()
        except OSError as error:
            raise ConfigError(f"[tls] ca_file: the CA bundle can no longer be read ({type(error).__name__})") from None
        if digest != self.ca_sha256:
            raise ConfigError("[tls] ca_file: the CA bundle changed since platform.toml was loaded; load it again")
        return self.ca_file

    def client_verify(self) -> ssl.SSLContext | bool:
        """What a Python HTTP client takes as its `verify`: True (its own default CAs), a context that trusts the bundle
        alone, or False (no verification)."""
        if self.insecure:
            return False
        if self.ca_file is None:
            return True
        return ssl.create_default_context(cafile=str(self.bundle()))


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
    try:
        loaded = ssl.create_default_context(cafile=str(path)).cert_store_stats()["x509"]
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
