"""The fault gate's heal on a scripted federation, shared by the tests of its answers (O35).

`gate_support` holds the scripted `Federation`, the `GateCheck` and `gate_of`; this module adds what the heal's tests
share on top of them: `next_process` a second process's gate on the same federation and journal, `core_gate` the core's
health gate around a check over a small planned campaign, and the readers of a heal's actions and record.
"""

import json
from datetime import UTC, datetime

from campaign_support import keys_under, planned_store
from gate_support import GateCheck
from heal_support import NAMES, STORES

from expcore.campaign.healthgate import HealthGate
from expcore_onedata.admission import RECOVERY
from expcore_onedata.fault_gate import FaultGate
from expcore_onedata.fault_journal import InjectionJournal


def acted(federation, *verbs):
    return [(verb, label) for verb, label, _ in federation.actions if verb in verbs]


def heal_lines(settings):
    path = settings.journal / "gate-heals.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


ANY_ACTION = ("arm_gate", "ping", "restart_link", "stop", "start")
UNCHARGED_WAIT = (False, True, False)  # (ok, deferred, charge): the hold a heal in progress answers


def healed(gate, federation):
    """The check that finds the stuck pair begins the heal and answers the uncharged deferral; once the heal has ended
    and its settle's drain record has passed, the answer of the next check."""
    first = gate()
    assert (first.ok, first.deferred, first.charge) == UNCHARGED_WAIT, first.detail
    assert gate.wait_for_heal(10)
    federation.clock.sleep(600)  # past the drain record each restart's settle wrote
    return gate()


def core_gate(tmp_path, check, **options):
    """The core's health gate around `check`, over a small planned campaign: the gate, its store and a run's key."""
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    store, schema, manifest = planned_store(campaign)
    clock = iter(f"2026-01-01T00:{minute:02d}:00Z" for minute in range(1, 60))
    core = HealthGate(store, check, lambda: next(clock), **options)
    core.load()
    return core, store, keys_under(schema, manifest, "mount-read")[0]


GONE = {"pid": 999999, "boot_id": "a boot that is over"}  # a holder record's process that has ended
GONE_PROCESS = "a-boot-that-is-over:999999:1"  # `liveness.this_process` of a process of a boot that is over


def left_by_a_killed_process(settings) -> None:
    """What a killed healing process leaves beside the federation's lock: its holder record (the kernel let its lock
    go with it, and no line was journalled past the point it reached)."""
    since = datetime.now(UTC).isoformat(timespec="seconds")
    InjectionJournal(settings.journal).write_holder(
        {"token": "killed", **GONE, "purpose": RECOVERY, "target": None, "since_utc": since}
    )


def next_process(settings, federation) -> tuple[FaultGate, GateCheck]:
    """The next process's fault gate, a gate and a check of its own, on the same federation, journal and clock."""
    check = GateCheck(federation)
    labels = {f"providers[{index}]": STORES[name] for index, name in enumerate(NAMES)}
    clock = federation.clock
    gate = FaultGate(
        settings,
        federation,
        check,
        labels=labels,
        links=check.take,
        relink=check.expire,
        clock=clock,
        sleep=clock.sleep,
        wall=clock,
    )
    return gate, check
