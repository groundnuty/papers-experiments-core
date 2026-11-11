"""R14: a scorer that takes `side_files` reads each side file's content as the bytes the loader verified, never by path,
and scores alike through `analyze`, `regenerate`, `rescore_files` and a replication package's `make check`.

The study is the example's copy with side files (`side_file_support.side_study`), given a second scorer, `RangeMeasure`,
which computes its outcome from the content of the run's side file: the count of facts served that the instrument wrote
into it. One campaign is run, checked, analysed and reported for the module; each test works on it or on a copy.
"""

import gzip
import hashlib
import json
from pathlib import Path

import pytest
from package_support import stand_in_vendor
from side_file_support import (
    SIDE,
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
from study_support import HEAD, Campaign, clean_environment, cli, copy_campaign, finish

from expcore.package import build as package_build
from expcore.package.build import build_package, verify_package
from expcore.provenance.codestamp import write_stamp
from expcore.provenance.hashfile import render_hash_file
from expcore.records.record import load_record
from expcore.records.rescore import rescore_files
from expcore.replay import side_files as side_module
from expcore.study import pipeline
from expcore.study.config import load_study
from expcore.study.plugin import StudyContext, load_plugin

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

PRIVATE = "vexmorandel"  # invented; the package's hash file treats it as a name private everywhere

SCORER = '''

class RangeMeasure:
    """A test scorer: a measure computed from the content of the run's side file, which the core hands it verified."""

    name = "ranges"
    version = "1"

    def score(self, record: RunRecord, side_files: Mapping[str, bytes]) -> dict[str, Any]:
        data = side_files.get("extent_ranges.json.gz")
        if data is None:
            return {"names": sorted(side_files), "outcomes": {"logged": None}}
        logged = json.loads(gzip.decompress(data))
        return {
            "names": sorted(side_files),
            "sha256": hashlib.sha256(data).hexdigest(),
            "outcomes": {"logged": float(logged["served"])},
        }
'''


def scoring_study(base: Path) -> Path:
    """The copy with side files, whose plugin also returns `RangeMeasure`, stamped again."""
    root = side_study(base)
    plugin = root / "plugin.py"
    edit(plugin, "\n\n# --- the executor", f"{SCORER}\n\n# --- the executor")
    edit(plugin, "scorers=[TinyScorer()]", "scorers=[TinyScorer(), RangeMeasure()]")
    write_stamp(root, HEAD, load_study(root).scope)
    return root


@pytest.fixture(scope="module", autouse=True)
def stand_in_wheels():
    """Every package built here vendors stand-in wheels."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(package_build, "vendor_wheels", stand_in_vendor)
        yield


@pytest.fixture(scope="module")
def finished(tmp_path_factory, clean_environment) -> Campaign:
    """The study, run to the end and checked, analysed and reported."""
    campaign = Campaign(scoring_study(tmp_path_factory.mktemp("sidescorers")))
    campaign.codes["run"], _ = cli(clean_environment, "run", str(campaign.root))
    finish(clean_environment, campaign)
    return campaign


@pytest.fixture
def copy(finished, tmp_path) -> Campaign:
    return copy_campaign(finished, tmp_path, "copy")


@pytest.fixture(scope="module")
def built(finished, tmp_path_factory) -> Path:
    hashes = tmp_path_factory.mktemp("hashes") / "private.sha256"
    hashes.write_text(render_hash_file([PRIVATE]), encoding="utf-8")
    return build_package(finished.root, tmp_path_factory.mktemp("dist"), hashes=hashes)


@pytest.fixture
def handed(monkeypatch) -> list[dict[str, bytes] | None]:
    """What the analysis hands the scorers of each record it scores, in the order it scores them."""
    seen: list[dict[str, bytes] | None] = []
    real = pipeline.rescore

    def spy(record, scorers, **kwargs):
        given = kwargs.get("side_files")
        seen.append(None if given is None else dict(given))
        return real(record, scorers, **kwargs)

    monkeypatch.setattr(pipeline, "rescore", spy)
    return seen


def scores_bytes(campaign: Campaign) -> bytes:
    return (campaign.root / "out" / "scores.json").read_bytes()


def side_file_of(campaign: Campaign, entry: dict) -> Path:
    """Where the side file of an analysed attempt lies, whether or not its run made one."""
    return campaign.runs / entry["run_dir"] / SIDE


def test_r14_through_analyze_a_scorer_computes_its_measure_from_the_verified_bytes_of_each_side_file(finished):
    """R14: every command passes; each scored record's measure comes from the bytes of the side file beside it, which
    have the hash its record names, and agrees with the instrument's own count; a record with none gets an empty
    mapping."""
    assert finished.codes == {"run": 0, "check": 0, "analyze": 0, "report": 0}
    held = finished.read("scores.json")
    sided = 0
    for entry in finished.read("export.json")["analysed_attempts"]:
        result, tiny = held[entry["run_key"]]["ranges"]["result"], held[entry["run_key"]]["tiny"]["result"]
        side = side_file_of(finished, entry)
        if not side.exists():
            assert result == {"names": [], "outcomes": {"logged": None}}
            continue
        sided += 1
        data = side.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert json.loads(record_beside(side).read_bytes())["instruments"]["extent"]["side_sha256"] == digest
        served = float(json.loads(gzip.decompress(data))["served"])
        assert result == {"names": [SIDE], "sha256": digest, "outcomes": {"logged": served}}
        assert tiny["outcomes"]["served"] in (None, served)
    assert sided
    assert sided < len(held)


def test_r14_analyze_hands_the_scorers_exactly_the_bytes_the_check_hashed(copy, handed):
    """R14: what the analysis hands over for each record is the content of the files the check hashed, keyed by the
    declared pattern, and an empty mapping for a record with none; never nothing."""
    before = scores_bytes(copy)
    code, error = cli({}, "analyze", str(copy.root))
    assert code == 0, error
    assert scores_bytes(copy) == before
    inputs = stored_check(copy.runs)["inputs"]
    hashed = {inputs[rel(copy.runs, path)] for path in side_files(copy.runs)}
    assert handed
    assert None not in handed
    assert {} in handed
    given = [mapping[SIDE] for mapping in handed if mapping]
    assert given
    assert {hashlib.sha256(data).hexdigest() for data in given} <= hashed


def test_r14_regenerate_derives_the_same_scores_byte_for_byte(copy):
    """R14: `regenerate`, which is what a package's `make check` runs, finds every output as `analyze` wrote it."""
    assert pipeline.regenerate(copy.root) == []


def test_r14_rescore_files_over_each_attempt_directory_gives_the_scores_analyze_gave(copy, tmp_path):
    """R14: re-scoring each analysed attempt's directory with the study's declaration hands the scorers the same
    verified bytes, so every record's scores equal those of scores.json, byte for byte as JSON."""
    study = load_study(copy.root)
    plugin = load_plugin(StudyContext(study, {}))
    held = copy.read("scores.json")
    for number, entry in enumerate(copy.read("export.json")["analysed_attempts"]):
        [written] = rescore_files(
            copy.runs / entry["run_dir"], plugin.scorers, out_dir=tmp_path / str(number), side_files=study.side_files
        )
        scored = load_record(written).scores
        assert json.dumps(scored, sort_keys=True) == json.dumps(held[entry["run_key"]], sort_keys=True)


def test_r14_an_unpacked_packages_make_check_derives_the_same_scores_from_its_side_files(
    built, finished, tmp_path, clean_environment
):
    """R14: the package holds the scores `analyze` wrote, and its own check and `make check`, which derive them anew
    from the records and the side files, find them byte for byte."""
    root = unpack(built, tmp_path)
    assert (root / "out" / "scores.json").read_bytes() == scores_bytes(finished)
    assert verify_package(root) == []
    done = make_check(root, clean_environment)
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.parametrize(
    "when", ["after-the-check", "with-the-check-file-edited", "after-the-check-compared-the-batch"]
)
def test_r14_a_side_file_changed_after_the_check_is_refused_before_any_scorer_runs(copy, handed, monkeypatch, when):
    """R14: a side file changed after the check, with the check file edited to vouch for it too, or changed in the
    window between the comparison of the batch with the check and the analysis's own reading, is refused, naming it,
    and no scorer runs: a scorer never sees bytes the loader did not verify."""
    target = side_files(copy.runs)[0]
    named = rel(copy.runs, target)
    expected = {
        "after-the-check": f"inputs changed since the replay check (changed {named})",
        "with-the-check-file-edited": f"{rel(copy.runs, record_beside(target))}: {SIDE} beside the record does not "
        "have the SHA-256 the record names",
        "after-the-check-compared-the-batch": f"{named}: changed after the replay check read it",
    }[when]
    if when == "after-the-check-compared-the-batch":
        scan = pipeline.require_current_check

        def scan_then_change(*args, **kwargs):
            stored = scan(*args, **kwargs)
            changed_log(target)
            return stored

        monkeypatch.setattr(pipeline, "require_current_check", scan_then_change)
    else:
        changed_log(target)
        if when == "with-the-check-file-edited":
            doctor(copy.runs)
    code, error = cli({}, "analyze", str(copy.root))
    assert code == 1
    assert expected in error
    assert handed == []


def test_r14_a_side_file_over_the_cap_is_refused_by_the_analysis_and_by_the_check_before_any_scorer_runs(
    copy, clean_environment, handed, monkeypatch
):
    """R14: under a cap below the side files' size, the analysis refuses a batch the check passed under the real cap,
    naming the file and the cap, before any scorer runs; and the check fails each side file at stage `side_file`."""
    monkeypatch.setattr(side_module, "MAX_SIDE_FILE_BYTES", 8)
    code, error = cli({}, "analyze", str(copy.root))
    assert code == 1
    assert f"{rel(copy.runs, side_files(copy.runs)[0])}: " in error
    assert "more than 8 bytes" in error
    assert handed == []
    code, _ = cli(clean_environment, "check", str(copy.runs))
    assert code == 1
    failures = [f for f in stored_check(copy.runs)["failures"] if f["stage"] == "side_file"]
    assert sorted(f["file"] for f in failures) == [rel(copy.runs, path) for path in side_files(copy.runs)]
    assert all("more than 8 bytes" in f["error"] for f in failures)
