# Provenance: new
"""Sanitising import: copy a source file into this repository with every study token replaced.

Identifiers, string literals and comments are rewritten token by token, on component boundaries only, so no
commit ever holds a source file verbatim. After rewriting, the study-token checker runs on the result against
the map's own tokens, and the import is refused if anything is left.
"""

import argparse
import io
import re
import sys
import tokenize
import warnings
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath

from expcore.provenance.candidates import iter_runs, iter_windows, normalise
from expcore.provenance.gitobjects import blob_id
from expcore.provenance.hashfile import hashes_from_tokens
from expcore.provenance.headers import (
    HeaderError,
    format_import_header,
    insert_header,
    read_header,
    split_lines,
    strip_header,
)
from expcore.provenance.sanitise_map import Entry, MapError, load_map, matches_on_components
from expcore.provenance.scanner import (
    FSTRING_END_TYPES,
    FSTRING_MIDDLE_TYPES,
    FSTRING_START_TYPES,
    scan_source,
)
from expcore.provenance.study_tokens import EXIT_ERROR, EXIT_FINDINGS, EXIT_OK

_STRING_PARTS_RE = re.compile(r"(?s)^([A-Za-z]*)('''|\"\"\"|'|\")(.*)\2$")
_ESCAPE_RE = re.compile(r"\\(?:N\{[^}]*\}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2}|[0-7]{1,3}|(?s:.))")
_PREFIX_RE = re.compile(r"[A-Za-z]*")
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,40}")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)?")
_REWRITTEN = frozenset({tokenize.NAME, tokenize.STRING, tokenize.COMMENT, *FSTRING_MIDDLE_TYPES})
_CHECK_PATH = "sanitised.py"


class SanitiseError(RuntimeError):
    """A source could not be sanitised into valid Python."""


def _mask_escapes(text: str) -> str:
    """Blank out each whole escape sequence (named, \\u, \\U, \\x, octal, or backslash plus one character)."""
    return _ESCAPE_RE.sub(lambda m: " " * len(m.group()), text)


class _Replacer:
    """Applies a map to identifiers and to text inside literals and comments."""

    def __init__(self, entries: Iterable[Entry]) -> None:
        """Index the entries: all by whole value, long run-character tokens also by component window."""
        self._whole: dict[str, Entry] = {}
        self._parts: dict[str, Entry] = {}
        for entry in entries:
            key = normalise(entry.token)
            self._whole[key] = entry
            if matches_on_components(entry.token):
                self._parts[key] = entry

    def whole(self, value: str) -> Entry | None:
        """Return the entry whose token equals the entire value, ignoring case and edge whitespace."""
        return self._whole.get(normalise(value))

    def within(self, text: str, *, escapes: bool) -> tuple[str, list[Entry]]:
        """Replace mapped tokens on component boundaries of each run, longest first, never inside escape syntax.

        Components are decided with Unicode word characters, so a token glued to an accented letter is left alone.
        """
        if not self._parts:
            return text, []
        view = _mask_escapes(text) if escapes else text
        found: list[tuple[int, int, Entry]] = []
        for run in iter_runs(view, unicode_words=True):
            value = run.group()
            for start, end in iter_windows(value, unicode_words=True):
                entry = self._parts.get(normalise(value[start:end]))
                if entry is not None:
                    found.append((run.start() + start, run.start() + end, entry))
        chosen: list[tuple[int, int, Entry]] = []
        for start, end, entry in sorted(found, key=lambda m: (m[0] - m[1], m[0])):
            if all(end <= c_start or start >= c_end for c_start, c_end, _ in chosen):
                chosen.append((start, end, entry))
        for start, end, entry in sorted(chosen, reverse=True):
            text = text[:start] + entry.replacement + text[end:]
        return text, [entry for _, _, entry in chosen]


def _name(replacer: _Replacer, name: str) -> str:
    """Return the sanitised form of an identifier; it must stay an identifier.

    A refusal names the map entry by its number, its category and its replacement, never its token or the identifier.
    """
    entry = replacer.whole(name)
    new, used = (entry.replacement, [entry]) if entry else replacer.within(name, escapes=False)
    if used and not new.isidentifier():
        bad = next((e for e in used if not e.replacement.isidentifier()), used[0])
        raise SanitiseError(
            f"map entry #{bad.number} (category {bad.category!r}): its replacement {bad.replacement!r} is used inside "
            "an identifier here, so it must itself be an identifier (letters, digits and underscores, not starting "
            "with a digit); give the spelling with underscores an entry of its own"
        )
    return new


def _swap_whole(content: str, entry: Entry) -> str:
    """Replace the stripped content with the entry's replacement, keeping the surrounding whitespace."""
    lead = content[: len(content) - len(content.lstrip())]
    trail = content[len(content.rstrip()) :]
    return lead + entry.replacement + trail


def _literal(replacer: _Replacer, raw: str) -> str:
    """Return the sanitised form of a string token: the whole content if it equals a token, else per component."""
    parts = _STRING_PARTS_RE.match(raw)
    if parts is None:
        raise SanitiseError("unrecognised string literal syntax")
    prefix, quote, content = parts.groups()
    entry = replacer.whole(content)
    if entry is not None:
        content = _swap_whole(content, entry)
    else:
        content, _ = replacer.within(content, escapes="r" not in prefix.lower())
    return f"{prefix}{quote}{content}{quote}"


def _rewrite(replacer: _Replacer, kind: int, raw: str, *, raw_fstring: bool, whole_fstring: bool) -> str:
    """Return the sanitised text of one token; tokens that cannot hold a study token come back unchanged."""
    if kind == tokenize.NAME:
        return _name(replacer, raw)
    if kind == tokenize.STRING:
        return _literal(replacer, raw)
    if kind == tokenize.COMMENT:
        return replacer.within(raw, escapes=False)[0]
    if kind in FSTRING_MIDDLE_TYPES:
        entry = replacer.whole(raw) if whole_fstring else None
        return _swap_whole(raw, entry) if entry else replacer.within(raw, escapes=not raw_fstring)[0]
    return raw


def _line_starts(lines: Sequence[str]) -> list[int]:
    """Return the absolute offset at which each line starts."""
    starts, total = [], 0
    for line in lines:
        starts.append(total)
        total += len(line)
    return starts


def _tokenise(text: str, filename: str) -> list[tokenize.TokenInfo]:
    """Tokenise Python text; raise SanitiseError naming the file when that fails."""
    try:
        return list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError) as error:
        raise SanitiseError(f"{filename}: cannot tokenise: {error}") from error


def _check_compiles(text: str, filename: str, what: str) -> None:
    """Raise SanitiseError naming the file when the text is not valid Python."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)  # only validity matters, not style warnings
            compile(text, filename, "exec")
    except (SyntaxError, ValueError) as error:
        raise SanitiseError(f"{filename}: {what} does not compile: {error}") from error


def _is_raw_prefix(start_token: str) -> bool:
    """Tell whether an f-string start token (for example `rf"`) carries the raw prefix."""
    return "r" in _PREFIX_RE.match(start_token).group().lower()  # the pattern matches the empty string at worst


def sanitise_python(text: str, entries: Iterable[Entry], *, filename: str = "<source>") -> str:
    """Replace mapped study tokens in Python source and return text that still compiles.

    Only the characters of replaced tokens change; every other byte of the source, layout included, is kept.
    """
    _check_compiles(text, filename, "the source")
    replacer = _Replacer(entries)
    starts = _line_starts(split_lines(text))
    edits: list[tuple[int, int, str]] = []
    raw_fstrings: list[bool] = []
    tokens = _tokenise(text, filename)
    for index, tok in enumerate(tokens):
        if tok.type in FSTRING_START_TYPES:
            raw_fstrings.append(_is_raw_prefix(tok.string))
        elif tok.type in FSTRING_END_TYPES:
            raw_fstrings.pop()
        if tok.type not in _REWRITTEN:
            continue
        begin, end = starts[tok.start[0] - 1] + tok.start[1], starts[tok.end[0] - 1] + tok.end[1]
        raw = text[begin:end]
        try:
            whole = tokens[index - 1].type in FSTRING_START_TYPES and tokens[index + 1].type in FSTRING_END_TYPES
            new = _rewrite(
                replacer, tok.type, raw, raw_fstring=bool(raw_fstrings and raw_fstrings[-1]), whole_fstring=whole
            )
        except SanitiseError as error:
            raise SanitiseError(f"{filename}:{tok.start[0]}: {error}") from error
        if new != raw:
            edits.append((begin, end, new))
    for begin, end, new in sorted(edits, reverse=True):
        text = text[:begin] + new + text[end:]
    _check_compiles(text, filename, "the sanitised source")
    return text


def sanitise_checked(text: str, entries: Iterable[Entry], *, filename: str = "<source>") -> str:
    """Sanitise, then run the study-token checker on the result against the map's own tokens; refuse leftovers.

    This catches what the rewriting missed: escapes, full-width forms, tokens glued to other letters, and so on.
    """
    entries = tuple(entries)
    result = sanitise_python(text, entries, filename=filename)
    remaining = scan_source(_CHECK_PATH, result, hashes_from_tokens(entry.token for entry in entries))
    if remaining:
        where = ", ".join(f"line {f.line} {f.kind} {f.hash_prefix}" for f in remaining)
        raise SanitiseError(f"{filename}: {len(remaining)} study-token finding(s) remain after sanitising: {where}")
    return result


def validate_source(source_repo: str, commit: str, source_path: str) -> str:
    """Check the three header fields of an import and return the commit in lower case; raise SanitiseError if bad."""
    if not _REPO_RE.fullmatch(source_repo) or {".", ".."} & set(source_repo.split("/")):
        raise SanitiseError(f"source repository {source_repo!r} must be a name or owner/name, never a URL")
    if not _COMMIT_RE.fullmatch(commit):
        raise SanitiseError(f"commit {commit!r} must be 7 to 40 hexadecimal characters")
    pure = PurePosixPath(source_path)
    if not source_path or not source_path.isprintable() or "\\" in source_path or pure.is_absolute():
        raise SanitiseError(f"source path {source_path!r} must be a relative path inside the source repository")
    if ".." in pure.parts:
        raise SanitiseError(f"source path {source_path!r} must not contain '..'")
    return commit.lower()


def _newline_of(text: str) -> str:
    """Return the line ending of the first line (CRLF or LF)."""
    first = text.split("\n", 1)[0]
    return "\r\n" if first.endswith("\r") else "\n"


def import_module(map_path: Path, source_repo: str, commit: str, source_path: str, src: Path, dest: Path) -> str:
    """Sanitise src into dest with a provenance header; refuse to overwrite; return the map's SHA-256."""
    commit = validate_source(source_repo, commit, source_path)
    token_map = load_map(map_path)
    data = src.read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SanitiseError(f"{src}: not UTF-8: {error}") from error
    body = sanitise_checked(text, token_map.entries, filename=str(src))
    header = format_import_header(source_repo, commit, source_path, blob_id(data), token_map.sha256)
    module = insert_header(body, header.replace("\n", _newline_of(body)))
    try:
        with dest.open("xb") as handle:
            handle.write(module.encode("utf-8"))
    except FileExistsError as error:
        raise SanitiseError(f"{dest} already exists; refusing to overwrite it") from error
    return token_map.sha256


def _first_difference(expected: str, actual: str) -> int:
    """Return the 1-based number of the first line at which two texts differ."""
    left, right = expected.split("\n"), actual.split("\n")
    for number, (a, b) in enumerate(zip(left, right, strict=False), start=1):
        if a != b:
            return number
    return min(len(left), len(right)) + 1


def verify_module(map_path: Path, src: Path, dest: Path) -> list[str]:
    """Re-run the sanitisation of src and list every way dest differs from it; empty when dest is exact.

    Both files are read as bytes and decoded without newline translation, so a CRLF import verifies.
    """
    token_map = load_map(map_path)
    data = src.read_bytes()
    module = dest.read_bytes().decode("utf-8")
    try:
        provenance = read_header(module)
    except HeaderError as error:
        return [f"{dest}: unreadable provenance header: {error}"]
    if provenance is None or provenance.new:
        return [f"{dest}: has no source header, so it is not a sanitised import"]
    problems = []
    if provenance.blob != blob_id(data):
        problems.append(f"{dest}: header blob id does not match {src}")
    if provenance.map_sha256 != token_map.sha256:
        problems.append(f"{dest}: header map hash does not match {map_path}")
    expected = sanitise_checked(data.decode("utf-8"), token_map.entries, filename=str(src))
    actual = strip_header(module)
    if expected != actual:
        line = _first_difference(expected, actual)
        problems.append(f"{dest}: body differs from the sanitised source, first at line {line}")
    return problems


def _cmd_import(args: argparse.Namespace) -> int:
    """Run the import command and print the map's SHA-256."""
    print(import_module(args.map, args.source_repo, args.commit, args.path, args.src, args.dest))
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace) -> int:
    """Run the verify command; exit 1 and list the problems when the module is not an exact import."""
    problems = verify_module(args.map, args.source, args.module)
    for problem in problems:
        print(f"FAIL {problem}")
    if not problems:
        print(f"ok {args.module}")
    return EXIT_FINDINGS if problems else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = argparse.ArgumentParser(prog="python -m expcore.provenance.sanitise", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="sanitise SRC into DEST with a provenance header")
    imp.add_argument("--map", type=Path, required=True)
    imp.add_argument("--source-repo", required=True, help="name of the source repository (never a URL)")
    imp.add_argument("--commit", required=True, help="source commit sha (7 to 40 hex characters)")
    imp.add_argument("--path", required=True, help="relative path of the file in the source repository")
    imp.add_argument("src", type=Path)
    imp.add_argument("dest", type=Path)
    imp.set_defaults(run=_cmd_import)
    ver = sub.add_parser("verify", help="check that MODULE is the exact sanitisation of SOURCE")
    ver.add_argument("--map", type=Path, required=True)
    ver.add_argument("--source", type=Path, required=True)
    ver.add_argument("--module", type=Path, required=True)
    ver.set_defaults(run=_cmd_verify)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line; return 0 on success, 1 when verify fails, 2 on an error."""
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except (OSError, SanitiseError, MapError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
