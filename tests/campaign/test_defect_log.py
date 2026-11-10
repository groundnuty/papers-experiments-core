"""The local log of harness defects: the redacted traceback that no record, marker or store holds (L08)."""

import collections
import json
import shutil
import subprocess

import pytest
from campaign_support import make_schema
from loop_support import FakeProvider
from record_support import CODE
from scheduler_support import FakeExecutor, attempts_of, build, key_of

from expcore.campaign.defect_log import HARNESS_LOG, log_path, write_defect
from expcore.campaign.executor import record_run
from expcore.config.loading import ConfigError
from expcore.loop.outcomes import harness_defect_of
from expcore.records.redact import Redactor

SCHEMA = make_schema()
SMALL = {"tasks": ("task-a",), "seeds": (1,)}
SECRET = "pebble-quartz-7781-secret"


def crashing_with(text):
    async def crash(request):
        raise TypeError(text)

    return crash


def test_l08_a_harness_defect_leaves_its_redacted_traceback_in_the_local_log_and_nowhere_else(tmp_path, monkeypatch):
    """L08: the attempt keeps the class and the stack hash; the traceback, with its message redacted, is in the log."""
    monkeypatch.setenv("TOYLAB_API_KEY", SECRET)
    crashed = key_of(SCHEMA, "mount-read")
    executor = FakeExecutor({crashed: [crashing_with(f"the parser choked on key {SECRET}")]})
    with build(tmp_path / "out", executor=executor, plan=SMALL) as scheduler:
        summary = scheduler.run()
    assert summary.statuses == {"done": 2, "harness_error": 1}
    assert summary.harness_log == tmp_path / "out" / HARNESS_LOG
    log = summary.harness_log.read_text(encoding="utf-8")
    markers = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "out" / "runs").rglob("*marker.json")]
    [defect] = [m["result"]["outcome"]["defect"] for m in markers if m["result"]["outcome"].get("defect")]
    assert f"{crashed} attempt 1: TypeError, stack {defect['stack_sha256']}" in log  # found from the marker
    assert "Traceback (most recent call last)" in log
    assert "the parser choked on key [redacted]" in log
    assert "crash" in log  # the frame that raised, which the stack hash names without its line
    for path in tmp_path.rglob("*"):  # neither the log, nor a record, a marker or the store holds the secret
        assert not path.is_file() or SECRET.encode() not in path.read_bytes(), path
    store = (tmp_path / "out" / "state.sqlite").read_bytes()
    assert b"the parser choked" not in store
    assert not list((tmp_path / "out" / "runs").rglob("*.log"))


def test_l08_a_campaign_with_no_harness_defect_has_no_log(tmp_path):
    """L08: the log is written only when there is something to diagnose, so its path is printed only then."""
    with build(tmp_path, plan=SMALL) as scheduler:
        summary = scheduler.run()
    assert summary.harness_log is None
    assert not log_path(tmp_path).exists()


def defect_of(text):
    try:
        raise ValueError(text)
    except ValueError as error:
        return error, harness_defect_of(error)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_l08_the_log_is_ignored_by_git_whatever_the_studys_own_gitignore_says(tmp_path):
    """L08: the log's directory ignores everything in it, so a study that does not ignore out/ never commits it."""
    error, defect = defect_of("boom")
    path = write_defect(tmp_path / "out", error, defect, stamp="T", run_key="k", attempt=1, redactor=_Plain)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    ignored = subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", str(path)], check=False)
    assert ignored.returncode == 0
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert status.stdout == ""


def test_l08_a_traceback_that_cannot_be_redacted_is_withheld_with_the_reason(tmp_path):
    """L08: a secret too short to redact refuses the redactor; the entry then says why and holds no traceback."""

    def refused():
        raise ConfigError("environment variable SHORT_TOKEN holds a secret of fewer than 4 characters")

    error, defect = defect_of("carries abc")
    write_defect(tmp_path, error, defect, stamp="T", run_key="k", attempt=2, redactor=refused)
    log = log_path(tmp_path).read_text(encoding="utf-8")
    assert "k attempt 2: ValueError" in log
    assert "withheld" in log
    assert "SHORT_TOKEN" in log
    assert "carries abc" not in log
    assert "Traceback" not in log


def test_l08_entries_are_appended_one_per_defect(tmp_path):
    """L08: a second defect adds an entry and keeps the first."""
    for attempt in (1, 2):
        error, defect = defect_of(f"crash {attempt}")
        write_defect(tmp_path, error, defect, stamp="T", run_key="k", attempt=attempt, redactor=_Plain)
    log = log_path(tmp_path).read_text(encoding="utf-8")
    assert log.count("=== T k attempt") == 2
    assert "crash 1" in log
    assert "crash 2" in log


class _Plain:
    """A redactor that changes nothing."""

    def text(self, value):
        return value

    def with_lines(self):
        return self


def test_r12_a_secret_with_both_quote_kinds_is_redacted_from_the_log_in_its_repr_form(tmp_path, monkeypatch):
    """R12: `KeyError(secret)` puts the secret's repr in the traceback, a quote escaped; the log redacts that too."""
    quoted = "quartz'pebble\"7781-amber"
    monkeypatch.setenv("TOYLAB_TOKEN", quoted)
    crashed = key_of(SCHEMA, "mount-read")

    async def lookup(request):
        raise KeyError(quoted)

    with build(tmp_path, executor=FakeExecutor({crashed: [lookup]}), plan=SMALL) as scheduler:
        log = scheduler.run().harness_log.read_text(encoding="utf-8")
    assert "KeyError: '[redacted]'" in log
    assert "pebble" not in log


PASSED = "willow-cobalt-5523-passed"  # a secret only the executor knows: given to record_run, in no environment


class ToolsFail:
    """A tool provider whose tool list cannot be built: a crash before the loop starts."""

    def tools(self):
        raise ValueError(f"the catalogue refused credential {PASSED}")


def test_r12_a_crash_inside_record_run_is_redacted_with_the_secrets_the_executor_passed_it(tmp_path):
    """R12: the log uses the attempt's whole redaction context, the `secrets` given to record_run included, not only
    the model table."""
    crashed = key_of(SCHEMA, "mount-read")
    executor = FakeExecutor()

    async def broken(request):
        return await record_run(
            request, [ToolsFail()], "be brief", "do it", executor.config, code=dict(CODE), secrets=[PASSED]
        )

    executor.behaviours[crashed] = collections.deque([broken])
    with build(tmp_path, executor=executor, plan=SMALL) as scheduler:
        log = scheduler.run().harness_log.read_text(encoding="utf-8")
    assert "ValueError: the catalogue refused credential [redacted]" in log
    assert PASSED not in log


def test_l08_a_defect_the_loop_catches_reaches_the_log_with_the_records_stack_hash(tmp_path, monkeypatch):
    """L08: a backend that raises inside the loop ends the attempt as a harness defect in its record, with only the
    class and the stack hash; the traceback goes to the same log, redacted, matched by that hash."""
    monkeypatch.setenv("TOYLAB_API_KEY", SECRET)
    crashed = key_of(SCHEMA, "mount-read")
    executor = FakeExecutor()

    async def lookup(request):
        request.backend.use([LookupError(f"no route for key {SECRET}")])
        record = await record_run(request, [FakeProvider({})], "be brief", "do it", executor.config, code=dict(CODE))
        executor.records.append(record)
        return record

    executor.behaviours[crashed] = collections.deque([lookup])
    with build(tmp_path, executor=executor, plan=SMALL) as scheduler:
        summary = scheduler.run()
    [record] = [r for r in executor.records if r.outcome["name"] == "harness_defect"]
    defect = record.outcome["defect"]
    assert defect["exception"] == "LookupError"
    log = summary.harness_log.read_text(encoding="utf-8")
    assert f"{crashed} attempt 1: LookupError, stack {defect['stack_sha256']}" in log
    assert "LookupError: no route for key [redacted]" in log
    assert SECRET not in log


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize(
    ("existing", "added"),
    [
        ("notes.txt\n", True),
        ("notes.txt", True),
        ("*.log\n", True),  # it would ignore the log, but nothing is read: the appended line settles it
        ("!harness-errors.log\nnotes.txt\n", True),
        (" harness-errors.log\n", True),  # a leading space is part of the pattern, which then names another file
        ("notes.txt\n/harness-errors.log\n\n", False),  # the rule is already the last line
    ],
    ids=["unrelated", "no-final-newline", "star", "negated-earlier", "leading-space", "already-last"],
)
def test_l08_an_existing_gitignore_of_the_log_directory_is_kept_and_made_to_ignore_the_log(tmp_path, existing, added):
    """L08: the author's rules stay as they are; a rule for the log is appended only when they do not ignore it."""
    ignore = tmp_path / "out" / "logs" / ".gitignore"
    ignore.parent.mkdir(parents=True)
    ignore.write_text(existing, encoding="utf-8")
    error, defect = defect_of("boom")
    path = write_defect(tmp_path / "out", error, defect, stamp="T", run_key="k", attempt=1, redactor=_Plain)
    text = ignore.read_text(encoding="utf-8")
    assert text.startswith(existing)
    assert (text != existing) is added
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", str(path)], check=False).returncode == 0
    write_defect(tmp_path / "out", error, defect, stamp="T", run_key="k", attempt=2, redactor=_Plain)
    assert ignore.read_text(encoding="utf-8") == text  # appended once


def test_r12_a_secret_whose_line_is_a_common_word_keeps_the_record_and_is_redacted_from_the_log(tmp_path, monkeypatch):
    """R12: a secret whose first line is `prompt` would rename the record's `prompt` key if its lines were secrets in a
    record; they are only in the log, where the exception group's `| ` splits the secret and over-redacting harms
    nothing."""
    secret = "prompt\nquartz-7781"
    monkeypatch.setenv("TOYLAB_TOKEN", secret)
    crashed = key_of(SCHEMA, "mount-read")
    executor = FakeExecutor()

    async def grouped(request):
        record = await record_run(request, [FakeProvider({})], "be brief", "do it", executor.config, code=dict(CODE))
        executor.records.append(record)
        raise ExceptionGroup("the batch failed", [ValueError(f"refused {secret}")])

    executor.behaviours[crashed] = collections.deque([grouped])
    with build(tmp_path, executor=executor, plan=SMALL) as scheduler:
        log = scheduler.run().harness_log.read_text(encoding="utf-8")
    [record] = [r for r in executor.records if r.run_key == crashed]
    assert "prompt" in record.to_json()  # the record was built and sealed, its keys intact
    assert "| ValueError: refused [redacted]" in log
    assert "quartz-7781" not in log


def test_r12_a_two_line_secret_in_an_exception_group_is_redacted_from_the_log(tmp_path):
    """R12: an exception group's traceback puts `| ` before every line of its members' messages, so a two-line
    secret never appears whole; each of its lines is redacted on its own."""
    secret = "amber-line-one\ncobalt-line-two"
    try:
        raise ExceptionGroup("the batch failed", [ValueError(f"refused {secret}")])
    except ExceptionGroup as error:
        defect = harness_defect_of(error)
        write_defect(tmp_path, error, defect, stamp="T", run_key="k", attempt=1, redactor=lambda: Redactor([secret]))
    log = log_path(tmp_path).read_text(encoding="utf-8")
    assert "| ValueError: refused [redacted]" in log
    assert "amber-line-one" not in log
    assert "cobalt-line-two" not in log


def test_c17_an_unwritable_log_never_costs_the_record_and_stops_dispatch_naming_it(tmp_path):
    """C17: the log cannot be written (its directory's place is taken by a file); the attempt still ends as a harness
    defect with its record and marker kept and stored, and only then does dispatch stop loudly, naming the log."""
    (tmp_path / "logs").write_text("a file where the log's directory goes\n", encoding="utf-8")
    crashed = key_of(SCHEMA, "mount-read")
    executor = FakeExecutor()

    async def lookup(request):
        request.backend.use([LookupError("no route")])
        record = await record_run(request, [FakeProvider({})], "be brief", "do it", executor.config, code=dict(CODE))
        executor.records.append(record)
        return record

    executor.behaviours[crashed] = collections.deque([lookup])
    with build(tmp_path, executor=executor, plan=SMALL) as scheduler:
        with pytest.raises(RuntimeError, match=r"the harness-error log .*harness-errors\.log could not be written"):
            scheduler.run()
        assert attempts_of(scheduler.store, crashed) == [("harness_error", 0, 1)]
    [record] = [r for r in executor.records if r.run_key == crashed]
    assert record.outcome["defect"]["exception"] == "LookupError"
    markers = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "runs").rglob("*marker.json")]
    assert [m["result"]["outcome"]["name"] for m in markers if m["run_key"] == crashed] == ["harness_defect"]


class RaisingSink:
    """An executor's own defect sink that fails, against the advice that a sink should not raise."""

    def redact_with(self, redactor):
        pass

    def __call__(self, error, defect):
        raise RuntimeError("the sink's disk is full")


def test_c17_a_custom_sink_that_raises_never_costs_the_record_and_stops_dispatch_naming_its_failure(tmp_path):
    """C17: the loop guards its sink: the attempt still ends as a harness defect with its record, marker and store row,
    and only then does dispatch stop loudly, naming what the sink raised."""
    crashed = key_of(SCHEMA, "mount-read")
    executor = FakeExecutor()

    async def lookup(request):
        request.backend.use([LookupError("no route")])
        record = await record_run(
            request, [FakeProvider({})], "be brief", "do it", executor.config, code=dict(CODE), defect_log=RaisingSink()
        )
        executor.records.append(record)
        return record

    executor.behaviours[crashed] = collections.deque([lookup])
    with build(tmp_path, executor=executor, plan=SMALL) as scheduler:
        with pytest.raises(RuntimeError, match=r"the executor's defect sink .*RuntimeError: the sink's disk is full"):
            scheduler.run()
        assert attempts_of(scheduler.store, crashed) == [("harness_error", 0, 1)]
    [record] = [r for r in executor.records if r.run_key == crashed]
    assert record.outcome["defect"]["exception"] == "LookupError"
    markers = [json.loads(p.read_text(encoding="utf-8")) for p in (tmp_path / "runs").rglob("*marker.json")]
    assert [m["result"]["outcome"]["name"] for m in markers if m["run_key"] == crashed] == ["harness_defect"]
