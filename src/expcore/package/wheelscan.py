# Provenance: new
"""The leak checks over the wheels a package vendors: every member of every wheel is opened, always.

A wheel is opened member by member. A member that is UTF-8 text without NUL bytes (Python source, METADATA, RECORD,
entry points, licences) is read; any other member is a compiled binary or data that is not text (an extension module,
a scientific wheel's test data), which no check can read: it goes in attested by its SHA-256, listed per wheel in
`PACKAGE.json` (`binaries`). Text is read with the study-token list, and in an anonymised package with the terms of
the anonymisation list and the identity it must not hold (`identity.find_identity`'s commits and URLs). The one
exception is the study-token list for a wheel proven to be a pypi.org release, whose bytes the study's `uv.lock`
records (`lockfile`): the anonymisation list and the identity still apply to it. Apart from these checks, every member
of every wheel is walked whole for a copy of a key file the study declares (`keycopies`), whatever its kind and
wherever its wheel came from.

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

from expcore.config.key_locations import NO_LOCATIONS, KeyFinders
from expcore.package.anonymise import Anonymisation, find_terms, holds_term, term_lines
from expcore.package.identity import FOUND_COMMIT, FOUND_URL, commit_lines, find_identity, url_lines
from expcore.package.leaks import describe_hit, find_copies, find_locations, path_holds, scan_tokens
from expcore.package.spec import Opaque, PackageError
from expcore.package.wheels import Wheel
from expcore.provenance.environment import CORE_DISTRIBUTION, canonical
from expcore.provenance.hashfile import Hashes
from expcore.provenance.redact import Redactor
from expcore.provenance.scanner import scan_source

DIRECT_URL = "direct_url.json"
CLEARED = (
    "code you did not write can name a key location by chance (a generic variable's name): once you have read this "
    "member, attest it as below; in your own code, remove it"
)


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
    label: str,
    data: bytes,
    tokens: Hashes,
    terms: Anonymisation | None,
    hidden,
    redactor: Redactor,
    locations: KeyFinders = NO_LOCATIONS,
) -> list[str]:
    """Read a member of what can be the author's own code with every view the checks use for a file: the key
    locations the study's models declare (K13), saying how a member that names one by chance, in a distribution the
    author did not write, is cleared; the study tokens; and in an anonymised package the terms and the identity. A
    copy of a key file is not looked for here: `scan_wheels` refuses it before, with no way to clear it."""
    one = {label: data}
    found = find_locations(one, locations, redactor=redactor, copies=False)
    if found:
        found.append(f"{redactor.name(label, hide=path_holds(label, locations))}: {CLEARED}")
    found += scan_tokens(one, tokens, redactor=redactor)
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
    locations: KeyFinders = NO_LOCATIONS,
) -> WheelScan:
    """Read every text member of every wheel; list the members that are not text; refuse nothing on its own.

    The key locations the study's models declare (`locations`) are looked for, with every view, in every wheel read
    with every view but the core's: whatever it was installed from, a repository, a local wheel, a URL or a directory,
    it can be the author's own code (K13). A member that names one is refused with how a distribution the author did
    not write, which can name a generic variable a study also uses as its `key_env`, is cleared: attested once read.
    The core's own code names such variables (its redaction names API_KEY), and holds nothing of the author's. Every
    member of every wheel, from an index or not, the core's included, is walked whole for a copy of a declared key file
    (`leaks.find_copies`): a member that is one, or holds one at any depth (archived, compressed, decoded), is refused
    whether attested or not, naming its path and saying to remove it, and so is one the walk could not finish; it gets
    no attestation hint and no entry in `binaries`, and nothing prints its SHA-256.

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
    core = canonical(CORE_DISTRIBUTION)
    for wheel in wheels:
        binaries[wheel.path] = []
        searched = locations if _is_authors(wheel, exempt, core) else NO_LOCATIONS
        for member, data in members(wheel, redactor):
            label = f"{wheel.path}/{member}"
            seen.add(label)
            copies = find_copies({label: data}, locations, redactor=redactor)  # every wheel's, walked whole
            if copies:  # removed, never attested: no hint or binaries entry, which would carry its digest
                findings += copies
                continue
            digest = hashlib.sha256(data).hexdigest()
            text = as_text(data)
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
            if text is None:
                binaries[wheel.path].append({"path": member, "sha256": digest})
                continue
            if wheel.path in exempt:
                found = _plain(label, text, None, terms, hidden, redactor)
            elif wheel.from_index:
                found = _plain(label, text, tokens, terms, hidden, redactor)
            else:
                found = _deep(label, data, tokens, terms, hidden, redactor, searched)
            if found:
                findings += found
                findings.append(_attest_hint(label, digest, redactor))
    findings += [
        f"{redactor.name(path)}: [[opaque]] attests a member no vendored wheel holds" for path in _stray(attested, seen)
    ]
    return WheelScan(findings, binaries)


def _is_authors(wheel: Wheel, exempt: Collection[str], core: str) -> bool:
    """Whether a wheel can be the author's own code, so that it is searched for key locations: not a release (from an
    index, or one the lock proves) and not the core (`core`, its canonical name), which has nothing of the author's."""
    return not (wheel.path in exempt or wheel.from_index or canonical(wheel.name) == core)


def _attest_hint(label: str, digest: str, redactor: Redactor) -> str:
    shown = redactor.name(label, hide=label in redactor.hidden.values())  # hidden by a finding, it stays hidden
    if shown != label:
        return (
            f"to attest {shown} once read by hand: [[opaque]] with its path (which --show-private-paths prints), "
            f'sha256 = "{digest}", reason = "..."'
        )
    return f'to attest it once read by hand: [[opaque]] path = "{label}", sha256 = "{digest}", reason = "..."'


def _stray(attested: Iterable[str], seen: set[str]) -> list[str]:
    return sorted(path for path in attested if path not in seen)
