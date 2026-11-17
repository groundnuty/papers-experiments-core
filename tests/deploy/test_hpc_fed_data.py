"""C06, O63: an HPC federation's spaces carry their data identity. `up` writes each non-imported space's manifest from
its baseline into the root (or names a shared import's own), renders it as `data_manifest`, and reads every space back
before the federation is ready: one whose data is not its manifest fails `up`, naming the space and never a path.
`shards.py plan` refuses a campaign on several federations whose platform files leave a space's data undeclared, and
a federation whose `up` has not finished. The bootstrap, the link check's transfers and the space reads stand in for a
live federation; nothing reaches one."""

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fingerprint_support import standin_python
from hpc_fed_support import LAYOUT, data_stand_in, hpc_on_path, pin_values
from hpc_rehearsal_support import BootStandIn, link_check_stand_in

from expcore_onedata.config import load_platform
from expcore_onedata.importmanifest import load_manifest
from expcore_onedata.linkprobe import probe_name, retired_name

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


def core(hpc, root: Path, label: str, ca: bytes, data=None, links=None) -> str:
    """The core's phase of the pilot's layout over a root of its own, the bootstrap's ids its federation's own. Run
    again over the same root, it is a rerun of `up`'s core phase (`hpcfed.py core`). `links` is the link
    check's stand-in (every pair passing, with none given)."""
    import fedlayout
    import fedspec

    (root / "secrets" / "users").mkdir(parents=True, exist_ok=True)
    (root / "ca").mkdir(exist_ok=True)
    (root / "ca" / "federation-ca.pem").write_bytes(ca)
    (root / "fault-journal").mkdir(exist_ok=True)
    (root / "secrets" / "zone.passphrase").write_text("A1b2C3d4E5f6G7h8", encoding="utf-8")
    os.chmod(root / "secrets" / "zone.passphrase", 0o600)
    spec = fedspec.load_spec(LAYOUT)
    hosts = {s.name: f"{s.name}.cluster.example.org" for s in spec.services}
    layout = fedlayout.Layout(root, root.parent, hpc.boot.hpc_spec(spec, hosts))
    values = {**hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, root), **pin_values(root.parent / "pins")}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", links or link_check_stand_in([]))
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


# -- the link check's own files in the probe space (live, 17 Nov 2026) -------------------------------------------------

PROBE_SPACE = "link-probes"


def holding(extra: dict[str, list[str]]) -> Any:
    """`data_stand_in`'s reader with more files in the root of the given spaces, as a federation holds them once the
    link check has written its probes: each is a file of some size and bytes, none of them declared."""
    base = data_stand_in()

    def reader(boot: Any) -> Any:
        read = base(boot)

        def read_with(space: str) -> Any:
            files, directories = read(space)
            more = {name: (len(name), hashlib.sha256(name.encode()).hexdigest()) for name in extra.get(space, ())}
            return {**files, **more}, directories

        return read_with

    return reader


def link_check_files() -> list[str]:
    """What the link check leaves in its probe space, named by the check's own functions: a probe of a pair, a probe of
    a higher generation, and the marker that retires a failed fetch's file."""
    return [probe_name("pair-one"), probe_name("pair-two"), probe_name("pair-one", 1), retired_name("pair-one", "id")]


def test_o63_a_probe_space_holding_the_link_checks_own_files_passes_the_data_check_and_the_count_is_recorded(
    hpc, tmp_path, ca
):
    """C06, O63: the link check writes its probes (and, after a failed fetch, its retirement markers) into the probe
    space, whose manifest is empty. Those files are the check's, not data: the data check of a federation whose link
    check has run leaves them out, and the result says how many."""
    printed = core(hpc, tmp_path / "root", "fed-a", ca, data=holding({PROBE_SPACE: link_check_files()}))
    assert "holds the data its manifest declares (4 space(s))" in printed
    assert f"4 link-check file(s) left out of the probe space ({PROBE_SPACE})" in printed


def test_o63_a_probe_space_that_holds_none_of_them_says_so(hpc, tmp_path, ca):
    """C06, O63: at the first `up` the data check runs before the link check, and records that it left nothing out."""
    printed = core(hpc, tmp_path / "root", "fed-a", ca)
    assert f"0 link-check file(s) left out of the probe space ({PROBE_SPACE})" in printed


def test_o63_a_study_space_holding_a_file_named_like_a_probe_is_still_refused(hpc, tmp_path, ca):
    """C06, O63: the link check's files are the probe space's alone: a probe-named file in a study space is data nobody
    declared, and the federation is not ready."""
    root = tmp_path / "root"
    with pytest.raises(RuntimeError, match=r"^alpha-space does not hold the data its manifest declares") as failed:
        core(hpc, root, "fed-a", ca, data=holding({"alpha-space": [probe_name("pair-one")], PROBE_SPACE: []}))
    assert "(1 file(s) not in it)" in str(failed.value)
    assert not (root / "link-check.json").exists()


def test_o63_a_probe_space_holding_any_other_undeclared_file_is_still_refused_and_counts_only_those(hpc, tmp_path, ca):
    """C06, O63: only the names the link check writes are left out. A stray file and the near misses of its names
    (upper-case hex, generation 0, a suffix, a wrong number of digits) are data nobody declared, however many probes
    sit beside them."""
    stem = probe_name("pair-one")
    upper = stem.upper().replace("EXPCORE-LINK-PROBE-", "expcore-link-probe-")
    near = ["stray.txt", upper, f"{stem}-g0", f"{stem}.bak", stem[:-1]]
    assert upper != stem
    assert len(set(near)) == 5
    with pytest.raises(RuntimeError, match=rf"^{PROBE_SPACE} does not hold the data its manifest declares") as failed:
        core(hpc, tmp_path / "root", "fed-a", ca, data=holding({PROBE_SPACE: [*link_check_files(), *near]}))
    assert "(5 file(s) not in it)" in str(failed.value)


def test_o63_a_probe_named_file_below_a_directory_of_the_probe_space_is_still_refused(hpc, tmp_path, ca):
    """C06, O63: the link check writes into the probe space's root, so its names are matched against a file's whole
    path: one in a directory is not the check's."""
    base = data_stand_in()
    name = f"stray/{probe_name('pair-one')}"

    def reader(boot: Any) -> Any:
        read = base(boot)

        def read_with(space: str) -> Any:
            files, directories = read(space)
            if space == PROBE_SPACE:
                files = {**files, name: (1, hashlib.sha256(b"x").hexdigest())}
                directories = {*directories, "stray"}
            return files, directories

        return read_with

    with pytest.raises(RuntimeError, match=rf"^{PROBE_SPACE} does not hold the data") as failed:
        core(hpc, tmp_path / "root", "fed-a", ca, data=reader)
    assert "1 file(s) not in it, 1 director(ies) not as listed" in str(failed.value)


def test_o63_the_core_phase_rerun_on_a_root_after_a_link_check_passes_its_data_check(hpc, tmp_path, ca):
    """C06, O63 (live, 17 Nov 2026, O35): `up`'s first run checks the data before the link check, so the probe space is
    empty then. The link check then leaves one probe per pair there, and a rerun of the core phase (`hpcfed.py core`)
    read them as undeclared data and refused the federation. The rerun passes, leaving the probes out and
    counting them, and the record the link check writes is rewritten."""
    root, spaces, pairs = tmp_path / "root", {PROBE_SPACE: []}, []
    stand_in = link_check_stand_in(pairs)

    def link_check(config: Any, client: Any) -> Any:
        report = stand_in(config, client)  # the pairs the rendered file declares, each passing
        spaces[PROBE_SPACE] = [probe_name(f"pair-{index}") for index, _ in enumerate(pairs[-1])]
        return report

    first = core(hpc, root, "fed-a", ca, data=holding(spaces), links=link_check)
    assert pairs[-1]
    assert len(spaces[PROBE_SPACE]) == len(pairs[-1])  # the check wrote a probe for each pair
    (root / "link-check.json").unlink()
    again = core(hpc, root, "fed-a", ca, data=holding(spaces), links=link_check)  # refused: the probes were "data"
    assert f"0 link-check file(s) left out of the probe space ({PROBE_SPACE})" in first
    assert f"{len(pairs[-1])} link-check file(s) left out of the probe space ({PROBE_SPACE})" in again
    assert (root / "link-check.json").is_file()


def test_o63_the_content_reader_does_not_read_the_link_checks_files_and_reads_every_other_file(hpc):
    """C06, O63, O35: reading a probe through the probe space's owner may replicate it onto the owner, and a retired
    probe's file is never read again (its fetch may never end): the data check drops the link check's files from the
    probe space, so the reader neither reads their bytes nor lists them as data. Every other file of that space, one
    below its directories and a probe-named file of a study space are read, and still refused if undeclared."""
    import fedcontent
    import fedspec

    spec = fedspec.load_spec(LAYOUT)
    assert spec.probe_space == PROBE_SPACE
    probe, retired, stray = probe_name("pair-one"), retired_name("pair-one", "id"), "stray.txt"
    listing = {
        f"root-{PROBE_SPACE}": [(probe, "id-probe"), (retired, "id-marker"), (stray, "id-stray"), ("sub", "dir-sub")],
        "dir-sub": [(probe, "id-below")],
        "root-alpha-space": [(probe, "id-study")],
    }
    read: list[str] = []

    class Content:
        def __init__(self, boot: Any) -> None:
            self.boot = boot

        def root(self, service: Any, space: str) -> str:
            return f"root-{space}"

        def children(self, service: Any, directory: str) -> list[dict[str, Any]]:
            return [
                {"name": name, "type": "DIR" if file_id.startswith("dir-") else "REG", "file_id": file_id, "size": 3}
                for name, file_id in listing[directory]
            ]

        def read_back(self, service: Any, file_id: str) -> bytes:
            read.append(file_id)
            return b"abc"

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fedcontent, "Content", Content)
        reader = hpc.data.content_reader(SimpleNamespace(layout=SimpleNamespace(spec=spec)))
        files, _ = reader(PROBE_SPACE)
        study, _ = reader("alpha-space")
    assert sorted(read) == [
        "id-below",
        "id-stray",
        "id-study",
    ]  # the probe and the marker of the probe space: never read
    assert set(files) == {probe, retired, stray, f"sub/{probe}"}
    (kept, _), left_out = hpc.data.without_link_check_files((files, set()))
    assert left_out == 2  # `check` still drops and counts the check's files
    assert set(kept) == {stray, f"sub/{probe}"}
    assert study == {probe: (3, hashlib.sha256(b"abc").hexdigest())}


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
