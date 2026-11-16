"""The data identity of an HPC federation's spaces (C06, O63): the manifest each space's data is declared by, which `up`
renders into `platform.toml` as `data_manifest`, and the check that each space holds exactly that before the federation
is reported ready.

A space is the same experimental condition across federations when its declared identity and its data identity match,
so a campaign's shards on two federations merge only when each space declares its data, and the data a federation holds
must be shown to be the declared data, never taken on a claim:

- **A shared imported space** (O59) is declared by the manifest the layout already names (`import.manifest_env`), and
  `fedimport.verify_imports` shows the space holds exactly it.
- **Every other space** holds what `up` writes into it, its baseline (`fedlayout.baseline_plan`, deterministic: one
  layout's federations write the same bytes), or nothing (`baseline = false`). Its manifest is made from that plan and
  written into the root (`data-manifests/<space>.json`), and `check` reads the space back through its owner (every
  directory, every file's size and bytes) and refuses one that is not exactly the manifest, naming the space and how
  many files differ, never a path.
"""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Sequence, Set, Tuple

from fedlayout import BaselineFile, Layout, baseline_plan

from expcore_onedata.importmanifest import Manifest, ManifestFile, write_manifest

MANIFESTS = "data-manifests"  # in the root: each non-imported space's manifest
WORKERS = 16
Tree = Tuple[Dict[str, Tuple[int, str]], Set[str]]  # every file's (size, SHA-256) by path, and every directory


def baseline_manifest(files: Sequence[BaselineFile]) -> Manifest:
    """The manifest of a space's baseline: each file with its size and the SHA-256 of its bytes, each directory."""
    entries = sorted(
        (ManifestFile(f.path, f.size, hashlib.sha256(f.content()).hexdigest()) for f in files), key=lambda e: e.path
    )
    directories = {str(parent) for f in files for parent in PurePosixPath(f.path).parents if str(parent) != "."}
    return Manifest(tuple(entries), tuple(sorted(directories)))


def data_manifests(layout: Layout, environ: Mapping[str, str]) -> Dict[str, Path]:
    """Each space's manifest: a shared import's, the layout's own file (its variable); every other space's, written into
    the root from its baseline."""
    plan, imported = baseline_plan(layout.spec), {s.name for s in layout.shared_imports()}
    directory = layout.root / MANIFESTS
    directory.mkdir(mode=0o700, exist_ok=True)
    found: Dict[str, Path] = {}
    for space in layout.spec.spaces:
        if space.name in imported:
            found[space.name] = Path(layout.import_manifest(space.name, environ))
        else:
            found[space.name] = directory / f"{space.name}.json"
            write_manifest(baseline_manifest(plan[space.name]), found[space.name])
    return found


def values(manifests: Mapping[str, Path]) -> Dict[str, str]:
    """The placeholders a platform template names them by: `@data_manifest.<space>@`, an absolute path."""
    return {f"data_manifest.{name}": str(path) for name, path in manifests.items()}


def differences(tree: Tree, manifest: Manifest) -> str:
    """What makes a space's tree other than its manifest, in counts alone; empty when it is exactly the manifest."""
    files, directories = tree
    wanted = {f.path: (f.size, f.sha256) for f in manifest.files}
    missing, extra = len(set(wanted) - set(files)), len(set(files) - set(wanted))
    other = sum(1 for path in set(wanted) & set(files) if files[path] != wanted[path])
    dirs = len(set(manifest.directories) ^ directories)
    said = [f"{n} {what}" for n, what in ((missing, "file(s) missing"), (extra, "file(s) not in it"),
                                           (other, "file(s) of another size or other bytes"),
                                           (dirs, "director(ies) not as listed")) if n]  # fmt: skip
    return ", ".join(said)


def check(layout: Layout, manifests: Mapping[str, Path], read: Callable[[str], Tree]) -> str:
    """Refuse a federation any of whose non-imported spaces does not hold exactly its manifest (`read(space)` gives
    its tree, read back through its owner): RuntimeError naming the space, never a path. What up prints."""
    from expcore_onedata.importmanifest import load_manifest

    imported = {s.name for s in layout.shared_imports()}  # verify_imports showed these
    for space in layout.spec.spaces:
        if space.name in imported:
            continue
        found = differences(read(space.name), load_manifest(manifests[space.name]))
        if found:
            raise RuntimeError(f"{space.name} does not hold the data its manifest declares ({found}): not ready")
    return f"data: every space holds the data its manifest declares ({len(layout.spec.spaces)} space(s))"


def content_reader(boot: Any) -> Callable[[str], Tree]:
    """A space's tree as its owner serves it, over the bootstrap's REST (`fedcontent.Content`): every directory walked
    page by page, every file's bytes read and hashed. A file that cannot be read is read as no bytes."""
    from fedcontent import Content, writer_of

    content = Content(boot)

    def read(space: str) -> Tree:
        service = writer_of(boot.layout.spec, space)
        files: Dict[str, str] = {}
        sizes: Dict[str, int] = {}
        directories: Set[str] = set()
        pending = [("", content.root(service, space))]
        while pending:
            prefix, directory = pending.pop()
            for entry in content.children(service, directory):
                path = f"{prefix}{entry['name']}"
                if entry["type"] == "DIR":
                    directories.add(path)
                    pending.append((f"{path}/", entry["file_id"]))
                else:
                    files[path], sizes[path] = entry["file_id"], int(entry.get("size") or 0)

        def digest(path: str) -> Tuple[str, Tuple[int, str]]:
            data = content.read_back(service, files[path]) or b""
            return path, (sizes[path], hashlib.sha256(data).hexdigest())

        with ThreadPoolExecutor(WORKERS) as pool:
            return dict(pool.map(digest, sorted(files))), directories

    return read
