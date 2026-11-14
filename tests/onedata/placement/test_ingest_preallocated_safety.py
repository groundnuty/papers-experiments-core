"""The preallocated ingest's safety conditions (O44, #107): every staged copy checked before the first delete, the
reset restoring each file whole at its path and the mirror's replica of it, the old-to-new id map, and a failure
mid-start or mid-reset marking the federation unhealthy until a verified recovery."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from expcore.errors import InfraError
from expcore_onedata import ingesthealth
from expcore_onedata.client import PlatformError
from expcore_onedata.fault_journal import boot_id
from expcore_onedata.ingest import ProgressiveIngest
from expcore_onedata.ingesthealth import baseline_health, last_marks
from expcore_onedata.ingestplan import load_ingest_plan
from expcore_onedata.sandbox.kinds import INGEST_FAILURE
from expcore_onedata.testing import ALPHA_ID, BETA_ID, with_placement, write_platform

DAY = bytes((i * 31 + i // 97) % 256 for i in range(10240))
KEPT = b"an earlier day, never touched\n" * 20
DAY_PATH, KEPT_PATH = "records/day-13.csv", "records/day-12.csv"
PREFIX = 1024
ROW = {
    "label": "day-13",
    "path": DAY_PATH,
    "source": "day-13.csv",
    "at_s": 30,
    "baseline": "complete",
    "prefix": PREFIX,
    "parts": 3,
    "over_s": 60,
}
KEPT_ROW = {
    "label": "day-12",
    "path": KEPT_PATH,
    "source": "day-12.csv",
    "at_s": 10,
    "baseline": "complete",
    "prefix": 100,
}
CHANGES = ("POST", "PUT", "DELETE")


def two_providers(data):
    with_placement(data)
    data["support"]["providers"] = ["alpha", "beta"]


@pytest.fixture
def platform_file(tmp_path):
    return write_platform(tmp_path, two_providers)


@pytest.fixture
def fake(fake):
    """beta supports the space too; the dataset's baseline is two days, both held whole by the data provider."""
    fake.add_provider(BETA_ID, "beta.test")
    fake.add_file(KEPT_PATH, KEPT)
    fake.add_file(DAY_PATH, DAY)
    return fake


def write_plan(tmp_path, rows):
    staging = tmp_path / "staged"
    staging.mkdir(exist_ok=True)
    (staging / "day-13.csv").write_bytes(DAY)
    (staging / "day-12.csv").write_bytes(KEPT)
    body = "".join(
        "\n[[ingest.files]]\n" + "".join(f"{key} = {json.dumps(value)}\n" for key, value in row.items()) for row in rows
    )
    path = tmp_path / "ingest.toml"
    path.write_text('[ingest]\nstaging = "staged"\nmode = "preallocated"\n' + body, encoding="utf-8")
    return path


@pytest.fixture
def make(provisioner, config, tmp_path):
    """Build an instrument over a preallocated plan; every one a test builds shares the journal and the change log."""
    built = []

    def build(*rows):
        plan = load_ingest_plan(write_plan(tmp_path, rows), config)
        ingest = ProgressiveIngest(provisioner, plan, journal=tmp_path / "journal.jsonl", max_wait_s=0.001)
        built.append(ingest)
        return ingest

    yield build
    for ingest in built:
        ingest.stop()


def changes(fake, since=0):
    return [c for c in fake.provider_calls[since:] if c[1] in CHANGES]


def content(fake, path):
    return bytes(fake.nodes[fake.id_of(path)].content)


# -- 1. every staged copy before the first delete ----------------------------------------------------------------------


@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_o44_a_damaged_or_missing_staged_copy_of_any_file_refuses_the_start_before_anything_is_removed(
    make, fake, tmp_path, damage
):
    """O44: the second file's staged copy no longer has the plan's hash (or is gone), so the start is refused before
    any file is removed, the first one included: nothing is created, written or deleted, and both days stay whole."""
    ingest = make(ROW, KEPT_ROW)
    staged = tmp_path / "staged" / "day-12.csv"
    if damage == "missing":
        staged.unlink()
    else:
        staged.write_bytes(KEPT[:-1] + b"?")
    with pytest.raises(InfraError) as refused:
        ingest.start({})
    assert refused.value.kind == INGEST_FAILURE
    assert changes(fake) == []
    assert (content(fake, DAY_PATH), content(fake, KEPT_PATH)) == (DAY, KEPT)
    assert (fake.held(DAY_PATH, ALPHA_ID), fake.held(DAY_PATH, BETA_ID)) == ([[0, len(DAY)]], [])


# -- 2. the reset restores each file whole, and the mirror's replica of it -------------------------------------------


MIRRORED = {**ROW, "replicas": ["beta"]}


@pytest.fixture
def mirrored(fake, ticker):
    """beta, the mirror, holds a replica of the day in the baseline, as an outage's mirror variant reads it."""
    fake.add_transfer("replication", DAY_PATH, BETA_ID, scheduled=ticker.now)
    assert fake.held(DAY_PATH, BETA_ID) == [[0, len(DAY)]]
    return fake


def test_o44_the_reset_restores_the_mirrors_replica_of_the_file_the_start_made_again(make, mirrored, ticker):
    """O44: the start removed the day with its mirror's replica and made it again, so the mirror holds nothing of the
    new file during the run; the reset completes it in place at the data provider, by its hash and the owner's block
    map, and replicates it to the mirror, whose own block map then covers it, so a run that reads through the mirror
    next starts from the baseline's mirror state."""
    ingest = make(MIRRORED)
    ingest.start({})
    assert (mirrored.held(DAY_PATH, ALPHA_ID), mirrored.held(DAY_PATH, BETA_ID)) == ([[0, PREFIX]], [])
    ingest.at_first_turn({"clock_s": 0.0})
    snapshot = ingest.finish()
    ingest.check()
    assert content(mirrored, DAY_PATH) == DAY
    assert (mirrored.held(DAY_PATH, ALPHA_ID), mirrored.held(DAY_PATH, BETA_ID)) == ([[0, len(DAY)]], [[0, len(DAY)]])
    assert snapshot["restore"]["files"]["day-13"]["replicas"] == {"providers[1]": "replicated"}
    assert snapshot["start"]["restore"]["day-13"]["replicas"] == {"providers[1]": "held"}


def test_o44_a_reset_whose_replication_places_nothing_on_the_mirror_fails(make, mirrored):
    """O44: the replication reports completed and the mirror's block map shows nothing of the file: the map judges,
    so the reset fails and the post-run check refuses the attempt; the data provider's copy is whole all the same."""
    ingest = make(MIRRORED)
    ingest.start({})
    ingest.at_first_turn({"clock_s": 0.0})
    mirrored.transfers_place_blocks = False
    snapshot = ingest.finish()
    assert snapshot["restore"]["ok"] is False
    assert snapshot["restore"]["files"]["day-13"]["ok"] is False
    with pytest.raises(InfraError, match="restore"):
        ingest.check()
    assert (content(mirrored, DAY_PATH), mirrored.held(DAY_PATH, BETA_ID)) == (DAY, [])


def test_o44_a_replica_the_plan_does_not_declare_refuses_the_start_before_anything_is_removed(make, mirrored):
    """O44: the mirror holds the day but the plan declares no replica: the start would remove it and the reset would
    not bring it back, so the start is refused with nothing removed, the mirror's replica kept."""
    ingest = make(ROW)
    deletes = sum(1 for c in mirrored.provider_calls if c[1] == "DELETE")
    with pytest.raises(InfraError, match="replicas"):
        ingest.start({})
    assert sum(1 for c in mirrored.provider_calls if c[1] == "DELETE") == deletes
    assert (content(mirrored, DAY_PATH), mirrored.held(DAY_PATH, BETA_ID)) == (DAY, [[0, len(DAY)]])


def test_o44_a_replica_on_the_data_provider_is_refused_when_the_instrument_is_built(
    provisioner, config, fake, tmp_path
):
    """O44: the data provider holds the file whole by definition; naming it as a replica is a plan the reset cannot
    keep apart from the owner's copy, so it is refused before anything is read."""
    plan = load_ingest_plan(write_plan(tmp_path, [{**ROW, "replicas": ["alpha"]}]), config)
    with pytest.raises(ValueError, match="data provider"):
        ProgressiveIngest(provisioner, plan, journal=tmp_path / "journal.jsonl")


def test_o44_a_reset_is_verified_by_the_owners_block_map_as_well_as_by_the_hash(make, fake):
    """O44: the reset writes the rest and the bytes read back have the plan's hash, but the data provider's block map
    keeps showing only the prefix: the file is not whole there, so the reset fails rather than pass on the hash."""
    ingest = make(ROW)
    ingest.start({})
    ingest.at_first_turn({"clock_s": 0.0})
    blocks = {"physicalSize": PREFIX, "blocks": [[0, PREFIX]]}
    fake.distribution_entries[(fake.id_of(DAY_PATH), ALPHA_ID)] = {
        "virtualSize": len(DAY),
        "distributionPerStorageBackend": {"storage-1": blocks},
    }
    snapshot = ingest.finish()
    assert content(fake, DAY_PATH) == DAY
    assert snapshot["restore"]["ok"] is False
    assert "block map" in snapshot["restore"]["files"]["day-13"]["detail"]


# -- 3. the old-to-new id map, and the start's duration ----------------------------------------------------------------


def test_o44_the_snapshot_maps_each_recreated_files_old_id_to_its_new_one_and_times_the_start(make, fake, ticker):
    """O44: a study's id map is keyed by file id (a data gate's rows are), so the snapshot maps the id each file had in
    the baseline to the one the start made, the id the run's reads and the reset use, for every file it made again;
    and it records the start's seconds, spent before the first turn and so outside the agent's time."""
    old = {label: fake.id_of(path) for label, path in (("day-13", DAY_PATH), ("day-12", KEPT_PATH))}
    ingest = make(ROW, KEPT_ROW)
    began = ticker.now
    ingest.start({})
    took = ticker.now - began
    new = {label: fake.id_of(path) for label, path in (("day-13", DAY_PATH), ("day-12", KEPT_PATH))}
    ingest.at_first_turn({"clock_s": 0.0})
    snapshot = ingest.finish()
    assert snapshot["id_map"] == {old[label]: new[label] for label in old}
    assert all(snapshot["files"][label]["restored_id"] == new[label] for label in old)
    assert snapshot["start_s"] == snapshot["start"]["seconds"] == round(took, 3)
    assert snapshot["start_s"] > 0


# -- 4. a failure mid-start or mid-reset marks the federation unhealthy until a verified recovery ---------------------


class Died(BaseException):
    """The run's process dies here: nothing after it runs, neither the start's own recovery nor the finish."""


STEPS = ("delete", "create", "extend", "prefix")


def is_step(step, method, path, options):
    """Whether a change the provisioner sends is the call of the step: the start state's removal, the new file's
    creation, its extension (the empty write at the final size), its prefix, or the reset's first write of content."""
    if step == "delete":
        return method == "DELETE"
    if step == "create":
        return method == "POST" and path.endswith("/children") and options["params"]["type"] == "REG"
    if method != "PUT" or not path.endswith("/content"):
        return False
    offset, body = int(options["params"]["offset"]), options.get("content", b"")
    return {"extend": offset == len(DAY) and not body, "prefix": offset == 0 and bool(body)}.get(step, bool(body))


class Fault:
    """The provisioner's changes, with one step made to fail once its call has reached the platform: the process dies
    there (`dies`), the answer is lost once (`once`), or the answer is lost and every later change fails until the
    test brings the provider back (`down`)."""

    def __init__(self, provisioner):
        self._real, self._provisioner = provisioner.mutate, provisioner
        self.step = self.how = None
        self.down = False
        provisioner.mutate = self

    def arm(self, step, how):
        self.step, self.how = step, how

    def __call__(self, method, path, **options):
        if self.down:
            raise PlatformError("providers[0]: a change failed (ConnectError)", transport=True)
        answer = self._real(method, path, **options)
        if self.step is not None and is_step(self.step, method, path, options):
            self.step = None
            if self.how == "dies":
                raise Died
            self.down = self.how == "down"
            raise PlatformError("providers[0]: the answer was lost (ReadTimeout)", transport=True)
        return answer

    def remove(self):
        del self._provisioner.mutate


@pytest.fixture
def dead_holder():
    """A holder whose process is gone: a child that has exited and been reaped, in this boot."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return {"pid": child.pid, "boot_id": boot_id()}


def mark_of(tmp_path):
    return last_marks(tmp_path / "journal.jsonl")["spaces[0]"]


def assert_recovered(make, fake, tmp_path):
    """No start passes the marked baseline; the recovery brings the day back whole at the data provider and on the
    mirror, verified, and marks it whole; the health check passes again, and so does the next start."""
    since = len(fake.provider_calls)
    refused = make(MIRRORED)
    with pytest.raises(InfraError, match="not whole") as error:
        refused.start({})
    assert error.value.kind == INGEST_FAILURE
    refused.finish()  # its reset brings nothing back: it changed nothing, and a refused start never recovers
    assert changes(fake, since) == []
    assert baseline_health([tmp_path / "journal.jsonl"]).ok is False
    recovered = make(MIRRORED).recover()
    assert recovered["ok"]
    assert recovered["files"]["day-13"]["ok"]
    assert content(fake, DAY_PATH) == DAY
    assert (fake.held(DAY_PATH, ALPHA_ID), fake.held(DAY_PATH, BETA_ID)) == ([[0, len(DAY)]], [[0, len(DAY)]])
    assert (mark_of(tmp_path)["state"], mark_of(tmp_path)["phase"]) == ("whole", "recover")
    assert baseline_health([tmp_path / "journal.jsonl"]).ok
    again = make(MIRRORED)
    again.start({})
    assert fake.held(DAY_PATH, ALPHA_ID) == [[0, PREFIX]]


@pytest.mark.parametrize("step", [*STEPS, "reset"])
def test_o44_a_run_that_dies_at_any_step_leaves_its_baseline_open_and_every_start_refused_until_a_recovery(
    make, mirrored, provisioner, tmp_path, monkeypatch, dead_holder, step
):
    """O44: the run's process dies after the start state's removal, after the new file's creation, after its extension,
    after its prefix, or in the middle of the reset, so the day is missing, empty, unwritten, a prefix or partly
    completed, and its mirror holds nothing of it. The journal's mark, written before the first change, stays open
    with a holder that is gone: the health check fails, and every start is refused with nothing sent, until the
    recovery brings it back whole, the mirror's replica included."""
    fault = Fault(provisioner)
    ingest = make(MIRRORED)
    with monkeypatch.context() as patch:
        patch.setattr(ingesthealth, "holder", lambda: dead_holder)
        if step == "reset":
            ingest.start({})
            ingest.at_first_turn({"clock_s": 0.0})
        fault.arm(step, "dies")
        with pytest.raises(Died):
            ingest.finish() if step == "reset" else ingest.start({})
    assert not mirrored.exists(DAY_PATH) or content(mirrored, DAY_PATH) != DAY  # the baseline is not whole
    assert mark_of(tmp_path)["state"] == "open"
    health = baseline_health([tmp_path / "journal.jsonl"])
    assert not health.ok
    assert "stopped during its" in health.detail
    assert_recovered(make, mirrored, tmp_path)
    fault.remove()


@pytest.mark.parametrize("step", [*STEPS, "reset"])
def test_o44_a_failure_at_any_step_whose_recovery_fails_too_marks_the_baseline_unhealthy_until_a_recovery(
    make, mirrored, provisioner, tmp_path, step
):
    """O44: a step's answer is lost and the provider then refuses every change, so neither the start's own recovery
    nor the finish can bring the day back; the journal marks the baseline unhealthy, naming the phase and, in the start,
    the file and its step, the health check fails, and every later start is refused with nothing sent until the
    recovery, once the provider is back, verifies the day whole at its owner and on its mirror."""
    fault = Fault(provisioner)
    ingest = make(MIRRORED)
    if step == "reset":
        ingest.start({})
        ingest.at_first_turn({"clock_s": 0.0})
        fault.arm(step, "down")
    else:
        fault.arm(step, "down")
        with pytest.raises(InfraError):
            ingest.start({})
    snapshot = ingest.finish()  # the executor's finally: the provider is still down
    assert snapshot["restore"]["ok"] is False
    mark = mark_of(tmp_path)
    assert (mark["state"], mark["phase"]) == ("unhealthy", "restore")
    marks = [
        json.loads(line)
        for line in (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("entry") == "baseline"
    ]
    if step != "reset":
        failed = next(m for m in marks if m["state"] == "unhealthy")
        assert (failed["phase"], failed["file"]) == ("start", "day-13")
    health = baseline_health([tmp_path / "journal.jsonl"])
    assert not health.ok
    assert "failed" in health.detail
    fault.down = False
    assert_recovered(make, mirrored, tmp_path)
    fault.remove()


def test_o44_a_failed_start_whose_own_recovery_verifies_the_baseline_leaves_it_whole_for_the_next_start(
    make, fake, provisioner, tmp_path
):
    """O44: the prefix's answer is lost once; the start fails and marks the baseline unhealthy before its own recovery,
    which completes the day in place, verified, and marks it whole, as a fault injector's verified clear settles an
    injection: the health check passes and the next start sets the start state."""
    fault = Fault(provisioner)
    fault.arm("prefix", "once")
    with pytest.raises(InfraError):
        make(ROW).start({})
    states = [
        (m["state"], m["phase"])
        for m in map(json.loads, (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines())
        if m.get("entry") == "baseline"
    ]
    assert states == [("open", "start"), ("unhealthy", "start"), ("whole", "start")]
    assert content(fake, DAY_PATH) == DAY
    assert baseline_health([tmp_path / "journal.jsonl"]).ok
    again = make(ROW)
    again.start({})
    assert fake.held(DAY_PATH, ALPHA_ID) == [[0, PREFIX]]
    fault.remove()


def test_o44_a_recovery_is_refused_while_another_live_process_holds_the_baseline(make, fake, tmp_path, monkeypatch):
    """O44: the open mark's holder is another process that still runs, a run in progress: the recovery would write
    into its files, so it is refused with nothing sent, and the health check reads the run as in progress."""
    alive = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        with monkeypatch.context() as patch:
            patch.setattr(ingesthealth, "holder", lambda: {"pid": alive.pid, "boot_id": boot_id()})
            make(ROW).start({})
        since = len(fake.provider_calls)
        with pytest.raises(InfraError, match="another process"):
            make(ROW).recover()
        assert changes(fake, since) == []
        assert baseline_health([tmp_path / "journal.jsonl"]).ok
    finally:
        alive.kill()
        alive.wait()


def test_o44_a_journal_line_a_crash_cut_short_is_cut_off_before_the_next_line_is_written(make, fake, tmp_path, ticker):
    """O44: a crash in the middle of a journal write leaves a torn last line. The next line written cuts it off first,
    so no mark is ever glued to it: the journal stays readable, line by line, and its last mark is the one written."""
    first = make(ROW)
    first.start({})
    first.at_first_turn({"clock_s": 0.0})
    first.finish()
    journal = tmp_path / "journal.jsonl"
    with open(journal, "a", encoding="utf-8") as handle:
        handle.write('{"space": "spaces[0]", "entry": "id", "lab')  # the process died while writing this
    again = make(ROW)
    again.start({})
    lines = journal.read_text(encoding="utf-8").splitlines()
    assert all(isinstance(json.loads(line), dict) for line in lines)
    assert (mark_of(tmp_path)["state"], mark_of(tmp_path)["phase"]) == ("open", "run")
    again.at_first_turn({"clock_s": 0.0})
    again.finish()
    again.check()
    assert mark_of(tmp_path)["state"] == "whole"


def test_o44_a_start_admitted_between_anothers_check_and_its_mark_refuses_the_other(make, fake, tmp_path, monkeypatch):
    """O44: two instruments over one journal: the second starts while the first is between reading the baseline's mark
    and writing its own (hashing its staged copies). The mark is read again under the journal's admission lock, so
    the first is refused once the second holds the baseline open, and never completes the second's sparse files."""
    first, second = make(ROW), make(ROW)
    hashing = first._base.check_sources

    def meanwhile():
        hashing()
        second.start({})  # the other run is admitted while this one has read the mark and not yet written its own

    monkeypatch.setattr(first._base, "check_sources", meanwhile)
    with pytest.raises(InfraError, match="held open"):
        first.start({})
    assert (mark_of(tmp_path)["state"], mark_of(tmp_path)["phase"]) == ("open", "run")
    assert fake.held(DAY_PATH, ALPHA_ID) == [[0, PREFIX]]  # the second's start state, untouched by the first
    second.at_first_turn({"clock_s": 0.0})
    second.finish()
    second.check()


@pytest.mark.parametrize("plan", ["no replicas", "another file only"])
def test_o44_a_recovery_with_another_plan_than_the_runs_is_refused_and_clears_nothing(
    make, mirrored, provisioner, tmp_path, monkeypatch, dead_holder, plan
):
    """O44: a mirrored run dies after the start made the day again, its mirror holding nothing. A recovery with a plan
    that leaves out the mirror, or the day, could not verify what the run left, so it is refused with nothing sent and
    the baseline stays marked; the run's own plan recovers it, the mirror's replica included."""
    fault = Fault(provisioner)
    with monkeypatch.context() as patch:
        patch.setattr(ingesthealth, "holder", lambda: dead_holder)
        fault.arm("prefix", "dies")
        with pytest.raises(Died):
            make(MIRRORED).start({})
    other = make(ROW) if plan == "no replicas" else make(KEPT_ROW)
    since = len(mirrored.provider_calls)
    with pytest.raises(InfraError, match="another plan"):
        other.recover()
    assert changes(mirrored, since) == []
    assert mark_of(tmp_path)["state"] == "open"
    assert not baseline_health([tmp_path / "journal.jsonl"]).ok
    assert make(MIRRORED).recover()["ok"]
    assert mirrored.held(DAY_PATH, BETA_ID) == [[0, len(DAY)]]
    fault.remove()


def test_o44_the_journal_and_its_directories_are_made_durable_when_the_first_line_creates_them(
    provisioner, config, fake, tmp_path, monkeypatch
):
    """O44: the first line of a new journal creates the file, and perhaps its directories; each new entry is pushed to
    disk in its parent before the line counts as written, so a host crash after the start's first removal cannot lose
    the journal, and with it the mark that refuses the next start. A later line creates nothing and syncs no
    directory."""
    synced = []
    real = os.fsync

    def fsync(descriptor):
        synced.append(Path(os.readlink(f"/proc/self/fd/{descriptor}")))
        real(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)
    journal = tmp_path / "new" / "deeper" / "journal.jsonl"
    plan = load_ingest_plan(write_plan(tmp_path, [ROW]), config)
    ingest = ProgressiveIngest(provisioner, plan, journal=journal, max_wait_s=0.001)
    ingest.start({})
    first = synced.index(journal)  # the first line, the open mark, before anything is removed
    assert {tmp_path, tmp_path / "new"} <= set(synced[:first])  # each new directory, in its parent
    assert synced[first + 1] == journal.parent  # the new journal's own entry, before anything is removed
    ingest.at_first_turn({"clock_s": 0.0})
    ingest.finish()
    later = synced[first + 2 :]
    assert later.count(journal.parent) == 0  # no later line creates anything
    assert last_marks(journal)["spaces[0]"]["state"] == "whole"
