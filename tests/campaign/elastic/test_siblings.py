"""C53: controllers of several ledgers cooperate through read-only reads of each other's ledgers: a shared model cap
holds across them, an unreadable sibling stops that model, and a ledger's runs waiting for capacity are counted from
its own state."""

import random
from pathlib import Path

from elastic_support import FakeClock, config, finish, join, plan

from expcore.campaign.elastic import Lease, Ledger
from expcore.campaign.elastic.siblings import SharedCaps, SharedLedger, slots_of, waiting_of


def two_ledgers(tmp_path: Path, cap: int = 2, models=("m1",), federations: int = 3, **changes):
    """Two ledgers in directories of their own (one dispatch lock each), each holding the other as its sibling under
    `shared_caps = {m1 = cap}`, with `federations` joined to each."""
    clock = FakeClock()
    paths = [tmp_path / name / "ledger.sqlite" for name in ("a", "b")]
    ledgers = []
    for path in paths:
        Ledger.create(path, plan(models=models, tasks=6), config(**changes), clock=clock).close()
    for path, other in zip(paths, reversed(paths), strict=True):
        ledger = SharedLedger.open(path, clock=clock)
        ledger.shared = SharedCaps((other,), {"m1": cap})
        for k in range(federations):
            join(ledger, clock, f"fed-{k}")
        ledgers.append(ledger)
    return ledgers, clock


def in_flight(ledgers, model: str = "m1") -> int:
    return sum(slots_of(ledger.path).get(model, 0) for ledger in ledgers)


def test_c53_a_shared_cap_holds_across_two_ledgers_over_many_rounds_and_a_freed_slot_goes_to_the_other(tmp_path):
    """C53: two ledgers sharing `{m1 = 2}`, each with three idle federations, issue and end leases in a random order
    for many rounds: never more than 2 of m1 hold slots across both, the cap is reached, and a slot one ledger frees
    goes to the other."""
    ledgers, clock = two_ledgers(tmp_path)
    rng = random.Random(53)
    peak, handed = 0, 0
    try:
        for _ in range(40):
            for ledger in rng.sample(ledgers, 2):
                for k in rng.sample(range(3), 3):
                    ledger.issue(f"fed-{k}")
                    peak = max(peak, in_flight(ledgers))
                    assert in_flight(ledgers) <= 2
            live = [(ledger, lease) for ledger in ledgers for lease in ledger.leases() if lease.state == "issued"]
            if live:
                ledger, lease = rng.choice(live)
                finish(ledger, lease)
                clock.advance(60)
                other = ledgers[1 - ledgers.index(ledger)]
                got = other.issue(next(f.label for f in other.federations() if f.label not in other_holders(other)))
                handed += isinstance(got, Lease)
                assert in_flight(ledgers) <= 2
        assert peak == 2
        assert handed > 0
    finally:
        for ledger in ledgers:
            ledger.close()


def other_holders(ledger) -> set:
    return {lease.federation for lease in ledger.leases() if lease.slot}


def test_c53_an_unreadable_sibling_issues_nothing_of_a_shared_model_and_the_rest_goes_on(tmp_path):
    """C53: a sibling that cannot be read (here, not a ledger) makes the shared model full: no lease of it, the wait
    naming its cap, while a model the shared caps do not name is still leased; once the sibling reads, m1 is leased."""
    ledgers, _ = two_ledgers(tmp_path, models=("m1", "m2"))
    a, b = ledgers
    sibling = b.path
    try:
        b.close()
        ledgers = [a]
        moved = sibling.with_name("moved.sqlite")
        sibling.rename(moved)
        sibling.write_text("not a ledger\n", encoding="utf-8")
        got = [a.issue(f"fed-{k}") for k in range(3)]
        assert {lease.model for lease in got if isinstance(lease, Lease)} == {"m2"}
        assert a.shared.unread
        assert slots_of(a.path).get("m1", 0) == 0
        assert a.caps()["m1"] == 0
        sibling.unlink()
        moved.rename(sibling)
        for lease in [lease for lease in got if isinstance(lease, Lease)]:
            finish(a, lease)
        assert isinstance(a.issue("fed-0"), Lease)
        assert slots_of(a.path).get("m1", 0) == 1
        assert not a.shared.unread
    finally:
        a.close()


def test_c53_a_sibling_whose_file_does_not_exist_yet_counts_as_full(tmp_path):
    """C53: a sibling named before its ledger exists is unread: the shared model issues nothing until it does."""
    clock = FakeClock()
    path = tmp_path / "a" / "ledger.sqlite"
    Ledger.create(path, plan(tasks=2), config(), clock=clock).close()
    ledger = SharedLedger.open(path, clock=clock)
    try:
        ledger.shared = SharedCaps((tmp_path / "later" / "ledger.sqlite",), {"m1": 4})
        join(ledger, clock, "fed-0")
        assert not isinstance(ledger.issue("fed-0"), Lease)
        assert "FileNotFoundError" in ledger.shared.unread[0]
    finally:
        ledger.close()


def test_c53_waiting_is_leases_wanted_less_free_federations_and_a_shared_cap_alone_is_not_waiting(tmp_path):
    """C53: a ledger with pending runs and no federation waits by its leases wanted; a free federation takes one off;
    a model held at a shared cap by its siblings' leases is not waiting for capacity."""
    clock = FakeClock()
    core = tmp_path / "core" / "ledger.sqlite"
    other = tmp_path / "other" / "ledger.sqlite"
    Ledger.create(core, plan(tasks=3), config(caps={"m1": 4}), clock=clock).close()
    Ledger.create(other, plan(tasks=3), config(), clock=clock).close()
    assert waiting_of(core, clock()).count == 4  # six pending, its own cap of 4, no federation
    ledger = Ledger.open(core, clock=clock)
    try:
        join(ledger, clock, "fed-0")
        assert (waiting_of(core, clock()).wanted, waiting_of(core, clock()).free) == (4, 1)
        assert waiting_of(core, clock()).count == 3
    finally:
        ledger.close()
    held = Ledger.open(other, clock=clock)
    try:
        for k in range(2):
            join(held, clock, f"fed-{k}")
            assert isinstance(held.issue(f"fed-{k}"), Lease)
    finally:
        held.close()
    shared = SharedCaps((core, other), {"m1": 2})
    shared.refresh()
    assert waiting_of(core, clock(), shared, own_slots={}).count == 0  # the other ledger holds both shared slots
    shared.siblings = (core, tmp_path / "gone.sqlite")
    shared.refresh()
    assert waiting_of(core, clock(), shared, own_slots={}).count == 3  # a sibling unread: its own caps alone
