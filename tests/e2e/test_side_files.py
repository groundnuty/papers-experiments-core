"""R14: a study whose runs write a side file beside each record, which the record names by its SHA-256, has it verified
by `check`, `analyze`, `regenerate` and its replication package's own check, every one of them fail-closed.

The study is the example's copy with side files (`side_file_support.side_study`): an instrument writes
`extent_ranges.json.gz` beside each record but those of task `dune`, and study.toml declares it and a second side file
that no run writes. One campaign is run, checked, analysed and reported for the module; each test changes a copy of it
and asks the commands what they make of it. Every refusal is made the same way (`tamper`) for `check`, for `analyze`
after a passing check, for `analyze` under a check file edited to vouch for the change, and for an unpacked package.
"""

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from package_support import stand_in_vendor
from side_file_support import (
    DECLARED,
    FIELD,
    SIDE,
    SURPLUS,
    SURPLUS_FIELD,
    changed_log,
    doctor,
    edit,
    make_check,
    record_beside,
    rel,
    side_files,
    side_study,
    stored_check,
    unpack,
)
from study_support import Campaign, clean_environment, cli, copy_campaign, finish

from expcore.campaign.lock import DispatchLock
from expcore.package import build as package_build
from expcore.package.archive import SUMS_FILE, render_sums
from expcore.package.build import PACKAGE_FILE, build_package, verify_package
from expcore.provenance.hashfile import render_hash_file
from expcore.records.record import RunRecord, decode
from expcore.study import pipeline

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

PRIVATE = "quorvantelle"  # invented; the package's hash file treats it as a name private everywhere

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


def records_without_side_file(runs: Path) -> list[Path]:
    return sorted(p for p in runs.rglob("*.*.json") if not (p.parent / SIDE).exists())


def side_failures(runs: Path) -> dict[str, str]:
    """The check's failures at stage `side_file`, by the file each names, relative to the batch."""
    return {f["file"]: f["error"] for f in stored_check(runs)["failures"] if f["stage"] == "side_file"}


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


def check(env: dict[str, str], runs: Path, *more: str) -> int:
    code, _ = cli(env, "check", str(runs), *more)
    return code


def resum(root: Path) -> None:
    """Make SHA256SUMS agree with the package's files again, as a careful tamperer would."""
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    (root / SUMS_FILE).write_bytes(render_sums(files))


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


def temporary_of(path: Path) -> Path:
    """The temporary name a side file has until its rename (`side_file_placement.temporary_name`), of attempt 1."""
    return path.with_name(f".{path.name}.tmp-1")


def test_r15_check_finishes_the_rename_an_attempt_killed_after_its_record_left_and_passes(copy, clean_environment):
    """R15: a kill after the record that names a side file and before the rename leaves the file under its temporary
    name; `check` renames it, since its hash is the one the record names, and passes, hashing it into its inputs."""
    placed = side_files(copy.runs)[0]
    placed.rename(temporary_of(placed))
    assert check(clean_environment, copy.runs) == 0
    assert placed.is_file()
    assert not temporary_of(placed).exists()
    assert stored_check(copy.runs)["failures"] == []
    assert rel(copy.runs, placed) in stored_check(copy.runs)["hashed_only"]


def test_r15_check_removes_a_temporary_file_no_record_names_and_passes(copy, clean_environment):
    """R15: a kill after the temporary file and before the record leaves a dot-name no record names, in a directory
    that holds no record or beside a record that names no hash for it; `check` removes it and passes."""
    beside_a_record = temporary_of(records_without_side_file(copy.runs)[0].parent / SIDE)
    beside_no_record = temporary_of(side_files(copy.runs)[0].parent.parent / "try9" / SIDE)
    beside_no_record.parent.mkdir()
    for stray in (beside_a_record, beside_no_record):
        stray.write_bytes(b"killed before its record")
    assert check(clean_environment, copy.runs) == 0
    assert not beside_a_record.exists()
    assert not beside_no_record.exists()
    assert stored_check(copy.runs)["failures"] == []


def test_r15_check_removes_nothing_while_a_dispatcher_holds_the_campaign_lock_and_still_finishes_a_rename(
    copy, clean_environment
):
    """R15: with the campaign's lock held by another process there may be an attempt staging a file whose record is
    not written yet, so `check` removes no temporary file; a rename a record names is finished all the same."""
    placed = side_files(copy.runs)[0]
    stray = temporary_of(records_without_side_file(copy.runs)[0].parent / SIDE)  # before `placed` joins that list
    placed.rename(temporary_of(placed))
    stray.write_bytes(b"an attempt that is staging its file")
    with DispatchLock(copy.runs.parent):
        code, output = cli(clean_environment, "check", str(copy.runs))
    assert code == 0, (output, stored_check(copy.runs)["failures"])
    assert placed.is_file()
    assert not temporary_of(placed).exists()
    assert stray.exists()


def test_r15_check_removes_nothing_in_a_batch_that_is_not_the_study_s_own(copy, clean_environment, tmp_path):
    """R15: the campaign's lock protects the batch under the study's `out/`; a batch named elsewhere with `--study` may
    belong to a campaign that is running, so no temporary file is removed there, though a rename a record names is
    finished."""
    elsewhere = tmp_path / "elsewhere" / "runs"
    shutil.copytree(copy.runs, elsewhere)
    placed = side_files(elsewhere)[0]
    stray = temporary_of(records_without_side_file(elsewhere)[0].parent / SIDE)  # before `placed` joins that list
    placed.rename(temporary_of(placed))
    stray.write_bytes(b"an attempt of another campaign that is staging its file")
    assert check(clean_environment, elsewhere, "--study", str(copy.root)) == 0
    assert placed.is_file()
    assert not temporary_of(placed).exists()
    assert stray.exists()


def test_r15_check_never_puts_a_file_in_place_that_is_not_the_one_its_record_names(copy, clean_environment):
    """R15: a temporary file whose bytes are not the ones the record names is removed, never renamed, and the record's
    hash with no file beside it fails the check, as it must."""
    placed = side_files(copy.runs)[0]
    subject = record_beside(placed)
    placed.rename(temporary_of(placed))
    temporary_of(placed).write_bytes(b"not what the record names")
    assert check(clean_environment, copy.runs) == 1
    assert not placed.exists()
    assert not temporary_of(placed).exists()
    assert side_failures(copy.runs)[rel(copy.runs, subject)].startswith(CHANGES["removed"][1])


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


PLACEMENTS = ["beside-a-record-that-names-none", "where-no-record-is"]
UNLISTED = "a side file the replay check did not list"


def unlisted_place(runs: Path, placement: str) -> Path:
    """Where a side file the check never saw lies: beside a record that names none, or where no record is."""
    if placement == "beside-a-record-that-names-none":
        return records_without_side_file(runs)[0].parent / SIDE
    return side_files(runs)[0].parent.parent / "try9" / SIDE


def added_after_the_scan(monkeypatch, target: Path, data: bytes) -> None:
    """Add a side file once `require_current_check` has compared the batch with the check, before the analysis reads
    the batch: the window between the two."""
    scan = pipeline.require_current_check

    def scan_then_add(*args, **kwargs):
        stored = scan(*args, **kwargs)
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(data)
        return stored

    monkeypatch.setattr(pipeline, "require_current_check", scan_then_add)


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_r14_analyze_refuses_a_side_file_added_after_the_check_compared_the_batch(copy, monkeypatch, placement):
    """R14: the analysis enumerates the declared side files of the batch again when it reads it, so one added after
    `require_current_check` compared the batch with the check, which the check never listed, is refused, named."""
    target = unlisted_place(copy.runs, placement)
    added_after_the_scan(monkeypatch, target, side_files(copy.runs)[0].read_bytes())
    code, error = cli({}, "analyze", str(copy.root))
    assert code == 1
    assert f"{rel(copy.runs, target)}: {UNLISTED}" in error


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


@pytest.mark.parametrize("placement", PLACEMENTS)
def test_r14_a_package_check_refuses_a_side_file_added_after_the_check_compared_the_batch(
    built, tmp_path, monkeypatch, placement
):
    """R14: the package's check (what `make check` runs) reads the batch as the analysis does, so a side file added
    after SHA256SUMS and the replay check were compared with the files, which the check never listed, is refused."""
    root = unpack(built, tmp_path)
    runs = root / "out" / "runs"
    target = unlisted_place(runs, placement)
    added_after_the_scan(monkeypatch, target, side_files(runs)[0].read_bytes())
    [problem] = verify_package(root)
    assert f"{rel(runs, target)}: {UNLISTED}" in problem


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
