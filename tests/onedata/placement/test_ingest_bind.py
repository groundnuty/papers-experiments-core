"""A recorded ingest snapshot binds to a plan by the plan's content: its hash, each file's schedule, size, source and
prefix, its mode and its parts' due times, never by the files' labels alone (O27, O44, #262)."""

import copy
import json

import pytest

from expcore_onedata.config import load_platform
from expcore_onedata.ingestbind import PlanMismatch, check_recorded_plan
from expcore_onedata.ingestplan import load_ingest_plan
from expcore_onedata.testing import with_placement, write_platform

DAY = bytes(range(256)) * 40  # 10240 bytes
NOTE = b"a short note\n" * 30


def two_providers(data):
    with_placement(data)
    data["support"]["providers"] = ["alpha", "beta"]


@pytest.fixture
def platform(tmp_path, fake):
    return load_platform(write_platform(tmp_path, two_providers))


def plan_of(tmp_path, platform, *, mode="arrival", day_at=30, note_at=10, prefix=1024, day_path="records/day-13.csv"):
    """Load a plan of two files, a day in four parts and a note, over the staged sources as they are now."""
    staging = tmp_path / "staged"
    staging.mkdir(exist_ok=True)
    if not (staging / "day-13.csv").exists():  # a test that changes the staged day does so after the first load
        (staging / "day-13.csv").write_bytes(DAY)
    (staging / "note.txt").write_bytes(NOTE)
    sparse = mode == "preallocated"
    rows = [
        {"label": "day-13", "path": day_path, "source": "day-13.csv", "at_s": day_at, "baseline": "complete"},
        {"label": "note", "path": "records/note.txt", "source": "note.txt", "at_s": note_at, "baseline": "absent"},
    ]
    rows[0].update(parts=4, over_s=60, **({"prefix": prefix} if sparse else {}))
    if sparse:
        rows[1].update(baseline="complete", **{"prefix": 0})
    body = f'[ingest]\nstaging = "staged"\nmode = "{mode}"\n' + "".join(
        "\n[[ingest.files]]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in row.items()) for row in rows
    )
    path = tmp_path / "ingest.toml"
    path.write_text(body, encoding="utf-8")
    return load_ingest_plan(path, platform)


def snapshot_of(plan):
    """What a finished instrument's snapshot holds of its plan: the summary, and one `planned` reading per part."""
    planned = [
        {"step": "planned", "label": s.label, "part": s.part, "parts": s.parts, "due_s": s.due_s}
        | {"bytes": s.stop, "sha256": s.sha256}
        for s in plan.steps()
    ]
    return {
        "version": 1,
        "ok": True,
        "start": {"ok": True},
        "plan": plan.summary(),
        "steps": planned,
        "files": {f.label: {"baseline": f.baseline, "state": "not_arrived"} for f in plan.files},
    }


def refusal(plan, snapshot):
    with pytest.raises(PlanMismatch) as caught:
        check_recorded_plan(plan, snapshot)
    return caught.value


def test_o27_the_plan_that_made_a_snapshot_binds_to_it(tmp_path, platform):
    """O27: the plan a snapshot was recorded under, loaded again, binds: its hash, files and parts all agree."""
    plan = plan_of(tmp_path, platform)
    check_recorded_plan(plan, snapshot_of(plan))
    check_recorded_plan(plan_of(tmp_path, platform), snapshot_of(plan))  # loaded again from its file


def test_o27_a_plan_whose_schedule_is_the_snapshots_plus_300_seconds_is_refused_naming_the_file_and_at_s(
    tmp_path, platform
):
    """O27 (#262): the same labels, sizes and sources with every part 300 s later bound by labels alone; it is
    refused, and the refusal names the file and the field that differs."""
    recorded = snapshot_of(plan_of(tmp_path, platform))
    later = plan_of(tmp_path, platform, day_at=330, note_at=310)
    error = refusal(later, recorded)
    assert (error.label, error.field) == ("day-13", "at_s")
    assert all(word in str(error) for word in ("day-13", "at_s", "330", "30"))


def test_o27_a_difference_in_one_file_alone_names_that_file(tmp_path, platform):
    """O27 (#262): the note's time moved and the day's did not: the refusal names the note."""
    recorded = snapshot_of(plan_of(tmp_path, platform))
    error = refusal(plan_of(tmp_path, platform, note_at=11), recorded)
    assert (error.label, error.field) == ("note", "at_s")


@pytest.mark.parametrize(
    ("field", "edit"),
    [
        ("over_s", lambda s: s["plan"]["files"][0].update(over_s=61.0)),
        ("parts", lambda s: s["plan"]["files"][0].update(parts=5)),
        ("baseline", lambda s: s["plan"]["files"][1].update(baseline="complete")),
        ("bytes", lambda s: s["plan"]["files"][0].update(bytes=10241)),
        ("sha256", lambda s: s["plan"]["files"][0].update(sha256="0" * 64)),
    ],
)
def test_o27_a_changed_size_source_part_count_spacing_or_baseline_is_refused_naming_the_field(
    tmp_path, platform, field, edit
):
    """O27 (#262): each field of a planned file that the snapshot records and the plan differs in is refused, the
    file and the field named, even when the recorded hash is the plan's own."""
    plan = plan_of(tmp_path, platform)
    recorded = snapshot_of(plan)
    edit(recorded)
    error = refusal(plan, recorded)
    assert error.field == field
    assert error.label in ("day-13", "note")
    assert all(word in str(error) for word in (error.label, field))


def test_o27_a_plan_over_another_staged_source_is_refused_naming_its_size_or_its_hash(tmp_path, platform):
    """O27 (#262): a plan whose staged day has the same size and other bytes differs in `sha256`, and one with a
    shorter day in `bytes`."""
    recorded = snapshot_of(plan_of(tmp_path, platform))
    (tmp_path / "staged" / "day-13.csv").write_bytes(DAY[:-1] + b"!")
    assert refusal(plan_of(tmp_path, platform), recorded).field == "sha256"
    (tmp_path / "staged" / "day-13.csv").write_bytes(DAY[:-1])
    error = refusal(plan_of(tmp_path, platform), recorded)
    assert (error.label, error.field) == ("day-13", "bytes")


def test_o44_a_changed_prefix_is_refused_naming_the_file_and_prefix(tmp_path, platform):
    """O44 (#262): a preallocated plan with another prefix for the day differs from the snapshot in `prefix`."""
    recorded = snapshot_of(plan_of(tmp_path, platform, mode="preallocated", prefix=1024))
    error = refusal(plan_of(tmp_path, platform, mode="preallocated", prefix=2048), recorded)
    assert (error.label, error.field) == ("day-13", "prefix")


def test_o44_a_plan_of_the_other_mode_is_refused_naming_the_mode(tmp_path, platform):
    """O44 (#262): an arrival snapshot does not bind to a preallocated plan, nor the other way round."""
    arrival = plan_of(tmp_path, platform)
    preallocated = plan_of(tmp_path, platform, mode="preallocated")
    assert refusal(preallocated, snapshot_of(arrival)).field == "mode"
    assert refusal(arrival, snapshot_of(preallocated)).field == "mode"


def test_o27_a_snapshot_from_before_the_mode_existed_is_an_arrival_snapshot_and_binds_to_an_arrival_plan_alone(
    tmp_path, platform
):
    """O27: a snapshot recorded before `mode` and `prefix` were kept holds neither key; every one of them came from an
    arrival plan, whose hash has not changed since, so it binds to the plan that made it and not to a preallocated
    one."""
    plan = plan_of(tmp_path, platform)
    old = snapshot_of(plan)
    del old["plan"]["mode"]
    for planned in old["plan"]["files"]:
        del planned["prefix"]
    check_recorded_plan(plan, old)
    error = refusal(plan_of(tmp_path, platform, mode="preallocated"), old)
    assert error.field == "mode"


def test_o27_a_snapshot_that_records_no_plan_or_no_hash_is_refused_as_unverifiable(tmp_path, platform):
    """O27 (#262): unknown is not a pass: a snapshot without a plan, without its hash, or with a hash that is not text
    cannot be bound, even when the files it lists would agree."""
    plan = plan_of(tmp_path, platform)
    for edit in (
        lambda s: s.pop("plan"),
        lambda s: s.update(plan=None),
        lambda s: s["plan"].pop("sha256"),
        lambda s: s["plan"].update(sha256=""),
        lambda s: s["plan"].update(sha256=7),
    ):
        recorded = snapshot_of(plan)
        edit(recorded)
        error = refusal(plan, recorded)
        assert "unknown is not a pass" in str(error)
    with pytest.raises(PlanMismatch, match="unknown is not a pass"):
        check_recorded_plan(plan, None)


def test_o27_a_difference_the_summary_does_not_itemise_is_refused_by_the_hash(tmp_path, platform):
    """O27 (#262): a file's path is not in a record, so a plan that differs only there agrees in every itemised field
    and is refused by the hash, which says where the difference must lie."""
    recorded = snapshot_of(plan_of(tmp_path, platform))
    error = refusal(plan_of(tmp_path, platform, day_path="records/day-14.csv"), recorded)
    assert (error.label, error.field) == (None, "sha256")
    assert all(word in str(error) for word in ("path", "visible_on"))


def test_o27_a_file_the_plan_has_and_the_snapshot_lacks_or_the_other_way_is_refused_naming_it(tmp_path, platform):
    """O27 (#262): a label on one side only, a label twice, and files in another order are each refused."""
    plan = plan_of(tmp_path, platform)
    short = snapshot_of(plan)
    short["plan"]["files"].pop()
    assert (refusal(plan, short).label, refusal(plan, short).field) == ("note", "label")
    extra = snapshot_of(plan)
    extra["plan"]["files"].append({**extra["plan"]["files"][1], "label": "ghost"})
    assert (refusal(plan, extra).label, refusal(plan, extra).field) == ("ghost", "label")
    twice = snapshot_of(plan)
    twice["plan"]["files"].append(copy.deepcopy(twice["plan"]["files"][0]))
    assert refusal(plan, twice).field == "label"
    swapped = snapshot_of(plan)
    swapped["plan"]["files"].reverse()
    assert refusal(plan, swapped).field == "label"


def test_o27_a_recorded_field_that_is_missing_or_of_another_type_is_refused(tmp_path, platform):
    """O27 (#262): a file entry without `at_s`, with `at_s` as text or a boolean, or that is not a mapping is a
    difference, never a pass; a hash alone is not enough."""
    plan = plan_of(tmp_path, platform)
    for edit in (
        lambda s: s["plan"]["files"][0].pop("at_s"),
        lambda s: s["plan"]["files"][0].update(at_s="30"),
        lambda s: s["plan"]["files"][0].update(at_s=True),
        lambda s: s["plan"].update(files=None),
        lambda s: s["plan"]["files"].__setitem__(0, "day-13"),
        lambda s: s["plan"]["files"][0].update(at_s=float("nan")),
    ):
        recorded = snapshot_of(plan)
        edit(recorded)
        refusal(plan, recorded)


def test_o27_a_part_recorded_at_another_time_is_refused_though_the_summary_agrees(tmp_path, platform):
    """O27 (#262): the readings an analysis pairs with the plan are checked too: a part whose due time, part count or
    bytes the plan does not have, a part the plan has no such number of, and a start that set its state but recorded
    only some of the plan's parts are each refused, naming the file and the field."""
    plan = plan_of(tmp_path, platform)
    cases = [
        (lambda s: s["steps"][2].update(due_s=s["steps"][2]["due_s"] + 300), "due_s"),
        (lambda s: s["steps"][2].update(parts=3), "parts"),
        (lambda s: s["steps"][2].update(bytes=1), "bytes"),
        (lambda s: s["steps"][2].update(sha256="0" * 64), "sha256"),
        (lambda s: s["steps"][2].update(part=9), "part"),
        (lambda s: s["steps"][1].update(part=True), "part"),  # a boolean is not part 1
        (lambda s: s["steps"][2].update(label="ghost"), "label"),
        (lambda s: s["steps"].pop(), "steps"),
        (lambda s: s["steps"].append(copy.deepcopy(s["steps"][0])), "steps"),
        (lambda s: s.pop("steps"), "steps"),
    ]
    for edit, field in cases:
        recorded = snapshot_of(plan)
        edit(recorded)
        assert refusal(plan, recorded).field == field, field


def test_o27_a_snapshot_whose_start_failed_binds_with_the_parts_it_never_recorded(tmp_path, platform):
    """O27: a start that did not set the start state records no part, and the plan it ran still binds to it; the
    readings it does hold are checked all the same."""
    plan = plan_of(tmp_path, platform)
    recorded = snapshot_of(plan)
    recorded.update(ok=False, start={"ok": False, "detail": "refused"}, steps=[])
    check_recorded_plan(plan, recorded)
    later = plan_of(tmp_path, platform, day_at=330, note_at=310)
    assert refusal(later, recorded).field == "at_s"


def test_o27_the_readings_of_a_run_bind_by_their_due_times_not_their_labels(tmp_path, platform):
    """O27 (#262): written, confirmed and not-due readings carry the part's label, number, count and due time, and
    each is checked against the plan's part."""
    plan = plan_of(tmp_path, platform)
    recorded = snapshot_of(plan)
    first = recorded["steps"][0]
    recorded["steps"].append({**first, "step": "written", "late_s": 0.1, "t_rel_s": first["due_s"] + 0.1})
    check_recorded_plan(plan, recorded)
    recorded["steps"][-1]["due_s"] += 300
    assert refusal(plan, recorded).field == "due_s"
