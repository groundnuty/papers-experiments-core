"""C06, O63: an HPC federation's spaces carry their data identity. `up` writes each non-imported space's manifest from
its baseline into the root (or names a shared import's own), renders it as `data_manifest`, and reads every space back
before the federation is ready: one whose data is not its manifest fails `up`, naming the space and never a path.
`shards.py plan` refuses a campaign on several federations whose platform files leave a space's data undeclared, and
a federation whose `up` has not finished. The bootstrap, the link check's transfers and the space reads stand in for a
live federation; nothing reaches one."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from fingerprint_support import standin_python
from hpc_fed_support import LAYOUT, data_stand_in, hpc_on_path, pin_values, write_address_map
from hpc_rehearsal_support import BootStandIn, link_check_stand_in

from expcore_onedata.config import load_platform
from expcore_onedata.importmanifest import load_manifest

SCRIPT = LAYOUT.parents[1] / "shards.py"
SPACES = ["alpha-space", "beta-space", "gamma-space", "delta-space"]


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import fedlinks
        import hpcdata

        modules.fedlinks, modules.data = fedlinks, hpcdata
        yield modules


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


def core(hpc, root: Path, label: str, ca: bytes, data=None) -> str:
    """The core's phase of the pilot's layout over a root of its own, the bootstrap's ids its federation's own."""
    import fedlayout
    import fedspec

    (root / "secrets" / "users").mkdir(parents=True)
    (root / "ca").mkdir()
    (root / "ca" / "federation-ca.pem").write_bytes(ca)
    (root / "fault-journal").mkdir()
    (root / "secrets" / "zone.passphrase").write_text("A1b2C3d4E5f6G7h8", encoding="utf-8")
    os.chmod(root / "secrets" / "zone.passphrase", 0o600)
    spec = fedspec.load_spec(LAYOUT)
    hosts = {s.name: f"{s.name}.cluster.example.org" for s in spec.services}
    layout = fedlayout.Layout(root, root.parent, hpc.boot.hpc_spec(spec, hosts))
    write_address_map(layout)  # `up` leaves one in a root of declared names
    values = {**hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, root), **pin_values(root.parent / "pins")}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
        return hpc.boot.core_phase(
            layout, (5001, 5001), values, environ={},
            rest=lambda ca_file, **kwargs: SimpleNamespace(close=lambda: None), bootstrap=BootStandIn(label),
            steps=lambda boot: None, content=lambda boot: SimpleNamespace(reset=lambda timeout: None),
            data=data or data_stand_in(),
        )  # fmt: skip


def test_o63_up_renders_each_spaces_manifest_and_two_federations_with_the_same_data_are_one_platform(hpc, tmp_path, ca):
    """C06, O63: each data space's data_manifest is a file up wrote into the root from the space's baseline, absolute;
    the federation's data is read back and found to be it; two federations whose ids differ give one platform
    fingerprint, holding each space's data identity."""
    printed = core(hpc, tmp_path / "a" / "root", "fed-a", ca)
    core(hpc, tmp_path / "b" / "root", "fed-b", ca)
    assert "data: every space that is not ingested holds the data its manifest declares" in printed
    one, two = (load_platform(tmp_path / name / "root" / "platform.toml") for name in ("a", "b"))
    assert [entry.space.id for entry in one.spaces] != [entry.space.id for entry in two.spaces]
    assert one.fingerprint() == two.fingerprint()
    plan = hpc.data.baseline_manifest
    import fedspec
    from fedlayout import baseline_plan

    baseline = baseline_plan(fedspec.load_spec(LAYOUT))
    for entry in one.spaces:
        path = tmp_path / "a" / "root" / "data-manifests" / f"{entry.space.name}.json"
        assert load_manifest(path).digest() == entry.data == plan(baseline[entry.space.name]).digest()
    assert [entry.space.name for entry in one.spaces] == SPACES
    assert not load_manifest(tmp_path / "a" / "root" / "data-manifests" / "delta-space.json").files  # baseline = false


def test_o63_a_federation_whose_data_differs_by_one_file_from_its_manifest_fails_up_naming_the_space(hpc, tmp_path, ca):
    """O63: the manifest is checked, never taken on a claim: one file of the study space with one byte other, and up
    fails before the link check (no record written), naming the space and never a path."""
    root = tmp_path / "root"
    with pytest.raises(RuntimeError, match=r"^alpha-space does not hold the data its manifest declares") as failed:
        core(hpc, root, "fed-a", ca, data=data_stand_in(differ="alpha-space"))
    assert "1 file(s) of another size or other bytes" in str(failed.value)
    assert str(tmp_path) not in str(failed.value)
    assert not (root / "link-check.json").exists()


def test_o63_the_differences_are_counts_of_files_missing_extra_other_and_directories(hpc):
    manifest = hpc.data.baseline_manifest(
        [SimpleNamespace(path="d/a.txt", size=1, content=lambda: b"a"), SimpleNamespace(path="b.txt", size=1,
                                                                                       content=lambda: b"b")]
    )  # fmt: skip
    files = {f.path: (f.size, f.sha256) for f in manifest.files}
    assert hpc.data.differences((files, {"d"}), manifest) == ""
    assert hpc.data.differences(({"b.txt": files["b.txt"], "x": (1, "0")}, set()), manifest) == (
        "1 file(s) missing, 1 file(s) not in it, 1 director(ies) not as listed"
    )


# -- shards.py plan ------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def shards():
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("hpc_shards_data", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_data"] = module
    try:
        spec.loader.exec_module(module)
        # The manifests here name an interpreter that does not exist; the core it imports is asked in
        # test_hpc_shards_core_identity.py (C06), with real interpreters.
        module.identify_core = lambda plan: {"known": True, "stale": False, "why": None}
        yield module
    finally:
        sys.modules.pop("hpc_shards_data", None)


def manifest(tmp_path, platforms, *, phase="linked", same_root=False) -> Path:
    """A shard manifest of two shards on two federations (or one), each root holding a finished state and the given
    platform.toml text."""
    roots = [tmp_path / "fed" / ("a" if same_root else name) for name in ("a", "b")]
    for root, text in zip(roots, platforms, strict=True):
        root.mkdir(parents=True, exist_ok=True)
        state = {"format": "expcore-hpc-federation/1", "allocation": {"job": "101", "name": "hpcop-x"},
                 "sandbox": {"placement": "het", "node": "node-s1", "het_group": 1}, "phase": phase,
                 "data": {"ingested": {}}}  # fmt: skip
        (root / "hpc-federation.json").write_text(json.dumps(state))
        (root / "platform.toml").write_text(text)
    (tmp_path / "study").mkdir(exist_ok=True)  # the campaign fingerprint is taken on a copy of it (O63)
    lines = [f'{k} = "{tmp_path / k}"' for k in ("study", "core_src", "results", "tmpfs")]
    lines += [f'python = "{standin_python(tmp_path / "venv")}"', 'step_path = "/usr/bin"', "cpus = 6", 'memory = "18g"']
    for label, root in zip(("fed-a", "fed-b"), roots, strict=True):
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{root}"']
    (tmp_path / "shards.toml").write_text("\n".join(lines) + "\n")
    return tmp_path / "shards.toml"


DECLARED = (
    '[[spaces]]\nname = "alpha"\ndata_manifest = "/m/a.json"\n[[spaces]]\nname = "beta"\ndata_manifest = "/m/b.json"\n'
)
UNDECLARED = '[[spaces]]\nname = "alpha"\ndata_manifest = "/m/a.json"\n[[spaces]]\nname = "beta"\n'
ONE_SPACE = '[space]\nname = "alpha"\n'


@pytest.mark.parametrize(
    ("platforms", "said"),
    [
        ((DECLARED, UNDECLARED), "undeclared: fed-b: beta"),
        ((ONE_SPACE, ONE_SPACE), "undeclared: fed-a: alpha; fed-b: alpha"),
    ],
    ids=["one space of several", "the one-space form"],
)
def test_o63_plan_refuses_several_federations_that_leave_a_spaces_data_undeclared(
    shards, tmp_path, capsys, platforms, said
):
    """C06, O63: shards on two federations whose platform files leave a space's data undeclared would never merge (the
    space's digest holds its Onezone id): `plan` refuses before anything runs, naming each shard and space."""
    assert shards.main(["plan", "--manifest", str(manifest(tmp_path, platforms))]) == 2
    assert said in capsys.readouterr().err


@pytest.mark.parametrize("platforms", [(DECLARED, DECLARED), (ONE_SPACE + 'data_manifest = "/m/a.json"\n',) * 2])
def test_o63_plan_takes_several_federations_that_declare_every_spaces_data(shards, tmp_path, capsys, platforms):
    assert shards.main(["plan", "--manifest", str(manifest(tmp_path, platforms))]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_o63_plan_on_one_federation_needs_no_declared_data(shards, tmp_path, capsys):
    """O63: on one federation every shard holds the same ids, so nothing is asked of it: as before."""
    assert shards.main(["plan", "--manifest", str(manifest(tmp_path, (ONE_SPACE, ONE_SPACE), same_root=True))]) == 0


def test_o63_plan_refuses_a_federation_whose_up_did_not_finish(shards, tmp_path, capsys):
    """O63: an up that failed its data check (or its link check) left the federation not ready: never planned."""
    assert shards.main(["plan", "--manifest", str(manifest(tmp_path, (DECLARED, DECLARED), phase="bootstrapped"))]) == 2
    assert "its federation's up has not finished (phase bootstrapped)" in capsys.readouterr().err


def test_o63_a_shared_imports_data_manifest_is_the_layouts_own_and_every_other_space_gets_its_baseline(hpc, tmp_path):
    """O63: up reuses the manifest a shared import already declares (its variable, M3's `manifest_env`), which
    verify_imports checks it against, and writes every other space's from its baseline into the root."""
    import fedlayout
    import fedspec
    from hpc_import_support import IMPORT_LAYOUT, make_environ

    environ = make_environ(tmp_path)
    root = tmp_path / "fed" / "root"
    root.mkdir(parents=True)
    layout = fedlayout.Layout(root, root.parent, fedspec.load_spec(IMPORT_LAYOUT))
    found = hpc.data.data_manifests(layout, environ)
    shared = {space.name for space in layout.shared_imports()}
    assert shared
    for space in layout.spec.spaces:
        if space.name in shared:
            assert found[space.name] == layout.import_manifest(space.name, environ)
        else:
            assert found[space.name] == root / "data-manifests" / f"{space.name}.json"
    assert hpc.data.values(found) == {f"data_manifest.{name}": str(path) for name, path in found.items()}
