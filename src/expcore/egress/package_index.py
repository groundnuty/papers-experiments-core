# Provenance: new
"""The declared package indexes: a `pip install` from them, or a URL on one of their hosts, is reported in its own
class, `package_index`, and does not flag the run (O95).

The egress guard is about data fetched from outside the federation. A campaign may declare the package indexes its
agents may install from, in campaign.toml:

    [egress]
    package_indexes = ["files.pythonhosted.org", "pypi.org"]

The list (bare host names, sorted) is in the campaign's caps, so in the campaign fingerprint every store records and
every resume and merge compares whole (`CampaignConfig.caps`), and the analysis reads it back from the recorded
fingerprint (`recorded_package_indexes`), never from the study's file. With no `[egress]` table nothing changes.

Under a declared list the static scan (`expcore.egress.scan`) reclassifies two of its hits, and no other:

- a `url` hit whose host is exactly one of the declared hosts (never a suffix match);
- a cell's `pip install` hit, when every `pip install` in the cell is one from the default index (`plain_installs`).
  That holds only when each install's arguments are plain requirements (a name, extras and version specifiers) and a
  few options that change no source (`-q`, `-U`, `--user`, `--no-cache-dir`, ...). Anything else keeps the flag: an
  index or find-links option of any spelling, a requirements or constraints file, `-e`, a path, a URL, a VCS
  requirement, a shell or format substitution, an argument list that is not all literals, a command string or list
  added to or formatted at run time, an install that names no requirement where it is written. A cell that names an
  index setting anywhere (an index or find-links option's name, any `PIP_` or `UV_` environment setting, `pip config`,
  a `pip.conf`) also keeps the flag. The rule reads text, as the scan does: what it cannot see, it does not clear.

`curl`, `wget`, `git clone`, `conda`, every other URL, every network call and every outside connection in the egress
log flag exactly as before: the log holds addresses, never names, so it cannot be allow-listed by host.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

PACKAGE_INDEX = "package_index"  # the class of a reclassified hit, and the key of the runs that have one
CAP, KEY = "egress", "package_indexes"  # the caps' section and its key, as campaign.toml's [egress] table names them
PIP_INSTALL = re.compile(r"\bpip3?\b[\s'\",]*\binstall\b")  # the scan's own `pip install`, every form it matches

_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_HOST = re.compile(rf"{_LABEL}(?:\.{_LABEL})*")
# A cell that names an index setting anywhere: the options and variables of pip and uv, pip's own configuration.
_CELL_SOURCE = re.compile(
    r"index[-_]url|find[-_]links|trusted[-_]host|\bpip\.(?:conf|ini)\b|\bpip3?\b[\s'\",]*\bconfig\b", re.IGNORECASE
)
_SETTING = re.compile(r"\b(?:PIP|UV)_[A-Z]")  # any of pip's or uv's environment settings
# A redirection to nowhere or a descriptor duplication, which ends no argument list and names no source.
_REDIRECT = re.compile(r"(?:\d?>>?|&>)\s*/dev/null|\d?>&\d")
_OPTIONS = frozenset(
    (
        "--quiet", "--upgrade", "--user", "--no-cache-dir", "--no-deps", "--pre", "--verbose",
        "--break-system-packages", "--no-input", "--disable-pip-version-check", "--no-warn-script-location",
        "--force-reinstall", "--ignore-installed", "--prefer-binary", "--no-color", "--progress-bar",
        "--upgrade-strategy", "--root-user-action",
    )
)  # fmt: skip
_SHORT = re.compile(r"-[qUvI]+")  # -q, -qq, -U, -v, -I and their clusters
_OPTION_VALUE = re.compile(r"(--progress-bar|--upgrade-strategy|--root-user-action)=[a-z-]+")
_V = r"(?:===|==|!=|<=|>=|~=|<|>)[A-Za-z0-9.*+!_-]+"
_REQUIREMENT = re.compile(rf"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?(?:\[[A-Za-z0-9._,-]*\])?(?:{_V}(?:,{_V})*)?")
_SPECIFIER = re.compile(rf"(?:===|==|!=|<=|>=|~=|<|>)|{_V}(?:,{_V})*")
_LITERALS = re.compile(r"(?:\s*,\s*(?:'[^'\\\n]*'|\"[^\"\\\n]*\"))*\s*,?\s*")
_OPENERS, _CLOSERS = "([{", ")]}"


def index_hosts(values: Any) -> tuple[str, ...]:
    """The declared hosts, lower-cased, without a trailing dot, sorted; ValueError for anything else."""
    if not isinstance(values, Sequence) or isinstance(values, str) or not values:
        raise ValueError("must list at least one host name; leave the table out to declare none")
    hosts = []
    for value in values:
        name = value.lower().rstrip(".") if isinstance(value, str) else None
        if name is None or not _HOST.fullmatch(name):
            raise ValueError(f"holds {value!r}, which is not a bare host name (no scheme, path, port or wildcard)")
        if name in hosts:
            raise ValueError(f"lists {name!r} more than once")
        hosts.append(name)
    return tuple(sorted(hosts))


def recorded_package_indexes(fingerprint: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The package indexes a campaign fingerprint's caps declare; none when it declares none or holds another form,
    so that every install then flags as it does without the table."""
    caps = None if fingerprint is None else fingerprint.get("caps")
    entry = caps.get(CAP) if isinstance(caps, Mapping) else None
    if not isinstance(entry, Mapping) or set(entry) != {KEY}:
        return ()
    try:
        hosts = index_hosts(entry[KEY])
    except ValueError:
        return ()
    return hosts if list(hosts) == entry[KEY] else ()


def _depth(text: str) -> int:
    """The brackets left open in `text`, counted plainly (an unmatched closer counts for nothing)."""
    depth = 0
    for char in text:
        if char in _OPENERS:
            depth += 1
        elif char in _CLOSERS:
            depth = max(depth - 1, 0)
    return depth


def _quote_open(line: str) -> str | None:
    """The quote a line's text leaves open, if any: the install is then inside a string literal."""
    for quote in "\"'":
        if (len(re.findall(rf"(?<!\\){quote}", line)) % 2) == 1:
            return quote
    return None


def _arguments(text: str, start: int, enclosed: bool, quote: str | None) -> tuple[str, int]:
    """The text of one install's arguments from `start`, and where it ends: at a command separator, a bracket that
    closes the enclosing one, the string's closing quote, or the end of a line outside brackets (a line ending in a
    backslash goes on)."""
    inner, i, kept = 0, start, []
    while i < len(text):
        char = text[i]
        if char == quote or char in ";|&`":
            break
        if char == "#" and quote is None:  # a comment, left out, to the end of its line
            end = text.find("\n", i)
            i = len(text) if end < 0 else end
            continue
        if char in _OPENERS:
            inner += 1
        elif char in _CLOSERS:
            if inner == 0:
                break
            inner -= 1
        elif char == "\n" and not (enclosed or inner) and text[i - 1] != "\\":
            break
        kept.append(char)
        i += 1
    return "".join(kept), i


def _plain_token(token: str) -> bool:
    return bool(
        token in _OPTIONS
        or _SHORT.fullmatch(token)
        or _OPTION_VALUE.fullmatch(token)
        or _REQUIREMENT.fullmatch(token)
        or _SPECIFIER.fullmatch(token)
    )


def _plain_install(text: str, match: re.Match[str]) -> bool:
    """Whether one `pip install` names nothing but plain requirements and harmless options."""
    line = text[text.rfind("\n", 0, match.start()) + 1 : match.start()]
    listed = any(c in "'\"," for c in match.group())  # the argument-list form: "pip", "install", ...
    start = match.end()
    if listed and text[start : start + 1] in ("'", '"'):
        start += 1  # past the quote that closes "install"
    quote = None if listed else _quote_open(line)
    span, end = _arguments(text, start, _depth(text[: match.start()]) > 0, quote)
    if (listed or quote) and text[end + 1 :].lstrip()[:1] in ("+", "%", "."):
        return False  # the list or the string is added to, or formatted, at run time
    if listed:
        if not _LITERALS.fullmatch(span):
            return False  # an element that is not a literal
        tokens = [t for literal in re.findall(r"'([^']*)'|\"([^\"]*)\"", span) for t in "".join(literal).split()]
    else:
        tokens = [t.strip("'\"") for t in span.replace("\\\n", " ").split()]
    # an install that names no requirement here is one whose arguments come from elsewhere
    return any(_REQUIREMENT.fullmatch(token) for token in tokens) and all(
        token and _plain_token(token) for token in tokens
    )


def plain_installs(code: str) -> bool:
    """Whether every `pip install` in a cell is one from the default index (the module's text); False for a cell
    without one, and for a cell that names an index setting anywhere."""
    if _CELL_SOURCE.search(code) or _SETTING.search(code):
        return False
    text = _REDIRECT.sub(" ", code)
    found = list(PIP_INSTALL.finditer(text))
    return bool(found) and all(_plain_install(text, match) for match in found)
