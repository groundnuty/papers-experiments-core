# Provenance: new
"""Anonymisation lists for double-blind submissions: whole words only, and only in the prose and metadata files named.

The list is a TOML file kept outside the study's repository, the study and the output directory, because it holds the
very names the package hides (K03: a private list never enters the repository or the package):

    files = ["README.md"]        # the prose and metadata files whose text is rewritten
    [terms]
    "Ada Example" = "Author A"   # a term, and the neutral label that replaces it

- Only the files the list names are rewritten. Each must be prose or metadata (a suffix in `REWRITABLE_SUFFIXES`),
  outside the code the records were made with and outside out/. YAML (`.yml`, `.yaml`, `CITATION.cff`) is refused:
  with no YAML parser its rewrite could not be checked, so the author leaves it out or ships an anonymised copy from
  another path. JSON is parsed, rewritten in its decoded keys and
  strings, and written out again in one fixed form, so an escape such as `\\u0056` cannot hide a term from the
  rewrite. A format that cannot be parsed and written out again safely (TOML, plain text) is rewritten as text,
  and a TOML file must still parse afterwards. Code, configuration, records and generated tables are
  never rewritten: relabelling them changes what runs and what was measured (relabelling by substring broke an
  earlier study's package: a class name, a network address, an endpoint; see docs/inventory.md).
- A term is replaced only as a whole word, ignoring case: never inside a longer word, an identifier, or a dotted or
  hyphenated name, so a term `re` stays in `re-run`.
- Then every file of the package, rewritten ones included, is searched for every term, ignoring case, wherever the
  term is not part of a longer run of letters and digits: every byte stream it holds, under every encoding, and
  decoded (its parsed JSON or TOML strings, HTML entities, percent-escapes and backslash escapes; `views`). A term
  found anywhere fails the build, naming the file and the line and never the term (K05); a path that holds a term is
  not shown. A term the plain-text rewrite of a TOML file could not reach, being escaped, is found here, and
  the build refuses that file. So a term in a code file is reported and
  never rewritten, and one the whole-word rule left inside a hyphenated name is reported too: nothing is relabelled
  by substring, and nothing slips through.
- A term must not change numbers or model names (K04): a term without a letter, and a term that is a key, API id or
  alias of the study's model table, are refused. A rewritten JSON file must still parse.
"""

import json
import re
import tomllib
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from expcore.package.spec import PackageError
from expcore.package.views import PARSED, streams, views
from expcore.study.config import CONFIG_FILES

REWRITABLE_SUFFIXES = frozenset({".md", ".markdown", ".rst", ".txt", ".bib", ".json", ".toml"})
YAML_SUFFIXES = frozenset({".yml", ".yaml", ".cff"})  # CITATION.cff is YAML
FINDING = "a term of the anonymisation list"
REDACTED_PATH = "<path redacted>"


@dataclass(frozen=True)
class Anonymisation:
    """A loaded anonymisation list: the files to rewrite and each term with its label."""

    source: Path
    files: tuple[str, ...]
    terms: Mapping[str, str]


def _inside(path: Path, directory: Path) -> bool:
    path, directory = path.resolve(), directory.resolve()
    return path == directory or directory in path.parents


def load_anonymisation(path: Path, *, keep_out_of: Iterable[Path], model_names: Collection[str]) -> Anonymisation:
    """Read and validate an anonymisation list; refuse one that lies inside any of `keep_out_of` (K03)."""
    path = Path(path)
    for directory in keep_out_of:
        if _inside(path, Path(directory)):
            raise PackageError(
                f"the anonymisation list lies inside {directory}; keep it outside the repository, the study and the "
                "output (K03)"
            )
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PackageError(f"{path}: cannot be read as an anonymisation list ({type(error).__name__})") from None
    files, terms = raw.get("files"), raw.get("terms")
    if set(raw) - {"files", "terms"} or not isinstance(files, list) or not isinstance(terms, dict) or not terms:
        raise PackageError(f"{path}: needs `files`, a list of paths, and a non-empty [terms] table, and nothing else")
    if not all(isinstance(f, str) and f for f in files) or len(set(files)) != len(files):
        raise PackageError(f"{path}: `files` must name distinct paths")
    models = {name.casefold() for name in model_names}
    for number, (term, label) in enumerate(terms.items(), start=1):
        where = f"{path}: term {number} of [terms]"  # a term is never shown, only its place in the list
        if not isinstance(label, str) or not label.strip():
            raise PackageError(f"{where} has no label to replace it with")
        if not term.strip() or not any(c.isalpha() for c in term):
            raise PackageError(f"{where} has no letter; numbers are never anonymised (K04)")
        if term.casefold() in models:
            raise PackageError(f"{where} names a model of the model table; model names are never anonymised (K04)")
        if any(_finder(other).search(label) for other in terms):
            raise PackageError(f"{where}: its label contains a term of the list")
    return Anonymisation(path, tuple(files), dict(terms))


def _body(term: str) -> str:
    """The term as a pattern: its words in order, any run of white space between them."""
    return r"\s+".join(re.escape(word) for word in term.split())


def _replacer(term: str) -> re.Pattern[str]:
    """A whole word only: not next to a word character, nor joined to one by a dot or a hyphen."""
    return re.compile(rf"(?<!\w)(?<!\w[-.]){_body(term)}(?!\w)(?![-.]\w)", re.IGNORECASE)


def _finder(term: str) -> re.Pattern[str]:
    """Anywhere it is not part of a longer run of letters and digits; stricter than the replacement on purpose."""
    return re.compile(rf"(?<![^\W_]){_body(term)}(?![^\W_])", re.IGNORECASE)


def rewrite(
    files: Mapping[str, bytes], anonymisation: Anonymisation, *, code: Collection[str], out_prefix: str = "out/"
) -> dict[str, bytes]:
    """Return the files with the named ones rewritten; refuse a named file that is not prose or metadata."""
    replacers = [(_replacer(term), label) for term, label in sorted(anonymisation.terms.items(), key=_longest_first)]
    result = dict(files)
    for name in anonymisation.files:
        where = f"{anonymisation.source}: `files` names {name}"
        if name not in files:
            raise PackageError(f"{where}, which the package does not include")
        if PurePosixPath(name).suffix.lower() in YAML_SUFFIXES:
            raise PackageError(
                f"{where}, which is YAML: without a YAML parser its rewrite can be neither parsed nor read decoded, so "
                "it is not rewritten. Leave it out of the package, or include an anonymised copy from another path"
            )
        protected = name in code or name in CONFIG_FILES or name.startswith(out_prefix)
        if protected or PurePosixPath(name).suffix.lower() not in REWRITABLE_SUFFIXES:
            raise PackageError(
                f"{where}, which is code, configuration, a record or a generated table, never rewritten; "
                f"only prose and metadata ({', '.join(sorted(REWRITABLE_SUFFIXES))}) outside out/ and the code"
            )
        try:
            text = files[name].decode("utf-8")
        except UnicodeDecodeError:
            raise PackageError(f"{where}, which is not UTF-8 text") from None
        suffix = PurePosixPath(name).suffix.lower()
        text = _rewrite_json(text, replacers, where) if suffix == ".json" else _relabel(text, replacers)
        if suffix == ".toml":
            try:
                tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                raise PackageError(f"{where}: after anonymisation it no longer parses as TOML (K04)") from None
        result[name] = text.encode("utf-8")
    return result


def _relabel(text: str, replacers: Sequence[tuple[re.Pattern[str], str]]) -> str:
    for replacer, label in replacers:
        text = replacer.sub(label, text)
    return text


def _rewrite_json(text: str, replacers: Sequence[tuple[re.Pattern[str], str]], where: str) -> str:
    """Parse, relabel every decoded key and string, and write the result out in one fixed form."""
    try:
        data = json.loads(text)
    except ValueError:
        raise PackageError(f"{where}, which does not parse as JSON") from None

    def walk(value: object) -> object:
        if isinstance(value, dict):
            relabelled = {_relabel(key, replacers): walk(item) for key, item in value.items()}
            if len(relabelled) != len(value):
                raise PackageError(f"{where}: anonymisation would merge two keys of one object (K04)")
            return relabelled
        if isinstance(value, list):
            return [walk(item) for item in value]
        return _relabel(value, replacers) if isinstance(value, str) else value

    return json.dumps(walk(data), ensure_ascii=False, indent=2) + "\n"


def _longest_first(item: tuple[str, str]) -> tuple[int, str]:
    return (-len(item[0]), item[0].casefold())


def _lines(text: str, finders: Sequence[re.Pattern[str]]) -> set[int]:
    return {text.count("\n", 0, m.start()) + 1 for f in finders for m in f.finditer(text)}


def holds_term(anonymisation: Anonymisation, text: str) -> bool:
    """Whether a text, a path for instance, holds a term of the list anywhere the residual check would find it."""
    return any(_finder(term).search(text) for term in anonymisation.terms)


def find_terms(
    files: Mapping[str, bytes], anonymisation: Anonymisation, *, opaque: Collection[str] = frozenset()
) -> list[str]:
    """Return one finding per stream and line that holds a term, never the term; empty when the package is clean.

    Every stream of every file is read as the token check reads it (`views`): text as it is and decoded, a database's
    or archive's bytes under every encoding. A term found only in the parsed strings of a JSON or TOML stream has no
    line of its own and is reported for the stream. What the token check refuses is its finding, not one of these;
    the files in `opaque` were attested by hash and are not read.
    """
    finders = [_finder(term) for term in anonymisation.terms]
    findings: list[str] = []
    for name in sorted(set(files) - set(opaque)):
        for stream in streams(name, files[name])[0]:
            shown = REDACTED_PATH if any(f.search(stream.label) for f in finders) else stream.label
            if shown != stream.label:
                findings.append(f"{shown}: the path holds {FINDING}")
            lines, parsed = set(), False
            for view, text in views(stream):
                hits = _lines(text, finders)
                if view in PARSED:
                    parsed = parsed or bool(hits)
                else:
                    lines |= hits
            findings += [f"{shown}:{line}: {FINDING}" for line in sorted(lines)]
            if parsed and not lines:
                findings.append(f"{shown}: {FINDING}, in its parsed strings")
    return findings
