# Provenance: new
"""The fault gate's heal of stuck pairs its own link check found, before any run's tokens exist (O35).

A restore heals a stuck link only while the injector holds the federation exclusively (`fault_heal`), and a heal is
skipped while a run's tokens are in play. Stuck pairs that no restore heals (its heal was skipped, or nothing was
restored) then wait. The platform's health check runs before any attempt launches anything, so the fault gate
(`fault_gate.FaultGate`) heals them there, with the restore heal's own rules:

- **Only on stuck pairs.** It acts only when the gate's own all-pairs link check, in the call that is answering and in
  its thread (the report the rest of the check made there, never one another call made), found a pair whose fetch
  failed (a stuck pair, `fault_matrix.PairState.stuck`), never as routine: a check whose pairs pass, one whose only
  failure is an untested pair, or one that failed before its link check (the space) heals nothing; and a heal whose own
  fresh matrix shows no stuck pair (it recovered meanwhile, or only an untested pair fails) does nothing either.
- **Only between runs, under an exclusive admission of the gate's own.** Never inside a run's admission (a post-run
  check, when the run's tokens may still be in play), and a check under a run's admission keeps a share of it until it
  ends, so one that outlives its caller still holds the federation. The gate lets its shared hold go and takes the
  federation exclusively (`admission.RECOVERY`), waiting at most `health_wait_s` for every shared holder (a campaign
  run, and a tool outside the campaign, which takes the same shared lock) and then for the drain record, as any
  admission does. When either outlasts the wait the check fails closed with nothing done, as the hold
  `federation_busy`, which charges no run. Once it holds the admission it reads the latch again (below). While it
  holds the federation no run is admitted, so no run's tokens exist during the heal.
- **The same heal.** `[faults] heal` names the mode (`"off"`: none here either), and the heal is the restore's
  (`Recovery.heal_stuck` over `fault_heal.LinkHeal`, no provider restored): its bounds (one `restart_link` and one
  container restart per provider, 2 passes, `heal_timeout_s`), its enforced no-fetch settle and its fresh reads of
  every pair before and after each action. A provider that was the source of a stuck pair, in the gate's check or any
  matrix the heal reads, never has its container restarted. A mode that may restart a container first arms the
  session gate's probe (`ComposeDriver.arm_gate`), so a restarted reader's gate reads a file that predates it.
- **The settle holds in every process.** Each restart is held before it is sent and again from its return, in this
  process (`Recovery.hold`) and in the federation's drain record (`FederationLock.hold_for`): no admission, in any
  process, is granted before the settle could have ended, even after this one died mid-heal.
- **A run checked before it is refused at its admission.** Before its record, the heal raises the federation's link
  epoch (`fault_epoch`), so a run whose pre-run check passed before the heal began is refused, uncharged, at its
  admission (`admission_recheck`); a heal that cannot raise it does not begin.
- **Once per stuck episode, durably.** The heal is recorded before its first action (`healing`, with the gate's check
  that found the pair and the process that heals, `liveness.this_process`: its boot id, pid and start time), and when
  it ends (`ended`, the heal's record). Once a heal has begun, no heal runs again until the gate's own link check finds
  every pair passing (`links_passed`), in this process or a later one: a heal that left a pair failing keeps the gate
  closed for an operator, and the link restarts never pile up against the supervisor's bound of 10 restarts of the
  link in 3,600 s. **A heal cut short is forgiven once.** A heal whose process ended before it did (a kill, a crash, or
  a clean exit that stopped it between two steps, which writes no `ended` line either) has no `ended` line and a
  process that is gone; the first such heal of an episode does not latch, so the next check that finds a pair stuck
  heals again, within the same bounds; a second one latches, so a process killed again and again never piles link
  restarts up (at most two heals, two link restarts per provider, per episode). A `healing` line whose process lives,
  or cannot be told gone (none recorded, or no process table to read), latches.
- **In the background, a wait that charges no run.** The gate hands its admission to the heal in a thread of its own
  and answers the uncharged deferral, as every check does while the heal runs, so a heal longer than the campaign's
  `health_timeout_s` is never counted as down and a pair it mends charges no run; a heal still running past
  `heal_timeout_s` plus `heal_settle_s` since it began closes the gate, uncharged.
- **Fail closed.** A heal that does not end `healed` leaves the pair to the next check, which finds it still stuck and
  fails closed, the latch keeping a second heal out; the gate opens only on its own link check, made afresh under the
  exclusive admission after the heal (a recent pass another call made before it is not reused), and passing.

**The record** is `gate-heals.jsonl` beside the fault journal, by label only (O12): a `healing` line (`id`,
`start_utc`, `mode`, `trigger`: the gate's check as a matrix, `process`), an `ended` line (the heal's record,
`fault_heal`: `ok`, `mode`, `outcome`, `why`, `seconds`, `before`, `after`, `shared_source`, `window`, `actions`,
`skipped`, `tried`, with `id`, `start_utc`, `end_utc`, `trigger` and `failing`, each pair the latest check that answered
found failing), and `links_passed` (`utc`). Lines are appended as the injection journal's are: a torn last line cut
off first, the line flushed and fsynced.
"""

import json
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from expcore.campaign.deferral import OPERATOR_ACTION
from expcore.campaign.lease import process_gone
from expcore.campaign.liveness import this_process
from expcore.errors import TARGET_LABEL
from expcore_onedata.fault_epoch import HEAL, LinkEpoch
from expcore_onedata.fault_heal import INTERRUPTED, HealResult
from expcore_onedata.fault_journal import InjectionJournal
from expcore_onedata.fault_matrix import LinkMatrix, label_order
from expcore_onedata.fault_recovery import Recovery
from expcore_onedata.fault_restore import unhealed_detail
from expcore_onedata.fault_settings import FaultSettings

GATE_HEALS = "gate-heals.jsonl"
_EVENTS = ("healing", "ended", "links_passed")
_UNTIL = "no heal runs again until a link check passes, so the gate stays closed for an operator"
FORGIVEN = 1  # heals cut short (their process gone, no `ended` line) that an episode lets a heal follow
NAMED_PAIRS = 2  # the stuck pairs a failed heal's reason names; it says how many more there are


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def failed_heal_reason(record: Mapping[str, Any]) -> str | None:
    """What the gate's holds say first while a failed heal's pairs wait for an operator, from the heal's `ended` line
    (`GateHeals.failed`): `a failed heal: operator action needed (stuck pairs: providers[2] reading from
    providers[0], ...)`, the pairs by label in label order (the first `NAMED_PAIRS`, then how many more), as the link
    check names them and never by a provider's name or id (O12). None when the line names no pair by a label. The core
    keeps the first 200 characters of a detail, so the reason leads and the rest of the words follow it
    (`healthgate.operator_reason`)."""
    failing = record.get("failing")
    pairs = {
        (pair["reader"], pair["source"])
        for pair in (failing if isinstance(failing, list) else [])
        if isinstance(pair, dict)
        and all(isinstance(pair.get(key), str) and TARGET_LABEL.fullmatch(pair[key]) for key in ("reader", "source"))
    }
    if not pairs:
        return None
    ordered = sorted(pairs, key=lambda pair: (label_order(pair[0]), label_order(pair[1])))
    named = ", ".join(f"{reader} reading from {source}" for reader, source in ordered[:NAMED_PAIRS])
    more = f" and {len(ordered) - NAMED_PAIRS} more" if len(ordered) > NAMED_PAIRS else ""
    return f"a failed heal: {OPERATOR_ACTION} (stuck pairs: {named}{more})"


class GateHeals:
    """The record of the fault gate's heals, `gate-heals.jsonl` in the fault journal's directory (see the module
    docstring)."""

    def __init__(self, directory: Path) -> None:
        self.path = Path(directory) / GATE_HEALS
        self._writer = InjectionJournal(Path(directory))  # its durable append, pointed at this file
        self._writer.path = self.path

    def append(self, line: Mapping[str, Any]) -> None:
        """Append one line durably; raise OSError when it cannot be written."""
        self._writer.append(line)

    def lines(self) -> list[dict[str, Any]]:
        """Every complete line; raise ValueError for one that is not a record of this file. An incomplete last line
        (a crash mid-append, or an append in flight) was never acted on and is left out."""
        if not self.path.exists():
            return []
        data = self.path.read_bytes()
        found = []
        for number, text in enumerate(data[: data.rfind(b"\n") + 1].decode("utf-8").splitlines(), 1):
            try:
                line = json.loads(text)
            except ValueError:
                raise ValueError(f"line {number} of the gate's heal record is not JSON") from None
            if not isinstance(line, dict) or line.get("event") not in _EVENTS:
                raise ValueError(f"line {number} of the gate's heal record is not a heal event")
            found.append(line)
        return found

    def _episode(self) -> list[dict[str, Any]]:
        """The lines since the gate's link check last passed (`links_passed`): the current stuck episode's."""
        lines = self.lines()
        passes = [number for number, line in enumerate(lines) if line["event"] == "links_passed"]
        return lines[passes[-1] + 1 :] if passes else lines

    def latched(self) -> bool:
        """Whether no heal may run until the gate's link check passes: a heal of this episode ended, or one runs or
        cannot be told gone, or more than `FORGIVEN` were cut short (no `ended` line, their process gone)."""
        episode = self._episode()
        if any(line["event"] == "ended" for line in episode):
            return True
        cut_short = 0
        for line in episode:
            if line["event"] != "healing":
                continue
            process = line.get("process")
            if not isinstance(process, str) or process_gone(process) is not True:
                return True  # its process lives, or cannot be told gone: a heal under way, or one that may yet end
            cut_short += 1
        return cut_short > FORGIVEN

    def failed(self) -> dict[str, Any] | None:
        """The heal of the current stuck episode that ended with a pair still failing (its `ended` line), None when
        the episode holds none: a heal under way or cut short has no `ended` line, and one that mended every pair left
        none failing. A heal's wait for a pair it will mend is never one that needs an operator."""
        for line in reversed(self._episode()):
            if line["event"] == "ended":
                return line if not line.get("ok") and line.get("failing") else None
        return None

    def unpassed(self) -> bool:
        """Whether a heal has begun since the gate's link check last passed (a `healing` line in the episode), however
        it ended: the gate has not seen every pair pass since."""
        return any(line["event"] == "healing" for line in self._episode())

    def passed(self) -> None:
        """The gate's own link check found every pair passing: the episode ends, a heal cut short in it included, and
        the next stuck pair may be healed again."""
        if self._episode():
            self.append({"event": "links_passed", "utc": _utc()})


class GateHeal:
    """One heal of the fault gate, under its exclusive admission (see the module docstring). `labels` maps each
    provider's label to its service; `hold_for(seconds)` closes the federation to every admission for that long;
    `stopping()` says the process is exiting cleanly, and no step of the heal starts after it does."""

    def __init__(
        self,
        settings: FaultSettings,
        driver: Any,
        recovery: Recovery,
        labels: Mapping[str, str],
        hold_for: Callable[[float], None],
        record: GateHeals,
        stopping: Callable[[], bool] | None = None,
    ) -> None:
        self._settings, self._driver, self._recovery, self._labels = settings, driver, recovery, dict(labels)
        self._hold_for, self._record, self._stopping = hold_for, record, stopping

    def _hold(self, seconds: float) -> None:
        """No read through the links for `seconds` from now, in this process and, by the drain record, in every
        process; a record that cannot be written raises, and no restart is then sent."""
        self._recovery.hold(seconds)
        self._hold_for(seconds)

    def _containers(self) -> bool:
        """Whether the declared mode may restart a reader's container."""
        heal = self._settings.heal
        return heal == "link_then_container" or (heal == "all_links" and self._settings.heal_container_fallback)

    def _arm(self, service: str) -> str | None:
        """The session gate's probe, made before any action when a container may be restarted; None when none may be,
        or the driver has no session gate. Raises when it cannot be made."""
        arm = getattr(self._driver, "arm_gate", None)
        return arm(service) if self._containers() and arm is not None else None

    def run(self, trigger: LinkMatrix) -> tuple[HealResult | None, str]:
        """Heal the stuck pairs `trigger` (the gate's own check) shows; return how it ended (None when it could not
        begin, with nothing done) and what to say of it."""
        service = self._labels[min(self._labels, key=label_order)]  # the driver's link check takes any service
        try:
            gate_probe = self._arm(service)
        except Exception as error:  # its type only (O12): nothing was done
            why = f"the session gate's probe could not be made ({type(error).__name__})"
            return None, f"the fault gate's heal did not begin: {why}"
        heal_id, started = uuid.uuid4().hex, _utc()
        begun = {"id": heal_id, "start_utc": started, "mode": self._settings.heal, "trigger": trigger.to_json()}
        begun["process"] = this_process()  # its boot id, pid and start time: a heal cut short is told by it (`latched`)
        try:  # before the first action: a run checked before it is refused at its admission (`fault_epoch`), and no
            # heal runs again until a link check passes
            LinkEpoch(self._settings.journal).raise_for(HEAL)
            self._record.append({"event": "healing", **begun})
        except (OSError, ValueError) as error:
            return None, f"the fault gate's heal did not begin: its record cannot be written ({type(error).__name__})"
        try:
            result, latest = self._recovery.heal_stuck(
                service, trigger, hold=self._hold, gate_probe=gate_probe, stopping=self._stopping
            )
        except Exception as error:  # a restart that raised was settled before it unwound; the gate stays closed
            why = f"the heal raised {type(error).__name__}"
            result = HealResult("links_stuck", {"ok": False, "mode": self._settings.heal, "outcome": "links_stuck"})
            result.record.update(why=why, tried="what the record cannot say: the heal raised")
            latest = trigger
        failing = [{"reader": pair.reader, "source": pair.source} for pair in latest.failing()]
        record = {**result.record, **begun, "end_utc": _utc(), "failing": failing}
        if result.how == INTERRUPTED:  # a clean exit stopped it between two steps: no `ended` line, as after a kill,
            record["recorded"] = False  # so its `healing` line, its process gone, is forgiven once (`latched`)
            return HealResult(result.how, record), self._said(result, latest, trigger)
        try:
            self._record.append({"event": "ended", **record})
        except OSError:  # the `healing` line already keeps any later heal out until a link check passes
            record["recorded"] = False
        return HealResult(result.how, record), self._said(result, latest, trigger)

    @staticmethod
    def _said(result: HealResult, latest: LinkMatrix, trigger: LinkMatrix) -> str:
        """What the check says of the heal: what it did, or why it left the gate closed and what still fails."""
        tried = result.record.get("tried", "nothing")
        if result.healed:
            return (
                f"the fault gate healed the stuck pairs its link check found ({result.record['mode']}; tried {tried})"
            )
        why = unhealed_detail(result.record, latest, trigger.detail)
        if result.how == INTERRUPTED:
            after = "once this process is gone, the next check that finds a pair stuck heals again, once"
            return f"the fault gate's heal stopped between two steps: {why}; it tried {tried}; {after}"
        return f"the fault gate's heal ended {result.how}: {why}; it tried {tried}; {_UNTIL}"
