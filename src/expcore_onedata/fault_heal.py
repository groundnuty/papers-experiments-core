# Provenance: new
"""The bounded heal of a link a restore left stuck (O35), declared by `[faults] heal` and off by default.

On the live slot of 14 Nov 2026 a provider that fetched from a stopped owner had its `rtransfer_link` crash and come
back, and a reader whose link to it never reconnected failed every fetch from it, while every other pair transferred;
the other study's federation showed the same pattern, every pair with the crashed provider as source failing but the
one whose reader started after the crash, for 46 minutes and more, and a pair untested at the first check failing
later. The stuck state is the readers' (their stale connections to the crashed source), so the heal acts on **readers,
one at a time**, never on a source.

**The matrix** (`LinkMatrix`) is the link check of every ordered pair (`linkcheck.check_links`), by label: each pair's
reader, source, whether it passed and whether its fetch failed (a stuck link; a pair whose probe could not be made ready
was not tested and fails too, but names no link to heal). The heal reads it **fresh before it chooses anything**, never
from the restore's first check alone, and again after each action, and chooses each action from the latest reading.

**The order.** At most `ROUNDS` passes; each takes the readers of the latest matrix's stuck pairs in label order. A
reader is skipped while it is the source of any failing pair, when no `[faults.services]` entry names its service, and
once its pairs pass. For each reader:

1. `ping` its node (`ComposeDriver.ping`), then read the matrix again: a reader that is now the source of a failing
   pair, or whose pairs now pass, is left alone. No `pong` goes straight to the container step when `heal` is
   `"link_then_container"`, and fails the heal closed otherwise: the node, its name, cookie or epmd is not answering,
   which a link restart cannot mend.
2. `restart_link` (`ComposeDriver.restart_link`), then `[faults] heal_settle_s` (90 s) of settling with **no fetch at
   all** (no probe, no check, and no run, the injector holding the federation exclusively), then the session gate for
   that reader, then every pair again, with fresh probes. Its result is a word; a timeout (`badrpc_timeout`,
   `command_timeout`) means the restart may still be running on the node, so it is **never issued again**: the matrix
   decides, and the container step follows if its pairs still fail.
3. With `"link_then_container"`, a reader whose pairs still fail has its **container** restarted by the injector's own
   stop and start (`Recovery.restart_container`: journalled as an injection of its own before the stop, so a process
   that dies midway leaves it for recovery to bring back, and cleared only when the heal ends with every pair passing),
   then served, settled, gated and checked again. The restored provider's own container is never restarted here: that
   is the outage again.

**Why the settle fetches nothing, and a pair an action disturbed.** A peer X sets up its link to R through a secret
exchange: X's `get_connection_secret` asks R, and R's `generate_secret` registers the pair with
`rtransfer_link:allow_connection(..., 60000)`, but when that fails, as it may while R's link restarts, R only logs
"Failed to allow rtransfer connection from <X>" and returns the secret all the same (op-worker f118ed9,
rtransfer_config.erl 188-198, 247-264). X then holds a secret R's link never allowed, and, inference from private code,
the pair stays failing until X's own link restarts. Seen live: after R's link restart a pair with R as its source failed
23 s later and still failed 6 minutes on. So nothing fetches through R while its link comes back, and a pair with R as
its source that passed before R's action and fails after the settle is **disturbed**: wedged, healed by its own
reader's `restart_link` (the reader then asks R afresh), followed by the same settle, within the bounds below, or the
heal fails closed. The heal takes each pass's next reader from the latest matrix, so such a reader is healed in the
same pass when it has not had its link restart yet.

**Bounds.** At most one `restart_link` and one container restart per provider per restore: the restore journals that
it heals before the first action, so neither a later clear nor another process's recovery heals the same injection
again. At most `ROUNDS` passes, and `[faults] heal_timeout_s` in all: no action starts unless its declared bounds fit in
what is left (`fault_settings.heal_link_reserve`, checked again after the ping, and `heal_container_reserve`, each with
its settle), and each gate after an action waits at most what is left. The link check's own time is outside the
reserve, so the heal can end one check's time past the bound.

**Fail closed.** An action after which a pair that passed before it fails, and does not have the acted-on reader as
its source, stops the heal at once (`heal_broke_pair`); that failure is never healed. A gate that does not pass after
an action stops it (`sessions_not_ready`), and so does a container restart that is not verified
(`heal_restart_failed`). A heal that ends with a pair still failing is `links_stuck`, as a restore with no heal is.
Only a matrix with every pair passing is `healed`, and only then is the restore verified; until it is, the injector
keeps the federation exclusively, so no run is admitted.

**The record** (`HealResult.record`) holds the mode, the outcome, the matrix before the heal and after every action, by
label, with the source every failing pair shares (`shared_source`: the crashed provider, when there is one), each
action (the provider by label, the action, `ping`, `restart_link` or `container_restart`, its result word, its start
and end in UTC, its settle, the gate's reading after it, the matrix after it, and the pairs it disturbed), and every
disturbed pair again under `disturbed`, with the provider whose action disturbed it and how it ended (`healed` or
`failing` in the last matrix). Nothing a command printed is kept.
"""

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from expcore_onedata.compose import DriverError

ROUNDS = 2
_LABEL = re.compile(r"providers\[(\d+)\]")


def _order(label: str) -> tuple[int, str]:
    found = _LABEL.fullmatch(label)
    return (int(found.group(1)) if found else 1 << 30, label)


@dataclass(frozen=True)
class PairState:
    """One ordered pair of the link check, by label: `reader` fetched from `source`. `stuck` is a failed fetch."""

    reader: str
    source: str
    ok: bool
    seconds: float | None = None
    stuck: bool = False

    def to_json(self) -> dict[str, Any]:
        seconds = None if self.seconds is None else round(self.seconds, 3)
        return {"reader": self.reader, "source": self.source, "ok": self.ok, "seconds": seconds, "stuck": self.stuck}


@dataclass(frozen=True)
class LinkMatrix:
    """The link check of every ordered pair, as the heal reads it."""

    ok: bool
    detail: str
    pairs: tuple[PairState, ...] = ()

    @classmethod
    def from_report(cls, report: Any) -> "LinkMatrix":
        """From a `linkcheck.LinkReport`: a pair is stuck when its fetch failed (its result names the remedy)."""
        pairs = tuple(
            PairState(p.local, p.source, p.ok, p.seconds, not p.ok and p.remedy is not None) for p in report.pairs
        )
        return cls(bool(report.ok), str(report.detail), pairs)

    def failing(self) -> list[PairState]:
        return [pair for pair in self.pairs if not pair.ok]

    def sources(self) -> set[str]:
        """The sources of every failing pair: no heal acts on one."""
        return {pair.source for pair in self.failing()}

    def stuck_readers(self) -> list[str]:
        """The readers of the stuck pairs, in label order."""
        return sorted({pair.reader for pair in self.failing() if pair.stuck}, key=_order)

    def shared_source(self) -> str | None:
        """The source every failing pair shares, when they share one (the crashed provider of the pattern)."""
        sources = self.sources()
        return next(iter(sources)) if len(sources) == 1 else None

    def broken_since(self, before: "LinkMatrix") -> list[tuple[str, str]]:
        """The pairs that passed in `before` and do not pass here, or are missing here."""
        now = {(pair.reader, pair.source): pair.ok for pair in self.pairs}
        return [(p.reader, p.source) for p in before.pairs if p.ok and now.get((p.reader, p.source)) is not True]

    def to_json(self) -> dict[str, Any]:
        failing = Counter(pair.source for pair in self.failing())
        return {
            "ok": self.ok,
            "pairs": [pair.to_json() for pair in self.pairs],
            "failing_sources": dict(sorted(failing.items(), key=lambda item: _order(item[0]))),
            "shared_source": self.shared_source(),
        }


def unreadable(error: BaseException) -> LinkMatrix:
    """The matrix of a link check that could not run: failed, with no pair, so nothing passes and nothing is healed."""
    return LinkMatrix(False, f"the link check raised {type(error).__name__}")


@dataclass
class HealResult:
    """How a heal ended: `how` is `healed`, `links_stuck`, `heal_broke_pair`, `sessions_not_ready` or
    `heal_restart_failed`; `record` goes into the injector's snapshot."""

    how: str
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def healed(self) -> bool:
        return self.how == "healed"


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class LinkHeal:
    """One heal of a restore's stuck links (see the module docstring). `services` maps a provider's label to its
    service; `restored` is the label of the provider the restore brought back. `matrix()` reads the link check of every
    pair; `gate(service, timeout_s)` passes the session gate for a provider within `timeout_s` and returns its reading
    (`ok` among its keys); `restart_container(label, service)` stops and starts a reader's container, verified, and
    returns how (`ok`, `how`, `serves_after_s`). `command_s` is the bound of one driver command, `settle_s` the wait
    after each action, and `link_s` and `container_s` the declared bounds of a ping with a link restart and of a
    container restart (`fault_settings.heal_link_reserve`, `heal_container_reserve`)."""

    def __init__(
        self,
        *,
        mode: str,
        driver: Any,
        services: Mapping[str, str],
        restored: str,
        bound_s: float,
        gate_s: float,
        command_s: float,
        settle_s: float,
        link_s: float,
        container_s: float,
        matrix: Callable[[], LinkMatrix],
        gate: Callable[[str, float], dict[str, Any]],
        restart_container: Callable[[str, str], dict[str, Any]],
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        utc: Callable[[], str] = _utc,
    ) -> None:
        self._mode, self._driver, self._services, self._restored = mode, driver, dict(services), restored
        self._bound_s, self._gate_s, self._settle_s = bound_s, gate_s, settle_s
        self._link_cost = link_s  # the ping's lookup and command, the restart's lookup, its RPC and one settle
        self._restart_cost = link_s - 2 * command_s  # what is left once the ping is done
        self._container_cost = container_s
        self._matrix, self._gate, self._restart = matrix, gate, restart_container
        self._clock, self._sleep, self._utc = clock, sleep, utc
        self._actions: list[dict[str, Any]] = []
        self._skipped: list[dict[str, str]] = []
        self._linked: set[str] = set()  # providers whose link was restarted, or whose node gave no pong
        self._restarted: set[str] = set()  # providers whose container was restarted
        self._started = 0.0

    # --- the readings -----------------------------------------------------------------------------------------------

    def _read(self) -> LinkMatrix:
        try:
            return self._matrix()
        except Exception as error:  # a check that cannot answer is a failed one, never a pass
            return unreadable(error)

    def _fits(self, cost: float) -> bool:
        """Whether an action whose declared bounds are `cost` seconds ends within `heal_timeout_s`."""
        return self._clock() - self._started + cost <= self._bound_s

    def _gate_within(self, service: str) -> dict[str, Any]:
        """The gate after an action, waiting at most `gate_timeout_s` and never past `heal_timeout_s`."""
        left = self._bound_s - (self._clock() - self._started)
        return self._gate(service, max(0.0, min(self._gate_s, left)))

    def _disturbed(self, current: LinkMatrix) -> list[dict[str, str]]:
        """Every pair an action disturbed, with the provider whose action it was and how it ended: `healed` when it
        passes in the last matrix, `failing` when it does not."""
        passing = {(pair.reader, pair.source) for pair in current.pairs if pair.ok}
        return [
            {
                **pair,
                "by": action["provider"],
                "ended": "healed" if (pair["reader"], pair["source"]) in passing else "failing",
            }
            for action in self._actions
            for pair in action.get("disturbed") or []
        ]

    def _end(self, how: str, current: LinkMatrix, before: LinkMatrix, why: str | None = None) -> HealResult:
        tried = ", ".join(f"{a['action']} on {a['provider']} ({a['result']})" for a in self._actions) or "nothing"
        record = {
            "ok": how == "healed",
            "mode": self._mode,
            "outcome": how,
            "why": why,
            "seconds": round(self._clock() - self._started, 3),
            "before": before.to_json(),
            "after": current.to_json(),
            "shared_source": before.shared_source(),
            "actions": list(self._actions),
            "disturbed": self._disturbed(current),
            "skipped": list(self._skipped),
            "tried": tried,
        }
        return HealResult(how, record)

    # --- the actions ------------------------------------------------------------------------------------------------

    def _record(
        self,
        label: str,
        action: str,
        result: str,
        start: str,
        gate: dict[str, Any] | None = None,
        after: LinkMatrix | None = None,
        **extra: Any,
    ) -> None:
        self._actions.append(
            {
                "provider": label,
                "action": action,
                "result": result,
                "start_utc": start,
                "end_utc": self._utc(),
                "gate": gate,
                "matrix_after": None if after is None else after.to_json(),
                **extra,
            }
        )

    def _ping(self, label: str, service: str) -> bool:
        start = self._utc()
        try:
            pong = self._driver.ping(service) is True
        except DriverError:
            pong = False
        self._record(label, "ping", "pong" if pong else "no_pong", start)
        return pong

    def _settle(self) -> float:
        """The wait after an action, before anything reads through what it restarted."""
        self._sleep(self._settle_s)
        return self._settle_s

    def _act(self, label: str, service: str, action: str, before: LinkMatrix) -> tuple[str | None, LinkMatrix, str]:
        """Run one action on a reader, then settle with no fetch at all, pass its gate and read every pair afresh
        (`_classify`); return the word the heal must stop with (None to go on), the matrix after it and why."""
        start = self._utc()
        extra: dict[str, Any] = {}
        if action == "restart_link":
            try:
                result = str(self._driver.restart_link(service))
            except DriverError:
                result = "not_run"
        else:
            done = self._restart(label, service)
            result, extra = str(done.get("how")), {"serves_after_s": done.get("serves_after_s")}
            if done.get("ok") is not True:
                self._record(label, action, result, start, **extra)
                return "heal_restart_failed", before, f"the container of {label} was not restored ({result})"
        settled = self._settle()  # no probe, fetch or run touches the reader meanwhile
        gate = self._gate_within(service)
        if gate.get("ok") is not True:
            self._record(label, action, result, start, gate, settle_s=settled, **extra)
            return "sessions_not_ready", before, f"the session gate for {label} did not pass after its {action}"
        after = self._read()
        stop, why, disturbed = self._classify(label, action, before, after)
        self._record(label, action, result, start, gate, after, settle_s=settled, disturbed=disturbed, **extra)
        return stop, after, why

    @staticmethod
    def _classify(
        label: str, action: str, before: LinkMatrix, after: LinkMatrix
    ) -> tuple[str | None, str, list[dict[str, str]]]:
        """Judge the pairs that failed after an action's settle and passed before it. One whose source is the reader
        acted on is disturbed: its reader's link asked that reader for a connection secret while the reader's link was
        coming back, which the reader handed out without its link allowing the connection (op-worker f118ed9,
        rtransfer_config.erl, `generate_secret`; the rest is inference), so it stays wedged until its own reader's link
        restarts. It is a cascade the heal goes on to heal by that reader's `restart_link`, within the bounds, or fails
        closed. Any other pair that fails is broken by the action, and the heal stops (`heal_broke_pair`). Return the
        stop word, why, and the disturbed pairs."""
        if not after.pairs:
            return "links_stuck", f"the link check after the {action} on {label} could not run", []
        broken = after.broken_since(before)
        others = [(reader, source) for reader, source in broken if source != label]
        disturbed = [{"reader": reader, "source": source} for reader, source in broken if source == label]
        if others:
            shown = ", ".join(f"{source}->{reader}" for reader, source in others)
            return "heal_broke_pair", f"the {action} on {label} broke {shown}, which passed before it", disturbed
        return None, "", disturbed

    def _heal_reader(self, label: str, current: LinkMatrix) -> tuple[str | None, LinkMatrix, str]:
        """The steps for one reader: ping and link restart, then the container when declared and still failing."""
        service = self._services[label]
        if label not in self._linked:
            if not self._fits(self._link_cost):
                return "links_stuck", current, "heal_timeout_s leaves no time for another link restart"
            self._linked.add(label)  # one restart_link per provider per restore, whatever the ping or the result
            pong = self._ping(label, service)
            current = self._read()  # what the ping's time changed: never act on a reading from before it
            if not current.pairs:
                return "links_stuck", current, f"the link check after the ping of {label} could not run"
            if label not in current.stuck_readers() or label in current.sources():
                return None, current, ""  # mended meanwhile, or now the source of a failing pair: left alone
            if pong:
                if not self._fits(self._restart_cost):
                    return "links_stuck", current, "heal_timeout_s leaves no time for the link restart after the ping"
                stop, current, why = self._act(label, service, "restart_link", current)
                if stop is not None:
                    return stop, current, why
            elif self._mode != "link_then_container":
                return "links_stuck", current, f"{label} answered no pong, so its link cannot be restarted"
        still = label in current.stuck_readers() and label not in current.sources()
        if still and self._mode == "link_then_container" and label not in self._restarted:
            self._restarted.add(label)  # one container restart per provider per restore, or none
            if label == self._restored:
                self._skipped.append({"provider": label, "why": "the restored provider's container is not restarted"})
                return None, current, ""
            if not self._fits(self._container_cost):
                return "links_stuck", current, "heal_timeout_s leaves no time for a container restart"
            return self._act(label, service, "container_restart", current)
        return None, current, ""

    def run(self) -> HealResult:
        """Heal what a fresh matrix shows stuck, reader by reader; return how it ended and its record."""
        self._started = self._clock()
        current = before = self._read()
        for _ in range(ROUNDS):
            if current.ok:
                break
            seen: set[str] = set()  # the readers this pass has taken, each once
            while True:  # the next reader of the latest matrix, so a cascade an action made is healed in this pass
                sources = current.sources()  # never acted on: the source of a failing pair
                waiting = [label for label in current.stuck_readers() if label not in seen and label not in sources]
                if not waiting:
                    break
                label = waiting[0]
                seen.add(label)
                if label not in self._services:
                    skip = {"provider": label, "why": "no service is declared for it in [faults]"}
                    self._skipped += [] if skip in self._skipped else [skip]
                    continue
                stop, current, why = self._heal_reader(label, current)
                if stop is not None:
                    return self._end(stop, current, before, why)
        if current.ok:
            return self._end("healed", current, before)
        return self._end("links_stuck", current, before, "a pair still fails after every action the bounds allow")
