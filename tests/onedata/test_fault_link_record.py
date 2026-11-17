"""O35: after a heal, or a restore's link check, the link-check record on disk is the latest check's verdict and time.

The restore's and the heal's reads of every pair go through the platform layer's probe (`ServingProbe.matrix`), which
writes the record, in `up`'s format, from the report of the check it just made. These tests run the whole restore, and
the fault gate's whole heal, over the scripted federations of the heal's own tests, with only the platform layer's
all-pairs check (`check_links`) replaced by the report the federation's script gives, on the test's clock, so the
record can be compared with the check that made it. The probe's own behaviour, over the fake federation's real check,
is in `tests/onedata/placement/test_link_record.py`.
"""

import contextlib
import json
import threading

from fault_support import FakeClock, injector_for
from gate_heal_support import healed
from gate_support import EVERY, Federation, gate_of
from heal_support import RESTORED, STORES, HealServices, four_providers, stale

from expcore_onedata import fault_serves
from expcore_onedata.admission import FederationLock
from expcore_onedata.config import load_platform
from expcore_onedata.fault_epoch import LinkEpoch
from expcore_onedata.fault_serves import ServingProbe
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.linkcheck import REMEDY, LinkReport, PairResult
from expcore_onedata.linkrecord import RECORD, record_text
from expcore_onedata.testing import write_platform
from expcore_onedata.testing.world import with_link_check

RUN = {"run_key": "task=task-a|model=m1|seed=1", "attempt": 1}
SERVICE = STORES["alpha"]


def report_of(matrix, started, ended) -> LinkReport:
    """The platform layer's report for a scripted matrix, as the real check would make it: a stuck pair failed its
    fetch (a remedy), a pair that passed carries its seconds and bytes."""
    pairs = tuple(
        PairResult(
            pair.reader,
            pair.source,
            pair.ok,
            pair.seconds,
            65536 if pair.ok else None,
            reason=None if pair.ok else f"no transfer from {pair.source} to {pair.reader} within 30 s",
            remedy=REMEDY.format(reader=pair.reader) if pair.stuck else None,
        )
        for pair in matrix.pairs
    )
    return LinkReport(matrix.ok, matrix.detail, pairs, started, ended)


class Wired:
    """The platform layer's probe over a scripted federation: every all-pairs check goes through
    `ServingProbe.matrix`, whose check is replaced by `script` (the federation's own, which notes the read), on the
    clock `now`. `reports` holds every report made, in order, and `on_disk` the record's verdict as it stood when each
    check began (None: no record yet)."""

    def __init__(self, platform, now, script, monkeypatch) -> None:
        config = load_platform(platform, require_secrets=False)
        self.settings = load_faults(platform, config)
        self.directory = platform.parent
        self.reports: list[LinkReport] = []
        self.on_disk: list[bool | None] = []
        self._now, self._script = now, script
        self._probe = ServingProbe(config, self.settings, None)  # the client is only the replaced check's
        monkeypatch.setattr(fault_serves, "check_links", self._check)

    def _check(self, config, client) -> LinkReport:
        self.on_disk.append(self.record_ok())
        started = self._now()
        matrix = self._script()
        report = report_of(matrix, started, self._now())
        self.reports.append(report)
        return report

    def matrix(self, service):
        return self._probe.matrix(service)

    def record_ok(self) -> bool | None:
        path = self.directory / RECORD
        return json.loads(path.read_text(encoding="utf-8"))["ok"] if path.exists() else None

    def text(self) -> str:
        return (self.directory / RECORD).read_text(encoding="utf-8")


class WiredGate(Federation):
    """The gate's federation, whose heal reads its pairs through the platform layer's probe."""

    wired: Wired

    def link_matrix(self, service):
        return self.wired.matrix(service)


class WiredRestore(HealServices):
    """The restore's federation, whose link check and heal read the pairs through the platform layer's probe."""

    wired: Wired

    def link_matrix(self, service):
        return self.wired.matrix(service)


def wire_gate(tmp_path, monkeypatch, federation=None, **faults):
    gate, federation, _, settings = gate_of(tmp_path, federation or WiredGate(), edit=with_link_check, **faults)
    wired = Wired(settings.file, federation.now, lambda: Federation.link_matrix(federation, ""), monkeypatch)
    federation.wired = wired
    return gate, federation, wired, settings


def wire_restore(tmp_path, monkeypatch, services, **faults):
    def document(data):
        four_providers(data, None, **faults)
        with_link_check(data)

    platform = write_platform(tmp_path, document)
    clock = FakeClock()
    services.clock = clock
    wired = Wired(platform, lambda: clock.now, lambda: HealServices.link_matrix(services, ""), monkeypatch)
    services.wired = wired
    return injector_for(platform, services, target=RESTORED, clock=clock), wired


# --- the fault gate's heal -------------------------------------------------------------------------------------------


def test_o35_after_a_passing_heal_the_record_holds_the_rechecks_verdict_and_time_in_ups_format(tmp_path, monkeypatch):
    """O35: the heal ends with a re-check of every pair that passes; the record on disk, which before it was the
    outage's, is that re-check's own: its verdict, its window on the clock, in `up`'s bytes. The heal's first read left
    its own failing verdict meanwhile, so the record was never older than the check before it."""
    gate, federation, wired, _ = wire_gate(tmp_path, monkeypatch, heal="all_links")
    assert wired.record_ok() is None
    reading = healed(gate, federation)
    assert reading.ok is True, reading.detail
    assert gate.last_heal["outcome"] == "healed"
    assert [report.ok for report in wired.reports] == [False, True]  # the first read found the pair stuck
    assert wired.on_disk == [None, False]  # the re-check began over the failing verdict the first read left
    last = wired.reports[-1]
    assert wired.text() == record_text(last)
    record = json.loads(wired.text())
    assert record["ok"] is True
    healed_at = [at for kind, at in federation.reads if kind == "heal"]
    assert record["window"]["end_epoch_s"] == healed_at[-1]  # the re-check's own time on the clock
    assert record["window"]["end_epoch_s"] > wired.reports[0].ended  # after the settle, not the outage's
    assert len(record["pairs"]) == 12  # every ordered pair of the four providers
    assert all(pair["ok"] and pair["bytes"] == 65536 for pair in record["pairs"])


def test_o35_a_heal_that_leaves_a_pair_stuck_leaves_the_failing_verdict_not_the_older_pass(tmp_path, monkeypatch):
    """O35: a heal whose re-check still finds the pair stuck ends `links_stuck`; the record is that failing re-check's,
    which names the pair, and no pass of the check before it."""
    federation = WiredGate()
    federation.link_fixes = dict.fromkeys(EVERY, ())  # no restart mends anything
    gate, federation, wired, _ = wire_gate(
        tmp_path, monkeypatch, federation, heal="all_links", heal_container_fallback=False
    )
    assert gate().deferred is True
    assert gate.wait_for_heal(10)
    assert gate.last_heal["outcome"] == "links_stuck"
    record = json.loads(wired.text())
    assert record["ok"] is False
    assert [(p["reader"], p["source"]) for p in record["pairs"] if not p["ok"]] == [("providers[2]", "providers[0]")]
    assert wired.text() == record_text(wired.reports[-1])


def test_o35_writing_the_record_leaves_the_link_epoch_and_the_journal_alone(tmp_path, monkeypatch):
    """O35: the record is advisory and the epoch is what a run's admission compares (`admission_recheck`): a write of
    the record, passing or failing, neither raises nor lowers the epoch, takes none of the journal's files and puts
    none in the journal's directory. The epoch's raises are the gate's (the check that found the pair failing) and
    the heal's, before its first action."""
    gate, federation, wired, settings = wire_gate(tmp_path, monkeypatch, heal="all_links")
    epochs, written = [], []
    real = fault_serves.write_record

    def spy(report, path):
        epochs.append(LinkEpoch(settings.journal).read())
        real(report, path)
        epochs.append(LinkEpoch(settings.journal).read())
        written.append(sorted(p.name for p in settings.journal.iterdir()))

    monkeypatch.setattr(fault_serves, "write_record", spy)
    healed(gate, federation)
    assert len(wired.reports) == 2  # so the record was written twice, the second over the first
    assert all(before == after and before is not None for before, after in zip(epochs[::2], epochs[1::2], strict=True))
    assert epochs[0] is not None
    assert epochs[0] == epochs[-1]  # the same record at the first write and after the last
    assert (epochs[0]["epoch"], epochs[0]["event"]) == (2, "heal")  # the gate's raise (1), then the heal's: earlier
    assert all(RECORD not in names for names in written)
    assert (settings.file.parent / RECORD).exists()


# --- the restore's link check ---------------------------------------------------------------------------------------


def test_o35_a_restores_passing_link_check_writes_its_verdict_and_time(tmp_path, monkeypatch):
    """O35: a restore with every pair passing is verified, and the record is that check's verdict and time."""
    services = WiredRestore()
    services.stuck = set()
    injector, wired = wire_restore(tmp_path, monkeypatch, services)
    injector.start(RUN)
    assert wired.record_ok() is None
    assert injector.clear() is True
    (report,) = wired.reports
    assert report.ok
    assert wired.text() == record_text(report)
    assert json.loads(wired.text())["window"]["end_epoch_s"] == services.checked_at[-1]


def test_o35_a_restores_failing_link_check_writes_its_failing_verdict_over_an_older_pass(tmp_path, monkeypatch):
    """O35: a restore whose check finds a pair stuck, with no heal declared, fails `links_stuck`, and the record is that
    failing check's: the latest verdict on disk is the latest check's, so a pre-check never reads the older pass."""
    services = WiredRestore()
    services.stuck = set()
    injector, wired = wire_restore(tmp_path, monkeypatch, services)
    wired.matrix(SERVICE)  # an earlier check passed, and left its record
    assert wired.record_ok() is True
    services.stuck = stale().stuck  # the outage left the crashed source's readers stuck
    injector.start(RUN)
    assert injector.clear() is False
    assert wired.record_ok() is False
    assert wired.text() == record_text(wired.reports[-1])
    assert json.loads(wired.text())["ok"] is False


def test_o35_a_restores_heal_ends_with_the_passing_rechecks_record(tmp_path, monkeypatch):
    """O35: a restore that heals ends with the heal's passing re-check, and that check's record is on disk, over the
    failing ones the restore's own check and the heal's first read left."""
    services = WiredRestore()
    services.stuck = stale().stuck
    services.wedge_s = 20.0
    injector, wired = wire_restore(tmp_path, monkeypatch, services, heal="all_links")
    injector.start(RUN)
    assert injector.clear() is True
    assert wired.reports[0].ok is False  # the restore's own check found the pairs stuck
    assert wired.reports[-1].ok is True  # the heal's re-check after its settle mended them
    assert wired.on_disk[-1] is False
    assert wired.text() == record_text(wired.reports[-1])
    assert json.loads(wired.text())["window"]["end_epoch_s"] == services.checked_at[-1]


# --- a record that was not written is said in the run's facts ---------------------------------------------------------


def no_lock(monkeypatch) -> None:
    """The record's lock is never taken (the `up` note's case on a root whose `flock` the filesystem refuses)."""

    @contextlib.contextmanager
    def unlocked(path):
        yield False

    monkeypatch.setattr(fault_serves, "recording", unlocked)


def refused_writes(monkeypatch) -> None:
    """The lock is taken and the write is refused (a read-only root, a full disk)."""

    def refused(report, path):
        raise PermissionError(13, "read-only file system")

    monkeypatch.setattr(fault_serves, "write_record", refused)


def said(reading) -> dict:
    """The two optional facts of a reading, those it holds."""
    return {key: reading[key] for key in ("link_record", "link_record_reason") if key in reading}


def mentioning(snapshot) -> list:
    """Every place of the snapshot that holds the fact: its top level and each step."""
    return [place for place in (snapshot, *snapshot["steps"]) if "link_record" in place]


NOT_TAKEN = {"link_record": "not_written", "link_record_reason": "lock_not_taken"}
NOT_WRITTEN = {"link_record": "not_written", "link_record_reason": "write_failed"}


def test_o35_a_restore_whose_check_wrote_its_record_says_nothing_of_it(tmp_path, monkeypatch):
    """O35: the optional fact is absent, never null, from the snapshot and from each of its steps, when the record was
    written as it always was."""
    services = WiredRestore()
    services.stuck = set()
    injector, _ = wire_restore(tmp_path, monkeypatch, services)
    injector.start(RUN)
    assert injector.clear() is True
    snapshot = injector.snapshot()
    assert mentioning(snapshot) == []
    assert "link_record_reason" not in json.dumps(snapshot)


def test_o35_a_restore_whose_check_could_not_take_the_lock_says_so_in_the_snapshot(tmp_path, monkeypatch):
    """O35: the log line is not the run's record. The restore's check made without the record's lock writes nothing, and
    the snapshot the run's record keeps says `link_record: "not_written"` with `lock_not_taken`, at its top level and
    as a step of its own; the restore is verified all the same, since the record is advisory."""
    services = WiredRestore()
    services.stuck = set()
    injector, wired = wire_restore(tmp_path, monkeypatch, services)
    no_lock(monkeypatch)
    injector.start(RUN)
    assert injector.clear() is True
    snapshot = injector.snapshot()
    assert snapshot["restore_verified"] is True
    assert said(snapshot) == NOT_TAKEN
    (step,) = [step for step in snapshot["steps"] if step["step"] == "link_record"]
    assert (step["ok"], said(step)) == (False, NOT_TAKEN)
    assert wired.record_ok() is None  # nothing was written


def test_o35_a_restore_whose_record_could_not_be_written_says_write_failed_in_the_snapshot(tmp_path, monkeypatch):
    """O35: the other reason, a write the directory refused, is its own word, and holds no path or error text."""
    services = WiredRestore()
    services.stuck = set()
    injector, _ = wire_restore(tmp_path, monkeypatch, services)
    refused_writes(monkeypatch)
    injector.start(RUN)
    assert injector.clear() is True
    snapshot = injector.snapshot()
    assert said(snapshot) == NOT_WRITTEN
    assert str(tmp_path) not in json.dumps(snapshot)


def test_o35_a_heal_whose_reads_could_not_take_the_lock_says_so_in_its_matrices_and_the_snapshot(tmp_path, monkeypatch):
    """O35: a restore that heals reads the pairs again and again; each read made without the lock says so in its matrix
    (the heal's `first`, `before` and `after`), and the snapshot says it once, at the top, for the run."""
    services = WiredRestore()
    services.stuck = stale().stuck
    services.wedge_s = 20.0
    injector, _ = wire_restore(tmp_path, monkeypatch, services, heal="all_links")
    no_lock(monkeypatch)
    injector.start(RUN)
    assert injector.clear() is True
    snapshot = injector.snapshot()
    assert said(snapshot) == NOT_TAKEN
    (heal,) = [step for step in snapshot["steps"] if step["step"] == "heal"]
    assert [said(heal[key]) for key in ("first", "before", "after")] == [NOT_TAKEN] * 3


def test_o35_a_recovery_that_settles_an_earlier_runs_injection_says_it_in_its_recovered_step(tmp_path, monkeypatch):
    """O35: the start's recovery of an injection an earlier run left open (`recovered`) carries the fact in its own
    step, as it carries the gate's and the heal's records, and the snapshot says it too."""
    services = WiredRestore()
    services.stuck = set()
    first, wired = wire_restore(tmp_path, monkeypatch, services)
    first.start(RUN)
    first._unlock()  # the run's process ends: the kernel drops its lock on the target, and its admission
    first._release_admission()
    no_lock(monkeypatch)
    following = injector_for(wired.settings.file, services, target=RESTORED, clock=services.clock)
    with FederationLock(wired.settings).held(exclusive=True, timeout_s=5):
        following.start({"run_key": "the-next-run", "attempt": 1})
        (recovered,) = [step for step in following.snapshot()["steps"] if step["step"] == "recovered"]
        assert following.clear() is True
    assert recovered["ok"] is True
    assert said(recovered) == NOT_TAKEN
    assert said(following.snapshot()) == NOT_TAKEN


def test_o35_the_fault_gates_heal_says_it_in_its_record_when_its_reads_were_not_written(tmp_path, monkeypatch):
    """O35: the gate's heal has no run and no snapshot; its record (`PlatformHealth.gate_heal`, the `ended` line) holds
    the pairs it read, and a read whose record was not written says so there."""
    gate, federation, _, _ = wire_gate(tmp_path, monkeypatch, heal="all_links")
    no_lock(monkeypatch)
    reading = healed(gate, federation)
    assert reading.ok is True, reading.detail
    assert gate.last_heal["outcome"] == "healed"
    assert said(gate.last_heal["after"]) == NOT_TAKEN


# --- checks that overlap ---------------------------------------------------------------------------------------------


def test_o35_a_check_that_finishes_late_never_replaces_the_record_of_a_check_that_started_after_it(
    tmp_path, monkeypatch
):
    """O35: one check passes but is slow, and another starts meanwhile and finds a pair stuck (a check that overlaps
    because something other than the federation's exclusive admission, `up` say, ran beside it). The second waits for
    the first's check and record, runs after it, and its failing verdict is the one left: the record is the
    latest-started check's, never an older pass that finished late."""
    platform = write_platform(tmp_path, lambda data: (four_providers(data, None), with_link_check(data)))
    config = load_platform(platform, require_secrets=False)
    probe = ServingProbe(config, load_faults(platform, config), None)
    fetched = PairResult("providers[1]", "providers[0]", True, 0.1, 1)
    slow_passing = LinkReport(True, "every pair passed", (fetched,), 1, 2)
    remedy = REMEDY.format(reader="providers[1]")
    stuck = PairResult("providers[1]", "providers[0]", False, 30.0, reason="no transfer", remedy=remedy)
    failing_report = LinkReport(False, "a provider link is stuck", (stuck,), 3, 4)
    first_in, release, second_in = threading.Event(), threading.Event(), threading.Event()
    calls: list[str] = []

    def check(config, client):
        if not calls:
            calls.append("first starts")
            first_in.set()
            release.wait(30)
            calls.append("first ends")
            return slow_passing
        calls.append("second starts")
        second_in.set()
        return failing_report

    monkeypatch.setattr(fault_serves, "check_links", check)
    first = threading.Thread(target=probe.matrix, args=(SERVICE,))
    first.start()
    assert first_in.wait(30)
    second = threading.Thread(target=probe.matrix, args=(SERVICE,))
    second.start()
    assert not second_in.wait(0.3)  # its check does not begin while the first is under way
    release.set()
    first.join(30)
    second.join(30)
    assert calls == ["first starts", "first ends", "second starts"]
    assert (tmp_path / RECORD).read_text(encoding="utf-8") == record_text(failing_report)
