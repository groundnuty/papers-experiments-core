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
KEYS = {"version", "template_sha256", "platform_fingerprint_sha256"} | PINS
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
) -> Path:
    """The core's phase of `layout` over a root of its own (as `hpcfed.py up` runs it), its pins the site's files
    `agent_site(…, sif=sif)` as declared and its sandbox's work root `work_root` (the step makes it under its tmpfs);
    the federation's root. What the phase prints is appended to `printed`."""
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
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
        out = hpc.boot.core_phase(
            federation, (5001, 5001), values, environ={},
            rest=lambda ca_file: SimpleNamespace(close=lambda: None), bootstrap=BootStandIn(label),
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
    declared set, each value a SHA-256 or the version; no address, id, node, path or secret is in it."""
    root = render(hpc, tmp_path / "a" / "root", ca)
    path = root / FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    text = path.read_text(encoding="utf-8")
    record = json.loads(text)
    assert set(record) == KEYS
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
              digest: str | None = None, **more):  # fmt: skip
    """The step's arguments as its generated line gives them: the expected record's path in the study and its SHA-256
    as the plan hashed it (`digest` to say another); `expected=None` with `path=None` declares none."""
    study = make_study(tmp_path, expected)
    declared = {}
    if path is not None:
        hashed = digest or (sha((study / path).read_bytes()) if (study / path).is_file() else sha(""))
        declared = {"expect_render_identity": path, "expect_render_identity_sha256": hashed}
    return argparse.Namespace(study=study, index=1, count=2, label="fed-a", tmpfs=tmp_path / "tmpfs",
                              results=tmp_path / "results", core_src=tmp_path, resume=False, archive_left=False,
                              snapshot_every=3600.0, **declared, **more)  # fmt: skip


def dispatcher(status: int = 0):
    seen = {}

    def run(argv, env, cwd):
        seen.update(argv=list(argv))
        out = Path(argv[argv.index("run") + 1]) / "out"
        (out / "runs").mkdir(parents=True, exist_ok=True)
        (out / "campaign.lock").write_text("")
        return status

    return run, seen


def run_step(shards, args, root: Path | None, monkeypatch):
    """The step over the federation `root` (its EXPCORE_FED_ROOT; unset when None); status, the dispatcher's calls and
    the core checks made."""
    if root is None:
        monkeypatch.delenv("EXPCORE_FED_ROOT", raising=False)
    else:
        monkeypatch.setenv("EXPCORE_FED_ROOT", str(root))
    run, seen = dispatcher()
    checks = []
    code = shards.step(args, runner=run, core_found=lambda path: checks.append(path) or True)
    return code, seen, checks


def nothing_made(tmp_path: Path, seen: dict, checks: list) -> None:
    """No dispatcher, no core check, no directory of the step: a refused step made nothing."""
    assert seen == {}
    assert checks == []
    assert not list((tmp_path / "tmpfs").iterdir())
    assert not list((tmp_path / "results").iterdir())


def archived(tmp_path: Path, name: str = f"out/{STUDY_STATE}/{FILE}") -> bytes | None:
    with tarfile.open(tmp_path / "results" / "fed-a.out.tar") as tar:
        if name not in tar.getnames():
            return None
        return tar.extractfile(name).read()


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


def test_o74_a_step_with_the_record_but_no_expectation_runs_and_archives_it(shards, tmp_path, monkeypatch):
    """O74: whether or not an expected record was given, the archive carries the federation's own."""
    root = federation_root(tmp_path, identity())
    code, _, _ = run_step(shards, step_args(tmp_path, path=None), root, monkeypatch)
    assert code == 0
    assert archived(tmp_path) == (root / FILE).read_bytes()


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
    """O74: the record is copied once the dispatcher has ended, before the post-run script and the archive: a script
    that reads it from `out/study-state/` passes."""
    post = f"import pathlib, sys\npathlib.Path(sys.argv[2], '{STUDY_STATE}', '{FILE}').read_text()\n"
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
                 "sandbox": {"placement": "het", "node": f"node-s{n}", "het_group": 1}, "phase": "linked"}  # fmt: skip
        (root / "hpc-federation.json").write_text(json.dumps(state))
        (root / "platform.toml").write_text('[space]\nname = "s"\ndata_manifest = "m.json"\n')
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
    assert set(shards.RENDER_IDENTITY_KEYS) == set(identity_module.KEYS) == KEYS
    assert shards.STUDY_STATE == STUDY_STATE
    assert shards.canonical_render_identity(identity()) == canonical(identity()).encode()
    assert identity_module.canonical(identity()) == canonical(identity())


def test_o74_collect_accepts_a_shard_whose_out_holds_the_record_in_study_state(shards, tmp_path):
    """O74: `collect` unpacks an out/ that holds `study-state/render-identity.json`, with its lock, read-only, and
    leaves the record in it for whoever reads the shard's archive."""
    results = tmp_path / "results"
    results.mkdir()
    for index, label in ((1, "fed-a"), (2, "fed-b")):
        out = tmp_path / "src" / label / "out"
        (out / "runs").mkdir(parents=True)
        (out / "runs" / "r.json").write_text("{}")
        (out / "campaign.lock").write_text("")
        put(out / STUDY_STATE / FILE, identity())
        shards.archive(out, results, {"index": index, "count": 2, "label": label}, 0)
    plan = shards.load_plan(manifest(tmp_path / "m"))
    plan = replace(plan, results=results)
    outs = shards.collect(plan, tmp_path / "merge")
    assert [(o / STUDY_STATE / FILE).read_bytes() for o in outs] == [canonical(identity()).encode()] * 2
    assert not os.access(outs[0] / STUDY_STATE / FILE, os.W_OK)
