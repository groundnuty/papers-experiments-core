"""O97: what a cell's deadline says on the call that ran it, and the digest of it a run's ending carries. The call keeps
the deadline cut to the keys the record's schema holds; a call the loop cut returns no output, so the first survivor
reaches the record by the run's ending, as one line of at most 300 characters that holds no path."""

import copy

from expcore.records.cell_deadline import problem
from expcore_onedata.sandbox import calldeadline, deadline, kinds

SURVIVOR = {
    "comm": "python3",
    "state": "D",
    "tasks": [{"state": "D", "wchan": "request_wait_answer", "syscall": {"nr": 0, "fd": 3}}],
    "waits_on": [{"fd": 3, "mount": "data", "space": "space-a", "path": "reports/owner-only.bin", "pos": 0}],
}
RECORD = {
    "utc": "2026-11-20T19:00:00.000+00:00", "grace_s": 5.0, "margin_s": 30.0, "outcome": "mount_aborted", "cell": 2,
    "why": "a process of the cell outlived its kill", "survivors": [SURVIVOR],
    "steps": [{"step": "abort", "t_s": 5.0, "ok": True, "method": "fusectl"}],
}  # fmt: skip


def test_o97_a_clean_kill_a_missing_record_and_an_undecided_one_have_nothing_to_say():
    """O97: a call whose cell was killed cleanly, one with no record, and a record never decided (no time of its cap,
    as the runner's `survived_kill` of a wait that gave up) carry no field."""
    assert calldeadline.cell_deadline_of(None) is None
    assert calldeadline.cell_deadline_of({}) is None
    assert calldeadline.cell_deadline_of({**RECORD, "outcome": deadline.KILLED}) is None
    assert calldeadline.cell_deadline_of({"outcome": "survived_kill", "steps": [], "why": "x"}) is None


def test_o97_the_call_keeps_only_the_keys_of_the_schema_and_a_copy_of_them():
    """O97: the runner's own bookkeeping (`cell`, `why`) is not the call's; what is kept is a deep copy, and it passes
    the record's own check."""
    kept = calldeadline.cell_deadline_of(RECORD)
    assert set(kept) == {"outcome", "utc", "grace_s", "margin_s", "steps", "survivors"}
    assert problem(kept) is None
    before = copy.deepcopy(kept)
    kept["steps"][0]["ok"] = False
    kept["survivors"][0]["state"] = "S"
    assert calldeadline.cell_deadline_of(RECORD)["steps"][0]["ok"] is True
    assert before["survivors"][0]["state"] == "D"


def test_o97_a_record_the_schema_would_refuse_is_cut_to_what_it_accepts_or_left_out():
    """O97: a step the schema refuses (a value that is not plain JSON) cuts the field down to its names and times; a
    record nothing of which passes has no field."""
    cut = calldeadline.cell_deadline_of({**RECORD, "steps": [{"step": "abort", "value": object()}, {"step": "ended"}]})
    assert cut["steps"] == [{"step": "abort"}, {"step": "ended"}]
    assert problem(cut) is None
    assert calldeadline.cell_deadline_of({**RECORD, "outcome": "Not A Name", "steps": []}) is None


def test_o97_the_digest_names_the_first_survivor_what_it_waited_on_and_the_abort_and_no_path():
    seen = calldeadline.digest(RECORD)
    assert (
        seen
        == "survivor python3 state D; wchan request_wait_answer; blocked on the data mount, fd 3; abort fusectl: ok"
    )
    assert "owner-only" not in seen  # no path of a file
    assert "space-a" not in seen


def test_o97_the_digest_is_empty_without_a_reading_and_never_longer_than_its_cap():
    assert calldeadline.digest({"outcome": "survived_kill", "steps": []}) == ""
    assert calldeadline.digest({"survivors": []}) == ""
    assert calldeadline.digest({"survivors": ["x"]}) == ""
    long = {**RECORD, "survivors": [{**SURVIVOR, "comm": "x" * 500}]}
    assert len(calldeadline.digest(long)) == calldeadline.DIGEST_CHARS == 300


def test_o97_a_runs_ending_carries_the_digest_in_its_detail_and_one_with_no_reading_is_unchanged():
    """O97: a call the loop cut returns no output, so the run's ending is the only carrier: its detail holds the digest
    in brackets after the outcome; a record that read no survivor has the detail it always had."""
    with_digest = kinds.run_ended_error(RECORD)
    assert with_digest.kind == kinds.CELL_UNKILLABLE
    assert "[survivor python3 state D; wchan request_wait_answer;" in with_digest.detail
    assert "cell 2's deadline (mount_aborted)" in with_digest.detail
    plain = {k: v for k, v in RECORD.items() if k != "survivors"}
    assert kinds.run_ended_error(plain).detail == (
        "the run was ended at cell 2's deadline (mount_aborted): a process of the cell outlived its kill; "
        "the cell's own result stands: nothing more runs"
    )
    assert "[" not in kinds.run_ended_error({"outcome": "survived_kill"}).detail
