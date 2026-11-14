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

1. `ping` its node (`ComposeDriver.ping`). No `pong` goes straight to the container step when `heal` is
   `"link_then_container"`, and fails the heal closed otherwise: the node, its name, cookie or epmd is not answering,
   which a link restart cannot mend.
2. `restart_link` (`ComposeDriver.restart_link`), then `QUIET_S` of quiet, then the session gate for that reader, then
   the matrix again. Its result is a word; a timeout (`badrpc_timeout`, `command_timeout`) means the restart may still
   be running on the node, so it is **never issued again**: the matrix decides, and the container step follows if its
   pairs still fail.
3. With `"link_then_container"`, a reader whose pairs still fail has its **container** restarted by the injector's own
   stop and start (`Recovery.restart_container`: journalled as an injection of its own, so a process that dies midway
   leaves it for recovery to bring back), then served, gated and checked again. The restored provider's own container
   is never restarted here: that is the outage again.

**Bounds.** At most one `restart_link` and one container restart per provider per restore (a later clear of the same
injection by the same injector does not heal it again), at most `ROUNDS` passes, and `[faults] heal_timeout_s` in all:
no action starts when its own longest wait and the gate's would carry the heal past it. The link check's own time is
not in that reserve, so the heal can end one check's time past the bound.

**Fail closed.** An action after which a pair that passed before it fails stops the heal at once (`heal_broke_pair`);
the new failure is never healed. A gate that does not pass after an action stops it (`sessions_not_ready`), and so does
a container restart that is not verified (`heal_restart_failed`). A heal that ends with a pair still failing is
`links_stuck`, as a restore with no heal is. Only a matrix with every pair passing is `healed`, and only then is the
restore verified.

**The record** (`HealResult.record`) holds the mode, the outcome, the matrix before the heal and after every action, by
label, with the source every failing pair shares (`shared_source`: the crashed provider, when there is one), and each
action: the provider by label, the action (`ping`, `restart_link`, `container_restart`), its result word, its start and
end in UTC, the gate's reading after it and the matrix after it. Nothing a command printed is kept.
"""

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from expcore_onedata.compose import RESTART_LINK_MARGIN_S, RPC_TIMEOUT_S, DriverError

ROUNDS = 2
QUIET_S = 5.0  # after a link restart, before anything reads through it
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
    pair; `gate(service)` passes the session gate for a provider and returns its reading (`ok` among its keys);
    `restart_container(label, service)` stops and starts a reader's container, verified, and returns how (`ok`,
    `how`, `serves_after_s`)."""

    def __init__(
        self,
        *,
        mode: str,
        driver: Any,
        services: Mapping[str, str],
        restored: str,
        bound_s: float,
        gate_s: float,
        ping_s: float,
        container_s: float,
        matrix: Callable[[], LinkMatrix],
        gate: Callable[[str], dict[str, Any]],
        restart_container: Callable[[str, str], dict[str, Any]],
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        utc: Callable[[], str] = _utc,
    ) -> None:
        self._mode, self._driver, self._services, self._restored = mode, driver, dict(services), restored
        self._bound_s, self._gate_s = bound_s, gate_s
        self._link_cost = ping_s + RPC_TIMEOUT_S + RESTART_LINK_MARGIN_S + QUIET_S
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
        return self._clock() - self._started + cost + self._gate_s <= self._bound_s

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

    def _act(self, label: str, service: str, action: str, before: LinkMatrix) -> tuple[str | None, LinkMatrix, str]:
        """Run one action on a reader, then its gate and the matrix; return the word the heal must stop with (None
        to go on), the matrix after it and why."""
        start = self._utc()
        extra: dict[str, Any] = {}
        if action == "restart_link":
            try:
                result = str(self._driver.restart_link(service))
            except DriverError:
                result = "not_run"
            self._sleep(QUIET_S)
        else:
            done = self._restart(label, service)
            result, extra = str(done.get("how")), {"serves_after_s": done.get("serves_after_s")}
            if done.get("ok") is not True:
                self._record(label, action, result, start, **extra)
                return "heal_restart_failed", before, f"the container of {label} was not restored ({result})"
        gate = self._gate(service)
        if gate.get("ok") is not True:
            self._record(label, action, result, start, gate, **extra)
            return "sessions_not_ready", before, f"the session gate for {label} did not pass after its {action}"
        after = self._read()
        self._record(label, action, result, start, gate, after, **extra)
        if not after.pairs:
            return "links_stuck", after, f"the link check after the {action} on {label} could not run"
        broken = after.broken_since(before)
        if broken:
            shown = ", ".join(f"{source}->{reader}" for reader, source in broken)
            return "heal_broke_pair", after, f"the {action} on {label} broke {shown}, which passed before it"
        return None, after, ""

    def _heal_reader(self, label: str, current: LinkMatrix) -> tuple[str | None, LinkMatrix, str]:
        """The steps for one reader: ping and link restart, then the container when declared and still failing."""
        service = self._services[label]
        if label not in self._linked:
            if not self._fits(self._link_cost):
                return "links_stuck", current, "heal_timeout_s leaves no time for another link restart"
            self._linked.add(label)  # one restart_link per provider per restore, whatever the ping or the result
            if self._ping(label, service):
                stop, current, why = self._act(label, service, "restart_link", current)
                if stop is not None:
                    return stop, current, why
            elif self._mode != "link_then_container":
                return "links_stuck", current, f"{label} answered no pong, so its link cannot be restarted"
        still = label in current.stuck_readers()
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
            for label in current.stuck_readers():
                if label not in current.stuck_readers() or label in current.sources():
                    continue  # mended by an earlier action, or the source of a failing pair: never acted on
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
