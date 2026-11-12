"""The plan of a progressive ingest: which files arrive when, from which staged source, and what the dataset holds
between runs (O27)."""

import hashlib
import json

import pytest

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.ingestplan import IngestPlan, fingerprint, load_ingest_plan
from expcore_onedata.testing import with_placement, write_platform

DAY = bytes(range(256)) * 40  # 10240 bytes
NOTE = b"a short note\n" * 30


def two_providers(data):
    with_placement(data)
    data["support"]["providers"] = ["alpha", "beta"]


@pytest.fixture
def platform(tmp_path, fake):
    """The test platform with the space supported by both providers; `fake` sets the admin credential it reads."""
    return load_platform(write_platform(tmp_path, two_providers))


def staged(tmp_path, files=None):
    """A staging directory with two sources."""
    directory = tmp_path / "staged"
    directory.mkdir(exist_ok=True)
    for name, content in (files or {"day-13.csv": DAY, "note.txt": NOTE}).items():
        (directory / name).write_bytes(content)
    return directory


def plan_file(tmp_path, body):
    path = tmp_path / "ingest.toml"
    path.write_text(body, encoding="utf-8")
    return path


PLAN = """
[ingest]
staging = "staged"

[[ingest.files]]
label = "day-13"
path = "records/day-13.csv"
source = "day-13.csv"
at_s = 30
baseline = "complete"
parts = 4
over_s = 60

[[ingest.files]]
label = "note"
path = "records/note.txt"
source = "note.txt"
at_s = 10
baseline = "absent"
"""


def test_o27_a_plan_names_each_file_its_source_its_time_and_its_baseline(tmp_path, platform):
    """O27: the plan reads each file's label, path, staged source (hashed), arrival time, parts and baseline; the
    steps are the parts in the order they fall due, each ending at its own byte and carrying the hash of its prefix."""
    staged(tmp_path)
    plan = load_ingest_plan(plan_file(tmp_path, PLAN), platform)
    assert isinstance(plan, IngestPlan)
    day, note = plan.files
    assert (day.label, day.path, day.size, day.baseline, day.parts) == (
        "day-13",
        "records/day-13.csv",
        10240,
        "complete",
        4,
    )
    assert day.sha256 == hashlib.sha256(DAY).hexdigest()
    assert (note.parts, note.over_s, note.baseline) == (1, 0.0, "absent")
    steps = plan.steps()
    assert [(s.label, s.part, s.due_s, s.stop) for s in steps] == [
        ("note", 1, 10.0, len(NOTE)),
        ("day-13", 1, 30.0, 2560),
        ("day-13", 2, 50.0, 5120),
        ("day-13", 3, 70.0, 7680),
        ("day-13", 4, 90.0, 10240),
    ]
    assert all(s.sha256 == hashlib.sha256((DAY if s.label == "day-13" else NOTE)[: s.stop]).hexdigest() for s in steps)
    assert plan.visible_on is None  # every provider of the space must see each arrival


def test_o27_the_plan_summary_names_files_by_label_and_holds_no_path(tmp_path, platform):
    """O27: what a record keeps of the plan names each file by its label, never by its path or its source."""
    staged(tmp_path)
    summary = json.dumps(load_ingest_plan(plan_file(tmp_path, PLAN), platform).summary())
    assert "records" not in summary
    assert "staged" not in summary
    assert ".csv" not in summary
    assert '"day-13"' in summary


def test_o27_the_fingerprint_follows_the_staged_content_and_the_schedule(tmp_path, platform):
    """O27: the plan is part of the campaign fingerprint, so a resume after its schedule or a staged source changed is
    refused; the fingerprint holds a hash per plan, never a path or a name."""
    staged(tmp_path)
    first = fingerprint({"arriving": load_ingest_plan(plan_file(tmp_path, PLAN), platform)})
    assert set(first) == {"progressive_ingest"}
    assert "records" not in json.dumps(first)
    later = fingerprint(
        {"arriving": load_ingest_plan(plan_file(tmp_path, PLAN.replace("at_s = 10", "at_s = 11")), platform)}
    )
    staged(tmp_path, {"day-13.csv": DAY[:-1] + b"?", "note.txt": NOTE})
    changed = fingerprint({"arriving": load_ingest_plan(plan_file(tmp_path, PLAN), platform)})
    assert len({json.dumps(f, sort_keys=True) for f in (first, later, changed)}) == 3


def test_o27_an_empty_plan_is_allowed(tmp_path, platform):
    """O27: a plan with no files is a sham control: it loads, and has no steps."""
    staged(tmp_path)
    plan = load_ingest_plan(plan_file(tmp_path, '[ingest]\nstaging = "staged"\n'), platform)
    assert (plan.files, plan.steps()) == ((), ())


@pytest.mark.parametrize(
    ("edit", "key"),
    [
        (lambda t: t.replace('baseline = "absent"', ""), "baseline"),
        (lambda t: t.replace('baseline = "absent"', 'baseline = "kept"'), "baseline"),
        (lambda t: t.replace('label = "note"', 'label = "day-13"'), "label"),
        (lambda t: t.replace('label = "note"', 'label = "9 lives"'), "label"),
        (lambda t: t.replace('path = "records/note.txt"', 'path = "records/day-13.csv"'), "path"),
        (lambda t: t.replace('path = "records/note.txt"', 'path = "records/day-13.csv/inner"'), "path"),
        (lambda t: t.replace('path = "records/note.txt"', 'path = "../note.txt"'), "path"),
        (lambda t: t.replace('source = "note.txt"', 'source = "../ingest.toml"'), "source"),
        (lambda t: t.replace('source = "note.txt"', 'source = "missing.txt"'), "source"),
        (lambda t: t.replace("at_s = 10", "at_s = -1"), "at_s"),
        (lambda t: t.replace("at_s = 10", 'at_s = "soon"'), "at_s"),
        (lambda t: t.replace("parts = 4", "parts = 0"), "parts"),
        (lambda t: t.replace("parts = 4", "parts = 20000"), "parts"),
        (lambda t: t.replace("over_s = 60", "over_s = 0"), "over_s"),
        (lambda t: t.replace('baseline = "absent"', 'baseline = "absent"\nover_s = 5'), "over_s"),
        (lambda t: t.replace('baseline = "absent"', 'baseline = "absent"\nwhen = 5'), "when"),
        (lambda t: t.replace('staging = "staged"', 'staging = "elsewhere"'), "staging"),
        (lambda t: t.replace('staging = "staged"', 'staging = "staged"\nvisible_on = ["gamma"]'), "visible_on"),
        (lambda t: t.replace('staging = "staged"', 'staging = "staged"\nspace = "nowhere"'), "space"),
    ],
)
def test_o27_a_plan_that_cannot_be_carried_out_is_refused_naming_the_file_and_the_key(tmp_path, platform, edit, key):
    """O27: each mistake is refused at load, naming the plan's file and the key, before any run."""
    staged(tmp_path)
    with pytest.raises(ConfigError, match=f"key '{key}'"):
        load_ingest_plan(plan_file(tmp_path, edit(PLAN)), platform)


def test_o27_a_provider_that_must_see_arrivals_must_support_the_space(tmp_path, fake):
    """O27: `visible_on` names declared providers of the plan's space; one that does not support it is refused."""
    platform = load_platform(write_platform(tmp_path, with_placement))  # the space has the data provider only
    staged(tmp_path)
    with pytest.raises(ConfigError, match="key 'visible_on'"):
        load_ingest_plan(
            plan_file(tmp_path, PLAN.replace('staging = "staged"', 'staging = "staged"\nvisible_on = ["beta"]')),
            platform,
        )
