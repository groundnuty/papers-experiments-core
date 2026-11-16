"""The zone's CA as the federation's root keeps it (`ca/federation-ca.pem`, `platform.toml`'s `ca_file`): the PEM
`CERTIFICATE` blocks of what `bundle/zone-export.sh ca` printed, and nothing else (DESIGN.md, section 5).

The image's test CA file (`OnedataTestWebServerCa.pem`) holds the CA's private key before its certificate. Every block
that is not a certificate (a private key of any kind, encrypted or not, parameters, anything else) is left out, so no
key ever reaches the root; the core's refusal of a key in `ca_file` (`expcore_onedata.tls`) stays as the backstop.

The CA is the image's own, the same for every federation of one image, and is kept so: its digest is in the campaign
fingerprint (`PlatformConfig.fingerprint()`, through the TLS setting's `record()`), so a CA made per federation would
stop shards run on different federations from merging, as a Onezone id did. If a per-federation CA is ever wanted, its
digest must leave the fingerprint first.
"""

import base64
import binascii
import re
from typing import List, Optional, Tuple

BEGIN = re.compile(r"-----BEGIN ([A-Z0-9][A-Z0-9 ]{0,63})-----")
BASE64_LINE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
CERTIFICATE = "CERTIFICATE"


class CaExportError(RuntimeError):
    """An export of the zone's CA the root cannot keep: no certificate, or a block that is not whole."""


def _blocks(text: str) -> List[Tuple[str, List[str]]]:
    """Every PEM block of the text, by its BEGIN and END lines, as (type, body lines); text outside the blocks is
    passed by. A block left open, or one that opens inside another, raises."""
    found: List[Tuple[str, List[str]]] = []
    kind: Optional[str] = None
    body: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if kind is None:
            begin = BEGIN.fullmatch(line)
            if begin is not None:
                kind, body = begin.group(1), []
            continue
        if line == f"-----END {kind}-----":
            found.append((kind, body))
            kind = None
        elif line.startswith("-----"):
            raise CaExportError(f"a PEM {kind} block is not closed before the next line of dashes")
        else:
            body.append(line)
    if kind is not None:
        raise CaExportError(f"a PEM {kind} block is never closed")
    return found


def ca_certificates(text: str) -> str:
    """The certificates of an exported CA, in their order, each block written whole; every other block left out.
    Raises `CaExportError` for an export with no certificate, a block that is not whole, or a certificate whose body
    is not base64. The message names block types only, never a block's content."""
    certificates = []
    for kind, body in _blocks(text):
        if kind != CERTIFICATE:
            continue
        lines = [line for line in body if line]
        if not lines or not all(BASE64_LINE.fullmatch(line) for line in lines):
            raise CaExportError("a CERTIFICATE block's body is not base64")
        try:
            base64.b64decode("".join(lines), validate=True)
        except (binascii.Error, ValueError):
            raise CaExportError("a CERTIFICATE block's body is not base64") from None
        certificates.append("-----BEGIN CERTIFICATE-----\n" + "\n".join(lines) + "\n-----END CERTIFICATE-----\n")
    if not certificates:
        raise CaExportError("the zone's exported CA holds no certificate")
    kept = "".join(certificates)
    if "PRIVATE KEY" in kept:  # never, as only certificate blocks are written; the core refuses a key in ca_file too
        raise CaExportError("the kept CA would hold a private key")
    return kept
