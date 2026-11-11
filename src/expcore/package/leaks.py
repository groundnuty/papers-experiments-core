# Provenance: new
"""The leak checks a package's files pass: the study-token scan, and for an anonymised package its terms and identity.

`scan_tokens` is the study-token scanner run over every stream of every file, under every view (`views`), with the
hashes of the names private everywhere. `findings_in` adds a key location models.toml still holds, which no package
may (`config.key_locations`), and what an anonymised package must not hold either.
"""

import importlib.resources
import json
import re
from collections.abc import Collection, Mapping
from pathlib import Path

import expcore
from expcore.config.key_locations import NO_LOCATIONS, KeyFinders, location_findings
from expcore.package.anonymise import Anonymisation, find_terms, holds_term
from expcore.package.identity import find_identity
from expcore.package.spec import PackageError
from expcore.package.views import JOINED, PARSED, Stream, streams, views
from expcore.provenance.hashfile import Hashes, parse_hash_file
from expcore.provenance.redact import Redactor
from expcore.provenance.scanner import Finding, scan_source
from expcore.study.config import MODELS_FILE

HASHES_NAME = "study_tokens.sha256"
PRIVATE_NAME = "a private name"
PLAIN_VIEWS = frozenset({"text"})  # the views whose line numbers are the stream's own lines


def default_hashes() -> Path:
    """The core's public study-token hashes: shipped in the installed package, or in the checkout when editable."""
    shipped = importlib.resources.files("expcore.provenance").joinpath(HASHES_NAME)
    if shipped.is_file():
        return Path(str(shipped))
    return Path(expcore.__file__).resolve().parents[2] / "tests" / "data" / HASHES_NAME


def private_hashes(path: Path, *, given: bool = True) -> Hashes:
    """The hashes of names private everywhere (`all` scope); refuse a list that is missing or holds none (K01).

    No message names the file's path, which is the author's and may hold a private name: it is the list given with
    `--hashes` (`given`) or the core's.
    """
    source = "the study-token list given with --hashes" if given else "the core's study-token list"
    try:
        hashes = parse_hash_file(Path(path).read_text(encoding="utf-8"), source).only_all()
    except FileNotFoundError:
        raise PackageError(f"{source} is not there; the token check cannot run (K01)") from None
    except (OSError, UnicodeDecodeError) as error:
        raise PackageError(
            f"{source} cannot be read ({type(error).__name__}); the token check cannot run (K01)"
        ) from None
    except ValueError as error:
        raise PackageError(str(error)) from None
    if not hashes:
        raise PackageError(f"{source} holds no hash of a name private everywhere; the check would check nothing (K01)")
    return hashes


def named(prefix: str) -> str:
    """What a study-token finding says it found: never the name, only the start of its hash."""
    return f"{PRIVATE_NAME} (study token {prefix})"


def describe_hit(shown: str, hit: Finding) -> str:
    """One scanner finding in the words of the package's checks: `<where>:<line>: a private name (study token ...)`,
    or `<where>: the path holds ...` for a path; `shown` is where, as the redactor names it."""
    if hit.kind == "path":
        return f"{shown}: the path holds {named(hit.hash_prefix)}"
    return f"{shown}:{hit.line}: {named(hit.hash_prefix)}"


def path_prefixes(label: str, hashes: Hashes) -> set[str]:
    """The hash prefixes of the private names a path or a stream's label holds."""
    return {hit.hash_prefix for hit in scan_source(label, "", hashes) if hit.kind == "path"}


def _joined_lines(text: str) -> list[int]:
    """For the text with its continuations joined (`views.JOINED`), the line of the text as it is that each of its
    lines starts on."""
    starts, joining = [], False
    for number, line in enumerate(text.split("\n"), start=1):
        if not joining:
            starts.append(number)
        joining = line.endswith(("\\", "\\\r"))
    return starts


def _stream_findings(stream: Stream, shown: str, hashes: Hashes, reported_paths: set[str]) -> set[str]:
    """The findings of one stream, once per occurrence: a name at a line of the text as it is; one a decoded view alone
    shows at a line, at that line of the text as it is (continuations accounted for), marked as decoded; one only
    parsed strings or raw bytes show, which have no lines, without one, unless it was found at a line. A name its
    label holds is reported unless `reported_paths` (its file's own path) already holds it."""
    plain: set[tuple[int, str]] = set()
    decoded: set[tuple[int, str]] = set()
    unlined: dict[str, str] = {}
    starts = _joined_lines(stream.text) if stream.text is not None else []
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
                joined = view in JOINED and 0 < hit.line <= len(starts)
                decoded.add((starts[hit.line - 1] if joined else hit.line, hit.hash_prefix))
    decoded -= plain
    lined = {prefix for _, prefix in plain | decoded}
    found = {f"{shown}: the path holds {named(p)}" for p in path_prefixes(stream.label, hashes) - reported_paths}
    found |= {f"{shown}:{line}: {named(prefix)}" for line, prefix in plain}
    found |= {f"{shown}:{line}: {named(prefix)}, once decoded" for line, prefix in decoded}
    found |= {f"{shown}: {named(prefix)}, {where}" for prefix, where in unlined.items() if prefix not in lined}
    return found


def scan_tokens(
    files: Mapping[str, bytes],
    hashes: Hashes,
    *,
    opaque: Collection[str] = frozenset(),
    redactor: Redactor | None = None,
) -> list[str]:
    """Run the study-token scanner over every stream of every file, under every view, with no exemption.

    Each file is classified and opened down to its last archived member (`views.streams`); a file of a kind the scan
    cannot read in full is a finding, a refusal. Text is read as it is and decoded, a database's or archive's bytes
    under every encoding (`views.views`). The provenance-header exemption of this repository's own check does not
    apply. The files in `opaque` were attested by hash and are not read. A finding reads `<where>:<line>: a private
    name (study token <hash prefix>)`, once per stream, line and name whichever views found it (`_stream_findings`),
    never the value. Where is named by `redactor` (by default one that knows these hashes), the same way every check
    names it: a label that holds a private name, or a term, is shown by the start of its SHA-256. A name the file's
    path holds is reported once, for the file, not again for each stream inside it.
    """
    redactor = Redactor(hashes) if redactor is None else redactor
    findings: set[str] = set()
    for path in sorted(files):
        if path in opaque:
            continue
        found, problems = streams(path, files[path])
        findings |= {f"{redactor.name(label)}: {why}" for label, why in problems}
        own = path_prefixes(path, hashes)
        for stream in found:
            shown = redactor.name(stream.label)
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
    redactor: Redactor | None = None,
    locations: KeyFinders = NO_LOCATIONS,
) -> list[str]:
    """Every leak the checks find: a key location left in models.toml, or one of `locations` (what finds each key
    location the study's models declare) anywhere, a declared key file itself included (K13); study tokens; and for an
    anonymised package its terms and its identity, each naming a path through the one `redactor`.

    The files in `generated` are the build's own (SHA256SUMS, PACKAGE.json, ...), which list the package's paths. A
    path whose own finding reports it is not reported again through them: they are read with each exact mention of it
    masked (`without_paths`).
    """
    if redactor is None:
        redactor = Redactor(tokens, (lambda text: holds_term(terms, text)) if terms is not None else None)
    reported = reported_paths(files, tokens, terms, opaque)
    files = {path: without_paths(data, reported) if path in generated else data for path, data in files.items()}
    findings = (
        [f"{MODELS_FILE}: {found}" for found in location_findings(files[MODELS_FILE])] if MODELS_FILE in files else []
    )
    findings += find_locations(files, locations, opaque=opaque, redactor=redactor)
    findings += scan_tokens(files, tokens, opaque=opaque, redactor=redactor)
    if terms is not None:
        findings += find_terms(files, terms, opaque=opaque, redactor=redactor)
        findings += find_identity(files, commits, urls, opaque=opaque, redactor=redactor)
    return findings


FOUND_LOCATION = "a key location the study's models.toml declares, which no package holds"


def path_holds(label: str, locations: KeyFinders) -> bool:
    """Whether a stream's path holds a key location, or is a declared key file itself (`KeyFinders.names_file`)."""
    return locations.names_file(label) or any(found.search(label) for found in locations.patterns)


def find_locations(
    files: Mapping[str, bytes],
    locations: KeyFinders,
    *,
    opaque: Collection[str] = frozenset(),
    redactor: Redactor | None = None,
) -> list[str]:
    """One finding per stream and line that holds a key location (`key_locations.location_finders`), never the value:
    every stream of every file, under every view, as the token check reads it; and the path of each stream
    (`path_holds`), shown by its hidden name when it holds one. The files in `opaque` were attested by hash
    and are not read. Empty when there is nothing to look for."""
    if not locations:
        return []
    redactor = Redactor() if redactor is None else redactor
    findings: set[str] = set()
    for name in sorted(files):
        if name in opaque:
            continue
        for stream in streams(name, files[name])[0]:
            held = path_holds(stream.label, locations)
            shown = redactor.name(stream.label, hide=held)
            if held:
                findings.add(f"{shown}: the path holds {FOUND_LOCATION}")
            for _, text in views(stream):
                found_at = (m.start() for found in locations.patterns for m in found.finditer(text))
                lines = {text.count("\n", 0, start) + 1 for start in found_at}
                findings |= {f"{shown}:{line}: {FOUND_LOCATION}" for line in lines}
    return sorted(findings)


def reported_paths(
    files: Mapping[str, bytes], tokens: Hashes, terms: Anonymisation | None, opaque: Collection[str]
) -> set[str]:
    """The paths the checks report as paths: those that hold a private name or a term, of files they read."""
    return {
        path
        for path in files
        if path not in opaque and (path_prefixes(path, tokens) or (terms is not None and holds_term(terms, path)))
    }


MASK = b"<*>"  # no letter or digit, so it can be neither a term nor a part of a name


def without_paths(data: bytes, paths: Collection[str]) -> bytes:
    """A generated file with each exact mention of `paths` masked: an entire JSON string, everything between its
    quotes, as the generated JSON writes a path; or an entire path field of a manifest line (`<sha256>  <path>`, to the
    end of the line). Nothing inside a longer string or field is masked, a note that names a path among other words
    say, so a name it holds is still found there."""
    for path in paths:
        quoted = {json.dumps(path, ensure_ascii=escaped)[1:-1].encode("utf-8") for escaped in (True, False)}
        for form in quoted:
            data = re.sub(rb'(?<!\\)"' + re.escape(form) + rb'"', b'"' + MASK + b'"', data)
        field = re.escape(path.encode("utf-8"))
        data = re.sub(rb"(?m)^(\S+  )" + field + rb"(\r?)$", rb"\g<1>" + MASK + rb"\g<2>", data)
    return data
