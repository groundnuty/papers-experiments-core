"""What the tests of a shared imported tree in the HPC federation driver (O59) share: the `pilot-shape-import` layout's
spaces, the eight variables of `site.env` over temporary trees and their manifests, and a layout with one edit."""

from pathlib import Path

from hpc_fed_support import HPC

from expcore_onedata.importmanifest import manifest_of_tree, write_manifest

IMPORT_LAYOUT = HPC / "layouts" / "pilot-shape-import.toml"
IMPORT_TEMPLATE = HPC / "layouts" / "pilot-shape-import.platform.toml.template"
INGEST_LAYOUT = HPC / "layouts" / "pilot-shape.toml"
# the four data spaces: the provider that imports each, and the stem of its two variables
SPACES = {
    "alpha-space": ("larch", "ALPHA"),
    "beta-space": ("rowan", "BETA"),
    "gamma-space": ("alder", "GAMMA"),
    "delta-space": ("hazel", "DELTA"),
}
PROVIDERS = ["aspen", "larch", "rowan", "alder", "hazel"]


def make_environ(tmp_path: Path) -> dict:
    """The eight variables of `site.env`: each space's tree (a directory of one file) and its manifest, outside it."""
    base = tmp_path.resolve() / "shared"
    base.mkdir()
    found = {}
    for _, stem in SPACES.values():
        tree = base / stem.lower()
        tree.mkdir()
        (tree / "day-01.csv").write_bytes(bytes(4096))
        write_manifest(manifest_of_tree(tree), base / f"{stem.lower()}.json")
        found[f"EXPCORE_FED_IMPORT_{stem}"] = str(tree)
        found[f"EXPCORE_FED_IMPORT_{stem}_MANIFEST"] = str(base / f"{stem.lower()}.json")
    return found


def tree_of(environ: dict, space: str) -> str:
    return environ[f"EXPCORE_FED_IMPORT_{SPACES[space][1]}"]


def edited_layout(folder: Path, old: str, new: str) -> Path:
    """The pilot-shape-import layout with one edit, beside a copy of its platform template."""
    folder.mkdir()
    text = IMPORT_LAYOUT.read_text(encoding="utf-8")
    assert old in text
    (folder / IMPORT_LAYOUT.name).write_text(text.replace(old, new, 1), encoding="utf-8")
    (folder / IMPORT_TEMPLATE.name).write_text(IMPORT_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return folder / IMPORT_LAYOUT.name
