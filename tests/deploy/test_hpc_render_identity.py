"""O74: a federation's render identity. `hpcfed.py up` writes `render-identity.json` in the federation's root: the
SHA-256 of each pin the rendered platform holds, of its canonical fingerprint and of the template before it was
rendered, and a version, and nothing of the site. A shard's step (deploy/hpc-federation/shards.py) compares the
federation's record with the one the plan expects, before it makes anything, and refuses on any difference, naming the
key; and it puts the record into `out/study-state/`, so the archive carries it. The core's phase, the bootstrap and the
link check's transfers stand in for a live federation; the dispatcher is a stand-in; nothing runs srun."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tarfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from hpc_fed_support import LAYOUT, PILOT_TEMPLATE, SITE, agent_site, data_stand_in, hpc_on_path, services_only
from hpc_rehearsal_support import BootStandIn, link_check_stand_in

HPC = LAYOUT.parents[1]
SCRIPT = HPC / "shards.py"
FILE = "render-identity.json"
EXPECTED = "render-identity.expected.json"
STUDY_STATE = "study-state"
VERSION = "expcore-render-identity/1"
PINS = {"sandbox_sif_sha256", "egress_shim_sha256", "egress_collector_sha256", "oneclient_bundle_sha256"}
ARTIFACTS = "study_artifacts_tree_sha256"  # in the record only when the site declares HPC_STUDY_ARTIFACTS
RENDERED = {"version", "template_sha256", "platform_fingerprint_sha256"} | PINS  # a render with no artifacts directory
KEYS = RENDERED | {ARTIFACTS}
HEX = re.compile(r"[0-9a-f]{64}")
SIF = b"an invented SIF image\n"


def sha(text: bytes | str) -> str:
    return hashlib.sha256(text if isinstance(text, bytes) else text.encode()).hexdigest()


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import fedlinks
        import hpcidentity

        modules.fedlinks, modules.identity = fedlinks, hpcidentity
        yield modules


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_render_identity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_render_identity"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_render_identity", None)


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


# -- up: the record ----------------------------------------------------------------------------------------------


def render(
    hpc,
    root: Path,
    ca: bytes,
    *,
    label: str = "a",
    sif: bytes = SIF,
    layout: Path = LAYOUT,
    printed: list | None = None,
    work_root: Path | None = None,
    tree: Path | None = None,
) -> Path:
    """The core's phase of `layout` over a root of its own (as `hpcfed.py up` runs it), its pins the site's files
    `agent_site(…, sif=sif)` as declared and its sandbox's work root `work_root` (the step makes it under its tmpfs);
    the federation's root. What the phase prints is appended to `printed`. With `tree`, the site declares that
    artifacts directory: the digest `up` took of it in its preflight is among the values the phase is handed."""
    import fedlayout
    import fedspec
    import hpcpins

    (root / "secrets" / "users").mkdir(parents=True)
    root.chmod(0o700)  # as `up` makes it
    (root / "ca").mkdir()
    (root / "ca" / "federation-ca.pem").write_bytes(ca)
    (root / "fault-journal").mkdir()
    (root / "secrets" / "zone.passphrase").write_text("A1b2C3d4E5f6G7h8", encoding="utf-8")
    os.chmod(root / "secrets" / "zone.passphrase", 0o600)
    spec = fedspec.load_spec(layout)
    hosts = {s.name: f"{s.name}.{label}.cluster.example.org" for s in spec.services}
    federation = fedlayout.Layout(root, root.parent, hpc.boot.hpc_spec(spec, hosts))
    site = {
        **SITE,
        **agent_site(root.parent / "pins", sif=sif, work_root=work_root),
        "SH": "/invented/images",
        "L": "/invented/logs",
    }
    pins = hpcpins.values(hpc.settings.site_from_environment(site))
    values = {**hpc.boot.hpc_values(["/venv/python", "hpcfed.py"], 690, 750, root), **pins}
    if tree is not None:
        values[ARTIFACTS] = hpc.identity.tree_sha256(tree)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
        out = hpc.boot.core_phase(
            federation, (5001, 5001), values, environ={},
            rest=lambda ca_file, **kwargs: SimpleNamespace(close=lambda: None), bootstrap=BootStandIn(label),
            steps=lambda boot: None, content=lambda boot: SimpleNamespace(reset=lambda timeout: None),
            data=data_stand_in(),
        )  # fmt: skip
    if printed is not None:
        printed.append(out)
    return root


def record_of(root: Path) -> dict:
    return json.loads((root / FILE).read_text(encoding="utf-8"))


def differing(one: dict, other: dict) -> set:
    return {key for key in KEYS if one.get(key) != other.get(key)}


def edited_layout(folder: Path, comment: str) -> Path:
    """The pilot's layout and its template with one comment line more: another template, the same platform."""
    folder.mkdir(parents=True)
    (folder / LAYOUT.name).write_text(LAYOUT.read_text(encoding="utf-8"), encoding="utf-8")
    template = PILOT_TEMPLATE.read_text(encoding="utf-8") + f"# {comment}\n"
    (folder / PILOT_TEMPLATE.name).write_text(template, encoding="utf-8")
    return folder / LAYOUT.name


def test_o74_up_writes_the_record_0600_with_exactly_the_declared_keys_and_nothing_of_the_site(hpc, tmp_path, ca):
    """O74: the root holds `render-identity.json`, mode 0600, canonical JSON with sorted keys; its keys are exactly the
    declared set (of a render whose site declares no artifacts directory), each value a SHA-256 or the version; no
    address, id, node, path or secret is in it."""
    root = render(hpc, tmp_path / "a" / "root", ca)
    path = root / FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    record = json.loads(text)
    assert set(record) == RENDERED
    assert record["version"] == VERSION
    assert all(HEX.fullmatch(value) for key, value in record.items() if key != "version")
    assert text == json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    for site_value in (str(tmp_path), "cluster.example.org", "sid-", "pid-", "uid-", "token", "passphrase", "A1b2C3"):
        assert site_value not in text


def test_o74_the_pins_are_the_hashes_up_checked_and_the_fingerprint_and_template_are_the_cores(hpc, tmp_path, ca):
    """O74: each pin is the SHA-256 the site declared and `up` checked (the collector's the bundle's own), the platform
    entry is the SHA-256 of the canonical JSON of the rendered file's `PlatformConfig.fingerprint()` as the core
    hashes a fingerprint, and the template entry is the SHA-256 of the template's own bytes."""
    from expcore.campaign.manifest import fingerprint_sha256
    from expcore_onedata.config import load_platform

    root = render(hpc, tmp_path / "a" / "root", ca)
    declared = agent_site(tmp_path / "a" / "pins")
    record = record_of(root)
    assert record["sandbox_sif_sha256"] == declared["HPC_AGENT_SIF_SHA256"] == sha(SIF)
    assert record["egress_shim_sha256"] == declared["HPC_EGRESS_SHIM_SHA256"]
    assert record["oneclient_bundle_sha256"] == declared["HPC_ONECLIENT_BUNDLE_SHA256"]
    assert record["egress_collector_sha256"] == sha((HPC / "bundle" / "egress-collector.py").read_bytes())
    assert record["platform_fingerprint_sha256"] == fingerprint_sha256(
        load_platform(root / "platform.toml").fingerprint()
    )
    assert record["template_sha256"] == sha(PILOT_TEMPLATE.read_bytes())


def test_o74_two_federations_rendered_from_one_template_give_equal_records(hpc, tmp_path, ca):
    """O74: two federations from one template, one set of pins and the same data, with other roots, hosts and ids,
    give the same record, byte for byte."""
    one = render(hpc, tmp_path / "a" / "root", ca, label="fed-a")
    other = render(hpc, tmp_path / "b" / "root", ca, label="fed-b")
    assert (one / "platform.toml").read_bytes() != (other / "platform.toml").read_bytes()  # they are two renders
    assert (one / FILE).read_bytes() == (other / FILE).read_bytes()


def test_o74_a_changed_pin_gives_another_record_in_the_pin_and_the_fingerprint_that_holds_it(hpc, tmp_path, ca):
    """O74: the SIF's hash changed: the record differs, in its pin and in the fingerprint (which holds the Apptainer
    backend's pins), and in nothing else."""
    one = render(hpc, tmp_path / "a" / "root", ca)
    other = render(hpc, tmp_path / "b" / "root", ca, sif=b"another SIF image\n")
    assert differing(record_of(one), record_of(other)) == {"sandbox_sif_sha256", "platform_fingerprint_sha256"}


def test_o74_a_changed_template_gives_another_record_in_the_template_entry_alone(hpc, tmp_path, ca):
    """O74: the template edited (a comment: the platform it renders is the same) differs in the template's hash only."""
    one = render(hpc, tmp_path / "a" / "root", ca, layout=edited_layout(tmp_path / "la", "one"))
    other = render(hpc, tmp_path / "b" / "root", ca, layout=edited_layout(tmp_path / "lb", "another"))
    assert differing(record_of(one), record_of(other)) == {"template_sha256"}


def test_o74_a_template_with_no_sandbox_holds_no_pin_entries(hpc, tmp_path, ca):
    """O74: the pins the rendered platform does not hold are not in its record (never an empty or null value)."""
    root = render(hpc, tmp_path / "a" / "root", ca, layout=services_only(tmp_path / "layout"))
    assert set(record_of(root)) == {"version", "template_sha256", "platform_fingerprint_sha256"}


def test_o74_up_says_the_record_was_written_beside_the_platform_file(hpc, tmp_path, ca):
    """O74: the core's phase's output names the record, with the platform file; a layout with no template says there
    is none."""
    printed = []
    render(hpc, tmp_path / "a" / "root", ca, printed=printed)
    assert f"facts.json and platform.toml written; {FILE} written" in printed[0]


def artifacts_tree(folder: Path, **changes: bytes) -> Path:
    """A study's artifacts directory made by hand under `folder`: a clone, the interpreter installed in it, a venv with
    its links (every link inside the directory), a manifest."""
    files = {"clone/server.py": b"print('serve')\n", "manifests/space.json": b"{}\n",
             "python/bin/python3.12": b"an interpreter\n",
             "venv/lib/python3.12/site-packages/pkg/__init__.py": b"VALUE = 1\n"}  # fmt: skip
    for name, data in {**files, **changes}.items():
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(data)
    (folder / "venv" / "bin").mkdir(exist_ok=True)
    (folder / "venv" / "bin" / "python").symlink_to("../../python/bin/python3.12")
    return folder


def test_o74_up_with_a_study_artifacts_directory_records_the_digest_of_its_tree_and_no_path(hpc, tmp_path, ca):
    """O74: the site declares `HPC_STUDY_ARTIFACTS`: the record holds `study_artifacts_tree_sha256`, the SHA-256 of the
    tree's canonical listing (`hpcidentity.tree_sha256`), beside every other key, and neither the directory's path nor
    any of the path it lies under is anywhere in the file (mode 0600, as the rest)."""
    tree = artifacts_tree(tmp_path / "studies" / "a-study" / "leaf-dir")
    root = render(hpc, tmp_path / "a" / "root", ca, tree=tree)
    text = (root / FILE).read_text(encoding="utf-8")
    record = json.loads(text)
    assert set(record) == KEYS
    assert record[ARTIFACTS] == hpc.identity.tree_sha256(tree)
    assert HEX.fullmatch(record[ARTIFACTS])
    assert text == json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
    assert stat.S_IMODE((root / FILE).stat().st_mode) == 0o600
    for part in (str(tree), str(tmp_path), "a-study", "leaf-dir", "python3.12"):
        assert part not in text


def test_o74_up_with_no_study_artifacts_directory_records_no_such_key_and_the_record_is_what_it_was(hpc, tmp_path, ca):
    """O74: a federation with no artifacts directory has no `study_artifacts_tree_sha256`, never a null or an empty
    one, and the rest of its record is that of a federation with one: the key is the only difference."""
    with_tree = render(hpc, tmp_path / "a" / "root", ca, tree=artifacts_tree(tmp_path / "artifacts"))
    without = render(hpc, tmp_path / "b" / "root", ca)
    assert ARTIFACTS in record_of(with_tree)
    assert ARTIFACTS not in (without / FILE).read_text(encoding="utf-8")
    assert set(record_of(without)) == RENDERED
    assert {k: v for k, v in record_of(with_tree).items() if k != ARTIFACTS} == record_of(without)


def test_o74_two_federations_with_copies_of_one_artifacts_tree_at_other_paths_give_equal_records(hpc, tmp_path, ca):
    """O74: the same tree at two paths, in two federations of one template: equal records, byte for byte."""
    one = artifacts_tree(tmp_path / "one" / "artifacts")
    other = tmp_path / "elsewhere" / "copy"
    shutil.copytree(one, other, symlinks=True)
    first = render(hpc, tmp_path / "a" / "root", ca, label="fed-a", tree=one)
    second = render(hpc, tmp_path / "b" / "root", ca, label="fed-b", tree=other)
    assert (first / FILE).read_bytes() == (second / FILE).read_bytes()
    assert ARTIFACTS in record_of(first)


def test_o74_a_changed_artifacts_tree_gives_another_record_in_the_artifacts_entry_alone(hpc, tmp_path, ca):
    """O74: one byte of one file of the tree (its venv's) differs: the record differs in the tree's digest and in
    nothing else, since the rendered platform does not hold the tree."""
    one = render(hpc, tmp_path / "a" / "root", ca, tree=artifacts_tree(tmp_path / "one"))
    pkg = "venv/lib/python3.12/site-packages/pkg/__init__.py"
    other = render(hpc, tmp_path / "b" / "root", ca, tree=artifacts_tree(tmp_path / "two", **{pkg: b"VALUE = 2\n"}))
    assert differing(record_of(one), record_of(other)) == {ARTIFACTS}


@pytest.mark.parametrize("bad", ["", "abc", "G" * 64, "A" * 64])
def test_o74_a_digest_that_is_no_sha256_is_refused_by_the_record_naming_the_key(hpc, tmp_path, bad):
    """O74: the record takes the digest `up` took and recorded, and refuses one that is no 64 lower-case hex digits,
    naming the key."""
    with pytest.raises(RuntimeError, match=ARTIFACTS):
        hpc.identity.build("# a template\n", {ARTIFACTS: bad}, tmp_path / "platform.toml")


# -- step: the expected record, the federation's record ------------------------------------------------------------


def canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"


def identity(**changes) -> dict:
    """A well-formed record, every key a hash made up from its name, then `changes` (a value of None drops the key)."""
    base = {key: sha(key) for key in KEYS - {"version"}} | {"version": VERSION}
    base.update(changes)
    return {key: value for key, value in base.items() if value is not None}


def put(path: Path, record: dict | str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record if isinstance(record, str) else canonical(record), encoding="utf-8")
    path.chmod(mode)
    return path


def make_study(tmp_path: Path, expected: dict | str | None = None) -> Path:
    """The stamped study copy; with `expected`, the plan's expected record in it."""
    study = tmp_path / "study"
    study.mkdir(parents=True, exist_ok=True)
    study.chmod(0o755)
    (study / "study.toml").write_text("[study]\n")
    (study / "campaign.toml").write_text("[scheduler]\nworkers = 1\n")
    if expected is not None:
        put(study / EXPECTED, expected, 0o644)
    for name in ("tmpfs", "results"):
        (tmp_path / name).mkdir(exist_ok=True)
    return study


def federation_root(tmp_path: Path, record: dict | str | None = None, name: str = "root") -> Path:
    root = tmp_path / "fed" / name
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    if record is not None:
        put(root / FILE, record)
    return root


def step_args(tmp_path: Path, expected: dict | str | None = None, *, path: str | None = EXPECTED,
              digest: str | None = None, resume: bool = False, **more):  # fmt: skip
    """The step's arguments as its generated line gives them: the expected record's path in the study and its SHA-256
    as the plan hashed it (`digest` to say another); `expected=None` with `path=None` declares none."""
    study = make_study(tmp_path, expected)
    declared = {}
    if path is not None:
        hashed = digest or (sha((study / path).read_bytes()) if (study / path).is_file() else sha(""))
        declared = {"expect_render_identity": path, "expect_render_identity_sha256": hashed}
    return argparse.Namespace(study=study, index=1, count=2, label="fed-a", tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=resume, archive_left=False,
                              snapshot_every=3600.0, **declared, **more)  # fmt: skip


def dispatcher(status: int = 0, *, makes_out: bool = True):
    """A stand-in dispatcher; what `out/study-state/` held when it started goes into `seen["at_start"]`."""
    seen = {}

    def run(argv, env, cwd):
        out = Path(argv[argv.index("run") + 1]) / "out"
        held = {p.name: p.read_bytes() for p in (out / STUDY_STATE).glob("render-identity.*.json")}
        seen.update(argv=list(argv), at_start=held)
        if makes_out:
            (out / "runs").mkdir(parents=True, exist_ok=True)
            (out / "campaign.lock").write_text("")
        return status

    return run, seen


def run_step(shards, args, root: Path | None, monkeypatch, **dispatching):
    """The step over the federation `root` (its EXPCORE_FED_ROOT; unset when None); status, the dispatcher's calls and
    the core checks made. `dispatching` is for the stand-in dispatcher (`dispatcher`); `snapshots` goes to the step."""
    if root is None:
        monkeypatch.delenv("EXPCORE_FED_ROOT", raising=False)
    else:
        monkeypatch.setenv("EXPCORE_FED_ROOT", str(root))
    snapshots = dispatching.pop("snapshots", None)
    run, seen = dispatcher(**dispatching)
    checks = []
    code = shards.step(args, runner=run, core_found=lambda path: checks.append(path) or True, snapshots=snapshots)
    return code, seen, checks


def nothing_made(tmp_path: Path, seen: dict, checks: list) -> None:
    """No dispatcher, no core check, no directory of the step: a refused step made nothing."""
    assert seen == {}
    assert checks == []
    assert not list((tmp_path / "tmpfs").iterdir())
    assert not list((tmp_path / "results").iterdir())


RECORD_NAME = re.compile(rf"out/{STUDY_STATE}/render-identity\.[0-9]{{8}}T[0-9]{{6}}Z-[0-9a-f]{{8}}\.json")


def archived_records(tmp_path: Path) -> dict[str, bytes]:
    """Every record the shard's archive carries under out/study-state/, by its name in the archive."""
    with tarfile.open(tmp_path / "results" / "fed-a.out.tar") as tar:
        names = tar.getnames()
        assert not [
            n for n in names if STUDY_STATE in n and not (RECORD_NAME.fullmatch(n) or n == f"out/{STUDY_STATE}")
        ]
        return {n: tar.extractfile(n).read() for n in names if RECORD_NAME.fullmatch(n)}


def archived(tmp_path: Path) -> bytes | None:
    """The one record of a shard of one step, or None."""
    found = archived_records(tmp_path)
    assert len(found) <= 1
    return next(iter(found.values()), None)


def test_o74_a_step_expecting_the_federations_own_record_runs_and_its_archive_carries_the_record(
    shards, tmp_path, monkeypatch
):
    """O74: the federation's record is the expected one: the step runs, and the archive's out/study-state/ holds the
    federation's record, byte for byte."""
    root = federation_root(tmp_path, identity())
    code, seen, _ = run_step(shards, step_args(tmp_path, identity()), root, monkeypatch)
    assert code == 0
    assert "run" in seen["argv"]
    assert archived(tmp_path) == (root / FILE).read_bytes()


def test_o74_a_step_expecting_the_old_record_refuses_before_anything_is_made_naming_the_keys(
    shards, tmp_path, monkeypatch, capsys
):
    """O74: the federation was rendered with another SIF: the step expecting the old record ends with the refusal's
    status 73, before the pre-step guard (a script of the study), the core check, a directory, a journal or an archive;
    the message names the keys that differ and no hash, path or other value."""
    pins = {"sandbox_sif_sha256": sha("a new SIF"), "platform_fingerprint_sha256": sha("a new fingerprint")}
    root = federation_root(tmp_path, identity(**pins))
    args = step_args(tmp_path, identity(), pre_step="guard.py", pre_step_sha256=sha("open('ran', 'w')\n"),
                     pre_step_timeout=20.0)  # fmt: skip
    (args.study / "guard.py").write_text("open('ran', 'w')\n")  # a guard that would leave a trace in the study
    code, seen, checks = run_step(shards, args, root, monkeypatch)
    err = capsys.readouterr().err
    assert code == shards.PRE_STEP_REFUSED == 73
    nothing_made(tmp_path, seen, checks)
    assert not (args.study / "ran").exists()
    assert "platform_fingerprint_sha256" in err
    assert "sandbox_sif_sha256" in err
    assert "template_sha256" not in err
    assert not HEX.search(err)
    assert str(tmp_path) not in err


@pytest.mark.parametrize(
    ("change", "named"),
    [({"version": "expcore-render-identity/2"}, {"version"}),
     ({"template_sha256": sha("an edited template")}, {"template_sha256"}),
     ({"egress_shim_sha256": None}, {"egress_shim_sha256"}),  # a pin the federation's render does not hold
     ({"oneclient_bundle_sha256": sha("another bundle"), "egress_collector_sha256": sha("another collector")},
      {"oneclient_bundle_sha256", "egress_collector_sha256"})],
)  # fmt: skip
def test_o74_every_entry_that_differs_is_named_and_only_those(shards, tmp_path, monkeypatch, capsys, change, named):
    """O74: a version, the template, a pin the federation does not hold, two pins: each key that differs is named."""
    root = federation_root(tmp_path, identity(**change))
    code, seen, checks = run_step(shards, step_args(tmp_path, identity()), root, monkeypatch)
    err = capsys.readouterr().err
    assert code == 73
    nothing_made(tmp_path, seen, checks)
    assert {key for key in KEYS if re.search(rf"\b{key}\b", err)} == named


@pytest.mark.parametrize(
    ("expected", "held"),
    [(identity(), identity(**{ARTIFACTS: sha("another tree")})),  # both hold a digest, and they differ
     (identity(), identity(**{ARTIFACTS: None})),  # only the expected record has one: the federation has no directory
     (identity(**{ARTIFACTS: None}), identity())],  # only the federation's does: the plan expected none
    ids=["the digests differ", "only the expected record has one", "only the federation's record has one"],
)  # fmt: skip
def test_o74_a_step_refuses_a_federation_whose_artifacts_digest_is_not_the_expected_one_naming_the_key(
    shards, tmp_path, monkeypatch, capsys, expected, held
):
    """O74: the artifacts directory's digest is compared like any other key: a federation whose digest differs, has
    none where one is expected or has one where none is expected is refused with status 73 before anything is made,
    naming `study_artifacts_tree_sha256` and no other key, and never a hash or a path."""
    root = federation_root(tmp_path, held)
    code, seen, checks = run_step(shards, step_args(tmp_path, expected), root, monkeypatch)
    err = capsys.readouterr().err
    assert code == shards.PRE_STEP_REFUSED == 73
    nothing_made(tmp_path, seen, checks)
    assert {key for key in KEYS if re.search(rf"\b{key}\b", err)} == {ARTIFACTS}
    assert not HEX.search(err)
    assert str(tmp_path) not in err


def test_o74_a_step_expecting_the_federations_own_artifacts_digest_runs_and_archives_the_record(
    shards, tmp_path, monkeypatch
):
    """O74: the expected record and the federation's hold one digest: the step runs, and the archive carries the
    federation's record with the key in it, byte for byte."""
    root = federation_root(tmp_path, identity())
    code, _, _ = run_step(shards, step_args(tmp_path, identity()), root, monkeypatch)
    assert code == 0
    assert json.loads(archived(tmp_path))[ARTIFACTS] == sha(ARTIFACTS)
    assert archived(tmp_path) == (root / FILE).read_bytes()


@pytest.mark.parametrize("bad", ["abc", sha("x").upper(), "", 7, None])
def test_o74_a_record_whose_artifacts_entry_is_no_sha256_is_not_a_record_and_echoes_none_of_it(
    shards, tmp_path, monkeypatch, capsys, bad
):
    """O74: the key is declared, and its value must be a SHA-256 like every hash of the record: any other is a
    malformed federation record, refused without echoing it."""
    root = federation_root(tmp_path, canonical(identity() | {ARTIFACTS: bad}))
    code, seen, checks = run_step(shards, step_args(tmp_path, identity()), root, monkeypatch)
    err = capsys.readouterr().err
    assert code == 73
    nothing_made(tmp_path, seen, checks)
    assert "not a well-formed render identity" in err
    assert "an entry is no SHA-256" in err  # the value is what is wrong, not that the key is undeclared
    assert not re.search(r"[0-9a-fA-F]{40}", err)


def test_o74_a_step_with_the_record_but_no_expectation_runs_and_archives_it(shards, tmp_path, monkeypatch):
    """O74: whether or not an expected record was given, the archive carries the federation's own."""
    root = federation_root(tmp_path, identity())
    code, _, _ = run_step(shards, step_args(tmp_path, path=None), root, monkeypatch)
    assert code == 0
    assert archived(tmp_path) == (root / FILE).read_bytes()


def test_o74_the_record_is_in_out_before_the_dispatcher_starts_and_before_the_snapshots_do(
    shards, tmp_path, monkeypatch
):
    """O74: a step writes the record into `out/study-state/` after the check and before the dispatcher, so a step killed
    mid-shard carries it, and before the snapshots start, which copy each file of `out/`; the file is the step's own,
    0600, and the canonical text of the record."""
    root = federation_root(tmp_path, identity())
    snapped = []
    snapshots = SimpleNamespace(
        start=lambda out: snapped.append(sorted(p.name for p in (out / STUDY_STATE).iterdir())), stop=lambda out: None
    )
    code, seen, _ = run_step(shards, step_args(tmp_path, identity()), root, monkeypatch, snapshots=snapshots)
    assert code == 0
    assert list(seen["at_start"].values()) == [canonical(identity()).encode()]
    assert snapped == [list(seen["at_start"])]
    assert RECORD_NAME.fullmatch(f"out/{STUDY_STATE}/{next(iter(seen['at_start']))}")


def test_o74_a_resumed_shard_keeps_the_record_of_every_step_each_under_its_own_name(shards, tmp_path, monkeypatch):
    """O74: a resume restores the earlier step's `out/` (with its record) and adds its own, under a name of its own:
    the archive holds both, the first one's unchanged, whatever federation the second ran against."""
    first = federation_root(tmp_path, identity(), name="first")
    assert run_step(shards, step_args(tmp_path, path=None), first, monkeypatch)[0] == 0
    before = archived_records(tmp_path)
    second = federation_root(tmp_path, identity(template_sha256=sha("another template")), name="second")
    code, seen, _ = run_step(shards, step_args(tmp_path, path=None, resume=True), second, monkeypatch)
    assert code == 0
    assert len(seen["at_start"]) == 2  # the restored one is there when the dispatcher starts, beside the new
    after = archived_records(tmp_path)
    assert len(after) == 2
    assert {name: after[name] for name in before} == before
    second_text = canonical(identity(template_sha256=sha("another template"))).encode()
    assert sorted(after.values()) == sorted([canonical(identity()).encode(), second_text])


def test_o74_a_record_is_never_written_over_another_and_a_step_that_cannot_write_it_does_not_start(
    shards, tmp_path, monkeypatch
):
    """O74: the step's file is made new (`O_EXCL`), in a directory that is no link: a name taken, or a link in place of
    the directory, refuses the write; and a step whose write fails makes no dispatcher run and leaves nothing."""
    out = tmp_path / "out"
    shards.note_render_identity(out, b"one\n", "an-id")
    with pytest.raises(shards.ShardsError, match="does not start"):
        shards.note_render_identity(out, b"two\n", "an-id")
    assert (out / STUDY_STATE / "render-identity.an-id.json").read_bytes() == b"one\n"
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / STUDY_STATE).symlink_to(tmp_path)
    with pytest.raises(shards.ShardsError, match="does not start"):
        shards.note_render_identity(linked, b"x\n", "an-id")
    assert not list(tmp_path.glob("render-identity.*"))
    root = federation_root(tmp_path, identity())

    def failing(*_):
        raise shards.ShardsError("the federation's render identity could not be written (OSError): does not start")

    monkeypatch.setattr(shards, "note_render_identity", failing)
    with pytest.raises(shards.ShardsError):
        run_step(shards, step_args(tmp_path / "step", identity()), root, monkeypatch)
    assert not list((tmp_path / "step" / "tmpfs").iterdir())
    assert not list((tmp_path / "step" / "results").glob("*.out.*"))


def test_o74_a_dispatcher_that_leaves_nothing_but_the_record_leaves_no_out_to_archive(shards, tmp_path, monkeypatch):
    """O74: the record is no `out/`: a dispatcher that made nothing (it failed at its start) ends the step with its
    status and no archive, so a fresh start is not refused as a shard that has results."""
    root = federation_root(tmp_path, identity())
    code, seen, _ = run_step(shards, step_args(tmp_path, identity()), root, monkeypatch, status=3, makes_out=False)
    assert code == 3
    assert len(seen["at_start"]) == 1
    assert not list((tmp_path / "results").glob("*.out.*"))
    assert not list((tmp_path / "tmpfs").iterdir())


def test_o74_a_step_with_no_expectation_and_no_record_runs_as_it_did_and_archives_none(shards, tmp_path, monkeypatch):
    """O74: a federation `up` rendered before the record existed (or one that renders no platform file) is run as
    before: nothing is refused and the archive has no out/study-state/."""
    root = federation_root(tmp_path)
    code, _, _ = run_step(shards, step_args(tmp_path, path=None), root, monkeypatch)
    assert code == 0
    assert archived(tmp_path) is None
    with tarfile.open(tmp_path / "results" / "fed-a.out.tar") as tar:
        assert not [name for name in tar.getnames() if STUDY_STATE in name]


def test_o74_the_record_is_in_out_when_the_post_run_script_runs(shards, tmp_path, monkeypatch):
    """O74: the record is in `out/study-state/` when the post-run script runs: a script that reads it there passes."""
    post = (
        "import pathlib, sys\n"
        f"next(pathlib.Path(sys.argv[2], '{STUDY_STATE}').glob('render-identity.*.json')).read_text()\n"
    )
    root = federation_root(tmp_path, identity())
    args = step_args(tmp_path, identity(), post_run="post.py", post_run_sha256=sha(post), post_run_timeout=20.0)
    (args.study / "post.py").write_text(post)
    code, _, _ = run_step(shards, args, root, monkeypatch)
    assert code == 0
    assert json.loads(archived(tmp_path)) == identity()
    assert json.loads((tmp_path / "results" / "fed-a.out.json").read_text())["final"] is True


@pytest.mark.parametrize(
    "bad",
    ["not json at all", "[]", "{}", canonical(identity(version=None)), canonical(identity(template_sha256="short")),
     canonical(identity(template_sha256=sha("x").upper())), canonical(identity(version=3)),
     canonical({**identity(), "/an/invented/path": sha("p")}), canonical({**identity(), "address": "10.1.2.3"}),
     canonical(identity(egress_shim_sha256=None) | {"sandbox_sif_sha256": None}),
     canonical(identity(sandbox_sif_sha256="/an/invented/path")),
     canonical(identity()).replace("{", '{"template_sha256":"/an/invented/path",', 1)],  # a key twice
)  # fmt: skip
def test_o74_a_federation_record_that_is_not_well_formed_refuses_the_step_and_echoes_none_of_it(
    shards, tmp_path, monkeypatch, capsys, bad
):
    """O74: a record that is not JSON, not an object, lacks a required key, holds an undeclared key, a key twice (the
    last of which is valid) or a value that is no SHA-256 refuses the step, naming it as the federation's and saying
    no more of what it holds."""
    root = federation_root(tmp_path, bad)
    code, seen, checks = run_step(shards, step_args(tmp_path, identity()), root, monkeypatch)
    err = capsys.readouterr().err
    assert code == 73
    nothing_made(tmp_path, seen, checks)
    assert "render identity" in err
    for leak in ("/an/invented/path", "10.1.2.3", "address", "not json at all"):
        assert leak not in err


def test_o74_what_the_archive_carries_is_the_validated_record_in_canonical_form_never_other_bytes(
    shards, tmp_path, monkeypatch
):
    """O74: a record `up` did not write byte for byte (here pretty-printed, with an escape in a value) that is valid is
    archived as its canonical text, so no byte the validation did not see reaches the archive."""
    value = identity()["template_sha256"]
    escaped = f"\\u00{ord(value[0]):02x}" + value[1:]  # the same string, spelled with an escape
    spelled = json.dumps(identity(), indent=2, sort_keys=True).replace(value, escaped)
    root = federation_root(tmp_path, spelled + "\n")
    code, _, _ = run_step(shards, step_args(tmp_path, identity()), root, monkeypatch)
    assert code == 0
    assert archived(tmp_path) == canonical(identity()).encode()


def test_o74_a_record_with_a_key_twice_is_not_archived_when_none_is_expected(shards, tmp_path, monkeypatch, capsys):
    """O74: with no expected record nothing is refused, but a record whose first value of a key is not a hash is never
    archived, whatever its last value is: the step says so and runs."""
    bad = canonical(identity()).replace("{", '{"template_sha256":"/an/invented/path",', 1)
    root = federation_root(tmp_path, bad)
    code, _, _ = run_step(shards, step_args(tmp_path, path=None), root, monkeypatch)
    assert code == 0
    assert archived(tmp_path) is None
    err = capsys.readouterr().err
    assert "render identity" in err
    assert "/an/invented/path" not in err


def test_o74_a_missing_record_with_an_expected_one_refuses(shards, tmp_path, monkeypatch, capsys):
    root = federation_root(tmp_path)
    code, seen, checks = run_step(shards, step_args(tmp_path, identity()), root, monkeypatch)
    assert code == 73
    nothing_made(tmp_path, seen, checks)
    assert "no render identity" in capsys.readouterr().err


def test_o74_a_step_that_names_no_federation_but_expects_a_record_refuses(shards, tmp_path, monkeypatch, capsys):
    code, seen, checks = run_step(shards, step_args(tmp_path, identity()), None, monkeypatch)
    assert code == 73
    nothing_made(tmp_path, seen, checks)
    assert "EXPCORE_FED_ROOT" in capsys.readouterr().err


def test_o74_a_record_that_is_a_link_or_whose_root_others_can_write_is_unreadable_and_refuses(
    shards, tmp_path, monkeypatch, capsys
):
    """O74: the record is read below the root with no link followed, and from a root of this user's that no group or
    other can write to; otherwise it is unreadable, which an expected record refuses."""
    root = federation_root(tmp_path, name="linked")
    put(tmp_path / "elsewhere.json", identity())
    (root / FILE).symlink_to(tmp_path / "elsewhere.json")
    code, seen, checks = run_step(shards, step_args(tmp_path, identity()), root, monkeypatch)
    assert code == 73
    nothing_made(tmp_path, seen, checks)
    assert "link" in capsys.readouterr().err
    open_root = federation_root(tmp_path, identity(), name="open")
    open_root.chmod(0o777)
    code, seen, checks = run_step(shards, step_args(tmp_path, identity()), open_root, monkeypatch)
    assert code == 73
    nothing_made(tmp_path, seen, checks)
    assert "written" in capsys.readouterr().err


def test_o74_a_record_that_cannot_be_read_without_an_expectation_is_left_out_of_the_archive_and_said(
    shards, tmp_path, monkeypatch, capsys
):
    """O74: with no expected record nothing is refused, but a record that cannot be taken is not archived, and the step
    says so rather than carry a file it did not read."""
    root = federation_root(tmp_path, name="linked")
    put(tmp_path / "elsewhere.json", identity())
    (root / FILE).symlink_to(tmp_path / "elsewhere.json")
    code, _, _ = run_step(shards, step_args(tmp_path, path=None), root, monkeypatch)
    assert code == 0
    assert archived(tmp_path) is None
    assert "render identity" in capsys.readouterr().err


@pytest.mark.parametrize("problem", ["changed", "link", "malformed", "missing"])
def test_o74_an_expected_file_that_is_not_the_plans_refuses_the_step_naming_the_file(
    shards, tmp_path, monkeypatch, capsys, problem
):
    """O74: the expected file is read below the study's root and must be the bytes the plan hashed, a well-formed
    record: edited since the plan, a link, malformed or missing, it refuses the step, nothing made."""
    root = federation_root(tmp_path, identity())
    args = step_args(tmp_path, identity())
    path = args.study / EXPECTED
    if problem == "changed":
        put(path, identity(template_sha256=sha("edited since the plan")), 0o644)
    elif problem == "link":
        put(tmp_path / "outside.json", identity())
        path.unlink()
        path.symlink_to(tmp_path / "outside.json")
    elif problem == "malformed":
        digest = sha("not a record")
        put(path, "not a record", 0o644)
        args.expect_render_identity_sha256 = digest
    else:
        path.unlink()
    code, seen, checks = run_step(shards, args, root, monkeypatch)
    err = capsys.readouterr().err
    assert code == 73
    nothing_made(tmp_path, seen, checks)
    assert EXPECTED in err
    assert "not a record" not in err


@pytest.mark.parametrize("words", [{"expect_render_identity": EXPECTED}, {"expect_render_identity_sha256": sha("x")}])
def test_o74_a_line_that_gives_the_expected_files_path_without_its_hash_or_the_reverse_refuses(
    shards, tmp_path, monkeypatch, capsys, words
):
    """O74: a hand-edited line with half the declaration is refused, as one with half a pre-step's is."""
    root = federation_root(tmp_path, identity())
    args = step_args(tmp_path, identity(), path=None, **words)
    code, seen, checks = run_step(shards, args, root, monkeypatch)
    assert code == 73
    nothing_made(tmp_path, seen, checks)
    assert "render" in capsys.readouterr().err


def test_o74_a_changed_pin_rendered_by_up_is_refused_by_a_step_expecting_the_old_render(
    hpc, shards, tmp_path, monkeypatch, capsys, ca
):
    """O74, end to end over the two halves: two real renders, one SIF changed; the first's record is the study's
    expected one. The step on the first runs and archives it; the step on the second is refused, naming the pin and the
    fingerprint that holds it, with nothing made."""
    old = render(hpc, tmp_path / "old" / "root", ca, work_root=tmp_path / "on-old" / "tmpfs" / "agent-work")
    new = render(hpc, tmp_path / "new" / "root", ca, sif=b"another SIF image\n",
                 work_root=tmp_path / "on-new" / "tmpfs" / "agent-work")  # fmt: skip
    expected = (old / FILE).read_text(encoding="utf-8")
    code, _, _ = run_step(shards, step_args(tmp_path / "on-old", expected), old, monkeypatch)
    assert code == 0
    assert archived(tmp_path / "on-old") == (old / FILE).read_bytes()
    code, seen, checks = run_step(shards, step_args(tmp_path / "on-new", expected), new, monkeypatch)
    err = capsys.readouterr().err
    assert code == 73
    nothing_made(tmp_path / "on-new", seen, checks)
    assert "sandbox_sif_sha256" in err
    assert "platform_fingerprint_sha256" in err
    assert not HEX.search(err)


# -- the plan: declaring the expected record -----------------------------------------------------------------------


def manifest(tmp_path: Path, extra: str = "", expected: dict | str | None = None) -> Path:
    """A shard plan of two federations; `extra` (top-level keys) goes before the `[[shards]]`; `expected` is the study's
    expected record, when there is one."""
    make_study(tmp_path, expected)
    lines = [f'study = "{tmp_path / "study"}"', f'python = "{tmp_path / "venv" / "python"}"',
             f'core_src = "{tmp_path / "core" / "src"}"', f'results = "{tmp_path / "results"}"',
             f'tmpfs = "{tmp_path / "tmpfs"}"', 'step_path = "/usr/bin:/bin"', "cpus = 6",
             'memory = "18g"']  # fmt: skip
    for n, name in enumerate(("a", "b"), 1):
        root = tmp_path / "fed" / name
        root.mkdir(parents=True, exist_ok=True)
        state = {"format": "expcore-hpc-federation/1", "allocation": {"job": str(100 + n), "name": f"hpcop-x-{n}"},
                 "sandbox": {"placement": "het", "node": f"node-s{n}", "het_group": 1}, "phase": "linked",
                 "data": {"ingested": {}}}  # fmt: skip
        (root / "hpc-federation.json").write_text(json.dumps(state))
        (root / "platform.toml").write_text('[space]\nname = "s"\ndata_manifest = "m.json"\n')
        (root / "facts.json").write_text("{}")  # what `up` writes beside the state: a collect archives both
    if extra:
        lines.append(extra)
    for label, name in (("fed-a", "a"), ("fed-b", "b")):
        lines += ["[[shards]]", f'label = "{label}"', f'root = "{tmp_path / "fed" / name}"']
    path = tmp_path / "shards.toml"
    path.write_text("\n".join(lines) + "\n")
    return path


def lines_of(shards, tmp_path, capsys, command, extra="", expected=None) -> list[str]:
    path = manifest(tmp_path, extra, expected)
    assert shards.main([command, "--manifest", str(path)]) == 0
    return capsys.readouterr().out.splitlines()


DECLARED = f'expect_render_identity = "{EXPECTED}"'


def test_o74_a_declared_expected_record_is_pinned_by_its_hash_in_the_plan_and_in_every_line(shards, tmp_path, capsys):
    """O74: `plan` records the expected file's path and SHA-256 for each shard, and each line carries them as the step's
    two arguments and is otherwise the line with no declaration; with none declared the plan's records carry no such
    key and the line no such word, which is the line it was."""
    base = lines_of(shards, tmp_path / "plain", capsys, "lines")
    plans = [json.loads(line) for line in lines_of(shards, tmp_path / "plain", capsys, "plan")]
    assert all(set(p) == {"shard", "argv"} for p in plans)
    assert not any(word.startswith("--expect") for p in plans for word in p["argv"])
    pin = sha(canonical(identity()))
    declared = [json.loads(x) for x in lines_of(shards, tmp_path / "pinned", capsys, "plan", DECLARED, identity())]
    assert [p["expect_render_identity"] for p in declared] == [{"path": EXPECTED, "sha256": pin}] * 2
    words = lines_of(shards, tmp_path / "pinned", capsys, "lines", DECLARED, identity())
    assert len(words) == len(base) == 2
    tail = f" --expect-render-identity {EXPECTED} --expect-render-identity-sha256 {pin}"
    for line, plain in zip(words, base, strict=True):
        assert line.endswith(tail)
        assert line[: -len(tail)].replace(str(tmp_path / "pinned"), "ROOT").split() == (
            plain.replace(str(tmp_path / "plain"), "ROOT").split()
        )


@pytest.mark.parametrize("value", ['"/abs/expected.json"', '"../outside.json"', '"a/../expected.json"', '"./e.json"',
                                   '""', "3", "true", '["expected.json"]'])  # fmt: skip
def test_o74_an_expected_path_that_leaves_the_study_refuses_the_plan(shards, tmp_path, capsys, value):
    path = manifest(tmp_path, f"expect_render_identity = {value}", identity())
    assert shards.main(["lines", "--manifest", str(path)]) == 2
    assert "expect_render_identity" in capsys.readouterr().err


@pytest.mark.parametrize("problem", ["missing", "link", "malformed"])
def test_o74_an_expected_file_that_is_missing_a_link_or_malformed_refuses_the_plan_before_a_line_is_printed(
    shards, tmp_path, capsys, problem
):
    path = manifest(tmp_path, DECLARED, identity() if problem != "malformed" else "not a record")
    expected = tmp_path / "study" / EXPECTED
    if problem == "missing":
        expected.unlink()
    elif problem == "link":
        put(tmp_path / "outside.json", identity())
        expected.unlink()
        expected.symlink_to(tmp_path / "outside.json")
    assert shards.main(["plan", "--manifest", str(path)]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert EXPECTED in out.err
    assert "not a record" not in out.err


def test_o74_the_generated_line_checks_the_render_identity_first_even_before_the_core_check(
    shards, tmp_path, capsys, monkeypatch
):
    """O74: run as typed (`main` on the generated line's own arguments), a line whose federation was rendered otherwise
    ends with 73, never the core check's 97 that comes next when the identity matches."""
    monkeypatch.chdir(tmp_path)
    line = lines_of(shards, tmp_path / "p", capsys, "lines", DECLARED, identity())[0]
    words = line.split()
    typed = words[words.index("step") :]
    same = federation_root(tmp_path, identity(), name="same")
    other = federation_root(tmp_path, identity(egress_shim_sha256=sha("another shim")), name="other")
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(same))
    assert shards.main(typed) == shards.CORE_MISMATCH  # the identity matched: the core check, which fails, was next
    monkeypatch.setenv("EXPCORE_FED_ROOT", str(other))
    capsys.readouterr()
    assert shards.main(typed) == shards.PRE_STEP_REFUSED
    assert "egress_shim_sha256" in capsys.readouterr().err
    assert not list((tmp_path / "p" / "tmpfs").iterdir())


# -- the two halves agree; the merge's side ----------------------------------------------------------------------


def test_o74_the_step_s_own_copy_of_the_records_names_is_the_driver_s(hpc, shards):
    """O74: shards.py runs alone on a node with `-I` and keeps its own copy of the record's file name, version and keys
    (as of `home_env`); they are the driver's."""
    identity_module = hpc.identity
    assert shards.RENDER_IDENTITY == identity_module.FILE == FILE
    assert shards.RENDER_IDENTITY_VERSION == identity_module.VERSION == VERSION
    assert set(shards.RENDER_IDENTITY_PINS) == set(identity_module.PINS) == PINS
    assert shards.RENDER_IDENTITY_ARTIFACTS == identity_module.ARTIFACTS == ARTIFACTS
    assert set(shards.RENDER_IDENTITY_KEYS) == set(identity_module.KEYS) == KEYS
    assert shards.STUDY_STATE == STUDY_STATE
    assert shards.canonical_render_identity(identity()) == canonical(identity()).encode()
    assert identity_module.canonical(identity()) == canonical(identity())


def test_o74_collect_accepts_a_shard_whose_out_holds_the_record_in_study_state(shards, tmp_path):
    """O74: `collect` unpacks an out/ that holds a step's `study-state/render-identity.<step id>.json`, with its lock,
    read-only, and leaves the record in it for whoever reads the shard's archive."""
    results = tmp_path / "results"
    results.mkdir()
    for index, label in ((1, "fed-a"), (2, "fed-b")):
        out = tmp_path / "src" / label / "out"
        (out / "runs").mkdir(parents=True)
        (out / "runs" / "r.json").write_text("{}")
        (out / "campaign.lock").write_text("")
        put(out / STUDY_STATE / "render-identity.20261117T010203Z-0a1b2c3d.json", identity())
        shards.archive(out, results, {"index": index, "count": 2, "label": label}, 0)
    plan = shards.load_plan(manifest(tmp_path / "m"))
    plan = replace(plan, results=results)
    outs = shards.collect(plan, tmp_path / "merge")
    kept = [next((o / STUDY_STATE).glob("render-identity.*.json")) for o in outs]
    assert [path.read_bytes() for path in kept] == [canonical(identity()).encode()] * 2
    assert not os.access(kept[0], os.W_OK)
