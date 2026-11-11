"""R14: a study whose runs write a side file beside each record, which the record names by its SHA-256, has it verified
by `check`, `analyze`, `regenerate` and its replication package's own check, every one of them fail-closed.

The example study is copied and the copy given a small instrument, `RangeLog`, which writes `extent_ranges.json.gz`
beside each record and records its SHA-256 at `instruments.extent.side_sha256`; the runs of task `dune` make none. The
copy's study.toml declares that side file, and a second one, `surplus_ranges.json.gz`, that no run writes. The example
itself is unchanged. One campaign is run, checked, analysed and reported for the module; each test changes a copy of it
and asks the commands what they make of it. Every refusal is made the same way (`tamper`) for `check`, for `analyze`
after a passing check, for `analyze` under a check file edited to vouch for the change, and for an unpacked package.
"""

import dataclasses
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
from package_support import stand_in_vendor
from study_support import HEAD, Campaign, clean_environment, cli, copy_campaign, finish, make_copy

from expcore.package import build as package_build
from expcore.package.archive import SUMS_FILE, render_sums
from expcore.package.build import PACKAGE_FILE, build_package, verify_package
from expcore.provenance.codestamp import write_stamp
from expcore.provenance.hashfile import render_hash_file
from expcore.records.record import RunRecord, decode
from expcore.replay.check import CHECK_FILE, input_files, input_hashes, inputs_sha256
from expcore.study import pipeline
from expcore.study.config import load_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

SIDE = "extent_ranges.json.gz"
SURPLUS = "surplus_ranges.json.gz"
FIELD = "instruments.extent.side_sha256"
SURPLUS_FIELD = "instruments.surplus.side_sha256"
DECLARED = [{"pattern": SIDE, "hash_field": FIELD}, {"pattern": SURPLUS, "hash_field": SURPLUS_FIELD}]
PRIVATE = "quorvantelle"  # invented; the package's hash file treats it as a name private everywhere
NAME = "tiny-study-package"

INSTRUMENT = '''

class RangeLog:
    """A test instrument: writes the run's log of byte ranges beside its record and names it by its SHA-256."""

    namespace = "extent"

    def __init__(self, run_dir: Path, shelf: Shelf) -> None:
        self.run_dir = Path(run_dir)
        self.shelf = shelf
        self.identity: dict[str, Any] = {}

    def start(self, identity: Mapping[str, Any]) -> None:
        self.identity = dict(identity)

    def finish(self) -> dict[str, Any]:
        if self.identity["factors"]["task"] == "dune":
            return {"ok": True}  # this run makes no side file
        text = json.dumps({"run_key": self.identity["run_key"], "served": self.shelf.served}, sort_keys=True)
        data = gzip.compress(text.encode("utf-8"), mtime=0)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "extent_ranges.json.gz").write_bytes(data)
        return {"ok": True, "side_sha256": hashlib.sha256(data).hexdigest()}
'''

DECLARATION = f"""
[[study.side_files]]
pattern = "{SIDE}"
hash_field = "{FIELD}"

[[study.side_files]]
pattern = "{SURPLUS}"
hash_field = "{SURPLUS_FIELD}"
"""

# Each refusal of R14 made after a passing check: how `require_current_check` names what it did, and the problem the
# pairing finds in it.
CHANGES = {
    "changed": ("changed", f"{SIDE} beside the record does not have the SHA-256 the record names at {FIELD}"),
    "swapped": ("changed", f"{SIDE} beside the record does not have the SHA-256 the record names at {FIELD}"),
    "removed": ("removed", f"the record names {SIDE} by its hash at {FIELD}, but no such file lies beside it"),
    "orphaned": ("added", "no record lies beside it to name it: a side file left from an earlier campaign or attempt"),
    "unnamed": ("added", f"{SIDE} lies beside the record, which names no hash for it at {FIELD}"),
    "malformed": ("changed", f"{FIELD} does not hold a SHA-256 of {SIDE} (64 lower-case hexadecimal digits)"),
}
SIDE_CHANGES = [c for c in CHANGES if c != "malformed"]  # a malformed hash changes its record: R04 refuses that
PACKAGE_CHANGES = [(c, False) for c in SIDE_CHANGES] + [(c, True) for c in CHANGES]


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def side_study(base: Path) -> Path:
    """A stamped copy of the example whose runs write a side file, and whose study.toml declares it."""
    root = make_copy(base, "study")
    plugin = root / "plugin.py"
    edit(plugin, "import asyncio\n", "import asyncio\nimport gzip\nimport hashlib\nimport json\n")
    edit(plugin, "\n\n# --- the stub model's script", f"{INSTRUMENT}\n\n# --- the stub model's script")
    edit(plugin, "[FactsServed(shelf)]", "[FactsServed(shelf), RangeLog(request.run_dir, shelf)]")
    (root / "study.toml").write_text((root / "study.toml").read_text(encoding="utf-8") + DECLARATION, "utf-8")
    write_stamp(root, HEAD, load_study(root).scope)  # the plugin and study.toml changed: stamp the copy again
    return root


@pytest.fixture(scope="module", autouse=True)
def stand_in_wheels():
    """Every package built here vendors stand-in wheels."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(package_build, "vendor_wheels", stand_in_vendor)
        yield


@pytest.fixture(scope="module")
def finished(tmp_path_factory, clean_environment) -> Campaign:
    """The copy with side files, run to the end and checked, analysed and reported."""
    campaign = Campaign(side_study(tmp_path_factory.mktemp("sidefiles")))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    return campaign


@pytest.fixture
def copy(finished, tmp_path) -> Campaign:
    return copy_campaign(finished, tmp_path, "copy")


@pytest.fixture(scope="module")
def hashes(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("hashes") / "private.sha256"
    path.write_text(render_hash_file([PRIVATE]), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def built(finished, hashes, tmp_path_factory) -> Path:
    return build_package(finished.root, tmp_path_factory.mktemp("dist"), hashes=hashes)


def side_files(runs: Path) -> list[Path]:
    return sorted(runs.rglob(SIDE))


def record_beside(path: Path) -> Path:
    """The one record in the directory of a side file."""
    [record] = [p for p in path.parent.glob("*.json") if not p.name.endswith("attempt-marker.json")]
    return record


def records_without_side_file(runs: Path) -> list[Path]:
    return sorted(p for p in runs.rglob("*.*.json") if not (p.parent / SIDE).exists())


def stored_check(runs: Path) -> dict:
    return json.loads((runs / CHECK_FILE).read_text(encoding="utf-8"))


def side_failures(runs: Path) -> dict[str, str]:
    """The check's failures at stage `side_file`, by the file each names, relative to the batch."""
    return {f["file"]: f["error"] for f in stored_check(runs)["failures"] if f["stage"] == "side_file"}


def rel(runs: Path, path: Path) -> str:
    return path.relative_to(runs).as_posix()


def changed_log(path: Path) -> None:
    """Replace a side file with another log, as a hand-edited one would be."""
    path.write_bytes(gzip.compress(b'{"run_key": "edited", "served": 99}', mtime=0))


def rename_hash(record_file: Path, named: object) -> None:
    """Rewrite a record so that its field holds `named` in place of its side file's hash."""
    record = RunRecord.from_json(decode(record_file.read_bytes()))
    instruments = {**record.instruments, "extent": {"ok": True, "side_sha256": named}}
    dataclasses.replace(record, instruments=instruments).write(record_file.parent, environ={})


def tamper(runs: Path, change: str) -> tuple[list[str], str]:
    """Make one refusal of R14 happen in a batch; return the paths it touched and the path the pairing refuses."""
    first, second = side_files(runs)[:2]
    subject = record_beside(first)
    if change == "changed":
        changed_log(first)
        touched = [first]
    elif change == "swapped":
        one, other = first.read_bytes(), second.read_bytes()
        assert one != other
        first.write_bytes(other)
        second.write_bytes(one)
        touched = [first, second]
    elif change == "removed":
        first.unlink()
        touched = [first]
    elif change == "orphaned":  # left in an attempt directory that holds no record
        subject = first.parent.parent / "try9" / SIDE
        subject.parent.mkdir()
        subject.write_bytes(first.read_bytes())
        touched = [subject]
    elif change == "unnamed":  # beside a record whose run made no side file
        subject = records_without_side_file(runs)[0]
        (subject.parent / SIDE).write_bytes(first.read_bytes())
        touched = [subject.parent / SIDE]
    else:
        rename_hash(subject, "A" * 64)
        touched = [subject]
    return sorted(rel(runs, path) for path in touched), rel(runs, subject)


def doctor(runs: Path) -> None:
    """Edit the check file to vouch for the batch as it is now, as someone covering up a change would."""
    check_file = stored_check(runs)
    patterns = check_file["hashed_only_patterns"]
    check_file["replayed"], check_file["hashed_only"] = input_files(runs, patterns)
    check_file["n_records"] = check_file["n_ok"] = len(check_file["replayed"])
    check_file["inputs"] = input_hashes(runs, patterns)
    check_file["inputs_sha256"] = inputs_sha256(check_file["inputs"])
    (runs / CHECK_FILE).write_text(json.dumps(check_file, indent=1) + "\n", encoding="utf-8")


def check(env: dict[str, str], runs: Path, *more: str) -> int:
    code, _ = cli(env, "check", str(runs), *more)
    return code


def unpack(archive: Path, where: Path) -> Path:
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def resum(root: Path) -> None:
    """Make SHA256SUMS agree with the package's files again, as a careful tamperer would."""
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    (root / SUMS_FILE).write_bytes(render_sums(files))


def make_check(root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    """The package's own `make check`, with the interpreter that already holds its environment."""
    return subprocess.run(
        ["make", "-C", str(root), "-f", "Makefile", "check-installed", f"PYTHON={sys.executable}"],
        capture_output=True,
        text=True,
        env=env,
    )


# --- the passing case ------------------------------------------------------------------------------------------------


def test_r14_a_study_with_side_files_runs_checks_and_analyses_with_each_side_file_hashed_into_the_check(finished):
    """R14: every command passes. The check records the declaration and holds each side file in its input digests,
    hashed and never replayed, with the hash its own record names; a record that names none has none beside it."""
    assert finished.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    check_file = stored_check(finished.runs)
    assert check_file["side_files"] == DECLARED
    assert check_file["hashed_only_patterns"] == [
        "*attempt-marker.json",
        "campaign-snapshot.sqlite",
        SIDE,
        f"*/{SIDE}",
        SURPLUS,
        f"*/{SURPLUS}",
    ]
    found = side_files(finished.runs)
    assert found
    assert records_without_side_file(finished.runs)  # the runs of `dune` made none
    for path in found:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert check_file["inputs"][rel(finished.runs, path)] == digest
        assert rel(finished.runs, path) in check_file["hashed_only"]
        assert rel(finished.runs, path) not in check_file["replayed"]
        assert json.loads(record_beside(path).read_bytes())["instruments"]["extent"]["side_sha256"] == digest
    assert not list(finished.runs.rglob(SURPLUS))
    assert pipeline.regenerate(finished.root) == []


# --- check -----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("change", list(CHANGES))
def test_r14_check_refuses_each_side_file_that_does_not_pair_with_its_record(copy, clean_environment, change):
    """R14: a side file changed, two swapped, one removed while its record names it, one left where no record is, one
    beside a record that names no hash for it, and a hash that is not a SHA-256 each fail the check at stage
    `side_file`, against the record or, for an orphan, against the file, which is never hashed into a passing check."""
    _, subject = tamper(copy.runs, change)
    assert check(clean_environment, copy.runs) == 1
    failures = side_failures(copy.runs)
    assert failures[subject].startswith(CHANGES[change][1])
    assert len(failures) == (2 if change == "swapped" else 1)
    assert subject not in stored_check(copy.runs)["replayed"]


@pytest.mark.parametrize(
    "where",
    ["{digest}", "."],
    ids=["a-run-directory", "the-top-of-the-batch"],
)
def test_r14_check_refuses_an_orphan_side_file_wherever_it_lies_in_the_batch(copy, clean_environment, where):
    """R14: a file of a declared name in any directory that holds no record is refused against its own path."""
    digest = side_files(copy.runs)[0].parent.parent.name
    orphan = copy.runs / where.format(digest=digest) / SIDE
    orphan.write_bytes(side_files(copy.runs)[0].read_bytes())
    assert check(clean_environment, copy.runs) == 1
    assert list(side_failures(copy.runs)) == [rel(copy.runs, orphan)]


def test_r14_check_refuses_a_declared_side_file_no_run_writes_beside_a_record(copy, clean_environment):
    """R14: a record without the declared field of the second side file, with that file beside it, is refused."""
    sided = record_beside(side_files(copy.runs)[0])
    (sided.parent / SURPLUS).write_bytes(b"\x1f\x8b not read")
    assert check(clean_environment, copy.runs) == 1
    assert side_failures(copy.runs) == {
        rel(copy.runs, sided): f"{SURPLUS} lies beside the record, which names no hash for it at {SURPLUS_FIELD}"
    }


@pytest.mark.parametrize("named", ["ab" * 16, 7], ids=["too-short", "a-number"])
def test_r14_check_refuses_a_hash_too_short_or_not_text(copy, clean_environment, named):
    """R14: a field that holds a short hex string or a number is refused like any other value that is not a SHA-256."""
    target = record_beside(side_files(copy.runs)[0])
    rename_hash(target, named)
    assert check(clean_environment, copy.runs) == 1
    assert side_failures(copy.runs) == {rel(copy.runs, target): CHANGES["malformed"][1]}


def test_r14_check_finds_side_files_in_exactly_the_batch_it_parses_however_the_command_names_it(
    copy, clean_environment, tmp_path, monkeypatch
):
    """R14: the side files are found in the batch directory the check parses and nowhere else: a copy of the batch
    checked with `--study` is judged by its own files, not by the study's own batch, and a batch named by a relative
    path through `..` from another directory is judged by the files there."""
    elsewhere = tmp_path / "elsewhere" / "runs"
    shutil.copytree(copy.runs, elsewhere)
    (copy.runs / SIDE).write_bytes(b"an orphan in the study's own batch, which this check does not parse")
    assert check(clean_environment, elsewhere, "--study", str(copy.root)) == 0
    missing = side_files(elsewhere)[0]
    record = record_beside(missing)
    missing.unlink()
    assert check(clean_environment, elsewhere, "--study", str(copy.root)) == 1
    assert list(side_failures(elsewhere)) == [rel(elsewhere, record)]
    monkeypatch.chdir(copy.runs)
    code, _ = cli(clean_environment, "check", "../runs")
    assert code == 1
    assert list(side_failures(copy.runs)) == [SIDE]


# --- analyze and regenerate ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("change", SIDE_CHANGES)
def test_r14_analyze_and_regenerate_refuse_each_change_to_the_side_files_after_the_check(copy, change):
    """R14: after a passing check, a side file changed, swapped, removed, or added where no record is or beside a
    record that names none, makes `analyze`, `report` and `regenerate` refuse, naming what changed, until the check is
    made again."""
    touched, _ = tamper(copy.runs, change)
    named = f"inputs changed since the replay check ({CHANGES[change][0]} {', '.join(touched)})"
    for command in ("analyze", "report"):
        code, error = cli({}, command, str(copy.root))
        assert code == 1, error
        assert named in error
    with pytest.raises(pipeline.Refused) as caught:
        pipeline.regenerate(copy.root)
    assert named in str(caught.value)


@pytest.mark.parametrize("change", list(CHANGES))
def test_r14_analyze_pairs_each_side_file_with_its_record_again_whatever_the_check_file_claims(copy, change):
    """R14: a check file edited to vouch for the batch after each refusal does not make the analysis accept it: the
    analysis reads each side file once and pairs it with the hash the record beside it names, as the check did."""
    _, subject = tamper(copy.runs, change)
    doctor(copy.runs)
    code, error = cli({}, "analyze", str(copy.root))
    assert code == 1
    assert f"{subject}: {CHANGES[change][1]}" in error


# --- the replication package -----------------------------------------------------------------------------------------


def test_r14_the_package_holds_the_side_files_records_the_declaration_and_passes_its_own_make_check(
    built, finished, tmp_path, clean_environment
):
    """R14: the package includes every side file, PACKAGE.json records the declaration, and the unpacked package's
    own `make check` passes, with the interpreter that holds its environment."""
    root = unpack(built, tmp_path)
    for path in side_files(finished.runs):
        assert (root / "out" / "runs" / rel(finished.runs, path)).read_bytes() == path.read_bytes()
    assert json.loads((root / PACKAGE_FILE).read_text(encoding="utf-8"))["side_files"] == DECLARED
    done = make_check(root, clean_environment)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    ("change", "doctored"), PACKAGE_CHANGES, ids=[f"{c}{'-check-edited' if d else ''}" for c, d in PACKAGE_CHANGES]
)
def test_r14_each_change_to_the_side_files_of_an_unpacked_package_fails_its_make_check_with_its_sums_updated(
    built, tmp_path, clean_environment, change, doctored
):
    """R14: each refusal made in an unpacked package fails the package's `make check` even once SHA256SUMS is made to
    agree, naming what changed; and with the replay check's file edited to vouch for the change too, it fails all the
    same, since the package's check pairs each side file with its record again."""
    root = unpack(built, tmp_path)
    runs = root / "out" / "runs"
    touched, subject = tamper(runs, change)
    if doctored:
        doctor(runs)
    resum(root)
    if doctored:
        named = f"{subject}: {CHANGES[change][1]}"
    else:
        named = f"inputs changed since the replay check ({CHANGES[change][0]} {', '.join(touched)})"
    [problem] = verify_package(root)
    assert named in problem
    done = make_check(root, clean_environment)
    assert done.returncode != 0
    assert named in done.stdout + done.stderr


def test_r14_a_package_whose_package_json_records_another_declaration_fails_its_check(built, tmp_path):
    """R14: the package's check compares the declaration PACKAGE.json records with its study.toml's, so a declaration
    dropped from PACKAGE.json, with SHA256SUMS made to agree, fails it."""
    root = unpack(built, tmp_path)
    package = json.loads((root / PACKAGE_FILE).read_text(encoding="utf-8"))
    package["side_files"] = DECLARED[:1]
    (root / PACKAGE_FILE).write_text(json.dumps(package, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    resum(root)
    assert verify_package(root) == [
        f"{PACKAGE_FILE} records another declaration of side files than the package's study.toml"
    ]


def test_r14_a_package_whose_inclusion_list_leaves_a_side_file_out_is_refused(copy, hashes, tmp_path):
    """R14: the package's check needs every side file the replay check hashed, so a list that selects the records and
    leaves the side files out is refused, naming one."""
    edit(copy.root / "package.toml", '    "out/runs",\n', '    "out/runs/**/*.json",\n    "out/runs/*.sqlite",\n')
    with pytest.raises(package_build.PackageError, match=rf"needs out/runs/\S+/{SIDE} and \d+ more"):
        build_package(copy.root, tmp_path / "dist", hashes=hashes)


def test_r14_an_anonymised_package_keeps_the_declaration_and_passes_its_own_check(copy, hashes, tmp_path):
    """R14: the anonymised build makes the replay check again over the rewritten records under the same declaration,
    so its package still pairs every side file and passes its own check."""
    listed = tmp_path / "anon.toml"
    listed.write_text('files = ["README.md"]\n\n[terms]\n"Quillon Varek" = "Author A"\n', encoding="utf-8")
    archive = build_package(copy.root, tmp_path / "dist", hashes=hashes, anonymise=listed)
    unpacked = unpack(archive, tmp_path / "unpacked")
    check_file = stored_check(unpacked / "out" / "runs")
    assert check_file["side_files"] == DECLARED
    assert any(path.endswith(SIDE) for path in check_file["hashed_only"])
    assert verify_package(unpacked) == []
