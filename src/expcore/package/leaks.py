# Provenance: new
"""The leak checks a package's files pass: the study-token scan, and for an anonymised package its terms and identity.

`scan_tokens` is the study-token scanner run over every stream of every file, under every view (`views`), with the
hashes of the names private everywhere. `findings_in` adds what an anonymised package must not hold either.
"""

import importlib.resources
import json
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from pathlib import Path

import expcore
from expcore.package.anonymise import Anonymisation, find_terms, holds_term
from expcore.package.identity import find_identity
from expcore.package.spec import PackageError
from expcore.package.views import PARSED, Stream, hidden_name, streams, views
from expcore.provenance.hashfile import Hashes
from expcore.provenance.scanner import Finding, scan_source
from expcore.provenance.study_tokens import load_hashes

HASHES_NAME = "study_tokens.sha256"
PRIVATE_NAME = "a private name"
PLAIN_VIEWS = frozenset({"text"})  # the views whose line numbers are the stream's own lines
_HIDDEN = re.compile(r"file (\d+) \(path sha256 [0-9a-f]{12}\)")  # how `views.hidden_name` names a path


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


def named(prefix: str) -> str:
    """What a study-token finding says it found: never the name, only the start of its hash."""
    return f"{PRIVATE_NAME} (study token {prefix})"


def describe_hit(shown: str, hit: Finding) -> str:
    """One scanner finding in the words of the package's checks: `<where>:<line>: a private name (study token ...)`,
    or `<where>: the path holds ...` for a path."""
    if hit.kind == "path":
        return f"{shown}: the path holds {named(hit.hash_prefix)}"
    return f"{shown}:{hit.line}: {named(hit.hash_prefix)}"


def _path_prefixes(label: str, hashes: Hashes) -> set[str]:
    """The hash prefixes of the private names a path or a stream's label holds."""
    return {hit.hash_prefix for hit in scan_source(label, "", hashes) if hit.kind == "path"}


def _stream_findings(stream: Stream, shown: str, hashes: Hashes, reported_paths: set[str]) -> set[str]:
    """The findings of one stream, each once: a name at a line of the text as it is; one only a decoded view shows,
    at its line of the decoded text; one only parsed strings or raw bytes show, which have no lines, without one.
    A name its label holds is reported unless `reported_paths` (its file's own path) already holds it."""
    plain: set[tuple[int, str]] = set()
    decoded: set[tuple[int, str]] = set()
    unlined: dict[str, str] = {}
    for view, text in views(stream):
        python = view == "text" and stream.label.endswith(".py")
        for hit in scan_source(stream.label, text, hashes, exempt_header=False, python=python):
            if hit.kind == "path":
                continue
            if view in PLAIN_VIEWS:
                plain.add((hit.line, hit.hash_prefix))
            elif view in PARSED:
                unlined.setdefault(hit.hash_prefix, "in its parsed strings")
            elif stream.text is None:  # a database's or archive's bytes, or the bytes a run decodes to
                unlined.setdefault(hit.hash_prefix, "in its raw bytes")
            else:
                decoded.add((hit.line, hit.hash_prefix))
    lined = {prefix for _, prefix in plain}
    decoded = {(line, prefix) for line, prefix in decoded if prefix not in lined}
    lined |= {prefix for _, prefix in decoded}
    found = {f"{shown}: the path holds {named(p)}" for p in _path_prefixes(stream.label, hashes) - reported_paths}
    found |= {f"{shown}:{line}: {named(prefix)}" for line, prefix in plain}
    found |= {f"{shown}:{line}: {named(prefix)}, once decoded" for line, prefix in decoded}
    found |= {f"{shown}: {named(prefix)}, {where}" for prefix, where in unlined.items() if prefix not in lined}
    return found


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
    apply. The files in `opaque` were attested by hash and are not read. A finding reads `<where>:<line>: a private
    name (study token <hash prefix>)`, once per stream, line and name whichever views found it (`_stream_findings`),
    never the value. A stream whose label holds a private name, or for which `hidden` is true (a term of an
    anonymisation list), is named by its file's position in the package and the start of its label's SHA-256
    (`views.hidden_name`), so that two such findings never fold into one; a name the file's path holds is reported
    once, for the file, not again for each stream inside it.
    """
    findings: set[str] = set()
    for position, path in enumerate(sorted(files), start=1):
        if path in opaque:
            continue
        found, problems = streams(path, files[path])
        for label, why in problems:
            private = hidden(label) or bool(_path_prefixes(label, hashes))
            findings.add(f"{hidden_name(position, label) if private else label}: {why}")
        own = _path_prefixes(path, hashes)
        for stream in found:
            private = hidden(stream.label) or bool(_path_prefixes(stream.label, hashes))
            shown = hidden_name(position, stream.label) if private else stream.label
            findings |= _stream_findings(stream, shown, hashes, own if stream.label != path else set())
    return sorted(findings)


def findings_in(
    files: Mapping[str, bytes],
    tokens: Hashes,
    terms: Anonymisation | None,
    commits: frozenset[str],
    urls: frozenset[str],
    opaque: Collection[str],
    *,
    generated: Collection[str] = frozenset(),
) -> list[str]:
    """Every leak the checks find: study tokens, and for an anonymised package its terms and its identity.

    The files in `generated` are the build's own (SHA256SUMS, PACKAGE.json, ...), which list the package's paths. A
    path whose own finding reports it is not reported again through them: they are read with it masked.
    """
    reported = reported_paths(files, tokens, terms, opaque)
    files = {path: without_paths(data, reported) if path in generated else data for path, data in files.items()}
    hidden = (lambda label: holds_term(terms, label)) if terms is not None else (lambda label: False)
    findings = scan_tokens(files, tokens, opaque=opaque, hidden=hidden)
    if terms is not None:
        findings += find_terms(files, terms, opaque=opaque)
        findings += find_identity(files, commits, urls, opaque=opaque)
    return findings


def reported_paths(
    files: Mapping[str, bytes], tokens: Hashes, terms: Anonymisation | None, opaque: Collection[str]
) -> set[str]:
    """The paths the checks report as paths: those that hold a private name or a term, of files they read."""
    return {
        path
        for path in files
        if path not in opaque and (_path_prefixes(path, tokens) or (terms is not None and holds_term(terms, path)))
    }


MASK = b"<*>"  # no letter or digit, so it can be neither a term nor a part of a name


def without_paths(data: bytes, paths: Collection[str]) -> bytes:
    """A generated file with each of `paths` masked, as written in a manifest and as a JSON string writes it."""
    for path in sorted(paths, key=len, reverse=True):  # a longer path first, so a shorter one inside it stays whole
        for form in dict.fromkeys((path, json.dumps(path)[1:-1])):
            data = data.replace(form.encode("utf-8"), MASK)
    return data


def hidden_paths(findings: Iterable[str], files: Mapping[str, bytes]) -> dict[str, str]:
    """For each name of a hidden path in `findings` (`views.hidden_name`), the path or stream label it stands for,
    in the order the findings give them; a name the files cannot resolve (a wheel member's, say) is left out."""
    ordered = sorted(files)
    resolved: dict[str, str] = {}
    for finding in findings:
        for match in _HIDDEN.finditer(finding):
            name, position = match.group(0), int(match.group(1))
            if name in resolved or not 1 <= position <= len(ordered):
                continue
            path = ordered[position - 1]
            found, problems = streams(path, files[path])
            labels = [path, *(stream.label for stream in found), *(label for label, _ in problems)]
            label = next((label for label in labels if hidden_name(position, label) == name), None)
            if label is not None:
                resolved[name] = label
    return resolved
