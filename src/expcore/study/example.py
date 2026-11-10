# Provenance: new
"""The example study, as the installed core carries it: `python -m expcore example DEST` writes it into a directory.

The example lives in the repository at `examples/tiny_study/` and the wheel carries it as `expcore/examples/tiny_study/`
(see `pyproject.toml`), so a study that depends on the core by tag or commit can write it out and run it without
copying a file from a checkout. An editable install has no copy in the package and reads the checkout's directory.
What the example's own runs left behind (`out/`, caches) is never written out.

The example's README links the core's documentation relative to the checkout (`../../docs/...`), which leads nowhere
in a study's own repository. The copy written out points at the documentation of the core that wrote it instead: a
link to the page on GitHub at the commit the core was installed from, when it came from a GitHub repository, and
otherwise the page's path in the core's repository with the commit or the version's tag it is at.

The core names no URL of its own repository anywhere it ships (K11): its wheel goes into every replication package,
and an anonymised build refuses a package that holds the URL the core was installed from. So the link is built from
the URL this install records, never from one written here.
"""

import importlib.resources
import json
import re
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import expcore
from expcore.provenance.codestamp import is_sha
from expcore.provenance.environment import CORE_DISTRIBUTION

EXAMPLE = "tiny_study"
LEFT_OUT = frozenset({"out", "__pycache__"})
README = "README.md"
# A link of the README into the checkout's docs, [shown text](../../docs/page), with the text and the page.
DOCS_LINK = re.compile(r"\[([^\]\n]+)\]\(\.\./\.\./docs/([^)\s]+)\)")
WEB_HOST = "github.com"  # the host whose page layout, /blob/<commit>/<path>, the link is built for


class ExampleError(RuntimeError):
    """The example cannot be written: it is missing from this install, or the destination is not empty."""


@dataclass(frozen=True)
class DocsPlace:
    """Where the documentation of this install is: `url`, the docs directory on the web ending in a slash, when it is
    known; and `where`, the commit, tag or branch of the core's repository it is at, for a reader to find it."""

    url: str | None
    where: str


def _direct_url(dist: metadata.Distribution) -> dict[str, Any]:
    """The distribution's `direct_url.json`; empty when it has none or it cannot be read."""
    try:
        data = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _web_docs(url: object, commit: str) -> str | None:
    """The docs directory at the commit, for a repository URL on the web host; None for any other URL."""
    parts = urlsplit(url) if isinstance(url, str) else None
    if parts is None or parts.scheme != "https" or parts.hostname != WEB_HOST or not parts.path.strip("/"):
        return None
    path = parts.path.rstrip("/").removesuffix(".git")
    return urlunsplit(("https", WEB_HOST, f"{path}/blob/{commit}/docs/", "", ""))  # no credentials, query or fragment


def docs_place() -> DocsPlace:
    """Where this install's documentation is: on the web at the commit it was installed from, when that was a GitHub
    repository; else in the core's repository at that commit, at its version's tag, or (no distribution installed)
    on the default branch."""
    try:
        dist = metadata.distribution(CORE_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return DocsPlace(None, "on its default branch")
    data = _direct_url(dist)
    vcs = data.get("vcs_info")
    commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
    if is_sha(commit):
        return DocsPlace(_web_docs(data.get("url"), commit), f"at commit {commit}")
    return DocsPlace(None, f"at tag v{dist.version.split('+')[0]}")  # a local version label is no tag


def readme_text(text: str, place: DocsPlace | None = None) -> str:
    """The README as written out: each link into the checkout's docs points at this install's docs instead, as a link
    when their web address is known and as a path in the core's repository otherwise."""
    place = docs_place() if place is None else place

    def pointed(link: re.Match[str]) -> str:
        shown, page = link.group(1), link.group(2)
        if place.url is not None:
            return f"[{shown}]({place.url}{page})"
        path = f"docs/{page.partition('#')[0]}"
        named = "" if shown == f"`{path}`" else f"`{path}` "
        return f"{shown} ({named}in the core's repository, {place.where})"

    return DOCS_LINK.sub(pointed, text)


def example_source() -> Path:
    """The directory of the example study: in the installed package, or in the checkout of an editable install."""
    shipped = importlib.resources.files("expcore").joinpath("examples", EXAMPLE)
    if shipped.is_dir():
        return Path(str(shipped))
    checkout = Path(expcore.__file__).resolve().parents[2] / "examples" / EXAMPLE
    if checkout.is_dir():
        return checkout
    raise ExampleError("this install of the core carries no example study")


def write_example(dest: Path) -> list[Path]:
    """Write the example study into `dest`, which must not exist or must be empty; return the files written."""
    dest = Path(dest)
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise ExampleError(f"{dest}: exists and is not an empty directory; the example is written only into a new one")
    source = example_source()
    written = []
    for path in sorted(source.rglob("*")):
        rel = path.relative_to(source)
        if LEFT_OUT.intersection(rel.parts) or path.suffix == ".pyc" or not path.is_file():
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if rel.as_posix() == README:
            target.write_bytes(readme_text(path.read_bytes().decode("utf-8")).encode("utf-8"))
        else:
            target.write_bytes(path.read_bytes())
        written.append(target)
    return written
