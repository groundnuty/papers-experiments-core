# Provenance: new
"""The example study, as the installed core carries it: `python -m expcore example DEST` writes it into a directory.

The example lives in the repository at `examples/tiny_study/` and the wheel carries it as `expcore/examples/tiny_study/`
(see `pyproject.toml`), so a study that depends on the core by tag or commit can write it out and run it without
copying a file from a checkout. An editable install has no copy in the package and reads the checkout's directory.
What the example's own runs left behind (`out/`, caches) is never written out.

The example's README links the core's documentation relative to the checkout (`../../docs/...`), which leads nowhere
in a study's own repository. The copy written out links the published documentation instead, at the commit the core
was installed from when it knows one, at its version's tag otherwise, and on the default branch when the core is not
installed as a distribution at all.
"""

import importlib.resources
import json
from importlib import metadata
from pathlib import Path

import expcore
from expcore.provenance.codestamp import is_sha
from expcore.provenance.environment import CORE_DISTRIBUTION

EXAMPLE = "tiny_study"
LEFT_OUT = frozenset({"out", "__pycache__"})
REPOSITORY = "https://github.com/groundnuty/papers-experiments-core"
README = "README.md"
CHECKOUT_DOCS = "](../../docs/"  # how the README links a page of the core's docs, as a Markdown link target


class ExampleError(RuntimeError):
    """The example cannot be written: it is missing from this install, or the destination is not empty."""


def _installed_commit(dist: metadata.Distribution) -> str | None:
    """The commit the distribution was installed from, from its `direct_url.json`; None when it records none."""
    try:
        data = json.loads(dist.read_text("direct_url.json") or "null")
    except ValueError:
        return None
    vcs = data.get("vcs_info") if isinstance(data, dict) else None
    commit = vcs.get("commit_id") if isinstance(vcs, dict) else None
    return commit if is_sha(commit) else None


def docs_url() -> str:
    """The published documentation of this install of the core, ending in a slash: at the commit it was installed
    from, else at its version's tag, else (no distribution installed) on the repository's default branch."""
    try:
        dist = metadata.distribution(CORE_DISTRIBUTION)
    except metadata.PackageNotFoundError:
        return f"{REPOSITORY}/blob/main/docs/"
    ref = _installed_commit(dist) or f"v{dist.version.split('+')[0]}"  # a local label is no tag
    return f"{REPOSITORY}/blob/{ref}/docs/"


def readme_text(text: str) -> str:
    """The README as written out: every link into the checkout's docs points at the published docs instead."""
    return text.replace(CHECKOUT_DOCS, f"]({docs_url()}")


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
