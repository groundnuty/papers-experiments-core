# Provenance: new
"""The leak checks over the wheels a package vendors: every member of every wheel is opened, always.

A wheel is opened member by member. A member that is UTF-8 text without NUL bytes (Python source, METADATA, RECORD,
entry points, licences) is read; any other member is a compiled binary or data that is not text (an extension module,
a scientific wheel's test data), which no check can read: it goes in attested by its SHA-256, listed per wheel in
`PACKAGE.json` (`binaries`). Text is read with the study-token list, and in an anonymised package with the terms of
the anonymisation list and the identity it must not hold (`identity.find_identity`'s commits and URLs). The one
exception is the study-token list for a wheel proven to be a pypi.org release, whose bytes the study's `uv.lock`
records (`lockfile`): the anonymisation list and the identity still apply to it.

How deep a member is read depends on where its wheel came from. A wheel of a distribution installed from a direct
URL, or supplied by the author, can be the author's own code, so its text members get every view the package's
checks use for any file (`views`: escapes, entities, base64 and hex runs decoded). A wheel packed from an index
install, or one the lock proves a release, is read as it is, line by line: its text runs to tens of megabytes, and
the decoded views would take an hour where the plain read takes a minute or two. Either way a finding refuses the
build. A member the author has read and found clean (a public release's code that merely contains a word the token
list also holds) can be attested in `package.toml` like any file, by its path inside the package and its SHA-256:

    [[opaque]]
    path = "wheels/<wheel file>/<member>"
    sha256 = "<the member's SHA-256>"
    reason = "read by hand; the word is the library's own"

A finding prints exactly that path and hash. In an anonymised build a wheel that holds a `direct_url.json` is
refused: that file names a URL and a commit.
"""

import hashlib
import io
import zipfile
import zlib
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from expcore.package.anonymise import Anonymisation, find_terms, holds_term, term_lines
from expcore.package.identity import FOUND_COMMIT, FOUND_URL, commit_lines, find_identity, url_lines
from expcore.package.leaks import describe_hit, scan_tokens
from expcore.package.spec import Opaque, PackageError
from expcore.package.wheels import Wheel
from expcore.provenance.hashfile import Hashes
from expcore.provenance.redact import Redactor
from expcore.provenance.scanner import scan_source

DIRECT_URL = "direct_url.json"


@dataclass(frozen=True)
class WheelScan:
    """What reading the wheels found, and the members of each wheel that are not text, by wheel path."""

    findings: list[str] = field(default_factory=list)
    binaries: Mapping[str, list[dict[str, str]]] = field(default_factory=dict)


_UNREADABLE = (zipfile.BadZipFile, OSError, EOFError, RuntimeError, NotImplementedError, ValueError, zlib.error)


def members(wheel: Wheel, redactor: Redactor | None = None) -> list[tuple[str, bytes]]:
    """The files of a wheel, in the order the archive holds them.

    A wheel that cannot be opened, or a member that cannot be read (encrypted, compressed in a way the reader does not
    know, damaged), refuses the build naming the wheel through `redactor` and the kind of failure only: the reader's
    own message names the member, whose path can hold a private name or a term.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(wheel.data)) as archive:
            return [(info.filename, archive.read(info)) for info in archive.infolist() if not info.is_dir()]
    except _UNREADABLE as error:
        shown = wheel.path if redactor is None else redactor.name(wheel.path)
        raise PackageError(f"{shown}: cannot be read as a wheel, in full ({type(error).__name__})") from None


def as_text(data: bytes) -> str | None:
    """The member as text, when it is UTF-8 without NUL bytes; None for anything else."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _plain(
    label: str, text: str, tokens: Hashes | None, terms: Anonymisation | None, hidden, redactor: Redactor
) -> list[str]:
    """Read a member of a release as it is: the token list line by line (unless the release is exempt from it), and
    the terms and identity if anonymised. The member is named through `redactor`, like every path of the build."""
    shown = redactor.name(label)
    hits = [] if tokens is None else scan_source(label, text, tokens, exempt_header=False, python=False)
    found = [describe_hit(shown, hit) for hit in hits]
    if terms is not None:
        commits, urls = hidden
        found += [f"{shown}:{line}: a term of the anonymisation list" for line in sorted(term_lines(text, terms))]
        found += [f"{shown}:{line}: {FOUND_COMMIT}" for line in sorted(commit_lines(text, commits))]
        found += [f"{shown}:{line}: {FOUND_URL}" for line in sorted(url_lines(text, urls))]
    return found


def _deep(
    label: str, data: bytes, tokens: Hashes, terms: Anonymisation | None, hidden, redactor: Redactor
) -> list[str]:
    """Read a member of the author's own code with every view the checks use for a file."""
    one = {label: data}
    found = scan_tokens(one, tokens, redactor=redactor)
    if terms is not None:
        found += find_terms(one, terms, redactor=redactor) + find_identity(one, *hidden, opaque=(), redactor=redactor)
    return found


def scan_wheels(
    wheels: Sequence[Wheel],
    tokens: Hashes,
    *,
    terms: Anonymisation | None,
    hidden: tuple[frozenset[str], frozenset[str]],
    attested: Mapping[str, Opaque],
    exempt: Collection[str] = frozenset(),
    redactor: Redactor | None = None,
) -> WheelScan:
    """Read every text member of every wheel; list the members that are not text; refuse nothing on its own.

    A wheel in `exempt` (`lockfile.token_exempt`: a pypi.org release whose bytes the study's lock records) is read as
    a release, without the study-token list; the terms and identity of an anonymised package still apply to it. Every
    message names a member through `redactor` (by default one that knows the tokens and the terms), so a member path
    that holds a private name or a term is never shown, in a finding, an attestation hint or a refusal.
    """
    if redactor is None:
        redactor = Redactor(tokens, (lambda text: holds_term(terms, text)) if terms is not None else None)
    findings: list[str] = []
    binaries: dict[str, list[dict[str, str]]] = {}
    seen: set[str] = set()
    for wheel in wheels:
        binaries[wheel.path] = []
        for member, data in members(wheel, redactor):
            label = f"{wheel.path}/{member}"
            seen.add(label)
            digest = hashlib.sha256(data).hexdigest()
            if label in attested:
                if attested[label].sha256 != digest:
                    findings.append(
                        f"{redactor.name(label)}: its SHA-256 is not the one [[opaque]] attests; read it again"
                    )
                continue
            if terms is not None and member.endswith(f".dist-info/{DIRECT_URL}"):
                findings.append(
                    f"{redactor.name(label)}: names where the distribution came from; an anonymised package refuses it"
                )
                continue
            text = as_text(data)
            if text is None:
                binaries[wheel.path].append({"path": member, "sha256": digest})
                continue
            if wheel.path in exempt:
                found = _plain(label, text, None, terms, hidden, redactor)
            elif wheel.from_index:
                found = _plain(label, text, tokens, terms, hidden, redactor)
            else:
                found = _deep(label, data, tokens, terms, hidden, redactor)
            if found:
                findings += found
                findings.append(_attest_hint(label, digest, redactor))
    findings += [
        f"{redactor.name(path)}: [[opaque]] attests a member no vendored wheel holds" for path in _stray(attested, seen)
    ]
    return WheelScan(findings, binaries)


def _attest_hint(label: str, digest: str, redactor: Redactor) -> str:
    shown = redactor.name(label)
    if shown != label:
        return (
            f"to attest {shown} once read by hand: [[opaque]] with its path (which --show-private-paths prints), "
            f'sha256 = "{digest}", reason = "..."'
        )
    return f'to attest it once read by hand: [[opaque]] path = "{label}", sha256 = "{digest}", reason = "..."'


def _stray(attested: Iterable[str], seen: set[str]) -> list[str]:
    return sorted(path for path in attested if path not in seen)
