"""The preallocated mode of the progressive ingest: each planned file at its final size from the start with only its
declared prefix written, the rest written in parts from the agent's first turn, each state confirmed on the block map,
and the reset completing the file in place without removing it (O44, #107)."""

import json
import time

import pytest

from expcore.errors import InfraError
from expcore.loop.timer import AgentTimer
from expcore.records.instruments import first_turn_instruments
from expcore_onedata.client import PlatformError
from expcore_onedata.ingest import ProgressiveIngest
from expcore_onedata.ingestplan import load_ingest_plan
from expcore_onedata.ingestsparse import SparseBaseline
from expcore_onedata.provision import ProvisionError
from expcore_onedata.sandbox.kinds import INGEST_FAILURE
from expcore_onedata.testing import ALPHA_ID, BETA_ID, with_placement, write_platform

DAY = bytes((i * 31 + i // 97) % 256 for i in range(10240))  # the dataset's last day, which the run sees still arriving
KEPT = b"an earlier day, never touched\n" * 20
DAY_PATH, KEPT_PATH = "records/day-13.csv", "records/day-12.csv"
PREFIX = 1024
STOPS = (4096, 7168, 10240)  # the three parts' ends: 1024 + ceil(9216 * k / 3)
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


def until(predicate, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while not predicate():
        assert time.monotonic() < deadline, "the schedule did not reach the expected state"
        time.sleep(0.001)


def run_to(ingest, ticker, t0, seconds, progress):
    ticker.now = max(ticker.now, t0 + seconds)
    until(lambda: ingest.progress >= progress)


def node(fake, path=DAY_PATH):
    return fake.nodes[fake.id_of(path)]


def content_writes(fake, file_id=None):
    return [
        r
        for r in fake.requests
        if r.method == "PUT" and r.path.endswith("/content") and (file_id is None or f"/data/{file_id}/" in r.path)
    ]


def first_turn(ingest, ticker, after_s=50.0):
    """Let the executor's setup take `after_s` seconds, then call the first-turn hook; return the schedule's zero."""
    ticker.now += after_s
    ingest.at_first_turn({"clock_s": 12.5})
    return ticker.now


# -- the start state ---------------------------------------------------------------------------------------------------


def test_o44_the_namespace_shows_the_file_at_its_final_size_from_the_start_with_only_its_prefix_held(make, fake):
    """O44: after the start the day is at its final size on both providers, its first bytes written and the rest
    zeros, the data provider holds exactly the prefix and no provider holds more; the new file was extended before
    any byte of it was written, so its size was never another, and the dataset's other day is untouched."""
    old = fake.id_of(DAY_PATH)
    ingest = make(ROW)
    ingest.start({"run_key": "r1"})
    new = fake.id_of(DAY_PATH)
    assert new != old
    assert bytes(node(fake).content) == DAY[:PREFIX] + bytes(len(DAY) - PREFIX)
    assert (fake.held(DAY_PATH, ALPHA_ID), fake.held(DAY_PATH, BETA_ID)) == ([[0, PREFIX]], [])
    first, *rest = content_writes(fake, new)
    assert (first.query, first.body) == ({"offset": str(len(DAY))}, b"")
    assert [int(r.query["offset"]) for r in rest] == list(range(0, PREFIX, 1024))
    assert bytes(node(fake, KEPT_PATH).content) == KEPT
    snapshot = ingest.finish()
    state = snapshot["start"]["files"]["day-13"]
    assert (state["baseline_id"], state["file_id"], state["size"], state["blocks"]) == (old, new, len(DAY), [[0, 1024]])
    assert snapshot["start"]["restore"]["day-13"]["action"] == "kept"  # the dataset's own file, its staged bytes
    events = [(e["event"], e["old_id"], e["new_id"]) for e in snapshot["ids"]]
    assert events[:3] == [("restored", old, old), ("removed", old, None), ("created", old, new)]


def test_o44_the_parts_fall_due_from_the_first_turn_and_the_block_map_grows_by_exactly_each(make, fake, ticker):
    """O44: the schedule waits for the agent's first turn, 50 s after the start, and counts from there; at each part's
    time the data provider holds exactly the prefix and the parts so far, the size stays final, and every write of
    content the run sends names its offset."""
    ingest = make(ROW)
    ingest.start({})
    ticker.now += 200
    time.sleep(0.01)
    assert (ingest.progress, fake.held(DAY_PATH, ALPHA_ID)) == (0, [[0, PREFIX]])  # nothing before the first turn
    t0 = first_turn(ingest, ticker)
    run_to(ingest, ticker, t0, 29, 0)
    assert fake.held(DAY_PATH, ALPHA_ID) == [[0, PREFIX]]
    for progress, (due, stop) in enumerate(zip((30, 60, 90), STOPS, strict=True), start=1):
        run_to(ingest, ticker, t0, due, progress)
        assert (fake.held(DAY_PATH, ALPHA_ID), fake.held(DAY_PATH, BETA_ID)) == ([[0, stop]], [])
        assert len(node(fake).content) == len(DAY)
    assert bytes(node(fake).content) == DAY
    snapshot = ingest.finish()
    ingest.raise_if_failed()
    assert (snapshot["anchor"], snapshot["first_turn"]["after_start_s"], snapshot["first_turn"]["run_clock_s"]) == (
        "first_turn",
        250.0,
        12.5,
    )
    written = [s for s in snapshot["steps"] if s["step"] == "written"]
    assert [(s["start"], s["bytes"]) for s in written] == [(1024, 4096), (4096, 7168), (7168, 10240)]
    assert all(s["t_rel_s"] >= s["due_s"] for s in written)
    confirmed = [s["blocks"] for s in snapshot["steps"] if s["step"] == "confirmed"]
    assert confirmed == [[[0, stop]] for stop in STOPS]
    assert snapshot["files"]["day-13"]["state"] == "arrived"
    assert all("offset" in r.query for r in content_writes(fake))
    shown = json.dumps(snapshot)  # providers by position, files by label: no name, path or staging directory
    assert all(word not in shown for word in ("records/", "alpha", "beta", "staged", ".csv"))


def test_o44_the_first_turn_hook_is_called_by_the_core_and_the_arrival_mode_has_none(
    make, fake, ticker, provisioner, config, tmp_path
):
    """O44: `first_turn_instruments` finds the hook of a preallocated ingest and gives it the run's clock reading; an
    arrival ingest has none, so no reading is taken for it and its runs are as they were."""
    ingest = make(ROW)
    ingest.start({})
    hooked_at = ticker.now
    first_turn_instruments([ingest], AgentTimer(600.0, clock=ticker.clock))
    assert ingest.finish()["first_turn"]["run_clock_s"] == hooked_at
    path = tmp_path / "arrival.toml"
    path.write_text('[ingest]\nstaging = "staged"\n', encoding="utf-8")
    arrival = ProgressiveIngest(provisioner, load_ingest_plan(path, config), journal=tmp_path / "arrival.jsonl")
    assert arrival.at_first_turn is None


# -- fail closed -------------------------------------------------------------------------------------------------------


def test_o44_a_part_with_the_wrong_hash_fails_closed_and_the_reset_still_completes_the_file(make, fake, ticker):
    """O44: the platform stores other bytes than the part's; the hash read back differs, so the part fails, the later
    parts are abandoned and the post-run check refuses the attempt as `ingest_failure`. The reset writes what the block
    map lacks, finds the hash still wrong and writes the file whole again at its offsets, never removing it."""
    ingest = make(ROW)
    ingest.start({})
    t0 = first_turn(ingest, ticker)
    fake.corrupt_writes = True
    ticker.now = t0 + 30
    until(lambda: ingest.failure is not None)
    fake.corrupt_writes = False
    snapshot = ingest.finish()
    assert [s["step"] for s in snapshot["steps"] if s["step"] != "planned"] == ["failed", "abandoned", "abandoned"]
    assert "hash" in snapshot["failure"]["detail"]
    with pytest.raises(InfraError) as refused:
        ingest.check()
    assert refused.value.kind == INGEST_FAILURE
    assert snapshot["restore"]["files"]["day-13"]["action"] == "rewritten"
    assert (bytes(node(fake).content), fake.held(DAY_PATH, ALPHA_ID)) == (DAY, [[0, len(DAY)]])


def test_o44_a_block_beyond_the_written_ranges_fails_the_next_part_with_nothing_written(make, fake, ticker):
    """O44: another writer put bytes into the gap, so before the next part the data provider's block map is not the
    plan's ranges: the part is refused with nothing sent, and the attempt is refused as `ingest_failure`."""
    ingest = make(ROW)
    ingest.start({})
    t0 = first_turn(ingest, ticker)
    file_id = fake.id_of(DAY_PATH)
    fake._write(node(fake), ALPHA_ID, 8000, b"ahead of the plan")
    sent = len(content_writes(fake, file_id))
    ticker.now = t0 + 30
    until(lambda: ingest.failure is not None)
    assert len(content_writes(fake, file_id)) == sent
    assert "beyond the written ranges" in ingest.failure["detail"]
    ingest.finish()
    with pytest.raises(InfraError):
        ingest.check()
    assert bytes(node(fake).content) == DAY


def test_o44_a_provider_platform_toml_does_not_declare_holding_bytes_beyond_fails_the_part(make, fake, ticker):
    """O44: the distribution lists a provider the configuration does not know, holding bytes in the gap; it is read
    like the space's own, so the next part fails closed with nothing written."""
    fake.add_provider("pr-stranger", "stranger.test", supports=False)
    ingest = make(ROW)
    ingest.start({})
    t0 = first_turn(ingest, ticker)
    file_id = fake.id_of(DAY_PATH)
    fake.blocks[(file_id, "pr-stranger")] = [[9000, 100]]
    sent = len(content_writes(fake, file_id))
    ticker.now = t0 + 30
    until(lambda: ingest.failure is not None)
    assert "a provider platform.toml does not declare holds bytes beyond" in ingest.failure["detail"]
    assert len(content_writes(fake, file_id)) == sent
    ingest.stop()


def test_o44_a_creation_whose_answer_was_lost_is_taken_back_and_the_file_written_whole(make, fake, provisioner):
    """O44: the provider made the new file but its answer never came, so no change-log line holds it; the journal's
    intent says the start was making it, so the start's own recovery removes the empty file and writes the day back
    whole, and the next start sets the start state."""
    real = provisioner.mutate
    lost = []

    def answer_lost(method, path, **options):
        answer = real(method, path, **options)
        if method == "POST" and path.endswith("/children") and options["params"]["type"] == "REG" and not lost:
            lost.append(answer["fileId"])
            raise PlatformError("providers[0]: POST failed (ReadTimeout)", transport=True)
        return answer

    provisioner.mutate = answer_lost
    failed = make(ROW)
    with pytest.raises(InfraError):
        failed.start({})
    assert fake.id_of(DAY_PATH) != lost[0]  # the empty file the answer was lost for was removed
    assert bytes(node(fake).content) == DAY  # and the recovery wrote the day back whole
    del provisioner.mutate
    again = make(ROW)
    again.start({})
    assert fake.held(DAY_PATH, ALPHA_ID) == [[0, PREFIX]]
    again.stop()


def test_o44_a_lost_creation_whose_removal_failed_is_removed_by_the_next_start(make, fake, provisioner, tmp_path):
    """O44: the answer of the new file's creation was lost and the recovery's delete of the empty file failed too, so
    the attempt is set aside with the empty file in place; its removal's intent is journalled, so the next start
    removes it, writes the day back whole and sets the start state."""
    real = provisioner.mutate
    lost = []

    def answer_lost(method, path, **options):
        answer = real(method, path, **options)
        if method == "POST" and path.endswith("/children") and options["params"]["type"] == "REG" and not lost:
            lost.append(answer["fileId"])
            fake.fail_deletes = True  # the recovery's delete of the empty file fails
            raise PlatformError("providers[0]: POST failed (ReadTimeout)", transport=True)
        return answer

    provisioner.mutate = answer_lost
    with pytest.raises(InfraError):
        make(ROW).start({})
    assert (fake.id_of(DAY_PATH), len(node(fake).content)) == (lost[0], 0)
    del provisioner.mutate
    fake.fail_deletes = False
    damaged = make(ROW)
    (tmp_path / "staged" / "day-13.csv").write_bytes(DAY[:-1] + b"?")  # the empty file stays, nothing is deleted
    deletes = sum(1 for c in fake.provider_calls if c[1] == "DELETE")
    with pytest.raises(InfraError):
        damaged.start({})
    assert fake.id_of(DAY_PATH) == lost[0]
    assert sum(1 for c in fake.provider_calls if c[1] == "DELETE") == deletes
    again = make(ROW)  # the staged copy written whole again
    again.start({})
    assert again.finish()["start"]["restore"]["day-13"]["action"] == "created"
    assert lost[0] not in {fake.id_of(DAY_PATH)}
    assert bytes(node(fake).content) == DAY


def test_o44_a_part_out_of_order_is_refused_before_anything_is_written(provisioner, config, fake, tmp_path):
    """O44: a part that does not start where the written ranges end is out of order: refused, nothing sent."""
    plan = load_ingest_plan(write_plan(tmp_path, [ROW]), config)
    sparse = SparseBaseline(provisioner, plan, tmp_path / "journal.jsonl", time.time)
    states = sparse.start_state()
    file_id = states["day-13"]["file_id"]
    sent = len(content_writes(fake, file_id))
    with pytest.raises(ProvisionError, match="out of order"):
        sparse.write_part(plan.files[0], plan.steps()[1], file_id)
    assert len(content_writes(fake, file_id)) == sent
    assert fake.held(DAY_PATH, ALPHA_ID) == [[0, PREFIX]]


def test_o44_a_run_whose_first_turn_never_came_is_refused_and_still_restored(make, fake):
    """O44: an executor that never calls the hook leaves every part not written; that is no manipulation, so the
    post-run check refuses the attempt, and the reset completes the file all the same."""
    ingest = make(ROW)
    ingest.start({})
    snapshot = ingest.finish()
    assert [s["step"] for s in snapshot["steps"] if s["step"] != "planned"] == ["not_due"] * 3
    assert snapshot["files"]["day-13"]["state"] == "prefix_only"
    assert (snapshot["ok"], snapshot["first_turn"]) == (False, None)  # as the check says: no manipulation
    with pytest.raises(InfraError, match="first model turn"):
        ingest.check()
    assert bytes(node(fake).content) == DAY


def test_o44_a_file_at_the_path_that_is_neither_the_ingests_nor_its_staged_source_refuses_the_start(make, fake):
    """O44: the dataset's file has other bytes than its staged copy and the ingest did not make it, so nothing is
    removed or written and the start is refused as `ingest_failure`."""
    fake.nodes[fake.id_of(DAY_PATH)].content[5] ^= 0xFF
    before = bytes(node(fake).content)
    ingest = make(ROW)
    with pytest.raises(InfraError) as refused:
        ingest.start({})
    assert refused.value.kind == INGEST_FAILURE
    assert [c for c in fake.provider_calls if c[1] in ("POST", "PUT", "DELETE")] == []
    assert bytes(node(fake).content) == before


# -- the reset ---------------------------------------------------------------------------------------------------------


def test_o44_the_reset_completes_the_file_in_place_and_removes_nothing(make, fake, ticker):
    """O44: the run ends after the first part; the reset writes the rest at its offsets into the same file, sends no
    DELETE, and leaves it whole: the staged bytes, the data provider's block map covering it, its id kept."""
    ingest = make(ROW)
    ingest.start({})
    t0 = first_turn(ingest, ticker)
    run_to(ingest, ticker, t0, 30, 1)
    arrived = fake.id_of(DAY_PATH)
    calls = len(fake.provider_calls)
    snapshot = ingest.finish()
    assert [c for c in fake.provider_calls[calls:] if c[1] == "DELETE"] == []
    assert (fake.id_of(DAY_PATH), bytes(node(fake).content)) == (arrived, DAY)
    assert fake.held(DAY_PATH, ALPHA_ID) == [[0, len(DAY)]]
    assert snapshot["restore"]["files"]["day-13"] == {"ok": True, "action": "completed", "file_id": arrived}
    assert (snapshot["files"]["day-13"]["state"], snapshot["files"]["day-13"]["restored_id"]) == ("partial", arrived)
    restored = [e for e in snapshot["ids"] if e["event"] == "restored" and e["phase"] == "restore"]
    assert [(e["old_id"], e["new_id"], e["action"]) for e in restored] == [(arrived, arrived, "completed")]
    ingest.check()


def test_o44_a_crash_mid_run_ends_with_the_complete_file_after_the_next_start(make, fake, ticker):
    """O44: a run's process dies after the first part, its reset never run; the next start completes the file the
    ingest made in place (no DELETE before the start state), then sets the start state again."""
    crashed = make(ROW)
    crashed.start({})
    t0 = first_turn(crashed, ticker)
    run_to(crashed, ticker, t0, 30, 1)
    crashed.stop()  # the process died: no finish, no restore
    left = fake.id_of(DAY_PATH)
    calls = len(fake.provider_calls)
    again = make(ROW)
    again.start({})
    first_delete = next(n for n, c in enumerate(fake.provider_calls[calls:]) if c[1] == "DELETE")
    completing = [c for c in fake.provider_calls[calls : calls + first_delete] if c[1] == "PUT"]
    assert completing  # the rest was written before the start state's removal
    snapshot = again.finish()
    assert snapshot["start"]["restore"]["day-13"] == {"ok": True, "action": "completed", "file_id": left}
    assert bytes(node(fake).content) == DAY


def test_o44_a_crash_between_the_removal_and_the_new_file_ends_complete_after_the_next_start(make, fake, ticker):
    """O44: the start removed the complete day and could not make it again (creations refused); its own recovery
    cannot either, so the attempt is set aside with the day missing. The next start finds the name empty, waits until
    every provider answers the old id absent, writes the day back whole from its staged copy, then sets the start
    state."""
    old = fake.id_of(DAY_PATH)
    fake.fail_creates.add(ALPHA_ID)
    failed = make(ROW)
    with pytest.raises(InfraError):
        failed.start({})
    assert not fake.exists(DAY_PATH)
    fake.fail_creates.clear()
    again = make(ROW)
    again.start({})
    first_turn(again, ticker)
    snapshot = again.finish()  # the run ends before the first part
    assert snapshot["start"]["restore"]["day-13"]["action"] == "created"
    restored = next(e for e in snapshot["ids"] if e["event"] == "restored")
    assert restored["old_id"] == old
    assert bytes(node(fake).content) == DAY
    again.check()


def test_o44_a_crash_after_the_new_file_was_made_and_before_its_extension_ends_complete_after_the_next_start(
    make, fake, ticker, tmp_path
):
    """O44: the new file was made, then every write of content failed (its extension first), so the start and its
    own recovery fail with the file left at size 0; its id was recorded as soon as it existed. The next start finds
    the ingest's file short, writes it whole at its offsets in place, and then sets the start state."""
    fake.writes_before_reset = 0  # the extension is the start's first write of content
    failed = make(ROW)
    with pytest.raises(InfraError):
        failed.start({})
    short = fake.id_of(DAY_PATH)
    assert len(node(fake).content) == 0
    journal = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    made = [e for e in journal if e.get("entry") == "id" and e["event"] == "created"]
    assert [(e["new_id"], e["action"]) for e in made] == [(short, "preallocated")]
    fake.writes_before_reset = None
    again = make(ROW)
    again.start({})
    first_turn(again, ticker)
    snapshot = again.finish()
    assert snapshot["start"]["restore"]["day-13"] == {"ok": True, "action": "completed", "file_id": short}
    assert bytes(node(fake).content) == DAY
    again.check()


def test_o44_an_empty_preallocated_plan_records_the_same_steps_and_passes(make, fake, ticker):
    """O44: a sham control: nothing to arrive, the hook anchors nothing to write, and the check passes."""
    ingest = make()
    ingest.start({})
    first_turn(ingest, ticker)
    snapshot = ingest.finish()
    ingest.check()
    assert (snapshot["anchor"], snapshot["steps"], snapshot["ok"]) == ("first_turn", [], True)
    assert snapshot["plan"]["mode"] == "preallocated"
