# Provenance: new
"""The leak checks a package's files pass: the study-token scan, and for an anonymised package its terms and identity.

`scan_tokens` is the study-token scanner run over every stream of every file, under every view (`views`), with the
hashes of the names private everywhere. `findings_in` adds what an anonymised package must not hold either.
"""

import importlib.resources
from collections.abc import Callable, Collection, Mapping
from pathlib import Path

import expcore
from expcore.package.anonymise import Anonymisation, find_terms, holds_term
from expcore.package.identity import find_identity
from expcore.package.spec import PackageError
from expcore.package.views import hidden_name, streams, views
from expcore.provenance.hashfile import Hashes
from expcore.provenance.scanner import REDACTED_PATH, scan_source
from expcore.provenance.study_tokens import load_hashes

HASHES_NAME = "study_tokens.sha256"


def default_hashes() -> Path:
    """The core's public study-token hashes: shipped in the installed package, or in the checkout when editable."""
    shipped = importlib.resources.files("expcore.provenance").joinpath(HASHES_NAME)
    if shipped.is_file():
        return Path(str(shipped))
    return Path(expcore.__file__).resolve().parents[2] / "tests" / "data" / HASHES_NAME


def private_hashes(path: Path) -> Hashes:
    """The hashes of names private everywhere (`all` scope); refuse a list that is missing or holds none (K01)."""
    try:
        hashes = load_hashes(Path(path)).only_all()
    except FileNotFoundError:
        raise PackageError(f"{path}: no study-token hash file here; the token check cannot run (K01)") from None
    except ValueError as error:
        raise PackageError(str(error)) from None
    if not hashes:
        raise PackageError(f"{path}: holds no hash of a name private everywhere; the check would check nothing (K01)")
    return hashes


def scan_tokens(
    files: Mapping[str, bytes],
    hashes: Hashes,
    *,
    opaque: Collection[str] = frozenset(),
    hidden: Callable[[str], bool] = lambda label: False,
) -> list[str]:
    """Run the study-token scanner over every stream of every file, under every view, with no exemption.

    Each file is classified and opened down to its last archived member (`views.streams`); a file of a kind the scan
    cannot read in full is a finding, a refusal. Text is read as it is and decoded, a database's or archive's bytes
    under every encoding (`views.views`). The provenance-header exemption of this repository's own check does not
    apply. The files in `opaque` were attested by hash and are not read. A finding shows a hash prefix and where,
    never the value. A stream whose label holds a private name, or for which `hidden` is true (a term of an
    anonymisation list), is named by its file's position in the package and the start of its label's SHA-256
    (`views.hidden_name`), so that two such findings never fold into one.
    """
    findings: set[str] = set()
    for position, path in enumerate(sorted(files), start=1):
        if path in opaque:
            continue
        found, problems = streams(path, files[path])
        for label, why in problems:
            private = hidden(label) or any(hit.kind == "path" for hit in scan_source(label, "", hashes))
            findings.add(f"{hidden_name(position, label) if private else label}: {why}")
        for stream in found:
            shown = hidden_name(position, stream.label) if hidden(stream.label) else None
            for view, text in views(stream):
                python = view == "text" and stream.label.endswith(".py")
                for hit in scan_source(stream.label, text, hashes, exempt_header=False, python=python):
                    line = str(hit)
                    if hit.path == REDACTED_PATH or shown:
                        line = f"{shown or hidden_name(position, stream.label)}{line[len(hit.path) :]}"
                    findings.add(f"{line} ({view})")
    return sorted(findings)


def findings_in(
    files: Mapping[str, bytes],
    tokens: Hashes,
    terms: Anonymisation | None,
    commits: frozenset[str],
    urls: frozenset[str],
    opaque: Collection[str],
) -> list[str]:
    """Every leak the checks find: study tokens, and for an anonymised package its terms and its identity."""
    hidden = (lambda label: holds_term(terms, label)) if terms is not None else (lambda label: False)
    findings = scan_tokens(files, tokens, opaque=opaque, hidden=hidden)
    if terms is not None:
        findings += find_terms(files, terms, opaque=opaque)
        findings += find_identity(files, commits, urls, opaque=opaque)
    return findings
