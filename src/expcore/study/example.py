# Provenance: new
"""The example study, as the installed core carries it: `python -m expcore example DEST` writes it into a directory.

The example lives in the repository at `examples/tiny_study/` and the wheel carries it as `expcore/examples/tiny_study/`
(see `pyproject.toml`), so a study that depends on the core by tag or commit can write it out and run it without
copying a file from a checkout. An editable install has no copy in the package and reads the checkout's directory.
What the example's own runs left behind (`out/`, caches) is never written out.
"""

import importlib.resources
from pathlib import Path

import expcore

EXAMPLE = "tiny_study"
LEFT_OUT = frozenset({"out", "__pycache__"})


class ExampleError(RuntimeError):
    """The example cannot be written: it is missing from this install, or the destination is not empty."""


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
        target.write_bytes(path.read_bytes())
        written.append(target)
    return written
