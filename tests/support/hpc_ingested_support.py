"""What the tests of an HPC federation's ingested spaces share (C06, O57; deploy/hpc-federation/hpcdata.py): a manifest
and the tree a space holds once the study has ingested it, the pilot's layout with its ingested space declaring its
manifest, and a federation brought up over the fake Slurm of hpc_fed_support with the core's phase a stand-in for a
live zone. Nothing reaches a cluster or a federation."""

import functools
import hashlib
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

import pytest
from hpc_fed_support import LAYOUT, PILOT_TEMPLATE, SITE, FakeSlurm, agent_site, data_stand_in
from hpc_rehearsal_support import BootStandIn, link_check_stand_in

from expcore_onedata.importmanifest import Manifest, ManifestFile, write_manifest

KEY = "HPC_STUDY_ARTIFACTS"
DECLARED = "@study_artifacts@/manifests/delta.json"  # what the pilot's ingested space declares, in these tests


# -- what the study ingests: a manifest, and the tree a federation holds once it has ingested it -----------------------


def manifest_of(files: dict[str, bytes]) -> Manifest:
    entries = tuple(ManifestFile(p, len(b), hashlib.sha256(b).hexdigest()) for p, b in sorted(files.items()))
    directories = sorted({str(d) for p in files for d in PurePosixPath(p).parents if str(d) != "."})
    return Manifest(entries, tuple(directories))


def tree_of(manifest: Manifest) -> tuple[dict, set]:
    """What a space whose owner holds exactly this manifest's data lists and reads back."""
    return {f.path: (f.size, f.sha256) for f in manifest.files}, set(manifest.directories)


ONE = manifest_of({"records/day-01.csv": b"alpha", "records/day-02.csv": b"beta", "notes.txt": b"gamma"})
OTHER = manifest_of({"records/day-01.csv": b"alpha", "records/day-02.csv": b"BETA", "notes.txt": b"gamma"})  # one file


def artifacts(directory: Path, manifest: Manifest | None = ONE) -> Path:
    """The study's artifacts directory: the manifest of its one ingested space, in the core's format."""
    (directory / "manifests").mkdir(parents=True)
    if manifest is not None:
        write_manifest(manifest, directory / "manifests" / "delta.json")
    return directory


def study_layout(folder: Path, declared: str | None = DECLARED, template_extra: str = "") -> Path:
    """The pilot's layout and template, delta-space (the ingested space) declaring its data manifest as `declared`."""
    folder.mkdir(parents=True, exist_ok=True)
    text = LAYOUT.read_text(encoding="utf-8")
    if declared is not None:
        assert text.count("[spaces.delta-space]") == 1
        text = text.replace("[spaces.delta-space]", f'[spaces.delta-space]\ndata_manifest = "{declared}"', 1)
    (folder / LAYOUT.name).write_text(text, encoding="utf-8")
    template = PILOT_TEMPLATE.read_text(encoding="utf-8") + template_extra
    (folder / PILOT_TEMPLATE.name).write_text(template, encoding="utf-8")
    return folder / LAYOUT.name


def held(data: dict[str, Manifest]) -> Any:
    """`hpcboot.core_phase`'s `data` (and verify's): each baseline space as up wrote it, each space of `data` holding
    that manifest's data, as the study's ingest left it."""
    base = data_stand_in()

    def reader(boot: Any) -> Any:
        baseline = base(boot)
        return lambda space: tree_of(data[space]) if space in data else baseline(space)

    return reader


# -- a federation brought up over the fake cluster ---------------------------------------------------------------------


def site_env(base: Path, artifacts_dir: Path | None) -> dict:
    """site.env as `up` reads it, over directories made under `base`; `artifacts_dir` is HPC_STUDY_ARTIFACTS."""
    images, logs = base / "images", base / "logs"
    for folder in (images, logs, base / "fed"):
        folder.mkdir(parents=True, exist_ok=True)
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    env = {**SITE, **agent_site(base / "agent"), "SH": str(images), "L": str(logs)}
    if artifacts_dir is not None:
        env[KEY] = str(artifacts_dir)
    return env


def driver_over(hpc: Any, base: Path, layout: Path, label: str = "fed-a", ca: bytes | None = None) -> SimpleNamespace:
    """The driver of `layout` over the fake cluster, its core's phase a stub that records what it is handed (a real one
    with a CA): the driver, its fake Slurm, its root and the values the core's phase was given."""
    fake = FakeSlurm(**({"ca_pem": ca.decode("ascii")} if ca else {}))
    (base / "fed").mkdir(parents=True, exist_ok=True)
    root = base / "fed" / "root"
    driver = hpc.cli.make_driver(layout, str(root), runner=fake)
    driver.sleep = driver.slurm.sleep = lambda s: None
    driver.poll_s = driver.slurm.poll_s = 0.0
    driver.relay_available = lambda: False
    given: list = []
    driver.core = lambda lay, owner, values, environ=None: given.append(dict(values)) or "core: linked"
    return SimpleNamespace(driver=driver, fake=fake, root=root, given=given, label=label)


def _stand_in_rest(ca_file: Path) -> SimpleNamespace:
    return SimpleNamespace(close=lambda: None)


def real_core(hpc: Any, fed: SimpleNamespace, data: Any) -> None:
    """The core's phase as `hpcfed.py up` runs it, over a bootstrap that answers what a live zone would, `data` what
    each space holds (`hpcboot.core_phase`); `verify-data` reads through the same stand-ins."""
    fed.driver.core = functools.partial(
        hpc.boot.core_phase,
        rest=_stand_in_rest,
        bootstrap=BootStandIn(fed.label),
        steps=lambda b: None,
        content=lambda b: SimpleNamespace(reset=lambda timeout: None),
        data=data,
    )
    verify_through(hpc, fed, data)


def verify_through(hpc: Any, fed: SimpleNamespace, data: Any) -> None:
    """`verify-data` reads each space through the factory `data` (`hpcboot.verify_ingested`'s)."""
    fed.driver.verify = functools.partial(
        hpc.boot.verify_ingested, rest=_stand_in_rest, bootstrap=BootStandIn(fed.label), data=data
    )


def verify_stand_in(hpc: Any, fed: SimpleNamespace, spaces: dict[str, Manifest]) -> None:
    """The ingest has run: `verify-data` now reads each space of `spaces` as holding that manifest's data."""
    verify_through(hpc, fed, held(spaces))


def up(hpc: Any, base: Path, layout: Path, ca: bytes, *, artifacts_dir: Path | None, label: str = "fed-a",
       data: dict[str, Manifest] | None = None) -> SimpleNamespace:  # fmt: skip
    """A federation brought up by `up` over the real core's phase: its platform.toml rendered, its link check passed."""
    fed = driver_over(hpc, base, layout, label, ca)
    fed.site = site_env(base, artifacts_dir)
    real_core(hpc, fed, held(data or {}))
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
        fed.printed = fed.driver.up(hpc.settings.site_from_environment(fed.site))
    return fed


def ingested_state(fed: SimpleNamespace) -> dict:
    return fed.driver.store.read()["data"]["ingested"]
