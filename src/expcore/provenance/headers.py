# Provenance: new
"""Parse, format, insert and strip the provenance header that starts every module under src/."""

import re
from dataclasses import dataclass

NEW_LINE = "# Provenance: new"
_START = "# Provenance:"
_ENCODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
_CONTINUATION_RE = re.compile(r"^#   (source|import|merged|changes): ?(.*?)\s*$")
_SOURCE_RE = re.compile(
    r"^(?P<repo>\S+) @ (?P<commit>[0-9a-f]{7,64}), (?P<path>.+), blob (?P<blob>[0-9a-f]{40,64})$",
)
_IMPORT_RE = re.compile(r"^sanitised with study-token map (?P<map>[0-9a-f]{64})$")


class HeaderError(ValueError):
    """A provenance header is present but malformed."""


@dataclass(frozen=True)
class Provenance:
    """The parsed header: `new`, or the source blob and map hash of a sanitised import."""

    new: bool
    source_repo: str | None = None
    commit: str | None = None
    path: str | None = None
    blob: str | None = None
    map_sha256: str | None = None
    merged: tuple[str, ...] = ()
    changes: str | None = None


def split_lines(text: str) -> list[str]:
    """Split on newline only, keeping line ends, the way the tokenizer sees a file."""
    return re.findall(r"[^\n]*\n|[^\n]+", text)


def _preamble_length(lines: list[str]) -> int:
    """Count the leading shebang and encoding lines that must stay first."""
    index = 0
    if index < len(lines) and lines[index].startswith("#!"):
        index += 1
    if index < len(lines) and _ENCODING_RE.match(lines[index]):
        index += 1
    return index


def header_span(lines: list[str]) -> tuple[int, int] | None:
    """Return the half-open line range of the header block, or None when the file has none."""
    start = _preamble_length(lines)
    if start >= len(lines) or not lines[start].startswith(_START):
        return None
    end = start + 1
    while end < len(lines) and _CONTINUATION_RE.match(lines[end]):
        end += 1
    return start, end


def blank_header(text: str) -> str:
    """Replace the header lines with empty lines, so line numbers elsewhere stay unchanged."""
    lines = split_lines(text)
    span = header_span(lines)
    if span is None:
        return text
    return "".join("\n" if span[0] <= i < span[1] else line for i, line in enumerate(lines))


def strip_header(text: str) -> str:
    """Remove the header block and leave every other line, including a shebang, as it was."""
    lines = split_lines(text)
    span = header_span(lines)
    if span is None:
        return text
    return "".join(lines[: span[0]] + lines[span[1] :])


def insert_header(text: str, header: str) -> str:
    """Put the header after any shebang and encoding lines; inverse of strip_header."""
    lines = split_lines(text)
    cut = _preamble_length(lines)
    head = "".join(lines[:cut])
    if head and not head.endswith("\n"):
        head += "\n"
    return head + header + "".join(lines[cut:])


def format_import_header(repo: str, commit: str, path: str, blob: str, map_sha256: str) -> str:
    """Render the header of a sanitised import, ending in a newline."""
    return (
        f"{_START}\n"
        f"#   source: {repo} @ {commit}, {path}, blob {blob}\n"
        f"#   import: sanitised with study-token map {map_sha256}\n"
    )


def read_header(text: str) -> Provenance | None:
    """Parse the header of a module; None when it has none, HeaderError when it is malformed."""
    lines = split_lines(text)
    span = header_span(lines)
    if span is None:
        return None
    kind = lines[span[0]].rstrip("\r\n")[len(_START) :].strip()
    fields: dict[str, list[str]] = {"source": [], "import": [], "merged": [], "changes": []}
    for line in lines[span[0] + 1 : span[1]]:
        match = _CONTINUATION_RE.match(line.rstrip("\r\n"))
        if match is None:  # pragma: no cover - header_span only admits matching lines
            raise HeaderError(f"unreadable provenance line: {line!r}")
        fields[match.group(1)].append(match.group(2))
    if kind == "new":
        if fields["source"] or fields["import"]:
            raise HeaderError("a 'Provenance: new' header cannot carry source or import lines")
        return Provenance(new=True, merged=tuple(fields["merged"]), changes=_last(fields["changes"]))
    if kind:
        raise HeaderError(f"unknown provenance kind {kind!r}; expected 'new' or a source block")
    return _imported(fields)


def _last(values: list[str]) -> str | None:
    """Return the last value of a repeated field, or None."""
    return values[-1] if values else None


def _imported(fields: dict[str, list[str]]) -> Provenance:
    """Build a Provenance from the lines of a source block."""
    if len(fields["source"]) != 1 or len(fields["import"]) != 1:
        raise HeaderError("a source header needs exactly one 'source:' line and one 'import:' line")
    source = _SOURCE_RE.match(fields["source"][0])
    if source is None:
        raise HeaderError("malformed 'source:' line; expected '<repo> @ <commit>, <path>, blob <id>'")
    imported = _IMPORT_RE.match(fields["import"][0])
    if imported is None:
        raise HeaderError("malformed 'import:' line; expected 'sanitised with study-token map <sha256>'")
    return Provenance(
        new=False,
        source_repo=source.group("repo"),
        commit=source.group("commit"),
        path=source.group("path"),
        blob=source.group("blob"),
        map_sha256=imported.group("map"),
        merged=tuple(fields["merged"]),
        changes=_last(fields["changes"]),
    )
