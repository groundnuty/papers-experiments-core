"""O22: the bytes a run's agent read through its data path, counted once from the stopped gate's log; unknown, not 0.

The count is a pure function of the gate's log lines and of what the gate's stop found (`gatelog.served`); the sandbox
takes it when its collection has stopped the gate, and the platform's instrument records it. Every line here is one
the gate writes, with invented values; the sandbox runs over the in-memory host.
"""

import json

import pytest
from sandbox_support import COMPLETE_LEVEL, add_sandbox, make_spec, start_options

from expcore.records.instruments import finish_instruments, start_instruments
from expcore_onedata.config import load_platform
from expcore_onedata.sandbox import gatelog, served
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.sandbox.served import NAMESPACE, ServedBytes
from expcore_onedata.testing import write_platform

STOPPED = {
    "signalled": True,
    "exit_code": 0,
    "output_read": True,
    "log_write_failed": False,
    "teardown_incomplete": False,
}
ALPHA, BRAVO, CHARLIE = "fileidalpha", "fileidbravo", "fileidcharlie"  # invented file ids
FOLDER, NOWHERE = "diridfolder", "diridnowhere"  # invented directory ids
TABLES = ("files", "files_total", "files_cut", "listings", "listings_total", "listings_cut")
DEFAULT_CAPS = {"max_files": 1000, "max_directories": 1000, "max_ranges_per_file": 64}  # without [sandbox.served_bytes]


def content(file_id: str) -> str:
    return f"/api/v3/oneprovider/data/{file_id}/content"


def children(dir_id: str) -> str:
    return f"/api/v3/oneprovider/data/{dir_id}/children"


def read(file_id: str, size: int, first: int | None = None) -> dict:
    """The gate's line of one answer of a file's content: a 200 from the start, or a 206 from `first`."""
    line = {"method": "GET", "path": content(file_id), "tag": "content", "status": 200, "body": "content"}
    if first is not None:
        line |= {"status": 206, "content_range": f"bytes {first}-{first + size - 1}/100000"}
    return line | {"body_bytes": size}


def listed(dir_id: str, size: int, status: int = 200) -> dict:
    """The gate's line of one answer of a listing: the platform's 200 is a listing, its error another answer."""
    body = "listing" if status == 200 else "other"
    return {
        "method": "GET",
        "path": children(dir_id),
        "tag": "children",
        "status": status,
        "body": body,
        "body_bytes": size,
    }


CUT = {"status": 502, "upstream_status": 206, "content_range": "bytes 100-199/5000", "cut": True, "unread_bytes": 60}
ANSWERS = [  # a run's answers, as the gate logs them
    read(ALPHA, 1000),
    read(ALPHA, 24, first=10),
    read(BRAVO, 40, first=100) | CUT,  # a transfer cut after 40 of its 100 bytes
    listed(FOLDER, 300) | {"complete_listing": True},
    {
        "method": "GET",
        "path": f"/api/v3/oneprovider/data/{CHARLIE}",  # a file only looked at: its attributes, never its content
        "tag": "attrs",
        "status": 200,
        "body": "other",
        "body_bytes": 90,
    },
    listed(NOWHERE, 50, status=404),
    {"method": "PUT", "path": content(CHARLIE), "decision": "deny", "status": 403, "body": "gate", "body_bytes": 70},
    {"event": "request_error", "status": 400, "body": "gate", "body_bytes": 11},
    {"event": "request_error", "status": None, "reason": "request headers not received in time"},
    {"event": "conn_close", "conn": "1-1", "after_request": 3, "ended": "eof"},
    {"event": "conn_close", "conn": "1-2", "after_request": 4, "ended": "teardown", "in_request": False},
    {"event": "connection_error", "error_origin": "client"},
    {"event": "teardown_complete", "connections": 1},  # the gate's last line: every close line was written
]
UNMARKED = ANSWERS[:-1]  # the same answers from a gate that never wrote its teardown mark


def log(*records) -> list[str]:
    return [json.dumps(r) for r in records]


def test_o22_the_bytes_served_add_up_each_answer_by_what_its_body_was():
    """O22: the platform's file content (a cut transfer's part included), its listings and its other answers make the
    bytes served; the gate's own answers are counted apart, and lines that served nothing count nothing."""
    got = gatelog.served(log(*ANSWERS), STOPPED)
    assert got == {
        "served_bytes": 1000 + 24 + 40 + 300 + 90 + 50,
        "content_bytes": 1064,
        "listing_bytes": 300,
        "other_bytes": 140,
        "gate_answer_bytes": 81,
        "files": {
            ALPHA: {
                "path": content(ALPHA),
                "requests": 2,
                "ranges": [[0, 1000]],
                "range_bytes": 1000,
                "ranges_cut": False,
                "body_bytes": 1024,
            },
            BRAVO: {
                "path": content(BRAVO),
                "requests": 1,
                "ranges": [[100, 140]],
                "range_bytes": 40,
                "ranges_cut": False,
                "body_bytes": 40,
            },
        },
        "files_total": 2,
        "files_cut": False,
        "listings": {FOLDER: {"path": children(FOLDER), "requests": 1, "body_bytes": 300}},
        "listings_total": 1,
        "listings_cut": False,
        "caps": DEFAULT_CAPS,
        "before_run_start": None,  # no mark of the run's start: nothing is counted apart (O24)
        "unknown": [],
    }


@pytest.mark.parametrize(
    ("lines", "stop", "reason"),
    [
        (None, STOPPED, "gate_log_unreadable"),
        (log(*ANSWERS), None, "gate_not_stopped"),
        (log(*ANSWERS), {**STOPPED, "signalled": False}, "gate_not_stopped"),
        (log(*ANSWERS), {k: v for k, v in STOPPED.items() if k != "exit_code"}, "gate_not_stopped"),
        (log(*ANSWERS), {**STOPPED, "log_write_failed": True}, "gate_log_write_failed"),
        (log(*ANSWERS), {k: v for k, v in STOPPED.items() if k != "teardown_incomplete"}, "gate_teardown_incomplete"),
        (
            log(*ANSWERS, {"event": "conn_close", "ended": "teardown", "in_request": True}),
            STOPPED,
            "request_in_flight_at_stop",
        ),
        ([*log(*ANSWERS), '{"method": "GET", "tag": "content", "body_by'], STOPPED, "gate_log_malformed"),
        (log(*UNMARKED, {"method": "GET", "tag": "attrs", "status": 200}, ANSWERS[-1]), STOPPED, "gate_log_malformed"),
        (log(*UNMARKED, {"method": "GET", "status": 200, "body_bytes": 5}, ANSWERS[-1]), STOPPED, "gate_log_malformed"),
        (
            log(*UNMARKED, {"method": "GET", "body": "content", "body_bytes": -1}, ANSWERS[-1]),
            STOPPED,
            "gate_log_malformed",
        ),
        (log(*ANSWERS, ["not", "an", "object"]), STOPPED, "gate_log_malformed"),
        (log(*UNMARKED, read(ALPHA, 5) | {"path": None}, ANSWERS[-1]), STOPPED, "gate_log_malformed"),
        (log(*UNMARKED, read(ALPHA, 5) | {"path": children(ALPHA)}, ANSWERS[-1]), STOPPED, "gate_log_malformed"),
        (log(*UNMARKED, listed(FOLDER, 5) | {"path": content(FOLDER)}, ANSWERS[-1]), STOPPED, "gate_log_malformed"),
        (log(*UNMARKED), {**STOPPED, "exit_code": 137}, "gate_teardown_unconfirmed"),
        (log(*UNMARKED), STOPPED, "gate_teardown_unconfirmed"),
        (log(*ANSWERS), {**STOPPED, "output_read": False}, "gate_output_unreadable"),
        (log(*ANSWERS), {k: v for k, v in STOPPED.items() if k != "output_read"}, "gate_output_unreadable"),
    ],
    ids=[
        "unreadable",
        "no-stop",
        "not-signalled",
        "not-exited",
        "log-write-failed",
        "teardown-unknown",
        "in-flight",
        "cut-line",
        "no-body-bytes",
        "no-body-kind",
        "negative",
        "not-an-object",
        "content-no-path",
        "content-not-a-file",
        "listing-not-a-directory",
        "killed-no-mark",
        "exited-no-mark",
        "output-not-read",
        "output-unknown",
    ],
)
def test_o22_a_count_that_may_be_missing_a_line_is_unknown_never_zero(lines, stop, reason):
    """O22: a log that may lack a line, or holds one that does not say what it served, makes every count None with the
    reason, never a partial sum taken for the whole: the tables of the files and directories too, never a part of them
    taken for the whole (an answer of a file's content or a listing must name the file or directory it served)."""
    got = gatelog.served(lines, stop)
    assert got["unknown"] == [reason]
    assert all(got[field] is None for field in gatelog.SERVED_FIELDS)
    assert all(got[field] is None for field in TABLES)


def tables(*records) -> dict:
    """The tables of a whole log of these answers, from a gate that stopped cleanly."""
    got = gatelog.served(log(*records, ANSWERS[-1]), STOPPED)
    assert got["unknown"] == []
    return got


def test_o22_two_files_read_in_overlapping_ranges_each_keep_their_own_merged_ranges():
    """O22: each file read through the gate has its own row: the answers of its content, the byte ranges they served
    merged where they overlap or touch, the bytes in those ranges, and the body bytes of every answer; a range of one
    file never merges with another file's. The rows add up to the run's content bytes."""
    got = tables(
        read(ALPHA, 100, first=0),
        read(BRAVO, 100, first=200),
        read(ALPHA, 100, first=50),
        read(BRAVO, 150, first=250),
        read(BRAVO, 50, first=600),
        read(BRAVO, 10, first=400),  # touches 250-399: one range
    )
    assert got["files"] == {
        ALPHA: {
            "path": content(ALPHA),
            "requests": 2,
            "ranges": [[0, 150]],
            "range_bytes": 150,
            "ranges_cut": False,
            "body_bytes": 200,
        },
        BRAVO: {
            "path": content(BRAVO),
            "requests": 4,
            "ranges": [[200, 410], [600, 650]],
            "range_bytes": 260,
            "ranges_cut": False,
            "body_bytes": 310,
        },
    }
    assert (got["files_total"], got["files_cut"]) == (2, False)
    assert sum(row["body_bytes"] for row in got["files"].values()) == got["content_bytes"]


def test_o22_a_file_read_again_counts_every_request_and_its_bytes_once_in_its_ranges():
    """O22: a file read whole twice and then by a range inside it was served its bytes three times: three requests and
    every body byte, but its unique ranges hold each byte once, which is what a claimed read is checked against."""
    got = tables(read(ALPHA, 500), read(ALPHA, 500), read(ALPHA, 100, first=100))
    row = got["files"][ALPHA]
    assert (row["requests"], row["body_bytes"]) == (3, 1100)
    assert (row["ranges"], row["range_bytes"]) == ([[0, 500]], 500)


def test_o22_a_file_only_looked_at_is_not_in_the_table_and_a_listing_counts_under_its_directory():
    """O22: a file whose attributes were read, whose content read was refused or answered with the platform's error,
    served none of its content, so it has no row. A listing's answers count under the directory listed, keyed by the
    directory's id as its path names it; a listing the platform answered with an error is no listing."""
    attributes = {"method": "GET", "path": f"/api/v3/oneprovider/data/{CHARLIE}", "tag": "attrs", "status": 200}
    refused = {"method": "GET", "path": content(CHARLIE), "decision": "deny", "status": 403, "body": "gate"}
    missing = {"method": "GET", "path": content(CHARLIE), "tag": "content", "status": 404, "body": "other"}
    got = tables(
        attributes | {"body": "other", "body_bytes": 90},
        refused | {"body_bytes": 70},
        missing | {"body_bytes": 40},
        listed(FOLDER, 300),
        listed(FOLDER, 280),
        listed(NOWHERE, 50, status=404),
    )
    assert (got["files"], got["files_total"], got["files_cut"]) == ({}, 0, False)
    assert got["listings"] == {FOLDER: {"path": children(FOLDER), "requests": 2, "body_bytes": 580}}
    assert (got["listings_total"], got["listings_cut"]) == (1, False)


@pytest.mark.parametrize(
    "unplaced",
    [
        {k: v for k, v in read(ALPHA, 20, first=40).items() if k != "content_range"},  # a 206 with no Content-Range
        read(ALPHA, 20, first=40) | {"content_range": "bytes 40-49/100000"},  # more bytes than its range holds
        read(ALPHA, 20, first=40) | {"content_range": "bytes 40-59,80-99/100000"},  # not one range
        read(ALPHA, 20) | {"status": 502},  # the gate's own failure after its body began: no status of the platform's
    ],
    ids=["no-content-range", "range-too-short", "several-ranges", "no-platform-status"],
)
def test_o22_a_range_the_log_cannot_place_leaves_that_files_ranges_unknown_and_its_bytes_counted(unplaced):
    """O22: where an answer's bytes began in its file is read from the platform's own answer (a 200 from the start, a
    206 from its Content-Range), never guessed; an answer the log cannot place leaves its file's ranges unknown, never
    a part of them, while its requests and body bytes, and every other file's row, stay known."""
    got = tables(read(ALPHA, 100), unplaced, read(BRAVO, 30, first=5))
    assert got["files"][ALPHA] == {
        "path": content(ALPHA),
        "requests": 2,
        "ranges": None,
        "range_bytes": None,
        "ranges_cut": None,
        "body_bytes": 120,
    }
    assert (got["files"][BRAVO]["ranges"], got["files"][BRAVO]["range_bytes"]) == ([[5, 35]], 30)
    nothing = read(ALPHA, 0) | {"status": None, "client_aborted": True}  # the client went before any byte: no range
    assert tables(read(ALPHA, 100), nothing)["files"][ALPHA]["ranges"] == [[0, 100]]


def test_o22_the_tables_are_bounded_and_say_when_their_bound_cut_them():
    """O22: a run lists at most its caps' `max_files` files and `max_directories` directories, the first read, and each
    file at most `max_ranges_per_file` ranges, the lowest (1000, 1000 and 64 unless `[sandbox.served_bytes]` declares
    others); a table the bound cut says so, with the number of files or directories read, and a file's unique bytes
    still count every range. The run's own counts are never cut."""
    caps = gatelog.Caps()
    assert caps.record() == DEFAULT_CAPS
    files = [read(f"fileid{n:05d}", 10) for n in range(caps.max_files + 1)]
    got = tables(*files)
    assert len(got["files"]) == caps.max_files
    assert list(got["files"]) == [f"fileid{n:05d}" for n in range(caps.max_files)]
    assert (got["files_total"], got["files_cut"]) == (caps.max_files + 1, True)
    assert got["content_bytes"] == 10 * (caps.max_files + 1)
    assert tables(*files[:-1])["files_cut"] is False
    directories = [listed(f"folderid{n:05d}", 10) for n in range(caps.max_directories + 1)]
    got = tables(*directories)
    assert len(got["listings"]) == caps.max_directories
    assert (got["listings_total"], got["listings_cut"]) == (caps.max_directories + 1, True)
    spread = [read(ALPHA, 10, first=100 * n) for n in reversed(range(caps.max_ranges_per_file + 1))]
    row = tables(*spread)["files"][ALPHA]
    assert row["ranges"] == [[100 * n, 100 * n + 10] for n in range(caps.max_ranges_per_file)]
    assert (row["ranges_cut"], row["range_bytes"]) == (True, 10 * (caps.max_ranges_per_file + 1))
    assert tables(*spread[1:])["files"][ALPHA]["ranges_cut"] is False


def test_o22_a_byte_range_past_a_files_range_cap_reads_as_cut_and_one_below_it_stays_known():
    """O22: a file's row keeps its lowest ranges, so every byte below the end of the last one listed is known: inside a
    range it was served, in a gap it was not. A byte past that end, in a row the cap cut, is unknown, never "not
    served", and a range the log could not place is unknown too. Raised far enough, the cap lists them all."""
    spread = [read(ALPHA, 10, first=0), read(ALPHA, 10, first=100), read(ALPHA, 10, first=200)]
    capped = gatelog.served(log(*spread, ANSWERS[-1]), STOPPED, gatelog.Caps(max_ranges_per_file=2))
    assert (capped["files"][ALPHA]["ranges"], capped["files"][ALPHA]["ranges_cut"]) == ([[0, 10], [100, 110]], True)
    assert capped["caps"] == DEFAULT_CAPS | {"max_ranges_per_file": 2}
    assert served.lookup_bytes(capped, ALPHA, 100, 110).state == "served"
    assert served.lookup_bytes(capped, ALPHA, 0, 5).state == "served"
    assert served.lookup_bytes(capped, ALPHA, 20, 30).state == "not_served"  # a gap below the cut: known unread
    assert served.lookup_bytes(capped, ALPHA, 5, 15).state == "not_served"  # its bytes 10 to 14 were never served
    beyond = served.lookup_bytes(capped, ALPHA, 200, 210)
    assert (beyond.state, beyond.reasons) == ("unknown", ("ranges_cut",))  # served, but past what the row lists
    assert served.lookup_bytes(capped, ALPHA, 105, 120).state == "unknown"  # its tail lies past the cut
    whole = gatelog.served(log(*spread, ANSWERS[-1]), STOPPED, gatelog.Caps(max_ranges_per_file=3))
    assert served.lookup_bytes(whole, ALPHA, 200, 210).state == "served"
    assert served.lookup_bytes(whole, ALPHA, 250, 260).state == "not_served"
    unplaced = tables(read(ALPHA, 10) | {"status": 502})
    assert (served.lookup_bytes(unplaced, ALPHA, 0, 5).state, served.lookup_bytes(unplaced, ALPHA, 0, 5).reasons) == (
        "unknown",
        ("ranges_unknown",),
    )


def test_o22_a_lookup_in_an_unknown_run_is_unknown_with_the_runs_reasons_and_a_cut_directory_reads_as_cut():
    """O22: when the run's count is unknown, so is every file, range and directory a reader looks up, with the run's
    own reasons. A directory left out of a cut listings table is unknown, never "not served"; with no cut, a directory
    without a row was not listed."""
    unknown = gatelog.served(None, STOPPED)
    for found in (
        served.lookup_file(unknown, ALPHA),
        served.lookup_bytes(unknown, ALPHA, 0, 1),
        served.lookup_directory(unknown, FOLDER),
    ):
        assert (found.state, found.reasons) == ("unknown", ("gate_log_unreadable",))
    folders = [listed(FOLDER, 300), listed(NOWHERE, 50)]
    capped = gatelog.served(log(*folders, ANSWERS[-1]), STOPPED, gatelog.Caps(max_directories=1))
    assert served.lookup_directory(capped, FOLDER).row == {"path": children(FOLDER), "requests": 1, "body_bytes": 300}
    assert (served.lookup_directory(capped, NOWHERE).state, served.lookup_directory(capped, NOWHERE).reasons) == (
        "unknown",
        ("listings_cut",),
    )
    assert served.lookup_directory(tables(*folders), "diridunlisted").state == "not_served"
    with pytest.raises(ValueError, match="not a snapshot of the bytes served"):
        served.lookup_file({"served_bytes": 10}, ALPHA)


def start(world, level):
    return DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))


def write_log(sbx, *records) -> None:
    (sbx.dir / "gatelog" / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), "utf-8")


def declared(world, **caps):
    """The world's `[sandbox]` table with `[sandbox.served_bytes]` declaring these caps, loaded from its file."""

    def edit(data):
        add_sandbox(data)
        data["sandbox"]["served_bytes"] = caps

    return load_platform(write_platform(world.tmp_path, edit), require_secrets=False).sandbox


THREE_FILES = (read(ALPHA, 100), read(BRAVO, 50, first=10), read(CHARLIE, 70), ANSWERS[-1])


def collected(world, tmp_path, settings, *records) -> dict:
    """The instrument's snapshot of a REST run under these settings whose gate logged these lines."""
    sbx = DockerSandbox.start(world.host, settings, make_spec(world, "via-gate"), **start_options(world))
    instrument = ServedBytes(sbx)
    write_log(sbx, *records)
    sbx.collect(tmp_path / "evidence")
    snapshot = instrument.finish()
    sbx.stop()
    return snapshot


def test_o22_a_file_past_the_declared_cap_reads_as_cut_never_as_served_zero_or_unread(world, tmp_path):
    """O22: a run that read more files than `[sandbox.served_bytes] max_files` lists the first it read and says the
    table was cut. A file left out has no row, and a lookup of it is unknown ("files_cut"), never served with 0 bytes
    and never "not served": once the table is cut, a file without a row may have been read, so even an unread one
    reads unknown."""
    snapshot = collected(world, tmp_path, declared(world, max_files=2), *THREE_FILES)
    assert snapshot["caps"] == DEFAULT_CAPS | {"max_files": 2}
    assert list(snapshot["files"]) == [ALPHA, BRAVO]
    assert (snapshot["files_total"], snapshot["files_cut"]) == (3, True)
    for found in (served.lookup_file(snapshot, CHARLIE), served.lookup_bytes(snapshot, CHARLIE, 0, 70)):
        assert (found.state, found.reasons, found.row) == ("unknown", ("files_cut",), None)
    assert served.lookup_file(snapshot, "fileidunread").state == "unknown"
    assert served.lookup_file(snapshot, ALPHA).row["body_bytes"] == 100
    assert snapshot["content_bytes"] == 220  # the run's own count is never cut


def test_o22_the_same_run_with_a_raised_cap_lists_every_file_and_an_unread_one_reads_as_not_served(world, tmp_path):
    """O22: the run above, with a cap that holds its files, lists all three and is not cut; the third file's row has its
    bytes, and a file without a row was not served."""
    snapshot = collected(world, tmp_path, declared(world, max_files=3), *THREE_FILES)
    assert snapshot["caps"] == DEFAULT_CAPS | {"max_files": 3}
    assert list(snapshot["files"]) == [ALPHA, BRAVO, CHARLIE]
    assert (snapshot["files_total"], snapshot["files_cut"]) == (3, False)
    assert served.lookup_file(snapshot, CHARLIE).row["ranges"] == [[0, 70]]
    assert served.lookup_bytes(snapshot, BRAVO, 10, 60).state == "served"
    found = served.lookup_file(snapshot, "fileidunread")
    assert (found.state, found.reasons, found.row) == ("not_served", (), None)


def test_o22_the_caps_in_effect_are_recorded_in_every_snapshot(world, tmp_path):
    """O22: the snapshot says what its tables were allowed, whatever the data path and whether or not the count is
    known: the caps `[sandbox.served_bytes]` declares, or the defaults when it declares none."""
    settings = declared(world, max_files=5, max_directories=7, max_ranges_per_file=9)
    caps = {"max_files": 5, "max_directories": 7, "max_ranges_per_file": 9}
    for level in ("via-gate", "mounted", "isolated"):
        sbx = DockerSandbox.start(world.host, settings, make_spec(world, level), **start_options(world))
        assert ServedBytes(sbx).finish()["caps"] == caps  # before the collection too, with every count unknown
        sbx.stop()
    assert collected(world, tmp_path, settings, *ANSWERS)["caps"] == caps
    plain = start(world, "via-gate")  # the world's own table declares no caps
    assert plain.served_bytes()["caps"] == DEFAULT_CAPS
    plain.stop()


@pytest.mark.parametrize(("level", "listing"), [(COMPLETE_LEVEL, "complete"), ("via-gate", "paged")])
def test_o22_a_rest_runs_count_is_taken_once_by_the_collection_and_the_instrument_records_it(
    world, tmp_path, level, listing
):
    """O22: before its collection a REST run's count is unknown, since its gate still runs; the collection stops the
    gate and counts its log once, the metadata and the instrument's snapshot carry that count with the data path and
    its listing mode, and it stays the same after the sandbox and its log are gone. So do the tables of the files and
    directories, which replay serves as recorded."""
    sbx = start(world, level)
    instrument = ServedBytes(sbx)
    start_instruments([instrument], {"run_key": "run-key-1", "factors": {}, "attempt": 1})
    assert sbx.served_bytes()["unknown"] == ["gate_not_collected"]
    assert sbx.served_bytes()["served_bytes"] is None
    assert all(sbx.served_bytes()[field] is None for field in TABLES)
    write_log(sbx, *ANSWERS)
    sbx.collect(tmp_path / "evidence")
    write_log(sbx)  # the log changing after the collection does not change the count
    expected = {
        "data_path": "rest",
        "listing": listing,
        "run_start_marked": False,
        **gatelog.served(log(*ANSWERS), STOPPED),
    }
    assert set(expected["files"]) == {ALPHA, BRAVO}
    assert sbx.served_bytes() == expected
    assert json.loads((tmp_path / "evidence" / "metadata.json").read_text())["served_bytes"] == expected
    snapshot = instrument.finish()
    assert snapshot == {"version": 1, **expected}
    sbx.stop()
    assert sbx.served_bytes() == expected
    assert NAMESPACE == "served_bytes"


def test_o22_a_second_collection_keeps_the_count_the_first_took(world, tmp_path):
    """O22: the count is taken once. A second collection stops a gate that is already gone (on a real host its stop
    then fails), so counting again could only lose what the first collection counted; it keeps that count."""
    sbx = start(world, "via-gate")
    write_log(sbx, *ANSWERS)
    sbx.collect(tmp_path / "first")
    counted = sbx.served_bytes()
    write_log(sbx)  # a recount of this log would give zero
    sbx.collect(tmp_path / "second")
    assert sbx.served_bytes() == counted
    assert counted["served_bytes"] == 1504
    sbx.stop()


def test_o22_the_instrument_puts_the_count_in_the_record_where_a_scorer_reads_it(world, tmp_path):
    """O22: through the core's instrument hooks the count lands in the record under its namespace, as a plain JSON
    object, so a scorer and replay read the recorded value and nothing measures it again."""
    from record_support import hand_made

    sbx = start(world, COMPLETE_LEVEL)
    instrument = ServedBytes(sbx)
    start_instruments([instrument], {"run_key": "run-key-1", "factors": {}, "attempt": 1})
    write_log(sbx, *ANSWERS)
    sbx.collect(tmp_path / "evidence")
    record = finish_instruments(hand_made(), [instrument])
    assert record.instruments[NAMESPACE]["served_bytes"] == 1504
    assert record.instruments[NAMESPACE]["listing"] == "complete"
    sbx.stop()


def test_o22_a_mounts_reads_are_unknown_and_no_data_path_read_nothing(world):
    """O22: a mount's reads go through oneclient, which no gate sees, so its count is unknown and never zero; a run with
    no data path had nothing to read through, so its count is zero."""
    mounted = start(world, "mounted")
    got = mounted.served_bytes()
    assert (got["data_path"], got["listing"], got["unknown"]) == ("mount", None, ["mount_not_through_gate"])
    assert all(got[field] is None for field in gatelog.SERVED_FIELDS)
    assert all(got[field] is None for field in TABLES)  # no file's count is known either
    assert ServedBytes(mounted).finish()["served_bytes"] is None
    mounted.stop()
    isolated = start(world, "isolated")
    got = isolated.served_bytes()
    assert (got["data_path"], got["unknown"]) == ("none", [])
    assert all(got[field] == 0 for field in gatelog.SERVED_FIELDS)
    assert {field: got[field] for field in TABLES} == {
        "files": {},
        "files_total": 0,
        "files_cut": False,
        "listings": {},
        "listings_total": 0,
        "listings_cut": False,
    }
    isolated.stop()


def test_o22_a_gate_whose_stop_left_a_request_unanswered_or_a_close_line_unwritten_is_unknown(world, tmp_path):
    """O22: the stop of the gate decides whether its log is whole: a request still being answered when it stopped, or a
    teardown that says it could not write every close line, makes the count unknown."""
    sbx = start(world, "via-gate")
    write_log(sbx, *ANSWERS, {"event": "conn_close", "conn": "1-9", "ended": "teardown", "in_request": True})
    sbx.collect(tmp_path / "first")
    assert sbx.served_bytes()["unknown"] == ["request_in_flight_at_stop"]
    sbx.stop()
    other = start(world, "via-gate")
    world.host.containers[other.names.gate].logs = "gate ready\nGATE TEARDOWN INCOMPLETE\n"
    write_log(other, *ANSWERS)
    other.collect(tmp_path / "second")
    assert other.served_bytes()["unknown"] == ["gate_teardown_incomplete"]
    assert other.metadata()["gate_stop"]["teardown_incomplete"] is True
    other.stop()


def test_o22_a_gate_log_that_is_not_json_is_unknown_and_the_metadata_still_written(world, tmp_path):
    """O22: a line cut short (a gate killed mid-write) leaves the count unknown, and the collection still writes the
    metadata, with the gate's summary unknown rather than a crash."""
    sbx = start(world, "via-gate")
    (sbx.dir / "gatelog" / "requests.jsonl").write_text('{"method": "GET", "body_by\n', encoding="utf-8")
    sbx.collect(tmp_path / "evidence")
    meta = json.loads((tmp_path / "evidence" / "metadata.json").read_text())
    assert meta["served_bytes"]["unknown"] == ["gate_log_malformed", "gate_teardown_unconfirmed"]
    assert meta["gate"]["unavailable"] == "the gate log could not be parsed"
    sbx.stop()


def test_o22_a_gate_that_exited_without_its_teardown_mark_is_unknown_whatever_its_exit_code(world, tmp_path):
    """O22: a gate killed by its memory limit exits too, before it can log an answer still owed or its close lines; an
    exit code shows nothing about that, even a clean one. Only the gate's `teardown_complete` line does, so a log
    without it is unknown."""
    sbx = start(world, "via-gate")
    write_log(sbx, *UNMARKED)
    sbx.collect(tmp_path / "evidence")
    assert sbx.metadata()["gate_stop"]["exit_code"] == 0  # shown exited, and still not shown whole
    assert sbx.served_bytes()["unknown"] == ["gate_teardown_unconfirmed"]
    assert sbx.served_bytes()["served_bytes"] is None
    sbx.stop()


def test_o22_a_gate_output_that_cannot_be_read_leaves_its_log_and_teardown_unknown(world, tmp_path):
    """O22: the gate reports a failed write of its log and a close line it could not write on its own output; a read of
    that output that fails shows neither, so whether the log is whole is unknown, never taken for "no failure"."""
    from expcore_onedata.sandbox.hostops import CmdResult

    sbx = start(world, "via-gate")
    write_log(sbx, *ANSWERS)
    docker = world.host.docker

    def failing_logs(*args, **kwargs):
        return CmdResult(1, "", "Error response from daemon") if args[:1] == ("logs",) else docker(*args, **kwargs)

    world.host.docker = failing_logs
    sbx.collect(tmp_path / "evidence")
    world.host.docker = docker
    stop = sbx.metadata()["gate_stop"]
    assert (stop["output_read"], stop["log_write_failed"], stop["teardown_incomplete"]) == (False, None, None)
    assert sbx.served_bytes()["unknown"] == ["gate_output_unreadable"]
    sbx.stop()


def test_o22_the_gate_marks_a_complete_teardown_and_only_a_complete_one(tmp_path, monkeypatch):
    """O22: the gate's stop writes the close line of every connection still open and then `teardown_complete`; when a
    close line cannot be written, it writes no mark, so the count of the bytes it served is unknown."""
    from expcore_onedata.sandbox import gate

    class Open:
        """A connection still open when the gate stops; `broken` makes its close line fail."""

        def __init__(self, broken):
            self.broken = broken

        def _teardown_line(self):
            if self.broken:
                raise OSError("the close line could not be built")
            gate._log({"event": "conn_close", "conn": "1-1", "ended": "teardown", "in_request": False})

    log_file = tmp_path / "requests.jsonl"
    monkeypatch.setattr(gate, "CFG", {"log": str(log_file)})
    for broken, marked in ((False, True), (True, False)):
        log_file.write_text("", encoding="utf-8")
        monkeypatch.setattr(gate, "_LIVE", {"1-1": Open(broken)})
        gate.teardown()
        events = [json.loads(x).get("event") for x in log_file.read_text().splitlines()]
        assert ("teardown_complete" in events) is marked
        if marked:
            assert events == ["conn_close", "teardown_complete"]  # the mark comes after every close line


def test_o22_a_request_counts_as_logged_only_once_its_line_is_written(monkeypatch):
    """O22: a teardown that reads a connection between a response and its log line must see that request outstanding
    (`in_request`), or it writes `teardown_complete` over a line that may never be written, and the count reads as a
    known zero. So the connection's last logged request moves only after the line is written."""
    from types import SimpleNamespace

    from expcore_onedata.sandbox import gate

    handler = object.__new__(gate.Handler)
    handler._w = SimpleNamespace(mark=0, n=123, failed=0, total=123)
    handler._conn, handler._last_logged = "1-1", 4
    seen: list[int] = []
    monkeypatch.setattr(gate, "_log", lambda rec: seen.append(handler._last_logged))
    handler._log({"event": "request", "conn_request": 5})
    assert seen == [4]  # at the moment the line was written, request 5 was still outstanding
    assert handler._last_logged == 5


def test_o22_a_request_that_starts_once_the_stop_began_answers_nothing(monkeypatch):
    """O22: a request read before the stop but counted after the teardown read its connection would be answered and
    logged after `teardown_complete`; once the stop has begun, a request that starts is closed unanswered instead."""
    from types import SimpleNamespace

    from expcore_onedata.sandbox import gate

    stopping = gate.threading.Event()
    handler = object.__new__(gate.Handler)
    handler.connection = SimpleNamespace(settimeout=lambda s: None)
    handler.server = SimpleNamespace(stopping=stopping)
    handler._served, handler.close_connection = 0, False
    stopping.set()
    handler._handle()  # with the stop begun, it returns before reading the request's host or writing anything
    assert handler.close_connection is True
    sent: list[int] = []
    monkeypatch.setattr(gate.BaseHTTPRequestHandler, "send_error", lambda self, code, *a: sent.append(code))
    handler.send_error(501)  # the parser's own answer is held to the same rule
    assert sent == []


def test_o22_a_request_line_after_the_teardown_mark_leaves_the_count_unknown():
    """O22: a line the mark did not cover means the mark proves nothing, so the count is unknown, never a total."""
    after = read(ALPHA, 5) | {"event": "request", "conn": "1-2"}
    lines = log(*ANSWERS, after)  # ANSWERS ends with the teardown mark
    assert gatelog.served(lines, STOPPED)["unknown"] == [gatelog.GATE_TEARDOWN_UNCONFIRMED]


def test_o22_a_final_head_after_a_100_continue_is_never_counted_as_body(monkeypatch):
    """O22: a `100 Continue` ends its own head, which marks where a body would begin; the final response's head then
    starts again from nothing, so a write of it that fails counts its header bytes as no body at all."""
    from types import SimpleNamespace

    from expcore_onedata.sandbox import gate

    handler = object.__new__(gate.Handler)
    handler._w = SimpleNamespace(mark=25, n=25, failed=0, total=None)  # the 100's head was written, 25 bytes
    handler._conn, handler._last_logged = "1-1", 0
    monkeypatch.setattr(gate.BaseHTTPRequestHandler, "send_response_only", lambda self, code, message=None: None)
    handler.send_response_only(403)
    handler._w.n, handler._w.failed = 37, 40  # 12 bytes of the 403's head went out, then the write failed
    lines: list[dict] = []
    monkeypatch.setattr(gate, "_log", lines.append)
    handler._log({"event": "request", "conn_request": 1})
    assert lines[0]["body_bytes"] == 0
