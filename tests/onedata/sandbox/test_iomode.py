"""O48: the run's record carries the mount's declared I/O mode and the one oneclient's own start lines show, and a run
whose two differ, or whose log cannot say, is refused and never scored as the declared mode.

The pure part reads invented log text; the instrument part runs `DockerSandbox` over the in-memory host, whose sidecar
answers a read of its INFO log with the text a test gives it. Every value here is invented.
"""

import json
from pathlib import Path

import pytest
from record_support import record_run
from sandbox_support import make_spec, start_options

from expcore.errors import InfraError, infra_kinds, is_treatable
from expcore.records.instruments import finish_instruments, start_instruments
from expcore.records.record import RunRecord, decode, load_record
from expcore.replay.check import check_record
from expcore.replay.replay import replay_record
from expcore_onedata.sandbox import directio, iomode
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.iomode import MountIoMode, agrees, judge
from expcore_onedata.sandbox.kinds import IO_MODE_INTEGRITY
from expcore_onedata.sandbox.sandbox import DockerSandbox

HEADER = "Log file created at: 2026/11/13 10:00:00\n"
START = (
    HEADER
    + "I1113 10:00:00.100300     1 logging.cc:84] Forced direct IO: {direct}\n"
    + "I1113 10:00:00.100400     1 logging.cc:85] Forced proxy IO: {proxy}\n"
)
PROXY, DIRECT, NEITHER, BOTH = (
    START.format(direct=d, proxy=p) for d, p in (("0", "1"), ("1", "0"), ("0", "0"), ("1", "1"))
)
DETECTION = (
    "I1113 10:00:06.000600    16 helpersCache.h:467] Performing automatic storage access type detection for storage "
    "st1 for file f1 with forced proxy io mode: {mode}\n"
)
FALLBACKS = {
    "proxy_fallback": (
        "E1113 10:00:02.000000    12 fsLogic.cc:1184] Reading from 0123 failed due to insufficient permissions\n"
    ),
    "switched_to_proxy": (
        "I1113 10:00:03.000000    16 helpersCache.h:497] Direct access failed, switching to proxy mode\n"
    ),
    "not_direct_on_provider": (
        "W1113 10:00:03.000000    16 helpersCache.h:645] Storage st1 is not accessible in direct IO mode\n"
    ),
    "automatic_detection": DETECTION.format(mode=1),
}
FIXTURES = sorted((Path(__file__).resolve().parents[2] / "data" / "record-v3").glob("*.json"))
UNKNOWN_LOGS = {
    "empty": "",
    "no start lines": HEADER + "I1113 10:00:00.100000     1 logging.cc:80] Mountpoint: /mnt/invented\n",
    "only one start line": HEADER + "I1113 10:00:00.100300     1 logging.cc:84] Forced direct IO: 1\n",
    "a start line without a value": START.format(direct="", proxy="1"),
    "a start line with another value": START.format(direct="yes", proxy="1"),
    "garbage": "\x00\x01 not a log \n{\n",
    "unreadable": None,
}


class SidecarLog:
    """What the stand-in sidecar says when its INFO log is read; a test changes it before the collection."""

    def __init__(self, world, text):
        self.text, self.readable, self.reads = text, True, 0
        default = world.host.exec_handler

        def handler(container, argv, stdin):
            if argv[:1] == ["cat"] and argv[-1].endswith(directio.LOG_FILE):
                self.reads += 1
                return CmdResult(0, self.text) if self.readable else CmdResult(1, "", "No such file")
            return default(container, argv, stdin)

        world.host.exec_handler = handler


def mounted_run(world, text):
    """A proxy-declared mount sandbox whose sidecar's log holds `text`."""
    log = SidecarLog(world, text)
    sbx = DockerSandbox.start(world.host, world.s, make_spec(world, "mounted"), **start_options(world))
    return sbx, log


def finished(world, tmp_path, text, **options):
    """The record of a run on a proxy mount whose log says `text`: instruments started, sandbox collected, finished."""
    sbx, log = mounted_run(world, text)
    instrument = MountIoMode(sbx, **options)
    start_instruments([instrument], {"run_key": "run-key-1", "factors": {}, "attempt": 1})
    record = record_run([[("look", {})]], {"look": lambda arguments: "seen"})
    sbx.collect(tmp_path / "evidence")
    record = finish_instruments(record, [instrument])
    sbx.stop()
    return record, log


# ---------------------------------------------------------------- the reading of the log


def test_o48_a_proxy_run_whose_start_lines_force_proxy_agrees_with_its_declared_proxy():
    """O48: oneclient's start lines say forced proxy 1 and forced direct 0: the effective mode is proxy, and the record
    of the run says both modes and that they agree."""
    assert judge("proxy", PROXY) == {
        "declared": "proxy",
        "effective": "proxy",
        "agree": True,
        "forced_direct": "0",
        "forced_proxy": "1",
        "lines": [],
        "problems": [],
    }


def test_o48_a_direct_run_whose_start_lines_force_direct_agrees_with_its_declared_direct():
    """O48: forced direct 1 and forced proxy 0, and no line of a detection or a fallback: effective direct."""
    got = judge("direct", DIRECT)
    assert (got["declared"], got["effective"], got["agree"]) == ("direct", "direct", True)
    assert (got["forced_direct"], got["forced_proxy"], got["lines"], got["problems"]) == ("1", "0", [], [])


@pytest.mark.parametrize(
    ("declared", "log", "effective"),
    [("proxy", DIRECT, "direct"), ("direct", PROXY, "proxy"), ("proxy", NEITHER, "auto"), ("direct", NEITHER, "auto")],
)
def test_o48_a_log_that_shows_another_mode_than_the_declared_one_disagrees_and_names_both(declared, log, effective):
    """O48: the declared mode against what the start lines show, in every combination: the other forced mode, and no
    forced mode at all (oneclient's own detection, the mode #227 forbids). The first problem names both modes, and the
    record never says they agree."""
    got = judge(declared, log)
    assert (got["declared"], got["effective"], got["agree"]) == (declared, effective, False)
    assert got["problems"][0] == f"declared {declared!r}, oneclient's start lines show {effective!r}"


def test_o48_both_options_forced_is_not_a_mode_and_never_agrees():
    """O48: a log that says forced direct 1 and forced proxy 1 contradicts itself: `mixed`, whatever was declared."""
    for declared in ("proxy", "direct"):
        got = judge(declared, BOTH)
        assert (got["effective"], got["agree"]) == ("mixed", False)
        assert any("both" in problem for problem in got["problems"])


@pytest.mark.parametrize("line", sorted(FALLBACKS))
def test_o48_a_direct_run_with_a_fallback_or_a_detection_line_is_mixed_and_never_agrees(line):
    """O48: forced direct at the start, but a line shows that a read did not go to the storage itself (a read refused
    for permissions and made again through the provider, oneclient's own detection, a switch to the provider's helper):
    the run did not run in the declared mode. The names of the lines found are in the record."""
    got = judge("direct", DIRECT + FALLBACKS[line])
    assert (got["effective"], got["agree"]) == ("mixed", False)
    assert line in got["lines"]
    assert "direct" in got["problems"][0]
    assert "mixed" in got["problems"][0]


def test_o48_a_proxy_run_that_ran_oneclients_automatic_detection_is_mixed():
    """O48: forced proxy at the start, yet a detection that did not force proxy (`forced proxy io mode: 0`) ran: the
    mode was not left unchanged. A detection that itself forced proxy (`1`) is the declared mode and is not a
    disagreement."""
    mixed = judge("proxy", PROXY + DETECTION.format(mode=0))
    assert (mixed["effective"], mixed["agree"], mixed["lines"]) == ("mixed", False, ["automatic_detection"])
    forced = judge("proxy", PROXY + DETECTION.format(mode=1))
    assert (forced["effective"], forced["agree"], forced["lines"]) == ("proxy", True, [])


@pytest.mark.parametrize("name", sorted(UNKNOWN_LOGS))
@pytest.mark.parametrize("declared", ["proxy", "direct"])
def test_o48_a_log_that_cannot_be_parsed_fails_closed_as_unknown_never_as_agreement(declared, name):
    """O48: no log, an empty one, one without the start lines or with a start line that has no 0 or 1, and garbage: the
    effective mode is `unknown`, the record says so with the reason, and `agree` is false."""
    got = judge(declared, UNKNOWN_LOGS[name])
    assert (got["effective"], got["agree"]) == ("unknown", False)
    assert got["problems"]
    assert all(isinstance(p, str) for p in got["problems"])


@pytest.mark.parametrize(
    ("declared", "log"),
    [("proxy", PROXY + DIRECT), ("direct", DIRECT + PROXY), ("proxy", PROXY + NEITHER), ("direct", DIRECT + BOTH)],
    ids=["proxy then direct", "direct then proxy", "proxy then neither", "direct then both"],
)
def test_o48_a_start_line_logged_again_with_another_value_is_never_agreement(declared, log):
    """O48: every logged occurrence of a start line counts, not the first alone: a log whose first start lines say the
    declared mode and whose later ones say another (a sidecar started again with other options, a line the log was
    given) is `mixed` and never agrees, whichever came first."""
    got = judge(declared, log)
    assert (got["effective"], got["agree"]) == ("mixed", False)
    assert any("more than once" in problem for problem in got["problems"])


@pytest.mark.parametrize("declared", ["proxy", "direct"])
def test_o48_a_start_line_logged_again_with_a_value_that_is_no_flag_is_unknown(declared):
    """O48: a later occurrence that is not a 0 or a 1 is not ignored because an earlier one was: unknown, never
    agree."""
    first = PROXY if declared == "proxy" else DIRECT
    got = judge(declared, first + "I1113 10:05:00.000000     1 logging.cc:84] Forced proxy IO: yes\n")
    assert (got["effective"], got["agree"]) == ("unknown", False)


@pytest.mark.parametrize("declared", ["proxy", "direct"])
def test_o48_a_start_line_logged_again_with_the_same_value_is_still_the_declared_mode(declared):
    """O48: the same start lines again (a sidecar started again with the same options) say the same mode: agreement."""
    log = PROXY * 2 if declared == "proxy" else DIRECT * 2
    got = judge(declared, log)
    assert (got["effective"], got["agree"]) == (declared, True)


def test_o48_a_value_the_log_holds_where_a_flag_belongs_is_never_copied_into_the_reading():
    """O48: a start line whose value is a path, a token or any other text is `unknown`, and the reading says that it was
    not a 0 or a 1 without repeating a character of it: the reading reaches the sandbox's metadata, the instrument's
    snapshot and a refusal's detail, which hold nothing the log holds."""
    secret = "/private/tok-0123456789"
    log = HEADER + f"I1113 10:00:00.100300     1 logging.cc:84] Forced direct IO: {secret}\n"
    log += "I1113 10:00:00.100400     1 logging.cc:85] Forced proxy IO: 1\n"
    for declared in ("proxy", "direct"):
        got = judge(declared, log)
        assert (got["effective"], got["agree"]) == ("unknown", False)
        assert "private" not in json.dumps(got)
        assert "tok-0123456789" not in json.dumps(got)
    assert "'0' or '1'" in json.dumps(judge("proxy", log))


def test_o48_a_declared_mode_that_is_neither_proxy_nor_direct_never_agrees():
    """O48: the declared mode is read from the settings, which only take the two words; anything else fails closed."""
    got = judge(None, PROXY)
    assert (got["effective"], got["agree"]) == ("proxy", False)
    assert "declared" in got["problems"][0]
    other = judge("/private/other", PROXY)  # nor does the reading repeat what it was given
    assert (other["declared"], other["agree"]) == (None, False)
    assert "private" not in json.dumps(other)


def test_o48_agrees_recomputes_the_flag_and_reads_anything_but_a_matching_mount_snapshot_as_no():
    """O48: a reader of a record never takes the `agree` flag alone: a snapshot whose flag says true but whose two modes
    differ, one that is not a snapshot of version 1, one of a run with no mount, and a value that is no mapping are
    all not agreement."""
    good = {"version": 1, "mount": True, **judge("proxy", PROXY)}
    assert agrees(good) is True
    assert agrees({**good, "effective": "direct"}) is False
    assert agrees({**good, "agree": False}) is False
    assert agrees({**good, "version": 2}) is False
    assert agrees({"version": 1, "mount": False}) is False
    assert agrees({"version": 1, "mount": True, "declared": "auto", "effective": "auto", "agree": True}) is False
    assert agrees(None) is False
    assert agrees("agree") is False


def test_o48_the_record_of_the_reading_holds_no_text_of_the_log():
    """O48: only the two start values, the names of the lines found and sentences of the module's own: nothing of the
    log, however long, reaches the record (a provider or a mountpoint line, a path, a token in a line)."""
    secret = "tok-test-0123456789 /mnt/invented provider.invented.test"
    log = PROXY + f"I1113 10:00:00.200000     1 logging.cc:90] Mountpoint: {secret}\n" + FALLBACKS["proxy_fallback"]
    text = json.dumps(judge("proxy", log)) + json.dumps(judge("direct", log))
    for word in secret.split():
        assert word not in text


# ---------------------------------------------------------------- the instrument over a sandbox


def test_o48_a_proxy_mount_run_records_both_modes_agreeing_and_is_not_refused(world, tmp_path):
    """O48: the proxy run's record holds the instrument's snapshot, declared proxy against effective proxy, agreeing;
    the check passes, so the run is not refused and is scored as the proxy run it was; the sandbox's metadata beside
    the campaign holds the same reading."""
    record, log = finished(world, tmp_path, PROXY)
    expected = {"declared": "proxy", "effective": "proxy", "agree": True, "forced_direct": "0", "forced_proxy": "1"}
    snapshot = record.instruments[iomode.NAMESPACE]
    assert snapshot == {"version": 1, "mount": True, **expected, "lines": [], "problems": []}
    assert agrees(snapshot) is True
    assert (record.instrument_refusal, record.set_aside) == (None, False)
    written = json.loads((tmp_path / "evidence" / "metadata.json").read_text())["mount"]
    assert written["io_mode"] == "proxy"
    assert written["effective"] == {**expected, "lines": [], "problems": []}
    assert log.reads >= 1


def test_o48_a_run_whose_log_shows_the_other_mode_is_refused_as_infrastructure_naming_both_modes(world, tmp_path):
    """O48: declared proxy, oneclient's start lines say forced direct: the instrument's snapshot says they disagree, and
    its check refuses with the kind of the mount's I/O mode, which no condition can have as its treatment. The record's
    `instrument_refusal` names the instrument, the kind and both modes; the attempt is set aside, so it is never scored
    as the declared mode, and it keeps its snapshot and its record."""
    record, _ = finished(world, tmp_path, DIRECT)
    snapshot = record.instruments[iomode.NAMESPACE]
    assert (snapshot["declared"], snapshot["effective"], snapshot["agree"]) == ("proxy", "direct", False)
    refusal = record.instrument_refusal
    assert (refusal["namespace"], refusal["infra_kind"]) == (iomode.NAMESPACE, IO_MODE_INTEGRITY)
    assert "'proxy'" in refusal["detail"]
    assert "'direct'" in refusal["detail"]
    assert record.set_aside is True
    assert record.outcome["name"] == "finished"  # what the loop saw; the refusal came after the run


@pytest.mark.parametrize("name", ["empty", "garbage", "no start lines", "unreadable"])
def test_o48_a_run_whose_log_cannot_be_parsed_is_refused_as_unknown_never_taken_as_the_declared_mode(
    world, tmp_path, name
):
    """O48: an unparsable log, a log that cannot be read at all and one with no start lines are not agreement: the
    snapshot says `unknown`, the attempt is refused and set aside, and the detail says what is unknown."""
    sbx, log = mounted_run(world, UNKNOWN_LOGS[name] or "")
    log.readable = UNKNOWN_LOGS[name] is not None
    instrument = MountIoMode(sbx)
    sbx.collect(tmp_path / "evidence")
    record = finish_instruments(record_run([[("look", {})]], {"look": lambda arguments: "seen"}), [instrument])
    sbx.stop()
    snapshot = record.instruments[iomode.NAMESPACE]
    assert (snapshot["declared"], snapshot["effective"], snapshot["agree"]) == ("proxy", "unknown", False)
    assert record.instrument_refusal["infra_kind"] == IO_MODE_INTEGRITY
    assert "'proxy'" in record.instrument_refusal["detail"]
    assert "'unknown'" in record.instrument_refusal["detail"]
    assert record.set_aside is True


def test_o48_a_snapshot_taken_before_the_collection_is_unknown_and_the_run_is_refused(world, tmp_path):
    """O48: the sandbox reads the log when it collects, so a `finish` before the collection has no reading: unknown, and
    refused, never the declared mode taken for the effective one."""
    sbx, _ = mounted_run(world, PROXY)
    instrument = MountIoMode(sbx)
    record = finish_instruments(record_run([[("look", {})]], {"look": lambda arguments: "seen"}), [instrument])
    sbx.stop()
    snapshot = record.instruments[iomode.NAMESPACE]
    assert (snapshot["effective"], snapshot["agree"]) == ("unknown", False)
    assert any("collect" in problem for problem in snapshot["problems"])
    assert record.instrument_refusal["infra_kind"] == IO_MODE_INTEGRITY


def test_o48_an_instrument_whose_finish_failed_is_refused_by_its_check_since_unknown_is_not_a_pass(world, tmp_path):
    """O48: `finish_instruments` records a failed `finish` as `{"ok": False}`, and then still asks the check, which
    refuses: the mode was never shown to be the declared one."""

    class Broken:
        def mount_io_mode(self):
            raise RuntimeError("a defect of the stand-in")

    record = finish_instruments(
        record_run([[("look", {})]], {"look": lambda arguments: "seen"}), [MountIoMode(Broken())]
    )
    assert record.instruments[iomode.NAMESPACE] == {"ok": False, "error": "RuntimeError"}
    assert record.instrument_refusal["infra_kind"] == IO_MODE_INTEGRITY
    assert "never read" in record.instrument_refusal["detail"]


@pytest.mark.parametrize("level", ["via-gate", "isolated"])
def test_o48_a_run_without_a_mount_records_that_it_has_none_and_is_never_refused(world, tmp_path, level):
    """O48: a REST run and a run with no data path have no oneclient: the snapshot says `mount: false`, with no mode
    and no `agree`, and the check passes. A study may list the instrument for every run."""
    sbx = DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))
    instrument = MountIoMode(sbx)
    sbx.collect(tmp_path / "evidence")
    record = finish_instruments(record_run([[("look", {})]], {"look": lambda arguments: "seen"}), [instrument])
    sbx.stop()
    assert record.instruments[iomode.NAMESPACE] == {"version": 1, "mount": False}
    assert (record.instrument_refusal, record.set_aside) == (None, False)
    assert agrees(record.instruments[iomode.NAMESPACE]) is False  # there is no mode to confirm


def test_o48_a_study_that_declares_no_refusal_keeps_the_disagreement_in_the_record_and_is_not_refused(world, tmp_path):
    """O48: `refuse=False` is the study's own choice not to set such an attempt aside: the record still says the modes
    disagree (`agree` false), the check passes, and `agrees` still reads it as not the declared mode, so a scorer that
    asks for a confirmed mode never takes it for one."""
    record, _ = finished(world, tmp_path, DIRECT, refuse=False)
    snapshot = record.instruments[iomode.NAMESPACE]
    assert (snapshot["effective"], snapshot["agree"]) == ("direct", False)
    assert (record.instrument_refusal, record.set_aside) == (None, False)
    assert agrees(snapshot) is False


def test_o48_the_kind_of_the_refusal_is_registered_and_no_condition_can_declare_it():
    """O48: a treatable kind would let the refused attempt be scored as the manipulation, which the refusal mechanism
    itself refuses; the kind is registered untreatable, with the platform's others (`expcore_onedata.sandbox.kinds`)."""
    assert IO_MODE_INTEGRITY in infra_kinds()
    assert is_treatable(IO_MODE_INTEGRITY) is False
    assert InfraError(IO_MODE_INTEGRITY, "x").kind == IO_MODE_INTEGRITY


def test_o48_the_instrument_takes_no_reading_before_the_run_and_has_the_modules_namespace():
    """O48: `start` takes no reading (the log is read by the collection), and the namespace is the module's."""
    assert MountIoMode.namespace == iomode.NAMESPACE == "mount_io_mode"
    assert MountIoMode(object()).start({"run_key": "k", "factors": {}, "attempt": 1}) is None


# ---------------------------------------------------------------- the record format


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_o48_a_record_of_a_run_without_a_mount_has_no_such_field_and_replays_byte_for_byte(path):
    """O48 and R03: the fixtures of version 3 written before the instrument hold nothing under its namespace, and replay
    to their own bytes under the code that has it: a record of a run without a mount gains nothing."""
    stored = path.read_bytes()
    assert iomode.NAMESPACE not in decode(stored)["instruments"]
    assert replay_record(load_record(path)).dumps().encode("utf-8") == stored


def test_o48_a_refused_mount_run_writes_validates_and_replays_byte_for_byte(world, tmp_path):
    """O48 and R01: the record of a run the instrument refused (its snapshot and the refusal naming both modes) is a
    valid record of version 3, written and read back to its own bytes, and replays to them: the field is additive."""
    record, _ = finished(world, tmp_path, DIRECT)
    text = record.dumps(environ={})
    loaded = RunRecord.from_json(decode(text.encode("utf-8")))
    assert loaded.dumps(environ={}) == text
    assert loaded.instrument_refusal == record.instrument_refusal
    assert list(decode(text.encode("utf-8")))[-1] == "instrument_refusal"
    assert replay_record(loaded).dumps(environ={}) == text
    assert check_record(text.encode("utf-8")) is None
