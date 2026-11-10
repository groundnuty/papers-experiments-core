# Provenance: new
"""An anonymised package withholds the provenance headers of the code it vendors.

The core's modules start with a provenance header (`expcore.provenance.headers`) that names the earlier repositories
their code was extracted from, with commits and blob ids: in a double-blind package, a trail back to the authors. An
anonymised build replaces each such header in every vendored wheel, the core's and any other wheel that carries the
format, with one neutral comment line, and writes the wheel's RECORD anew. The rewrite may touch comments only: each
rewritten module's syntax tree must equal the original's, or the build is refused. A header that starts like one but
does not parse is refused too, since what it names could not be collected. Every repository name and every commit or
blob id the headers held is returned, and the package's final check refuses the build if any of them survives
anywhere in it, inside the wheels included. A package that is not anonymised keeps the headers.
"""

import ast
import base64
import csv
import hashlib
import io
import re
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

from expcore.package.spec import PackageError
from expcore.package.wheels import Wheel, zip_entries
from expcore.provenance.headers import HeaderError, header_span, read_header, split_lines

NEUTRAL = "# (provenance withheld for an anonymous review)\n"
_NAMED = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*) @ ([0-9a-f]{7,64})\b")
_IDS = re.compile(r"\b[0-9a-f]{7,64}\b")


@dataclass
class Withheld:
    """What the withheld headers named: repositories, and commit, blob and map ids."""

    repositories: set[str] = field(default_factory=set)
    ids: set[str] = field(default_factory=set)


def _neutral(label: str, text: str, withheld: Withheld) -> str | None:
    """The module with its provenance header replaced by the neutral line; None when it has no header."""
    lines = split_lines(text)
    span = header_span(lines)
    if span is None:
        return None
    try:
        read_header(text)
    except HeaderError as error:
        raise PackageError(
            f"{label}: a provenance header that cannot be read ({error}); it cannot be withheld"
        ) from None
    for line in lines[span[0] : span[1]]:
        for repository, commit in _NAMED.findall(line):
            withheld.repositories.add(repository)
            withheld.ids.add(commit)
        withheld.ids.update(_IDS.findall(line))
    rewritten = "".join([*lines[: span[0]], NEUTRAL, *lines[span[1] :]])
    try:
        same = ast.dump(ast.parse(text)) == ast.dump(ast.parse(rewritten))
    except SyntaxError:
        same = False
    if not same:
        raise PackageError(f"{label}: withholding its provenance header would change its code")
    return rewritten


def _record(text: str, changed: dict[str, bytes]) -> str:
    """RECORD with the hash and size of each changed member written anew."""
    rows = []
    for row in csv.reader(io.StringIO(text)):
        if row and row[0] in changed:
            data = changed[row[0]]
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode("ascii")
            row = [row[0], f"sha256={digest}", str(len(data))]
        rows.append(row)
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue()


def _withhold_one(wheel: Wheel, withheld: Withheld) -> Wheel:
    with zipfile.ZipFile(io.BytesIO(wheel.data)) as archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        contents = {info.filename: archive.read(info) for info in infos}
    changed = {}
    for name, data in contents.items():
        if name.endswith(".py"):
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            rewritten = _neutral(f"{wheel.path}/{name}", text, withheld)
            if rewritten is not None:
                changed[name] = rewritten.encode("utf-8")
    if not changed:
        return wheel
    records = [name for name in contents if re.fullmatch(r"[^/]+\.dist-info/RECORD", name)]
    for name in records:
        changed[name] = _record(contents[name].decode("utf-8"), changed).encode("utf-8")
    entries = [
        (info.filename, changed.get(info.filename, contents[info.filename]), bool((info.external_attr >> 16) & 0o111))
        for info in infos
    ]
    return replace(wheel, data=zip_entries(entries))


def withhold_headers(wheels: Sequence[Wheel]) -> tuple[list[Wheel], Withheld]:
    """Every wheel with its provenance headers withheld, and what they named."""
    withheld = Withheld()
    return [_withhold_one(wheel, withheld) for wheel in wheels], withheld
