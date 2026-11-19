"""A Compose federation's data shown to be its declared manifests: `federation.py verify-data` (and `composesite.py
verify-data`, which a Compose site's prepare runs after the study's ingest).

Every space the root's rendered `platform.toml` declares data for (`data_manifest`, in `[[spaces]]` or the one-space
form) is read back through its owner, every directory listed and every file's bytes hashed, by the HPC driver's own
reader (`deploy/hpc-federation/hpcdata.content_reader`, built on this directory's bootstrap) and compared with its
manifest (`hpcdata.differences`: counts, never a path). Only when every such space is exactly its manifest is
`data-verified.json` written in the root: for each space its id (the one the platform file names, which must be the one
`up` recorded in `facts.json`), its manifest's core digest (`manifest_sha256`: `Manifest.digest()`, the SHA-256 of its
canonical JSON, what the campaign fingerprint holds and the study's ingest plans) and the SHA-256 of the manifest file's
exact bytes (`manifest_file_sha256`), both of one read. The marker holds only while both are the file's now. Any
difference, an id that disagrees, a manifest that cannot be read, refuses, naming the space, and removes an earlier
marker first: a re-ingest that went wrong never keeps the verdict of the one before. A Compose site's member reads
verified only by that marker (`composesite.data_verified`)."""

import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import tomllib
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any

MARKER, FORMAT = "data-verified.json", "expcore-compose-data-verified/2"  # a /1 marker reads unverified
LOCK = ".data-verified.lock"  # one check, or one invalidation, of a root's data at a time
PLATFORM, FACTS = "platform.toml", "facts.json"
HPC_DIR = Path(__file__).resolve().parents[1] / "hpc-federation"


def declared(root: Path) -> dict[str, tuple[Path, Any]] | None:
    """Every space of the root's `platform.toml` that declares its data, by name: (its manifest's path, its id). None
    when the file cannot be read, or names a manifest by a relative path: nothing is verified then. A relative path
    resolves against the root here and against the study beside the platform file's copy where a run loads it, so the
    two could be two manifests: a template names it absolute (`@study_artifacts@/...` or `@hpc_root@/...`)."""
    try:
        data = tomllib.loads((root / PLATFORM).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    entries = data.get("spaces") if isinstance(data.get("spaces"), list) else [data.get("space")]
    found = {}
    for entry in entries:
        if isinstance(entry, dict) and entry.get("data_manifest"):
            path = Path(str(entry["data_manifest"]))
            if not path.is_absolute():
                return None
            found[str(entry.get("name"))] = (path, entry.get("id"))
    return found


def hpcdata() -> ModuleType:
    """`hpcdata.py` by its path (it imports this directory's modules, already importable here, and the core)."""
    loaded = sys.modules.get("hpcdata")
    if loaded is not None:
        return loaded
    spec = importlib.util.spec_from_file_location("hpcdata", HPC_DIR / "hpcdata.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def locked(root: Path):
    """The root's data held by one check or invalidation at a time (an exclusive lock on `.data-verified.lock`)."""
    descriptor = os.open(root / LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def invalidate(root: Path) -> None:
    """The root's marker removed, under the lock: before anything that changes its data (`reset --clean`) or checks
    it again, so a verdict never outlives the data it was about."""
    with locked(root):
        (root / MARKER).unlink(missing_ok=True)


def read_once(path: Path, load: Callable[[Path], Any]) -> tuple[Any, str, str]:
    """The manifest at `path`, its core digest (`Manifest.digest()`) and the SHA-256 of the file's exact bytes, all of
    one read: parsed from a private copy of those bytes, so a file replaced meanwhile is never checked as one manifest
    and published as another."""
    raw = path.read_bytes()
    with tempfile.TemporaryDirectory(prefix=".manifest-") as scratch:
        copy = Path(scratch) / path.name
        copy.write_bytes(raw)
        manifest = load(copy)
        return manifest, manifest.digest(), hashlib.sha256(raw).hexdigest()


def digests(path: Path) -> tuple[str, str]:
    """(`manifest_sha256`, `manifest_file_sha256`) of the manifest file as it is now: its core digest and its bytes'."""
    from expcore_onedata.importmanifest import load_manifest

    _, digest, file_digest = read_once(path, load_manifest)
    return digest, file_digest


def write_marker(root: Path, spaces: dict[str, dict[str, str]]) -> None:
    """The marker written whole: a draft beside it, mode 0600, renamed over it."""
    descriptor, draft = tempfile.mkstemp(prefix=f".{MARKER}.", dir=root)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"format": FORMAT, "spaces": spaces}, sort_keys=True, indent=2) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(draft, root / MARKER)


def verify_data(layout: Any, read: Callable[[str], Any] | None = None, *, held: bool = False) -> str:
    """Verify every declared space of the federation at `layout` (the module's docstring); what was done. `read` gives
    a space's tree (`hpcdata.Tree`), the owner's REST reader by default; a test's stand-in otherwise. RuntimeError for
    a refusal, the marker removed first."""
    root = layout.root
    if held:  # the caller holds the root's lock, from its own invalidation on (`locked`)
        (root / MARKER).unlink(missing_ok=True)
        return _verify(layout, root, read)
    with locked(root):
        (root / MARKER).unlink(missing_ok=True)  # an earlier verdict never outlives a new check
        return _verify(layout, root, read)


def _verify(layout: Any, root: Path, read: Callable[[str], Any] | None) -> str:
    spaces = declared(root)
    if spaces is None:
        raise RuntimeError("platform.toml cannot be read, or names a data_manifest by a relative path: not verified")
    if not spaces:
        return "data: the platform file declares no space's data; nothing to verify (no marker is needed)"
    data = hpcdata()
    try:
        facts = json.loads((root / FACTS).read_text(encoding="utf-8"))["spaces"]
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError("facts.json cannot be read: no data is verified") from None
    for name, (_, space_id) in spaces.items():
        if not isinstance(facts.get(name), dict) or facts[name].get("id") != space_id:
            raise RuntimeError(f"{name}: the platform file names another space id than up recorded: not verified")
    if read is None:
        from fedboot import Bootstrap
        from fedrest import Rest

        rest = Rest(layout.ca_pem)
        try:
            boot = Bootstrap(rest, layout)
            boot.ids.spaces.update({name: facts[name]["id"] for name in spaces})
            return _check(root, spaces, data, data.content_reader(boot))
        finally:
            rest.close()
    return _check(root, spaces, data, read)


def _check(root: Path, spaces: dict[str, tuple[Path, Any]], data: ModuleType, read: Callable[[str], Any]) -> str:
    verdict = {}
    for name, (path, space_id) in spaces.items():
        try:
            manifest, digest, file_digest = read_once(path, data.load_manifest)  # one read: checked as published
        except (OSError, ValueError, RuntimeError) as error:  # the core's ConfigError is a RuntimeError
            raise RuntimeError(f"{name}: its manifest cannot be read ({type(error).__name__}): not verified") from None
        found = data.differences(read(name), manifest)
        if found:
            raise RuntimeError(f"{name} does not hold the data its manifest declares ({found}): not verified")
        verdict[name] = {"space_id": space_id, "manifest_sha256": digest, "manifest_file_sha256": file_digest}
    write_marker(root, verdict)
    return f"data: {len(verdict)} space(s) hold exactly their manifests; {MARKER} written"
