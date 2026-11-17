"""O63: `shards.py collect --roots-only --root <root> --into <dir>` collects ONE federation's root evidence with no
shard plan, for a federation that ran no campaign (a stop measurement through the relay's verbs, which leaves the
state file, the fault journal and the relay's journals and no step record). It archives the root's evidence
allow-list exactly as `collect --roots-only` does (the same archive, the same exclusion of `secrets/`, mode 0600),
lists the archive and its manifest in an `archives.sha256.json` of the root-only form that `verify-copy` checks, and
writes the collected-evidence marker into the root so that `hpcfed.py down` goes on without `--discard-evidence`.
Every path, name and token here is invented."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from down_rig_support import Rig, make_site
from hpc_fed_support import hpc_on_path
from root_evidence_support import EVIDENCE, NOT_EVIDENCE, PASSPHRASE, REQUEST_ID, TOKEN, make_root, mode_of, record_step

SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "shards.py"
MANIFEST = "archives.sha256.json"
ROOT_ONLY_FORMAT = "expcore-hpc-root-archives/1"
TAR, INFO = "federation.root.tar", "federation.root.json"
STATE_AFTER_STOPS = (
    b'{"format": "expcore-hpc-federation/1", "phase": "linked", "allocation": {"name": "hpcop-demo-7f3a"}, '
    b'"stops": [{"service": "aspen", "verb": "stop", "seconds": 26.1}, {"service": "birch", "verb": "stop"}]}\n'
)
# What a root holds after stop measurements through the relay's verbs: no campaign ran, so no step record, no shard
# claim and no day-0 wiring; the injector's journal and the relay's request, taken and answer files have lines.
STOP_EVIDENCE = {
    **{name: data for name, data in EVIDENCE.items() if not name.startswith(("shard-", "day0-"))},
    "hpc-federation.json": STATE_AFTER_STOPS,
}


@pytest.fixture(scope="module")
def shards():
    spec = importlib.util.spec_from_file_location("hpc_shards_single_root", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["hpc_shards_single_root"] = module  # its dataclasses look their module up by name
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop("hpc_shards_single_root", None)


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcevidence

        modules.evidence = hpcevidence
        yield modules


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _members(path: Path) -> dict[str, bytes]:
    with tarfile.open(path) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


def _stopped_root(tmp_path, name="fed-root"):
    return make_root(tmp_path / name, evidence=STOP_EVIDENCE)


def _collect(shards, root, into, *flags):
    return shards.main(["collect", "--roots-only", "--root", str(root), "--into", str(into), *flags])


def _written(into: Path) -> set[str]:
    return {p.name for p in into.iterdir()} if into.exists() else set()


def _nothing_collected(root: Path, into: Path):
    """The refusal of a collect left no copy, no stage beside it and no marker in the root."""
    assert not into.exists()
    assert not [p for p in into.parent.iterdir() if p.name.startswith(".collect-")]
    assert not (root / "evidence-collected.json").exists()


# ------------------------------------------------------------------------------------------------ the collect
def test_a_single_root_after_a_relay_stop_is_collected_with_no_plan(shards, tmp_path, capsys):
    """O63: the root of a federation that ran only stop measurements (a state file with stops, a fault journal with
    lines, the relay's journals) is collected with no shard plan: the archive holds exactly the evidence allow-list, the
    manifest of the copy is listed in `archives.sha256.json`, whose hash is the only line of stdout."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    assert _collect(shards, root, into) == 0
    assert _written(into) == {TAR, INFO, MANIFEST}
    assert _members(into / TAR) == STOP_EVIDENCE
    out = capsys.readouterr().out
    assert out == f"{_sha(into / MANIFEST)}\n"
    assert (root / "evidence-collected.json").is_file()


def test_the_root_only_manifest_lists_the_archive_and_its_manifest_with_their_hashes_and_nothing_else(shards, tmp_path):
    """O63: `archives.sha256.json` of the root-only form: a format of its own (the full form, which refuses an empty
    archives list, is unchanged) and an `evidence` list of exactly the archive and its manifest, each with its size and
    SHA-256; no path, no time. A second collect of the unchanged root into another directory writes the same bytes."""
    root = _stopped_root(tmp_path)
    assert _collect(shards, root, tmp_path / "first") == 0
    document = json.loads((tmp_path / "first" / MANIFEST).read_bytes())
    assert set(document) == {"format", "evidence"}
    assert document["format"] == ROOT_ONLY_FORMAT
    assert document["evidence"] == [
        {"name": name, "bytes": (tmp_path / "first" / name).stat().st_size, "sha256": _sha(tmp_path / "first" / name)}
        for name in sorted((TAR, INFO))
    ]
    assert str(root) not in (tmp_path / "first" / MANIFEST).read_text()
    assert _collect(shards, root, tmp_path / "second") == 0
    assert (tmp_path / "second" / MANIFEST).read_bytes() == (tmp_path / "first" / MANIFEST).read_bytes()


def test_the_copy_is_private(shards, tmp_path):
    """O63: the directory is 0700 and each of its files 0600, as the root archive of a full collect is."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    assert _collect(shards, root, into) == 0
    assert mode_of(into) == 0o700
    assert {name: mode_of(into / name) for name in _written(into)} == {TAR: 0o600, INFO: 0o600, MANIFEST: 0o600}


def test_no_secret_reaches_the_single_root_archive_and_the_root_is_left_whole(shards, tmp_path):
    """O63: nothing of `secrets/`, no file outside the allow-list and no planted token or passphrase is in the copy, in
    any of its files; the evidence is copied, never moved."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    assert _collect(shards, root, into) == 0
    members = _members(into / TAR)
    assert not [name for name in members if name.startswith("secrets/") or name.endswith((".token", ".key"))]
    assert not set(members) & set(NOT_EVIDENCE)
    for name in _written(into):
        assert TOKEN.encode() not in (into / name).read_bytes()
        assert PASSPHRASE.encode() not in (into / name).read_bytes()
    for name in NOT_EVIDENCE:
        assert (root / name).exists()


@pytest.mark.parametrize(
    "name", ["facts.json", "fault-journal/injections.jsonl", f"fault-relay/answers/{'0' * 32}.json"]
)
def test_a_token_inside_an_evidence_file_refuses_the_collect_naming_the_file_and_never_the_token(
    shards, tmp_path, capsys, name
):
    """O63: an allow-listed file that holds the content of a secret is refused, never archived: the message names the
    file and not the value, no copy is made and no marker is written, so `down` still refuses and only
    `--discard-evidence` is left (which this slot must not use)."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    leaking = root / name
    leaking.parent.mkdir(parents=True, exist_ok=True)
    leaking.write_text(f'{{"admin": "{TOKEN}"}}\n')
    assert _collect(shards, root, into) == 2
    err = capsys.readouterr().err
    assert name in err
    assert TOKEN not in err
    _nothing_collected(root, into)


@pytest.mark.parametrize("name", ["hpc-federation.json", "fault-journal/injections.jsonl"])
def test_a_link_among_the_evidence_refuses_the_collect_and_is_never_followed(shards, tmp_path, capsys, name):
    """O63: a link where an evidence file is (here into `secrets/`) is refused naming it, and nothing is made."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    (root / name).unlink()
    (root / name).symlink_to(root / "secrets" / "admin.token")
    assert _collect(shards, root, into) == 2
    assert name in capsys.readouterr().err
    _nothing_collected(root, into)


def test_a_root_holding_step_records_is_collected_with_a_warning_that_only_its_root_evidence_is(
    shards, tmp_path, capsys
):
    """O63: `--roots-only` marks a root that holds step records (it is what a shard about to move needs), so `--root`
    does too; it says on stderr that the shards' own archives are not collected by it."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    record_step(root, "shard-one")
    assert _collect(shards, root, into) == 0
    assert "WARNING" in capsys.readouterr().err
    assert "shard-steps/shard-one.json" in _members(into / TAR)


# ------------------------------------------------------------------------------------------------ the copy back
def test_verify_copy_passes_on_the_pulled_copy_and_fails_on_a_changed_file(shards, tmp_path, capsys):
    """O63: the pulled copy is checked against the manifest and the hash collect printed; one changed byte, a missing
    file or a truncated one fails it, naming the file."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    assert _collect(shards, root, into) == 0
    given = capsys.readouterr().out.strip()
    pulled = tmp_path / "pulled"
    shutil.copytree(into, pulled)

    def args(local):
        return ["verify-copy", str(local / MANIFEST), str(local), "--manifest-sha256", given]

    assert shards.main(args(pulled)) == 0
    assert "verify-copy: ok" in capsys.readouterr().out
    data = bytearray((pulled / TAR).read_bytes())
    data[len(data) // 2] ^= 0xFF
    (pulled / TAR).write_bytes(bytes(data))
    assert shards.main(args(pulled)) == 1
    assert f"{TAR}: SHA-256 differs" in capsys.readouterr().err
    (pulled / TAR).write_bytes(bytes(data[:100]))
    (pulled / INFO).unlink()
    assert shards.main(args(pulled)) == 1
    err = capsys.readouterr().err
    assert f"{TAR}: size differs" in err
    assert f"{INFO}: missing" in err


def test_verify_copy_refuses_a_manifest_that_is_not_the_one_collect_printed(shards, tmp_path, capsys):
    """O63: the root-only manifest is bound to the printed hash as the full one is."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    assert _collect(shards, root, into) == 0
    capsys.readouterr()
    assert shards.main(["verify-copy", str(into / MANIFEST), str(into), "--manifest-sha256", "0" * 64]) == 2
    assert "not the one given" in capsys.readouterr().err


def test_verify_copy_of_the_root_only_form_runs_alone_under_isolated_python(shards, tmp_path):
    """O63: `verify-copy` runs where the core may not be, `python -I -S`: the root-only form needs nothing else."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    assert _collect(shards, root, into) == 0
    done = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), "verify-copy", str(into / MANIFEST), str(into),
         "--manifest-sha256", _sha(into / MANIFEST)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.stderr
    assert "verify-copy: ok" in done.stdout


def _write_manifest(tmp_path, document) -> tuple[Path, str]:
    path = tmp_path / "forged" / MANIFEST
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(document))
    return path, _sha(path)


def _entry(name):
    return {"name": name, "bytes": 1, "sha256": "a" * 64}


@pytest.mark.parametrize(
    ("document", "why"),
    [
        ({"format": ROOT_ONLY_FORMAT, "evidence": []}, "names no evidence file"),
        ({"format": ROOT_ONLY_FORMAT, "evidence": [_entry(TAR)]}, "exactly"),
        ({"format": ROOT_ONLY_FORMAT, "evidence": [_entry(TAR), _entry("other.root.json")]}, "exactly"),
        ({"format": ROOT_ONLY_FORMAT, "evidence": [_entry(TAR), _entry("other.root.tar")]}, "exactly"),
        ({"format": ROOT_ONLY_FORMAT, "evidence": [_entry(INFO), _entry("other.root.json")]}, "exactly"),
        ({"format": ROOT_ONLY_FORMAT, "evidence": [_entry(TAR), _entry(INFO), _entry("extra.json")]}, "exactly"),
        ({"format": ROOT_ONLY_FORMAT, "evidence": [_entry(TAR), _entry(TAR)]}, "twice"),
        ({"format": ROOT_ONLY_FORMAT, "evidence": [_entry(TAR), _entry(INFO)], "archives": [_entry("a.out.tar")]},
         "key"),
        ({"format": ROOT_ONLY_FORMAT, "evidence": [_entry(TAR), _entry(INFO)], "plan_id": "x"}, "key"),
        ({"format": "expcore-hpc-archives/1", "archives": [], "evidence": [_entry(TAR), _entry(INFO)]},
         "no archive"),
    ],
    ids=[
        "empty", "one file", "another pair", "two archives", "two manifests", "a third file", "twice",
        "an archives list", "another key", "full form",
    ],
)  # fmt: skip
def test_verify_copy_refuses_a_root_only_manifest_that_would_verify_less_than_the_pair(
    shards, tmp_path, capsys, document, why
):
    """O63: the root-only form is exactly a `.root.tar` and its `.root.json` and no other key (an entry in a list the
    form does not check would pass unchecked), and the full form's refusal of an empty archives list is unchanged."""
    path, given = _write_manifest(tmp_path, document)
    assert shards.main(["verify-copy", str(path), str(tmp_path / "forged"), "--manifest-sha256", given]) == 2
    assert why in capsys.readouterr().err


# ------------------------------------------------------------------------------------------------ what is refused
@pytest.mark.parametrize("kind", ["gone", "a file", "empty", "no state", "state is a link", "root is a link"])
def test_a_path_that_is_not_a_federation_root_is_refused_and_nothing_is_made(shards, tmp_path, capsys, kind):
    """O63: a `--root` that is missing, a file, a directory with no state file (only `facts.json` there), one whose
    state
    file is a link, or a link to a real root, is refused; no copy is made and no marker is written anywhere."""
    real = _stopped_root(tmp_path, "real")
    root = tmp_path / "candidate"
    if kind == "a file":
        root.write_text("x")
    elif kind == "empty":
        root.mkdir()
    elif kind == "no state":
        root.mkdir()
        (root / "facts.json").write_bytes(EVIDENCE["facts.json"])
    elif kind == "state is a link":
        make_root(root, evidence=STOP_EVIDENCE)
        (root / "hpc-federation.json").unlink()
        (root / "hpc-federation.json").symlink_to(real / "hpc-federation.json")
    elif kind == "root is a link":
        root.symlink_to(real)
    into = tmp_path / "copy"
    assert _collect(shards, root, into) == 2
    assert "shards: ERROR" in capsys.readouterr().err
    _nothing_collected(real, into)
    assert not (root / "evidence-collected.json").exists()


def test_each_refusal_names_what_is_wrong_with_the_root(shards, tmp_path, capsys):
    """O63: a directory with no state file is `not a federation root`, a link is `a link`, a missing one `not there`."""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _collect(shards, empty, tmp_path / "a") == 2
    assert "not a federation root" in capsys.readouterr().err
    link = tmp_path / "link"
    link.symlink_to(_stopped_root(tmp_path))
    assert _collect(shards, link, tmp_path / "b") == 2
    assert "a link" in capsys.readouterr().err
    assert _collect(shards, tmp_path / "missing", tmp_path / "c") == 2
    assert "not there" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flags", "why"),
    [
        (["--manifest", "shards.toml"], "--root takes no --manifest"),
        (["--shard", "fed-a"], "--root takes no --manifest, --shard or --run"),
        (["--run"], "--root takes no --manifest, --shard or --run"),
    ],
)
def test_a_root_is_not_mixed_with_a_plans_shards(shards, tmp_path, capsys, flags, why):
    """O63: `--root` names one federation and a plan names its shards: giving both (`--manifest`, `--shard`, `--run`) is
    refused, and nothing is made."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    assert _collect(shards, root, into, *flags) == 2
    assert why in capsys.readouterr().err
    _nothing_collected(root, into)


@pytest.mark.parametrize(
    "argv",
    [
        ["collect", "--root", "{root}", "--into", "{into}"],
        ["collect", "--roots-only", "--root", "{root}"],
        ["collect", "--roots-only", "--into", "{into}"],
        ["collect", "--into", "{into}"],
    ],
    ids=["no --roots-only", "no --into", "no --root and no --manifest", "a full collect with no manifest"],
)
def test_the_flags_that_go_together_are_required_together(shards, tmp_path, capsys, argv):
    """O63: `--root` belongs to `collect --roots-only` and needs `--into`; a collect with neither `--manifest` nor
    `--root` is refused by `shards.py` itself, never by a traceback."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    assert shards.main([a.format(root=root, into=into) for a in argv]) == 2
    assert "shards: ERROR" in capsys.readouterr().err
    _nothing_collected(root, into)


@pytest.mark.parametrize(
    "where", ["inside the root", "the root itself", "above the root", "a link", "not empty", "loose"]
)
def test_the_directory_to_collect_into_is_refused_when_it_would_lose_or_mix_the_evidence(
    shards, tmp_path, capsys, where
):
    """O63: `down` removes the root with whatever is inside it, so a copy inside the root is no copy; a directory above
    the root, a link, a directory with something in it and one other users can read are refused, and nothing is made."""
    root = _stopped_root(tmp_path, "fed/root")
    into = {
        "inside the root": root / "results",
        "the root itself": root,
        "above the root": tmp_path / "fed",
        "a link": tmp_path / "linked",
        "not empty": tmp_path / "busy",
        "loose": tmp_path / "loose",
    }[where]
    if where == "a link":
        (tmp_path / "real-dir").mkdir(mode=0o700)
        into.symlink_to(tmp_path / "real-dir")
    elif where == "not empty":
        into.mkdir(mode=0o700)
        (into / "keep.txt").write_text("an earlier copy")
    elif where == "loose":
        into.mkdir(mode=0o755)
        into.chmod(0o755)
    before = sorted(p.name for p in root.iterdir())
    assert _collect(shards, root, into) == 2
    assert "shards: ERROR" in capsys.readouterr().err
    assert sorted(p.name for p in root.iterdir()) == before
    assert not (root / "evidence-collected.json").exists()
    if where == "not empty":
        assert (into / "keep.txt").read_text() == "an earlier copy"
    assert not list(tmp_path.rglob(".collect-*"))


def test_an_empty_private_directory_is_collected_into_and_stays_the_directory(shards, tmp_path):
    """O63: as for a full collect, `--into` may exist if it is empty and private."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    into.mkdir(mode=0o700)
    assert _collect(shards, root, into) == 0
    assert _written(into) == {TAR, INFO, MANIFEST}


def test_a_marker_that_cannot_be_written_fails_the_collect_and_keeps_the_copy(shards, tmp_path, capsys, monkeypatch):
    """O63: the marker is what lets `down` go on, and it is written last; if it cannot be, the collect fails (exit 2,
    not a warning) but the copy it made stays whole in the directory: a copy is never removed once it is in place. The
    root holds no marker, so `down` refuses it; the same command is refused (the copy is not replaced) and a new
    directory collects it."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"

    def refuse(*args, **kwargs):
        raise OSError("no space")

    monkeypatch.setattr(shards._evidence(), "write_marker", refuse)
    assert _collect(shards, root, into) == 2
    err = capsys.readouterr().err
    assert "marker" in err
    assert "stays in the directory" in err
    assert _written(into) == {TAR, INFO, MANIFEST}
    assert not (root / "evidence-collected.json").exists()
    assert not list(tmp_path.glob(".collect-*"))
    monkeypatch.undo()
    assert _collect(shards, root, into) == 2  # the copy is not replaced
    assert "new or empty" in capsys.readouterr().err
    assert _collect(shards, root, tmp_path / "again") == 0
    assert (root / "evidence-collected.json").is_file()


def test_a_marker_that_landed_before_its_write_failed_never_costs_the_only_copy(shards, tmp_path, capsys, monkeypatch):
    """O63: the marker's rename can succeed and the sync of the root's directory after it fail, so that the root holds a
    current marker when `write_marker` raises. The collect fails (exit 2), the copy stays, so `down` (which goes on with
    that marker) has its evidence in the directory, and the same command run again says it is already collected, exit
    0, and verify-copy passes on the copy. A copy deleted on that error would have left a marker and no evidence."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    evidence = shards._evidence()
    written = evidence._write_private

    def land_then_fail(path, payload):
        written(path, payload)
        raise OSError("the directory could not be synced")

    monkeypatch.setattr(evidence, "_write_private", land_then_fail)
    assert _collect(shards, root, into) == 2
    capsys.readouterr()
    assert (root / "evidence-collected.json").is_file()  # the marker is in the root
    assert _written(into) == {TAR, INFO, MANIFEST}  # and so is the copy it vouches for
    monkeypatch.undo()
    assert _collect(shards, root, into) == 0
    captured = capsys.readouterr()
    assert "already collected" in captured.err
    assert shards.main(["verify-copy", str(into / MANIFEST), str(into), "--manifest-sha256", captured.out.strip()]) == 0


# ------------------------------------------------------------------------------------------------ collected twice
def test_a_root_already_collected_and_unchanged_is_said_so_and_nothing_is_rewritten(shards, tmp_path, capsys):
    """O63: the same command run again (a retry) while the evidence is unchanged and the copy is in `--into`: exit 0,
    it says so, the manifest's hash is printed again, and not one file of the copy or the marker is rewritten."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    assert _collect(shards, root, into) == 0
    first = capsys.readouterr().out
    seen = {name: ((into / name).read_bytes(), (into / name).stat().st_mtime_ns) for name in _written(into)}
    marker = root / "evidence-collected.json"
    marked = (marker.read_bytes(), marker.stat().st_mtime_ns)
    assert _collect(shards, root, into) == 0
    captured = capsys.readouterr()
    assert "already collected" in captured.err
    assert captured.out == first
    assert {name: ((into / name).read_bytes(), (into / name).stat().st_mtime_ns) for name in _written(into)} == seen
    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == marked


def test_a_root_marked_and_unchanged_whose_copy_is_not_in_into_is_collected_again_there(shards, tmp_path, capsys):
    """O63: a current marker is no copy: if the operator's directory is a new one, the evidence is collected into it
    (exit 0, said so), or the pull would find nothing and `down` would go on all the same."""
    root = _stopped_root(tmp_path)
    assert _collect(shards, root, tmp_path / "first") == 0
    capsys.readouterr()
    assert _collect(shards, root, tmp_path / "second") == 0
    assert "not in" in capsys.readouterr().err
    assert _written(tmp_path / "second") == {TAR, INFO, MANIFEST}


def test_a_changed_root_is_collected_again_into_a_new_directory_and_never_over_the_first_copy(shards, tmp_path, capsys):
    """O63: after a late journal line the same `--into` is refused (its copy would be replaced while it may be being
    pulled); a new directory holds the evidence as it is now, and the first copy is left exactly as it was."""
    root, into = _stopped_root(tmp_path), tmp_path / "copy"
    assert _collect(shards, root, into) == 0
    first = {name: (into / name).read_bytes() for name in _written(into)}
    with (root / "fault-journal" / "injections.jsonl").open("ab") as journal:
        journal.write(b'{"event": "late"}\n')
    capsys.readouterr()
    assert _collect(shards, root, into) == 2
    assert "new or empty" in capsys.readouterr().err
    assert {name: (into / name).read_bytes() for name in _written(into)} == first
    assert _collect(shards, root, tmp_path / "again") == 0
    assert b'"late"' in _members(tmp_path / "again" / TAR)["fault-journal/injections.jsonl"]
    assert b'"late"' not in _members(into / TAR)["fault-journal/injections.jsonl"]


# ------------------------------------------------------------------------------------------------ down
def _stopped_rig(hpc, tmp_path, site):
    """A root brought up by the real `up` of the rig, then what stop measurements through the relay leave in it: the
    injector's journal and the relay's request and answer, and no step record."""
    rig = Rig(hpc, tmp_path, site)
    for name in ("fault-journal/injections.jsonl", f"fault-relay/requests/{REQUEST_ID}.json",
                 f"fault-relay/answers/{REQUEST_ID}.json"):  # fmt: skip
        path = rig.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(STOP_EVIDENCE[name])
    assert not (rig.root / "shard-steps").exists()
    return rig


def _gated(rig, **options):
    """The state after down's gate: down is stopped right after it, so that the gate is all that is run."""

    def stopped():
        raise RuntimeError("stopped for the test, after the gate")

    rig.driver.take_down = stopped
    with pytest.raises(RuntimeError, match="stopped for the test"):
        rig.driver.down(**options)
    return rig.driver.store.read()


def test_down_refuses_the_stopped_root_until_it_is_collected_and_then_goes_on_without_discarding(hpc, shards, tmp_path):
    """O63: the order of a stop-measurement slot, end to end on a root `up` made: `down` refuses a root that holds the
    stop measurements' evidence and no marker (nothing stopped, nothing cancelled), the refusal names `collect
    --roots-only --root ... --into ...`; after that collect `down` passes its gate, and records no discarded
    evidence."""
    rig = _stopped_rig(hpc, tmp_path, make_site(tmp_path))
    with pytest.raises(RuntimeError) as refused:
        rig.driver.down()
    assert "was not collected" in str(refused.value)
    assert "collect --roots-only --root" in str(refused.value)
    assert "--into" in str(refused.value)
    assert "one federation, no plan" in str(refused.value)
    assert rig.fake.cancelled == []
    assert rig.fake.calls == []
    assert _collect(shards, rig.root, tmp_path / "copy") == 0
    state = _gated(rig)
    assert "evidence_discarded" not in state


def test_a_line_written_after_the_collect_makes_down_refuse_until_the_next_collect(hpc, shards, tmp_path):
    """O63: the relay's late-step watch or another verb can write after the collect: `down` refuses as stale (and names
    the single-root form), a second collect into a new directory unlocks it, and the first copy is left as it was."""
    rig = _stopped_rig(hpc, tmp_path, make_site(tmp_path))
    assert _collect(shards, rig.root, tmp_path / "first") == 0
    with (rig.root / "fault-journal" / "injections.jsonl").open("ab") as journal:
        journal.write(b'{"event": "late"}\n')
    with pytest.raises(RuntimeError) as refused:
        rig.driver.down()
    assert "changed after it was collected" in str(refused.value)
    assert "collect --roots-only --root" in str(refused.value)
    assert _collect(shards, rig.root, tmp_path / "second") == 0
    assert "evidence_discarded" not in _gated(rig)


def test_the_collect_of_a_root_that_holds_only_what_up_writes_marks_it_and_down_goes_on(hpc, shards, tmp_path):
    """O63: a root after `up` alone holds no evidence a run left, and `--roots-only` writes its marker whatever the root
    holds (as it does from a plan), so a slot that collects as a matter of course loses nothing and is not refused."""
    rig = Rig(hpc, tmp_path, make_site(tmp_path))
    assert _collect(shards, rig.root, tmp_path / "copy") == 0
    assert (rig.root / "evidence-collected.json").is_file()
    assert "evidence_discarded" not in _gated(rig)
