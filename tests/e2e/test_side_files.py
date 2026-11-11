"""R14: a study whose runs write a side file beside each record, which the record names by its SHA-256, has it verified
by `check`, `analyze`, `regenerate` and its replication package's own check, every one of them fail-closed.

The example study is copied and the copy given a small instrument, `RangeLog`, which writes `extent_ranges.json.gz`
beside each record and records its SHA-256 at `instruments.extent.side_sha256`; the runs of task `dune` make none. The
copy's study.toml declares that side file, and a second one, `spare_ranges.json.gz`, that no run writes. The example
itself is unchanged. One campaign is run, checked, analysed and reported for the module; each test changes a copy of it
and asks the commands what they make of it.
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
from expcore.replay.check import inputs_sha256
from expcore.study import pipeline
from expcore.study.config import load_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

SIDE = "extent_ranges.json.gz"
SPARE = "spare_ranges.json.gz"
FIELD = "instruments.extent.side_sha256"
SPARE_FIELD = "instruments.spare.side_sha256"
DECLARED = [{"pattern": SIDE, "hash_field": FIELD}, {"pattern": SPARE, "hash_field": SPARE_FIELD}]
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
        text = json.dumps({"key": self.identity["run_key"], "served": self.shelf.served}, sort_keys=True)
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
pattern = "{SPARE}"
hash_field = "{SPARE_FIELD}"
"""


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
    campaign = Campaign(side_study(tmp_path_factory.mktemp("sided")))
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


def side_files(campaign: Campaign) -> list[Path]:
    return sorted(campaign.runs.rglob(SIDE))


def record_beside(path: Path) -> Path:
    """The one record in the directory of a side file."""
    [record] = [p for p in path.parent.glob("*.json") if not p.name.endswith("attempt-marker.json")]
    return record


def records_without_side_file(campaign: Campaign) -> list[Path]:
    return sorted(p for p in campaign.runs.rglob("*.*.json") if not (p.parent / SIDE).exists())


def stored_check(campaign: Campaign) -> dict:
    return json.loads((campaign.runs / "replay-check.json").read_text(encoding="utf-8"))


def side_failures(campaign: Campaign) -> dict[str, str]:
    """The check's failures at stage `side_file`, by the file each names, relative to the batch."""
    return {f["file"]: f["error"] for f in stored_check(campaign)["failures"] if f["stage"] == "side_file"}


def rel(campaign: Campaign, path: Path) -> str:
    return path.relative_to(campaign.runs).as_posix()


def changed_log(path: Path) -> None:
    """Replace a side file with another log, as a hand-edited one would be."""
    path.write_bytes(gzip.compress(b'{"key": "edited", "served": 99}', mtime=0))


def check(env: dict[str, str], campaign: Campaign, *more: str) -> int:
    code, _ = cli(env, "check", str(campaign.runs), *more)
    return code


def unpack(archive: Path, where: Path) -> Path:
    with tarfile.open(archive) as members:
        members.extractall(where, filter="data")
    return where / NAME


def resum(root: Path) -> None:
    """Make SHA256SUMS agree with the package's files again, as a careful tamperer would."""
    files = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    (root / SUMS_FILE).write_bytes(render_sums(files))


# --- the passing case ------------------------------------------------------------------------------------------------


def test_r14_a_study_with_side_files_runs_checks_and_analyses_with_each_side_file_hashed_into_the_check(finished):
    """R14: every command passes. The check records the declaration and holds each side file in its input digests,
    hashed and never replayed, with the hash its own record names; a record that names none has none beside it."""
    assert finished.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    check_file = stored_check(finished)
    assert check_file["side_files"] == DECLARED
    assert check_file["hashed_only_patterns"] == [
        "*attempt-marker.json",
        "campaign-snapshot.sqlite",
        SIDE,
        f"*/{SIDE}",
        SPARE,
        f"*/{SPARE}",
    ]
    found = side_files(finished)
    assert found
    assert records_without_side_file(finished)  # the runs of `dune` made none
    for path in found:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert check_file["inputs"][rel(finished, path)] == digest
        assert rel(finished, path) in check_file["hashed_only"]
        assert rel(finished, path) not in check_file["replayed"]
        assert json.loads(record_beside(path).read_bytes())["instruments"]["extent"]["side_sha256"] == digest
    assert not list(finished.runs.rglob(SPARE))
    assert pipeline.regenerate(finished.root) == []


# --- check -----------------------------------------------------------------------------------------------------------


def test_r14_check_refuses_a_changed_side_file(copy, clean_environment):
    """R14: a side file whose bytes are not the ones its record names fails its record at stage `side_file`."""
    target = side_files(copy)[0]
    changed_log(target)
    assert check(clean_environment, copy) == 1
    assert side_failures(copy) == {
        rel(copy, record_beside(target)): (
            f"{SIDE} beside the record does not have the SHA-256 the record names at {FIELD}: it was changed after "
            "the run, or swapped with another record's"
        )
    }
    assert rel(copy, record_beside(target)) not in stored_check(copy)["replayed"]


def test_r14_check_refuses_two_records_side_files_swapped(copy, clean_environment):
    """R14: two side files swapped between their records fail both records, though each file is a genuine log."""
    first, second = side_files(copy)[:2]
    one, other = first.read_bytes(), second.read_bytes()
    assert one != other
    first.write_bytes(other)
    second.write_bytes(one)
    assert check(clean_environment, copy) == 1
    assert set(side_failures(copy)) == {rel(copy, record_beside(first)), rel(copy, record_beside(second))}


def test_r14_check_refuses_a_record_that_names_a_side_file_that_is_missing(copy, clean_environment):
    """R14: a record whose field holds a hash with no such file beside it is refused."""
    target = side_files(copy)[0]
    record = record_beside(target)
    target.unlink()
    assert check(clean_environment, copy) == 1
    assert side_failures(copy) == {
        rel(copy, record): f"the record names {SIDE} by its hash at {FIELD}, but no such file lies beside it"
    }


@pytest.mark.parametrize(
    "where",
    ["{digest}/try9", "{digest}", "."],
    ids=["an-attempt-directory-with-no-record", "a-run-directory", "the-top-of-the-batch"],
)
def test_r14_check_refuses_an_orphan_side_file_where_no_record_names_it(copy, clean_environment, where):
    """R14: a file of a declared name in a directory that holds no record, left from an earlier campaign or attempt,
    is refused against its own path, and never hashed into a passing check."""
    digest = side_files(copy)[0].parent.parent.name
    orphan = copy.runs / where.format(digest=digest) / SIDE
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(side_files(copy)[0].read_bytes())
    assert check(clean_environment, copy) == 1
    assert side_failures(copy) == {
        rel(copy, orphan): (
            "no record lies beside it to name it: a side file left from an earlier campaign or attempt; remove it"
        )
    }


def test_r14_check_refuses_a_side_file_beside_a_record_that_names_no_hash_for_it(copy, clean_environment):
    """R14: a record without the declared field, with a side file beside it, is refused, for a record whose run made
    no side file and for a declared side file no run writes; a record with neither is fine."""
    bare = records_without_side_file(copy)[0]
    (bare.parent / SIDE).write_bytes(side_files(copy)[0].read_bytes())
    sided = record_beside(side_files(copy)[0])
    (sided.parent / SPARE).write_bytes(b"\x1f\x8b not read")
    assert check(clean_environment, copy) == 1
    assert side_failures(copy) == {
        rel(copy, bare): f"{SIDE} lies beside the record, which names no hash for it at {FIELD}",
        rel(copy, sided): f"{SPARE} lies beside the record, which names no hash for it at {SPARE_FIELD}",
    }


@pytest.mark.parametrize("named", ["A" * 64, "ab" * 16, 7], ids=["upper-case", "too-short", "a-number"])
def test_r14_check_refuses_a_hash_that_is_not_64_lower_case_hex_digits(copy, clean_environment, named):
    """R14: a record field that holds anything but a SHA-256 in lower-case hex is refused, whatever lies beside it."""
    target = record_beside(side_files(copy)[0])
    record = RunRecord.from_json(decode(target.read_bytes()))
    instruments = {**record.instruments, "extent": {"ok": True, "side_sha256": named}}
    dataclasses.replace(record, instruments=instruments).write(target.parent, environ={})
    assert check(clean_environment, copy) == 1
    assert side_failures(copy) == {
        rel(copy, target): f"{FIELD} does not hold a SHA-256 of {SIDE} (64 lower-case hexadecimal digits)"
    }


def test_r14_check_finds_side_files_in_exactly_the_batch_it_parses_however_the_command_names_it(
    copy, clean_environment, tmp_path, monkeypatch
):
    """R14: the side files are found in the batch directory the check parses and nowhere else: a copy of the batch
    checked with `--study` is judged by its own files, not by the study's own batch, and a batch named by a relative
    path through `..` from another directory is judged by the files there."""
    elsewhere = Campaign(tmp_path / "elsewhere")
    shutil.copytree(copy.runs, elsewhere.runs)
    (copy.runs / SIDE).write_bytes(b"an orphan in the study's own batch, which this check does not parse")
    assert check(clean_environment, elsewhere, "--study", str(copy.root)) == 0
    missing = side_files(elsewhere)[0]
    missing.unlink()
    assert check(clean_environment, elsewhere, "--study", str(copy.root)) == 1
    assert list(side_failures(elsewhere)) == [rel(elsewhere, next(missing.parent.glob("*.*.json")))]
    monkeypatch.chdir(copy.root / "out" / "runs")
    code, _ = cli(clean_environment, "check", "../runs")
    assert code == 1
    assert list(side_failures(copy)) == [SIDE]


# --- analyze and regenerate ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["changed", "removed", "added"])
def test_r14_analyze_and_regenerate_refuse_a_side_file_changed_removed_or_added_after_the_check(copy, change):
    """R14: after a passing check, a side file changed, removed, or one added where no record names it, makes
    `analyze`, `report` and `regenerate` refuse, naming it, until the check is made again."""
    target = side_files(copy)[0]
    if change == "changed":
        changed_log(target)
    elif change == "removed":
        target.unlink()
    else:
        target = target.parent.parent / "try9" / SIDE
        target.parent.mkdir()
        target.write_bytes(side_files(copy)[0].read_bytes())
    for command in ("analyze", "report"):
        code, error = cli({}, command, str(copy.root))
        assert code == 1, error
        assert f"inputs changed since the replay check ({change} {rel(copy, target)})" in error
    with pytest.raises(pipeline.Refused, match=f"{change} {rel(copy, target)}"):
        pipeline.regenerate(copy.root)


def test_r14_analyze_pairs_each_side_file_with_its_record_again_whatever_the_check_file_claims(copy):
    """R14: a check file edited to hold a changed side file's new hash still does not make the analysis accept it:
    the analysis reads each side file once and pairs it with the hash its record names, as the check did."""
    target = side_files(copy)[0]
    changed_log(target)
    check_file = stored_check(copy)
    check_file["inputs"][rel(copy, target)] = hashlib.sha256(target.read_bytes()).hexdigest()
    check_file["inputs_sha256"] = inputs_sha256(check_file["inputs"])
    (copy.runs / "replay-check.json").write_text(json.dumps(check_file, indent=1) + "\n", encoding="utf-8")
    code, error = cli({}, "analyze", str(copy.root))
    assert code == 1
    assert f"{rel(copy, record_beside(target))}: {SIDE} beside the record does not have the SHA-256" in error


# --- the replication package -----------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def built(finished, hashes, tmp_path_factory) -> Path:
    return build_package(finished.root, tmp_path_factory.mktemp("dist"), hashes=hashes)


def test_r14_the_package_holds_the_side_files_records_the_declaration_and_passes_its_own_make_check(
    built, finished, tmp_path, clean_environment
):
    """R14: the package includes every side file, PACKAGE.json records the declaration, and the unpacked package's
    own `make check` passes, with the interpreter that holds its environment."""
    root = unpack(built, tmp_path)
    for path in side_files(finished):
        assert (root / "out" / "runs" / rel(finished, path)).read_bytes() == path.read_bytes()
    assert json.loads((root / PACKAGE_FILE).read_text(encoding="utf-8"))["side_files"] == DECLARED
    done = subprocess.run(
        ["make", "-C", str(root), "-f", "Makefile", "check-installed", f"PYTHON={sys.executable}"],
        capture_output=True,
        text=True,
        env=clean_environment,
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize("change", ["changed", "added"])
def test_r14_a_side_file_changed_or_added_in_an_unpacked_package_fails_its_make_check_with_its_sums_updated(
    built, tmp_path, clean_environment, change
):
    """R14: a side file changed, or one added where no record names it, in an unpacked package fails the package's
    `make check` even once SHA256SUMS is made to agree, naming the side file."""
    root = unpack(built, tmp_path)
    target = sorted((root / "out" / "runs").rglob(SIDE))[0]
    if change == "changed":
        changed_log(target)
    else:
        source, target = target, target.parent.parent / "try9" / SIDE
        target.parent.mkdir()
        target.write_bytes(source.read_bytes())
    resum(root)
    named = target.relative_to(root / "out" / "runs").as_posix()
    [problem] = verify_package(root)
    assert f"inputs changed since the replay check ({change} {named})" in problem
    done = subprocess.run(
        ["make", "-C", str(root), "-f", "Makefile", "check-installed", f"PYTHON={sys.executable}"],
        capture_output=True,
        text=True,
        env=clean_environment,
    )
    assert done.returncode != 0
    assert named in done.stdout + done.stderr


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
    check_file = json.loads((unpacked / "out" / "runs" / "replay-check.json").read_text(encoding="utf-8"))
    assert check_file["side_files"] == DECLARED
    assert any(path.endswith(SIDE) for path in check_file["hashed_only"])
    assert verify_package(unpacked) == []
