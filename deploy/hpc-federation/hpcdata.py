"""The data identity of an HPC federation's spaces (C06, O63, O57): the manifest each space's data is declared by, which
`up` renders into `platform.toml` as `data_manifest`, and the check that each space holds exactly that before the
federation is reported ready, or, for a space the study ingests into, after the ingest.

A space is the same experimental condition across federations when its declared identity and its data identity match,
so a campaign's shards on two federations merge only when each space declares its data, and the data a federation holds
must be shown to be the declared data, never taken on a claim:

- **A shared imported space** (O59) is declared by the manifest the layout already names (`import.manifest_env`), and
  `fedimport.verify_imports` shows the space holds exactly it.
- **A baseline space** holds what `up` writes into it (`fedlayout.baseline_plan`, deterministic: one layout's
  federations write the same bytes). Its manifest is made from that plan and written into the root
  (`data-manifests/<space>.json`), and `check` reads the space back through its owner (every directory, every file's
  size and bytes) and refuses one that is not exactly the manifest, naming the space and how many files differ, never a
  path.
- **An ingested space** is none of those: its data comes from the study's own ingest, after `up` (`baseline = false`,
  no import, and its `@data_manifest.<space>@` named by the template: `ingested`). Its baseline is empty, so a manifest
  made from that would fingerprint two federations that ingested different bytes alike. It declares its manifest
  instead (`data_manifest` on the space, a path under `@study_artifacts@` or absolute, in the core's import-manifest
  format, `expcore_onedata.importmanifest`: `Manifest.digest()` is what `PlatformConfig.fingerprint()` holds), and `up`
  renders that path. A space that declares none is held to the empty manifest. `up` checks neither (the space holds
  nothing yet): `verify_ingested` (`hpcfed.py verify-data`) reads each back through its owner once the ingest is done,
  and `shards.py plan` refuses a federation whose ingested spaces have no marker of that.
"""

import hashlib
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Set, Tuple

import tomllib
from fedlayout import BaselineFile, Layout, baseline_plan
from fedspec import STUDY_ARTIFACTS, Space

from expcore.config.loading import ConfigError
from expcore_onedata.importmanifest import Manifest, ManifestFile, load_manifest, write_manifest

MANIFESTS = "data-manifests"  # in the root: each non-imported space's manifest
WORKERS = 16
Tree = Tuple[Dict[str, Tuple[int, str]], Set[str]]  # every file's (size, SHA-256) by path, and every directory
STUDY_KEY = "study_artifacts"  # the placeholder's name in the values a template is rendered with (hpcpins)
NAMED = re.compile(r"@data_manifest\.([a-z0-9][a-z0-9_-]*)@")  # a template's reference to a space's manifest
EMPTY = Manifest((), ())  # an ingested space that declares nothing is held to this


def baseline_manifest(files: Sequence[BaselineFile]) -> Manifest:
    """The manifest of a space's baseline: each file with its size and the SHA-256 of its bytes, each directory."""
    entries = sorted(
        (ManifestFile(f.path, f.size, hashlib.sha256(f.content()).hexdigest()) for f in files), key=lambda e: e.path
    )
    directories = {str(parent) for f in files for parent in PurePosixPath(f.path).parents if str(parent) != "."}
    return Manifest(tuple(entries), tuple(sorted(directories)))


def ingested(layout: Layout) -> Dict[str, Optional[str]]:
    """The spaces the study ingests data into, by the manifest each declares (None: it declares none): those that are
    neither baseline spaces nor imports and whose `@data_manifest.<space>@` the platform template names, or that
    declare a manifest. A probe space is `baseline = false` and nothing names its manifest, so it is not one."""
    template = layout.spec.platform_template
    named = set(NAMED.findall(template.read_text(encoding="utf-8"))) if template is not None else set()
    return {
        space.name: space.data_manifest
        for space in layout.spec.spaces
        if not space.baseline and space.imported is None and (space.data_manifest or space.name in named)
    }


def study_values(study_artifacts: str) -> Dict[str, str]:
    """The values a declared manifest's path is rendered with, from the site's `HPC_STUDY_ARTIFACTS` (empty: none)."""
    return {STUDY_KEY: study_artifacts} if study_artifacts else {}


def manifest_path(space: Space, values: Mapping[str, str]) -> Path:
    """The path a space's declared `data_manifest` names, `@study_artifacts@` filled from the site's directory. The
    site's value is checked as a path (`hpcsettings`) and the layout's own text by `fedspec`, so the result is
    absolute, of plain names, and holds no `..`; a declaration that needs the directory the site does not set is
    refused, naming the variable and the space."""
    text = space.data_manifest or ""
    if text.startswith(STUDY_ARTIFACTS):
        base = values.get(STUDY_KEY, "")
        if not base:
            raise RuntimeError(f"HPC_STUDY_ARTIFACTS is not set: {space.name}'s data_manifest names {STUDY_ARTIFACTS}")
        text = base + text[len(STUDY_ARTIFACTS) :]
    if not os.path.isabs(text) or ".." in text.split("/"):
        raise RuntimeError(f"{space.name}: data_manifest is no absolute path of plain names")
    return Path(os.path.normpath(text))


def ingested_manifests(layout: Layout, values: Mapping[str, str]) -> Dict[str, Manifest]:
    """Each ingested space's manifest: the declared file, loaded and checked (a mistake raises RuntimeError naming the
    space and the key, never the path), or the empty manifest of a space that declares none."""
    found: Dict[str, Manifest] = {}
    for name, declared in ingested(layout).items():
        if declared is None:
            found[name] = EMPTY
            continue
        path = manifest_path(layout.spec.space(name), values)
        try:
            found[name] = load_manifest(path)
        except ConfigError as error:  # the loader names the file first: its problem is kept, the path never shown
            said, prefix = str(error), f"{path}: manifest: "
            problem = "unreadable"
            if said.startswith(prefix):
                problem = said[len(prefix) :].replace(str(path), "<data_manifest>")
            raise RuntimeError(f"{name}: data_manifest is no manifest of a space's data ({problem})") from None
    return found


def ingested_record(layout: Layout, values: Mapping[str, str]) -> Dict[str, Dict[str, Any]]:
    """What the state file records of each ingested space at `up` (`data.ingested`): the digest of the manifest it is
    to be verified against, whether the layout declares it, and no verdict yet."""
    declared = ingested(layout)
    manifests = ingested_manifests(layout, values)
    return {
        name: {"manifest": manifests[name].digest(), "declared": declared[name] is not None, "verified": None}
        for name in declared
    }


def data_manifests(
    layout: Layout, environ: Mapping[str, str], values: Optional[Mapping[str, str]] = None
) -> Dict[str, Path]:
    """Each space's manifest: a shared import's, the layout's own file (its variable); an ingested space's declared
    file, where the study keeps it (never copied); every other space's, written into the root from its baseline.
    `values` are the placeholders' values, `@study_artifacts@` among them."""
    plan, imported = baseline_plan(layout.spec), {s.name for s in layout.shared_imports()}
    study = ingested(layout)
    directory = layout.root / MANIFESTS
    directory.mkdir(mode=0o700, exist_ok=True)
    found: Dict[str, Path] = {}
    for space in layout.spec.spaces:
        if space.name in imported:
            found[space.name] = Path(layout.import_manifest(space.name, environ))
        elif study.get(space.name):
            found[space.name] = manifest_path(space, values or {})
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


def check_rendered(layout: Layout, manifests: Mapping[str, Path]) -> None:
    """Refuse a rendered platform.toml whose entry for a space names another `data_manifest` than the one `up` rendered
    for it (`manifests`): a template that writes a path of its own would leave the data the campaign's fingerprint holds
    unchecked, since only the manifests `up` renders are read back or verified. Nor may an entry declare one for a space
    that is neither a baseline space nor an import unless the template names that space's `@data_manifest.<space>@`
    (an ingested space): written as a path to the root's own file, it would pass the first rule and fingerprint the
    empty baseline of a space the study fills. RuntimeError naming the space."""
    data = tomllib.loads(layout.platform.read_text(encoding="utf-8"))
    entries = data.get("spaces") if isinstance(data.get("spaces"), list) else [data.get("space")]
    waiting = ingested(layout)
    for entry in entries:
        if not (isinstance(entry, Mapping) and entry.get("data_manifest") and entry.get("name") in manifests):
            continue
        space = layout.spec.space(entry["name"])
        if not space.baseline and space.imported is None and space.name not in waiting:
            raise RuntimeError(
                f"{space.name}: its platform entry declares a data_manifest the template does not take from "
                "@data_manifest.<space>@: the space's data would go unverified"
            )
        named = os.path.normpath(os.path.join(layout.root, str(entry["data_manifest"])))
        if named != os.path.normpath(str(manifests[entry["name"]])):
            raise RuntimeError(
                f"{entry['name']}: the platform template names a data_manifest of its own, not the one up renders "
                "(@data_manifest.<space>@, from the layout's data_manifest): its data would go unchecked"
            )


def check(layout: Layout, manifests: Mapping[str, Path], read: Callable[[str], Tree]) -> str:
    """Refuse a federation any of whose spaces that are neither shared imports nor ingested does not hold exactly its
    manifest (`read(space)` gives its tree, read back through its owner): RuntimeError naming the space, never a path.
    What up prints. An ingested space holds nothing yet: `verify` checks it after the ingest."""
    imported = {s.name for s in layout.shared_imports()}  # verify_imports showed these
    waiting = ingested(layout)
    checked = 0
    for space in layout.spec.spaces:
        if space.name in imported or space.name in waiting:
            continue
        found = differences(read(space.name), load_manifest(manifests[space.name]))
        if found:
            raise RuntimeError(f"{space.name} does not hold the data its manifest declares ({found}): not ready")
        checked += 1
    said = f"data: every space that is not ingested holds the data its manifest declares ({checked} space(s))"
    if waiting:
        names = ", ".join(waiting)
        said += f"; {len(waiting)} ingested space(s) await(s) `hpcfed.py verify-data` after the ingest: {names}"
    return said


def verify(manifests: Mapping[str, Manifest], read: Callable[[str], Tree]) -> Dict[str, str]:
    """What makes each ingested space other than its manifest (`read(space)` gives its tree, read back through its
    owner): its differences in counts alone, empty for a space that is exactly it. Never a path."""
    return {space: differences(read(space), manifest) for space, manifest in manifests.items()}


def failure(space: str, found: str, declared: bool) -> str:
    """The refusal of one ingested space whose data is not its manifest: the space and the counts, and, for a space that
    declares no manifest, where to declare it."""
    said = f"{space} does not hold the data its manifest declares ({found})"
    if not declared:
        where = f"spaces.{space}.data_manifest"
        said += f"; it declares no data_manifest, so its manifest is the empty one: declare it ({where})"
    return said


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
