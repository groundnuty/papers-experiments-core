# Provenance: new
"""The openssl configuration a CA is made from, and the private file it is written to.

`openssl req -x509` applies the `x509_extensions` section of its default configuration. OpenSSL 1.1.1 adds the
extensions that `-addext` names on top of that section instead of replacing a same-named one, so a CA made with
`-addext basicConstraints=...` carries basicConstraints twice there (the default section already has it), and such a CA
is no trust anchor: `openssl verify` says error 20 and Python's ssl store does not load it. A CA is therefore made with
`-config` on a configuration of this module's, which names its extensions once and reads nothing of the host's default,
and with no `-addext`. The extensions are those OpenSSL 3's default configuration plus the old `-addext` gave.
"""

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

# `-subj` names the subject, but the request section must still name a section for it, hence `dn`. The key identifier
# comes before the authority key identifier, which copies it.
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


@contextlib.contextmanager
def private_file(directory: Path, text: str, suffix: str) -> Iterator[Path]:
    """A new file of mode 0600 in `directory` holding `text`, removed when the block ends, however it ends."""
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".openssl-", suffix=suffix)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        yield path
    finally:
        path.unlink(missing_ok=True)
