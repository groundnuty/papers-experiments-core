# Provenance: new
"""Where a model's key lives: never part of the experiment, and never published in a replication package (K13).

`key_env` and `key_file` of a `models.toml` entry say where a request finds its key. That is deployment: the key goes
into the request's authorisation, never into what the model receives, and the location never vouched for the key or
its account anyway (the same path with another key in it was the same configuration). A location can name what a
package must not show, though: a home directory, an earlier study's folder, an account.

`withhold_key_locations` gives models.toml with each such value replaced by a fixed placeholder, `KEY_FILE_WITHHELD` or
`KEY_ENV_WITHHELD`, and every other byte as it was. That form is the file's identity:

- the code identity hashes models.toml in that form (`CodeScope.canonical`, set by `study.config`), and so does the
  campaign fingerprint, in the file's hash and in each model's digest (`withheld_entry`), so moving a key never changes
  either. A table that names no key location is its own form, so its hashes are what they always were;
- every replication package ships models.toml in that form, so the identity a reader recomputes from the shipped file
  is the one the records were checked under, with no transformation to take on trust. The build refuses a package
  whose models.toml still holds any other key location (`location_findings`), and one in which any file still names
  a key location the study's models declare, is a declared key file, or is or holds a copy of one under any name
  (`location_finders`), as safety nets behind the rewrite.

The rewrite is minimal by construction, and checked. It reads the file's bytes once, as UTF-8 with no newline
translation, which is how the table is loaded too. It marks the lines that begin a statement, outside every string,
array and inline table (`_statement_starts`); a value is replaced only on such a line that is a plain assignment
(`key_file = "..."` or `key_env = '...'`, the key bare or quoted, the value a one-line string, a comment after it kept)
directly under a `[models.<key>]` header. Then every key location the parsed table declares must have been found
exactly once, only those lines may differ, and the rewritten file must parse to the original with exactly those values
replaced. Anything else, a key location in an inline table, under a dotted key or as a multi-line string, or bytes that
are not a TOML table as they are, raises KeyLocationUnreachable: loading the table refuses it, naming the file, and so
does hashing it, so no such study runs, is checked or ships.
"""

import dataclasses
import hashlib
import os
import re
import stat
import tomllib
from collections.abc import Iterable, Mapping
from typing import Any

from expcore.config.loading import ConfigError

KEY_FILE_WITHHELD = "<withheld>"  # where the key file lives is never published
KEY_ENV_WITHHELD = "KEY_WITHHELD"  # a variable name, as key_env must be one, and none an author chose
PLACEHOLDERS = {"key_env": KEY_ENV_WITHHELD, "key_file": KEY_FILE_WITHHELD}
FOUND_KEY = "a key location, which a package never holds (the build writes a placeholder there)"

_ASSIGNMENT = re.compile(
    r"(?P<lead>[ \t]*)(?P<key>(?P<quote>[\"']?)(?P<name>key_env|key_file)(?P=quote))(?P<eq>[ \t]*=[ \t]*)"
    r"(?P<value>\"(?:[^\"\\]|\\.)*\"|'[^']*')(?P<rest>[ \t]*(?:#.*)?)"
)
_LINES = re.compile(r"(?<=\n)")  # TOML ends a line at LF (CRLF included) and nowhere else
_NAME_EDGE = r"A-Za-z0-9_"  # a variable's name is found only as a whole identifier
_PART = r"\w\-~+@%"  # what continues a part of a path written in text, besides a full stop
_QUOTE = "\"'`"
_RIGHT = rf"(?![{_PART}/\\]|\.+[{_PART}/\\])"  # nothing that continues a part or a path follows
_HOME_DIRECTORY = re.compile(r"/(?:home/[^/]+|Users/[^/]+|root)/(?P<tail>.+)")  # a home directory of any machine
_HOMES = (  # what a tail is written behind: `~`, `~name`, a variable, a home directory of any machine
    rf"(?:~[{_PART}.]*|\$HOME|\$\{{HOME\}}|%(?:USERPROFILE|HOME)%|/home/[{_PART}.]+|/Users/[{_PART}.]+|/root)"
)
MAX_KEY_FILE_BYTES = 2**20  # a key is a small secret: a file over 1 MiB is not one
MIN_NEEDLE_BYTES = 16  # a key file's content is searched for inside other bytes from this length: a shorter run of
# bytes occurs by chance in a large package, a random 16 bytes (128 bits) never does


class KeyLocationUnreachable(ConfigError):
    """A key location the rewrite cannot replace, or a file it cannot read: a package could not withhold it."""


def _statement_starts(text: str) -> list[bool]:
    """For each line of the text, whether it begins outside every string, array and inline table: only such a line
    can begin a statement. Comments are skipped; a multi-line string may close on up to five quotes, of which the last
    three are its delimiter; a backslash in a basic string escapes the next character, a newline included."""
    starts: list[bool] = []
    quote: str | None = None  # the open string's delimiter: ", ', """ or '''
    depth = 0  # brackets and braces open around the position (a header's open and close on its own line)
    line_start, i, size = True, 0, len(text)
    while i < size:
        if line_start:
            starts.append(quote is None and depth == 0)
            line_start = False
        char = text[i]
        if quote is None:
            if char == "#":
                end = text.find("\n", i)
                i = size if end < 0 else end
                continue
            if char in "\"'":
                quote = char * 3 if text.startswith(char * 3, i) else char
                i += len(quote)
                continue
            depth += (char in "[{") - (char in "]}")
        elif char == "\\" and quote[0] == '"':
            line_start = text.startswith("\n", i + 1)
            i += 2
            continue
        elif text.startswith(quote, i):
            run = len(quote)
            while 3 <= run < 5 and text.startswith(quote[0], i + run):
                run += 1
            quote, i = None, i + run
            continue
        line_start = char == "\n"
        i += 1
    return starts


def _header(line: str) -> tuple[str, ...] | None:
    """The keys of a standard table header (`[models."a.b"]`); None for an array of tables or anything else."""
    if line.lstrip().startswith("[["):
        return None
    try:
        parsed: Any = tomllib.loads(line)
    except tomllib.TOMLDecodeError:
        return None
    path: list[str] = []
    while isinstance(parsed, dict) and len(parsed) == 1:
        key, parsed = next(iter(parsed.items()))
        path.append(key)
    return tuple(path) if path and parsed == {} else None


def _assignments(lines: list[str], starts: list[bool]) -> list[tuple[int, str, re.Match[str]]]:
    """Each line that is a plain key-location assignment directly under a `[models.<key>]` header, beginning a
    statement: its number, the model's key and the match. Any header resets the table, an array of tables to none."""
    table: tuple[str, ...] | None = ()
    found = []
    for number, (line, start) in enumerate(zip(lines, starts, strict=False)):
        content = line.rstrip("\n").removesuffix("\r")
        if not start:
            continue
        if content.lstrip().startswith("["):
            table = _header(content)
            continue
        match = _ASSIGNMENT.fullmatch(content)
        if match is not None and table is not None and len(table) == 2 and table[0] == "models":
            found.append((number, table[1], match))
    return found


def _rewritten(line: str, match: re.Match[str]) -> str:
    """A plain key-location assignment with its value replaced by the placeholder, everything else of it kept."""
    ending = line[len(match.string) :]
    value = PLACEHOLDERS[match["name"]]
    return f'{match["lead"]}{match["key"]}{match["eq"]}"{value}"{match["rest"]}{ending}'


def _entries(parsed: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """The model entries of a parsed table that are tables."""
    models = parsed.get("models")
    if not isinstance(models, Mapping):
        return {}
    return {key: entry for key, entry in models.items() if isinstance(entry, Mapping)}


def _expected(parsed: dict[str, Any]) -> dict[str, Any]:
    """The parsed table with every key location of every model replaced by its placeholder, and nothing else changed."""
    entries = _entries(parsed)
    models = {key: {f: PLACEHOLDERS.get(f, v) for f, v in entry.items()} for key, entry in entries.items()}
    return {**parsed, "models": {**parsed["models"], **models}} if entries else parsed


def _unreachable(where: str) -> KeyLocationUnreachable:
    return KeyLocationUnreachable(
        f"{where} cannot be withheld from a replication package as it is written; write it on a line of its own in "
        'the model\'s table, as key_file = "..." or key_env = "...", outside any multi-line string'
    )


def withhold_key_locations(data: bytes) -> bytes:
    """models.toml with every key location replaced by its placeholder, and every other byte as it was.

    The bytes come back unchanged, the same object, when they name no key location. A placeholder already there is
    written again in the one form, so two tables that differ only in where their keys live give the same bytes.
    Raises KeyLocationUnreachable, naming the model and the key, when a key location is written where the rewrite
    cannot reach it, and when the bytes are not a TOML table as they are (UTF-8, no bare carriage return).
    """
    try:
        text = data.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise KeyLocationUnreachable(
            f"cannot be read as a TOML table as its bytes are ({type(error).__name__}), so its key locations cannot "
            "be withheld"
        ) from None
    declared = [(key, field) for key, entry in _entries(parsed).items() for field in PLACEHOLDERS if field in entry]
    if not declared:
        return data
    lines = _LINES.split(text)
    found = _assignments(lines, _statement_starts(text))
    pairs = [(key, match["name"]) for _, key, match in found]
    for key, field in declared:
        if pairs.count((key, field)) != 1:
            raise _unreachable(f"[models.{key}]: key {field!r}")
    if len(pairs) != len(declared):
        raise _unreachable("a line taken for a key location")
    out = list(lines)
    for number, _, match in found:
        out[number] = _rewritten(lines[number], match)
    rewritten = "".join(out)
    changed = {number for number, (before, after) in enumerate(zip(lines, out, strict=True)) if before != after}
    try:
        same = changed <= {number for number, _, _ in found} and tomllib.loads(rewritten) == _expected(parsed)
    except tomllib.TOMLDecodeError:
        same = False
    if not same:
        raise _unreachable("a key location")
    return rewritten.encode("utf-8")


def withheld_entry(spec: Any) -> dict[str, Any]:
    """A model entry as the campaign fingerprint digests it: its settings, each key location as its placeholder.

    An entry that names no key location gives exactly its settings, so its digest is the one it always had.
    """
    entry = dataclasses.asdict(spec)
    return {**entry, **{field: value for field, value in PLACEHOLDERS.items() if entry.get(field) is not None}}


def location_findings(data: bytes) -> list[str]:
    """The key locations a package's models.toml still holds, each as `[models.<key>]: key '<field>': ...` and never
    with its value; a file that cannot be read as TOML is one finding."""
    try:
        parsed = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        return ["cannot be read as TOML, so its key locations cannot be checked"]
    return [
        f"[models.{key}]: key {field!r}: {FOUND_KEY}"
        for key, entry in _entries(parsed).items()
        for field, placeholder in PLACEHOLDERS.items()
        if field in entry and entry[field] != placeholder
    ]


def _path_finder(path: str) -> re.Pattern[str]:
    """What finds a key file's path in text, case-sensitively, wherever no part of a path continues it.

    On the left that is the start, or any character but a letter, a digit or one of `_-~+@%.`: white space, a quote,
    a bracket, `=`, `,`, `:`, a separator (so a relative path is found inside a longer one, and an absolute one after
    `file://`). On the right it is the end, or any character but those and a separator: white space, a quote, a
    bracket, `:` (a mount, a line number), `,`, `;`; a run of full stops ends it too where nothing that continues a path
    follows. A bare file name that is an identifier (`secrets`) is an ordinary word, so on the left it is found only
    after a separator or a quote (`/x/lab.key`, `"lab.key"`); one with a full stop or a hyphen (`prior-study.key`) is
    a path of one part, found like any other (a `.gitignore` line, `KEY_FILE=prior-study.key`, a sentence). A packaged
    file named as the key file is found by its path (`KeyFinders.names_file`)."""
    left = rf"(?<![{_PART}.])" if _is_path(path) else rf"(?<=[{_QUOTE}/\\])"
    return re.compile(rf"{left}{re.escape(path)}{_RIGHT}")


def _is_path(text: str) -> bool:
    """Whether the text is a path with a directory, or a name that is not a plain word (`False` for `key`)."""
    return any(char in text for char in "/\\.-")


def _home_finder(tail: str) -> re.Pattern[str]:
    """What finds a tail that is one bare word, only behind a home directory (`_HOMES`): `~/key`, `$HOME/key`,
    `${HOME}/key`, `/home/someone/key`. On its own it is an ordinary word (`d["key"]`, `{"token": t}`)."""
    return re.compile(rf"(?<![{_PART}.]){_HOMES}/{re.escape(tail)}{_RIGHT}")


def _tails(key_file: str) -> set[str]:
    """What follows the home directory in a declared key file's path: after `~/` or `~user/`, after this machine's
    home for a path under it, and after `/home/<name>/`, `/Users/<name>/` or `/root/` for a path of any machine's
    (`$HOME/`, `${HOME}/`, `~/` and another host's home precede it where it is written). None is empty."""
    expanded = os.path.expanduser(key_file)
    tails = set()
    if key_file.startswith("~"):
        tails.add(key_file.partition("/")[2])
    home = os.path.expanduser("~").rstrip("/")
    if home.startswith("/") and expanded.startswith(f"{home}/"):
        tails.add(expanded[len(home) + 1 :])
    for path in {key_file, expanded}:
        if (under := _HOME_DIRECTORY.fullmatch(path)) is not None:
            tails.add(under["tail"])
    return {tail for tail in tails if tail.strip("./\\")}


def _spellings(key_file: str) -> tuple[set[str], set[str]]:
    """The ways a declared key file's path is written elsewhere, as (whole, bare). `whole` is searched as a relative
    path: as declared, with `~` expanded, without a leading `./`, and each tail after a home directory (`_tails`).
    `bare` is the tails that are one word (`~/key`'s `key`): searched only behind a home directory (`_home_finder`)."""
    expanded = os.path.expanduser(key_file)
    spellings = {key_file, expanded}
    unrooted = key_file
    while unrooted.startswith("./"):
        unrooted = unrooted[2:]
    spellings.add(unrooted)
    tails = _tails(key_file)
    spellings |= {tail for tail in tails if _is_path(tail)}
    return {spelling for spelling in spellings if spelling.strip("./\\")}, {
        tail for tail in tails if not _is_path(tail)
    }


def _ends_with(path: str, tail: str) -> bool:
    """Whether `tail` is the whole path or its last parts."""
    return bool(tail) and (path == tail or path.endswith(f"/{tail}"))


@dataclasses.dataclass(frozen=True)
class KeyFinders:
    """What finds each key location a study's models declare: in text (`patterns`), as a packaged file (`files`, each
    key file's spellings), as a copy of a key file under any name (`digests`, the size and SHA-256 of each key file
    that could be read), and as the content of one inside other bytes of any format (`needles`, each key file's bytes
    as they are and with the white space around them stripped, those of at least `MIN_NEEDLE_BYTES` only: a shorter key
    is found as a whole stream, by `digests`, and not inside other bytes). `needles` hold the key itself: they are
    kept out of the repr, and nothing may print them."""

    patterns: tuple[re.Pattern[str], ...] = ()
    files: tuple[str, ...] = ()
    digests: frozenset[tuple[int, str]] = frozenset()
    needles: tuple[bytes, ...] = dataclasses.field(default=(), repr=False)

    def __bool__(self) -> bool:
        return bool(self.patterns or self.files or self.digests)

    def names_file(self, label: str) -> bool:
        """Whether a packaged file can be a declared key file itself: its path, or the path of the archive member it
        is (after the last `!` of its label), and the key file's agree on their last parts, the shorter one whole,
        without regard to case (a label is a name, which a file system may fold). So `lab.key` and `notes/Lab.key`
        are `lab.key`, and `lab.key` is `/srv/study/lab.key` (a key file declared from another directory);
        `my-lab.key` is neither. Text is searched case-sensitively, by `patterns`."""
        parts = {part.casefold() for part in (label, label.rpartition("!")[2])}
        folded = [path.casefold() for path in self.files]
        return any(_ends_with(part, path) or _ends_with(path, part) for part in parts for path in folded)

    def is_key_file(self, data: bytes) -> bool:
        """Whether the bytes are a declared key file's own, whatever they are named: the same size and SHA-256."""
        if all(size != len(data) for size, _ in self.digests):
            return False
        return (len(data), hashlib.sha256(data).hexdigest()) in self.digests

    def holds_key_file(self, data: bytes) -> bool:
        """Whether the bytes hold a declared key file's content anywhere in them, in a container of any format, a
        text or a binary: a substring search (linear in the bytes), for the key file's exact bytes and for its bytes
        with the white space around them stripped, since a copy in a script or a note usually lacks the key file's
        trailing newline. A key shorter than `MIN_NEEDLE_BYTES` has no needle: `is_key_file` alone finds that one."""
        return any(needle in data for needle in self.needles)


NO_LOCATIONS = KeyFinders()  # nothing to look for


def _key_file(spec: Any) -> bytes | None:
    """The bytes of a model's key file, read once as the key is read (`ModelSpec.read_key`); None when it is not a
    readable file or holds nothing a key could. A file over `MAX_KEY_FILE_BYTES` is refused: no key is that large, and
    a copy of it could not be told by its bytes. The message names the model, never the file."""
    path = os.path.expanduser(spec.key_file)
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        with open(path, "rb") as handle:
            data = handle.read(MAX_KEY_FILE_BYTES + 1)
    except (OSError, ValueError):
        return None
    if len(data) > MAX_KEY_FILE_BYTES:
        raise KeyLocationUnreachable(
            f"[models.{getattr(spec, 'key', '?')}]: key 'key_file': the file it names is over 1 MiB, too large to be a "
            "key, so a copy of it could not be found by its bytes; point key_file at the key itself"
        )
    return data if data.strip() else None


def _needles(key: bytes) -> set[bytes]:
    """What is searched for inside other bytes of a key file's content: the bytes, and the bytes with the white space
    around them stripped, when long enough to be told from chance (`MIN_NEEDLE_BYTES`)."""
    return {needle for needle in (key, key.strip()) if len(needle) >= MIN_NEEDLE_BYTES}


def location_finders(specs: Iterable[Any]) -> KeyFinders:
    """What finds each key location the models declare: a key file's path in the spellings of `_spellings`, in text
    (`_path_finder`, `_home_finder`) and as a packaged file (`KeyFinders.names_file`); the key file itself as a copy
    under any name, and its content inside other bytes when it is long enough (`_key_file`, `_needles`), when it can be
    read now; and a variable's name as a whole identifier. Text is searched case-sensitively. A placeholder is never
    looked for.
    """
    paths, bare, names, digests, needles = set(), set(), set(), set(), set()
    for spec in specs:
        if spec.key_file and spec.key_file != KEY_FILE_WITHHELD:
            whole, words = _spellings(spec.key_file)
            paths |= whole
            bare |= words
            key = _key_file(spec)
            if key is not None:
                digests.add((len(key), hashlib.sha256(key).hexdigest()))
                needles |= _needles(key)
        if spec.key_env and spec.key_env != KEY_ENV_WITHHELD:
            names.add(spec.key_env)
    return KeyFinders(
        patterns=(
            *(_path_finder(path) for path in sorted(paths)),
            *(_home_finder(tail) for tail in sorted(bare)),
            *(re.compile(rf"(?<![{_NAME_EDGE}]){re.escape(name)}(?![{_NAME_EDGE}])") for name in sorted(names)),
        ),
        files=tuple(sorted(paths)),
        digests=frozenset(digests),
        needles=tuple(sorted(needles)),
    )
