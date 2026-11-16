"""The dataset directories of the imported spaces: the source of each is checked, and a writable import's source is
copied into the root.

An imported space is held by its owner on a POSIX storage whose directory is the dataset as it lies on disk, and
Onedata finds the files by a scan. A read-only import mounts the source itself, so up only checks it. A writable import
mounts a copy that up makes once inside the root, in the owner's directory: the platform may then change the copy, the
dataset is never touched, and down removes the copy with the root.

The scan skips a symbolic link without a word (op-worker's CHANGELOG, VFS-9118), so a copy that kept or followed one
would give a space short of files with nothing to say so. Up therefore refuses a source that holds anything but regular
files and directories, with a count and no path, before it creates the copy. What is copied is the bytes alone: the
times and the modes of the source are not kept, files get 0664 and directories 0775, as op-worker's own uploads do
(fslogic_common.hrl), so an import and an upload look alike to the platform.

The copy is made beside its final directory and renamed into place, so a copy that stops midway is never mistaken for a
finished one by the next up, which would otherwise leave the space short of files for good.

`reset --clean` is refused for a layout with an imported space (`require_resettable`): it deletes through the providers.

A tree that other federations import too (`import.shared`, O56) is shown, after up, to be what its manifest says
(`verify_imports`): the core's own check of an imported space, through the rendered platform.toml.
"""

import os
import shutil
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

from fedlayout import Layout

FILE_MODE = 0o664  # an uploaded file's mode in op-worker (fslogic_common.hrl)
DIR_MODE = 0o775  # an uploaded directory's mode
UNFINISHED = ".copying"  # the suffix of a copy that is not done


def tree(source: Path) -> Iterator[tuple[Path, str]]:
    """Every entry below `source`, a directory before what it holds, as its path relative to the source and its kind:
    `dir`, `file`, or `other` for a symbolic link (whatever it points to) and anything that is neither a regular file
    nor a directory. A directory that is a link is not entered."""
    pending = [Path()]
    while pending:
        here = pending.pop()
        with os.scandir(source / here) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
        for entry in entries:
            relative = here / entry.name
            if entry.is_symlink():
                kind = "other"
            elif entry.is_dir(follow_symlinks=False):
                kind = "dir"
                pending.append(relative)
            elif entry.is_file(follow_symlinks=False):
                kind = "file"
            else:
                kind = "other"
            yield relative, kind


def odd_entries(space: str, count: int) -> RuntimeError:
    """The refusal of a source with entries that are neither regular files nor directories: a count, never a path."""
    what = "entry" if count == 1 else "entries"
    return RuntimeError(
        f"{count} {what} of the dataset of {space} cannot be imported: anything but a regular file or a directory (a "
        "symbolic link included) is skipped by Onedata's storage import without a word; nothing was copied"
    )


def check_tree(space: str, source: Path) -> None:
    """Refuse a source that holds anything but regular files and directories, before anything is copied."""
    try:
        odd = sum(1 for _, kind in tree(source) if kind == "other")
    except OSError as error:
        raise RuntimeError(f"the dataset of {space} could not be read ({type(error).__name__})") from None
    if odd:
        raise odd_entries(space, odd)


def pending_copies(layout: Layout, environ: Mapping[str, str]) -> list[tuple[str, Path]]:
    """Check every imported space's source and return the writable imports that still need their copy, as (space,
    source); nothing is created.

    A read-only import is never copied: its source is checked and that is all. A writable import whose copy exists
    is done: its source may have moved on, or gone, and is no longer read."""
    pending = []
    for space in layout.spec.spaces:
        if space.imported is None:
            continue
        if not space.imported.readonly:
            require_no_link(layout, space.name)  # before exists(), which would follow one
            if layout.import_dir(space.name).exists():
                continue
        try:
            source = layout.import_source(space.name, environ)
        except ValueError as error:
            raise RuntimeError(str(error)) from None
        if not space.imported.readonly:
            check_tree(space.name, source)
            pending.append((space.name, source))
    return pending


def require_no_link(layout: Layout, space: str) -> None:
    """Refuse a writable import whose copy, or a directory on the way to it below the root, is a symbolic link: the
    copy, its modes, the removal of an unfinished one and the read-write mount all lie below the root, which down
    empties, and a link would lead them outside it. Nothing is created or followed."""
    target = layout.import_dir(space)
    steps = [p for p in reversed(target.parents) if layout.root in p.parents]
    for step in (*steps, target, target.with_name(target.name + UNFINISHED)):
        if step.is_symlink():
            raise RuntimeError(
                f"the copy of the dataset of {space} would pass through a symbolic link below the root; refused, "
                "nothing was copied"
            )


def require_resettable(layout: Layout) -> None:
    """Refuse `reset --clean` for a layout with an imported space, before anything is touched.

    The reset deletes through the providers' REST interface, and a delete in an imported space unlinks the file from
    the import directory on a writable storage, which no reset can put back, and on a read-only storage leaves the file
    there, so a later scan imports it again under a new id. Down, then up, brings such a layout back to clean."""
    imported = [space.name for space in layout.spec.spaces if space.imported is not None]
    if imported:
        raise RuntimeError(
            f"reset --clean refuses a layout with an imported space ({', '.join(imported)}): a delete in an imported "
            "space unlinks the file from the import directory on a writable storage, and on a read-only one leaves it "
            "there for a later scan to import again under a new id; run down, then up, to bring it back to clean"
        )


def copy_tree(space: str, source: Path, target: Path) -> None:
    """Copy `source` to `target`, which must not exist, with the modes an upload has; a symbolic link or anything else
    that appeared since the check is refused too."""
    target.mkdir()
    target.chmod(DIR_MODE)
    for relative, kind in tree(source):
        if kind == "other":
            raise odd_entries(space, 1)
        if kind == "dir":
            (target / relative).mkdir()
            (target / relative).chmod(DIR_MODE)
        else:
            shutil.copyfile(source / relative, target / relative)  # the bytes alone: no times, no modes
            (target / relative).chmod(FILE_MODE)


def copy_imports(layout: Layout, copies: list[tuple[str, Path]]) -> None:
    """Make each copy beside its directory and rename it into place. A copy that stops leaves no directory of its
    name, and the next up makes it again from nothing; what was left unfinished is removed first."""
    for space, source in copies:
        require_no_link(layout, space)  # again: nothing is touched through a link, whenever it appeared
        target = layout.import_dir(space)
        unfinished = target.with_name(target.name + UNFINISHED)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.parent.chmod(DIR_MODE)
        if unfinished.exists():
            shutil.rmtree(unfinished)
        try:
            copy_tree(space, source, unfinished)
        except OSError as error:
            raise RuntimeError(
                f"the dataset of {space} could not be copied ({type(error).__name__}, errno {error.errno}); "
                "no copy was kept"
            ) from None
        os.replace(unfinished, target)


def verify_imports(
    layout: Layout,
    environ: Mapping[str, str],
    *,
    timeout_s: float,
    hashes: bool = False,
    http: Any = None,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, dict[str, Any]]:
    """Show, once up has rendered platform.toml, that every shared import (`import.shared`, O56) holds exactly its
    manifest, by the core's own check (`ImportedSpace.provision`): the owner's panel shows the imported storage
    read-only, the scan has completed with no failed file, the listing on every provider that supports the space holds
    exactly the manifest's paths, kinds and sizes, and every file is whole on the owner by its block map; with `hashes`,
    every file's bytes read back. Return each space's record by its name.

    Before any call: every manifest is checked and loaded (`Layout.load_manifest`, which names a variable and never a
    path), and the rendered platform.toml must
    declare each space as the layout does (`import = { readonly = true, shared = true }`), so a platform file that
    leaves the sharing out, and so would not refuse a writable storage, is refused. A space that fails is a
    RuntimeError naming it; the message holds no path (the core's never does). Nothing is written to a space: the
    check only reads. `http`, `clock` and `sleep` are for a test's fakes."""
    spaces = layout.shared_imports()
    if not spaces:
        return {}
    loaded = {space.name: layout.load_manifest(space.name, environ) for space in spaces}
    from expcore.config.loading import ConfigError
    from expcore_onedata.client import OnezoneClient
    from expcore_onedata.config import load_platform
    from expcore_onedata.provision import Provisioner, resolve_provider_ids
    from expcore_onedata.rangemap import load_placement
    from expcore_onedata.storageimport import ImportedSpace, ImportFailed

    try:
        config = load_platform(layout.platform)
        settings = load_placement(layout.platform, config).settings
    except ConfigError as error:
        said = str(error).replace(str(layout.root), "<root>")  # the root is the host's own place
        raise RuntimeError(f"the import check cannot start: {said}") from None
    entries = {entry.space.name: entry for entry in config.spaces}
    for space in spaces:
        declared = entries[space.name].imported if space.name in entries else None
        if declared is None or not (declared.shared and declared.readonly):
            raise RuntimeError(
                f"the rendered platform.toml does not declare {space.name} as a shared, read-only import "
                "(import = { readonly = true, shared = true }), as the layout does"
            )
    timing: dict[str, Any] = {k: v for k, v in (("clock", clock), ("sleep", sleep)) if v is not None}
    client = OnezoneClient(config, **({"http": http} if http is not None else {}))
    records: dict[str, dict[str, Any]] = {}
    try:
        ids = resolve_provider_ids(config)
        for space in spaces:
            entry = entries[space.name]
            log = layout.root / f"import-check-{space.name}.jsonl"
            provisioner = Provisioner(client, config, settings, provider_ids=ids, log=log, space=entry.label, **timing)
            try:
                records[space.name] = ImportedSpace(provisioner, loaded[space.name]).provision(
                    timeout_s=timeout_s, providers=list(entry.support.providers), hashes=hashes
                )
            except ImportFailed as failed:
                raise RuntimeError(f"the import of {space.name} was not shown to hold its dataset: {failed}") from None
    finally:
        client.close()
    return records
