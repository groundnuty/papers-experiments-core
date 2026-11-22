"""A lost lease's spend read with its spend journal (#462 step 6): `dead_store_spend` for a store snapshotted mid-run.

One real lease run with `--spend-journal` beside its study copy (`<place>/spend.jsonl`); each test snapshots that
store mid-run (`make_running`: the last attempt running, no marker) and changes the journal as a kill would leave it.
"""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from merge_by_run_support import FAR, FakeClock, _new_ledger, by_run_study, make_running
from shard_support import IDENTITY, declaring
from study_support import Campaign, clean_environment, cli

from expcore.campaign.elastic import Lease
from expcore.campaign.manifest import fingerprint_sha256
from expcore.campaign.run_lease import LEASE_JOURNAL
from expcore.study.config import load_study
from expcore.study.merge_by_run import dead_store_spend
from expcore.study.pipeline import campaign_fingerprint, campaign_identity

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module


def _run_lease(base: Path, env: dict[str, str], name: str, federations: tuple[str, ...] = ("fed-a1",)) -> Path:
    """Run the last lease issued to `federations` for real with a spend journal beside its study copy; return its place
    (`<place>/study/out`, `<place>/spend.jsonl`)."""
    sha = fingerprint_sha256(campaign_fingerprint(by_run_study(base, f"template-{name}")))
    (base / f"ledger-{name}").mkdir()
    ledger, _ = _new_ledger(base / f"ledger-{name}", sha, FakeClock())
    try:
        for label in federations:
            lease = ledger.issue(label)
            assert isinstance(lease, Lease), lease
            ledger.mark_sent(lease.lease_id)
    finally:
        ledger.close()
    place = base / name
    place.mkdir()
    document = {
        "schema": "expcore-lease/1",
        "run_key": lease.run_key,
        "lease_id": lease.lease_id,
        "attempt_base": lease.attempt_base,
        "grant_micro_usd": lease.grant_micro,
        "campaign_sha256": sha,
        "expires_utc": FAR,
    }
    (place / "lease.json").write_text(json.dumps(document), encoding="utf-8")
    root = by_run_study(place, "study")
    with pytest.MonkeyPatch.context() as patch:
        declaring(patch, IDENTITY)
        code, errors = cli(
            env, "run", str(root), "--lease", str(place / "lease.json"), "--spend-journal", str(place / LEASE_JOURNAL)
        )
    assert code == 0, errors
    return place


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("dead-journal")


@pytest.fixture(scope="module")
def ended(base, clean_environment) -> Path:
    """A lease's place as its run left it: the store whole, the journal whole."""
    return _run_lease(base, clean_environment, "ended")


def _mid_run(base: Path, ended: Path, name: str) -> Path:
    """A copy of the place as a federation lost mid-run leaves it: the store as of the start of its last attempt
    (running, no marker), the journal as the kill left it."""
    place = Path(shutil.copytree(ended, base / name))
    make_running(Campaign(place / "study"))
    return place


def _lines(place: Path) -> list[dict]:
    return [json.loads(line) for line in (place / LEASE_JOURNAL).read_text(encoding="utf-8").splitlines()]


def _append(place: Path, *entries: dict) -> None:
    with (place / LEASE_JOURNAL).open("a", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _read(place: Path):
    return dead_store_spend(place / "study" / "out", load_study(place / "study"))


def _later_attempt(place: Path, state: str, charged: int | None = None) -> list[dict]:
    """The journal lines of a further attempt of the run, begun after the snapshot: one that ended in `state` with the
    last attempt's counts and no call log (`charged`: the line's own field, none for a journal that predates it), or
    (`running`) one that sent a request and never heard back."""
    last = [line for line in _lines(place) if line["kind"] == "attempt"][-1]
    began = {**last, "id": last["id"] + 1, "started_utc": "2099-01-01T00:00:00Z", "pid": 1}
    if state == "running":
        silent = {**began, "state": "running", "calls_sent": 0, "calls_closed": 0, "topups": 0, "topup_usd": 0.0}
        sent = {"kind": "call", "id": began["id"], "run_key": began["run_key"], "phase": "sent"}
        return [{**silent, **dict.fromkeys(("input_tokens", "output_tokens"), 0)}, {**sent, **_campaign_of(last)}]
    ended = {key: value for key, value in began.items() if key != "charged"}  # the copied line's field is the run's own
    ended = {**ended, "state": state, "calls_sent": None, "calls_closed": None}
    return [ended if charged is None else {**ended, "charged": charged}]


def _campaign_of(line: dict) -> dict:
    return {"campaign": line["campaign"], "shard": line["shard"]}


def test_a_store_that_ended_is_read_without_its_journal(ended):
    """The store alone says what an ended run spent: a journal beside it is not read."""
    whole = _read(ended)
    assert whole is not None
    garbled = Path(shutil.copytree(ended, ended.parent / "garbled"))
    (garbled / LEASE_JOURNAL).write_text("not a journal\n", encoding="utf-8")
    assert _read(garbled) == whole


def test_a_store_snapshotted_mid_run_is_read_with_its_journal(base, ended):
    """C46: The snapshot holds the last attempt running; the journal saw it end. The figure is the whole lease's, never
    below what the ended store reads."""
    place = _mid_run(base, ended, "mid-run")
    found = _read(place)
    whole = _read(ended)
    assert found is not None
    assert whole is not None
    assert (found.lease_id, found.run_key) == (whole.lease_id, whole.run_key)
    assert found.spend_micro >= whole.spend_micro


def test_attempts_the_snapshot_lacks_raise_the_spend_and_the_charged_attempts(base, ended):
    """C46: An attempt begun after the snapshot, which an older journal (its lines carry no `charged`) saw end as an
    infrastructure failure, is spend the store does not hold; with its kind unknown it is counted as charged (the
    ledger's own charge is as high): a journal without the field reads as it did."""
    place = _mid_run(base, ended, "later-attempt")
    before = _read(place)
    _append(place, *_later_attempt(place, "infra_fail"))
    after = _read(place)
    assert before is not None
    assert after is not None
    assert after.spend_micro > before.spend_micro
    assert after.charged_attempts == before.charged_attempts + 1


def test_an_infra_fail_the_journal_alone_saw_end_costs_its_run_an_attempt_only_if_it_was_charged(base, ended):
    """C46: The journal's attempt line says whether its end was charged (an infrastructure failure of a kind the key's
    or the budget's own is not): one that ended uncharged is spend and no attempt, as the store would count it, so a
    settle never exhausts a run one attempt early; one that ended charged is both."""
    place = _mid_run(base, ended, "later-uncharged")
    before = _read(place)
    _append(place, *_later_attempt(place, "infra_fail", charged=0))
    uncharged = _read(place)
    assert before is not None
    assert uncharged is not None
    assert uncharged.spend_micro > before.spend_micro
    assert uncharged.charged_attempts == before.charged_attempts
    charged = _mid_run(base, ended, "later-charged")
    _append(charged, *_later_attempt(charged, "infra_fail", charged=1))
    found = _read(charged)
    assert found is not None
    assert found.spend_micro == uncharged.spend_micro
    assert found.charged_attempts == before.charged_attempts + 1


@pytest.mark.parametrize("charged", [0, None], ids=["the line says 0", "a journal that predates the field"])
def test_a_spared_pre_run_outage_the_journal_alone_saw_end_costs_its_run_no_attempt(base, ended, charged):
    """A pre-run outage set aside uncharged after a failed post-run check (`start_after_check`) ends `outage_discarded`
    with `charged` 0 in the journal: a settle that reads the dead store takes it as no attempt, as the store would,
    whether the line says so or (a journal that predates the field) only its state does."""
    place = _mid_run(base, ended, f"later-spared-{charged}")
    before = _read(place)
    _append(place, *_later_attempt(place, "outage_discarded", charged=charged))
    after = _read(place)
    assert before is not None
    assert after is not None
    assert after.spend_micro > before.spend_micro
    assert after.charged_attempts == before.charged_attempts


def test_the_journal_a_lease_run_writes_names_whether_each_attempt_was_charged(ended):
    """C46: Each `attempt` line of a real run's journal carries the row's `charged`, as the store holds it."""
    db = sqlite3.connect(ended / "study" / "out" / "state.sqlite")
    try:
        held = {row[0]: row[1] for row in db.execute("SELECT id, charged FROM attempts")}
    finally:
        db.close()
    lines = [line for line in _lines(ended) if line["kind"] == "attempt"]
    assert held
    assert {line["id"]: line["charged"] for line in lines if line["state"] != "running"} == held


def test_a_later_attempt_still_running_when_the_federation_was_lost_is_spend_and_the_losss_one_attempt(base, ended):
    """C42: An attempt the journal never saw end was in flight at the loss: its spend counts and it is the one charged
    attempt a lost lease costs its run, so a run that keeps losing its federation reaches `max_attempts` and ends."""
    place = _mid_run(base, ended, "later-running")
    before = _read(place)
    _append(place, *_later_attempt(place, "running"))
    after = _read(place)
    assert before is not None
    assert after is not None
    assert after.spend_micro > before.spend_micro
    assert after.charged_attempts == before.charged_attempts + 1


def test_a_journal_of_another_lease_is_ignored(base, ended, clean_environment):
    """C46: A journal that names another lease's store (its shard spec) is never read: the mid-run store gives none."""
    place = _mid_run(base, ended, "foreign-journal")
    other = _run_lease(base, clean_environment, "other-lease", ("fed-a1", "fed-a2"))
    shutil.copyfile(other / LEASE_JOURNAL, place / LEASE_JOURNAL)
    assert _read(place) is None


def test_a_journal_that_is_not_the_stores_own_is_ignored(base, ended):
    """A store that names another journal than the one beside it, or none, gives no figure while mid-run."""
    place = _mid_run(base, ended, "other-name")
    db = sqlite3.connect(place / "study" / "out" / "state.sqlite")
    try:
        row = db.execute("SELECT value FROM meta WHERE key='lost_in_kill'").fetchone()
        record = json.loads(row[0])
        record["journal"] = "elsewhere.jsonl"
        db.execute("UPDATE meta SET value=? WHERE key='lost_in_kill'", (json.dumps(record, sort_keys=True),))
        db.commit()
    finally:
        db.close()
    assert _read(place) is None


def test_a_mid_run_store_with_no_journal_gives_no_figure(base, ended):
    """Where the journal cannot be read the lease stays charged its grant: none."""
    place = _mid_run(base, ended, "no-journal")
    (place / LEASE_JOURNAL).unlink()
    assert _read(place) is None


def test_a_torn_last_line_is_not_read(base, ended):
    """C46: A last line a kill cut short was never spent (`read_journal`): the figure is the one without it."""
    place = _mid_run(base, ended, "torn")
    _append(place, *_later_attempt(place, "infra_fail"))
    whole = _read(place)
    with (place / LEASE_JOURNAL).open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "attempt", "id": 99, "run_k')  # no newline: cut short
    assert _read(place) == whole


def test_a_line_that_is_not_whole_before_the_last_gives_no_figure(base, ended):
    """A journal is never read in part: a broken line in the middle gives none."""
    place = _mid_run(base, ended, "broken")
    text = (place / LEASE_JOURNAL).read_text(encoding="utf-8").splitlines()
    text.insert(1, "{broken")
    (place / LEASE_JOURNAL).write_text("\n".join(text) + "\n", encoding="utf-8")
    assert _read(place) is None


def test_the_journal_read_never_writes_beside_the_store(base, ended):
    """The store and the journal are read from private copies: nothing beside them changes."""
    place = _mid_run(base, ended, "unwritten")
    before = {p: p.read_bytes() for p in sorted(place.rglob("*")) if p.is_file()}
    assert _read(place) is not None
    assert {p: p.read_bytes() for p in sorted(place.rglob("*")) if p.is_file()} == before


def test_what_a_federation_joins_with_is_what_its_lease_store_pins(ended):
    """The fingerprint and environment identity `campaign_identity` reads beside a federation's platform file are the
    lease's `campaign_sha256` and the identity the lease store pins, so a join with them is never refused at merge."""
    with pytest.MonkeyPatch.context() as patch:
        declaring(patch, IDENTITY)
        fingerprint, environment = campaign_identity(ended / "study")
    store = sqlite3.connect(ended / "study" / "out" / "state.sqlite")
    try:
        pinned = json.loads(store.execute("SELECT value FROM meta WHERE key='environment_identity'").fetchone()[0])
    finally:
        store.close()
    assert environment == pinned == IDENTITY
    lease = json.loads((ended / "lease.json").read_text(encoding="utf-8"))
    assert fingerprint_sha256(fingerprint) == lease["campaign_sha256"]


def test_a_journal_with_no_header_a_short_one_or_a_line_missing_its_state_gives_no_figure(base, ended):
    """An empty file would read as "nothing spent", and a journal that does not list every attempt the store holds is
    short or damaged: none for each, and none (never an error) for an attempt line with no state."""
    empty = _mid_run(base, ended, "empty-journal")
    (empty / LEASE_JOURNAL).write_text("", encoding="utf-8")
    assert _read(empty) is None
    short = _mid_run(base, ended, "short-journal")
    first = (short / LEASE_JOURNAL).read_text(encoding="utf-8").splitlines()[0]
    (short / LEASE_JOURNAL).write_text(first + "\n", encoding="utf-8")
    assert _read(short) is None
    stateless = _mid_run(base, ended, "stateless-line")
    (line,) = _later_attempt(stateless, "infra_fail")
    _append(stateless, {key: value for key, value in line.items() if key != "state"})
    assert _read(stateless) is None
