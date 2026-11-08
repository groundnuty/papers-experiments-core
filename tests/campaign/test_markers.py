"""The outcome marker: written atomically, read back as the result it records, and refused when it cannot be trusted."""

import json

import pytest
from campaign_support import LATER, finished, infra

from expcore.campaign.markers import MARKER_NAME, AttemptResult, MarkerError, read_marker, write_marker
from expcore.loop.outcomes import OUTCOMES, HarnessDefect, Outcome


def every_result() -> list[AttemptResult]:
    extra = {
        "infrastructure": {"infra_kind": "knowledge"},
        "error": {"error_type": "ContextOverflow"},
        "harness_defect": {"defect": HarnessDefect("KeyError", "c" * 64)},
    }
    return [AttemptResult(Outcome(name, "d", **extra.get(name, {})), LATER, 2.5) for name in OUTCOMES]


@pytest.mark.parametrize("result", every_result(), ids=lambda r: r.outcome.name)
def test_a_marker_reads_back_the_result_it_was_written_from(tmp_path, result):
    write_marker(tmp_path / "run", "k", 7, result)
    assert read_marker(tmp_path / "run", "k", 7) == result


def test_a_marker_keeps_the_treatment_kinds_the_record_carried(tmp_path):
    result = AttemptResult(Outcome("finished"), LATER, 9.0, treatment_kinds=("sandbox", "endpoint"))
    write_marker(tmp_path, "k", 1, result)
    assert read_marker(tmp_path, "k", 1).treatment_kinds == ("endpoint", "sandbox")
    assert read_marker(tmp_path, "k", 1) == result


def test_a_marker_keeps_usage_and_an_outage_kind(tmp_path):
    result = AttemptResult(
        Outcome("finished"),
        LATER,
        9.0,
        outage_kind="sandbox",
        input_tokens=5,
        cache_read_tokens=3,
        usage_complete=False,
    )
    write_marker(tmp_path, "k", 1, result)
    assert read_marker(tmp_path, "k", 1) == result


def test_writing_a_marker_leaves_no_temporary_file_and_replaces_an_older_marker(tmp_path):
    write_marker(tmp_path, "k", 1, finished())
    write_marker(tmp_path, "k", 2, infra())
    assert [p.name for p in tmp_path.iterdir()] == [MARKER_NAME]
    assert read_marker(tmp_path, "k", 1) is None
    assert read_marker(tmp_path, "k", 2).outcome.name == "infrastructure"


def test_no_marker_reads_as_none(tmp_path):
    assert read_marker(tmp_path / "absent", "k", 1) is None


def test_a_marker_for_another_run_reads_as_none(tmp_path):
    write_marker(tmp_path, "k", 1, finished())
    assert read_marker(tmp_path, "other", 1) is None


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda body: body.update(schema=3), "unknown marker schema 3"),
        (lambda body: body["result"].update(surprise=1), "unknown result fields: surprise"),
        (lambda body: body["result"]["outcome"].update(name="won"), "unknown outcome 'won'"),
        (lambda body: body["result"].pop("wall_s"), "not a valid marker"),
    ],
)
def test_a_marker_of_another_schema_or_with_impossible_content_is_refused(tmp_path, edit, message):
    path = write_marker(tmp_path, "k", 1, finished())
    body = json.loads(path.read_text(encoding="utf-8"))
    edit(body)
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(MarkerError, match=message):
        read_marker(tmp_path, "k", 1)
