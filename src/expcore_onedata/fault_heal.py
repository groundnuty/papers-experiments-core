# Provenance: new
"""The bounded heal of a link a restore left stuck (O35), declared by `[faults] heal` and off by default.

On the live slot of 14 Nov 2026 a provider that fetched from a stopped owner had its `rtransfer_link` crash and come
back, and a reader whose link to it never reconnected failed every fetch from it, while every other pair transferred.
Another study's federation then showed every pair with the crashed provider as source failing but the one whose reader
started after the crash. **The rule its live tests settled:** after `restart_link` on a provider R, nothing may fetch
through R until a settle has ended. A restart followed by a clean 3-minute settle with no fetch healed R's own pair and
left every pair with R as its source healthy, one whose connection was minutes old included; the one reverse pair left
stale came from a check made 23 s after R's restart. (Why a fetch so early wedges the pair is in the link's private
code.) So every restart here is followed by an **enforced settle**: no probe, no gate read, no check and no run (the
injector holds the federation exclusively) until `heal_settle_s` has passed, with no active transfer before the restart
or at the settle's end.

**`"all_links"`, the one-round mode.** Between runs, under the injector's exclusive admission:

1. a fresh matrix of every pair (`LinkMatrix`); nothing more is done when it passes;
2. `ping` every provider with a declared service, in label order (`ComposeDriver.ping`); one with no `pong` fails the
   heal closed before any restart, since a partial sequence is what leaves pairs stale;
3. no active transfer in the probe space or a study space (`ComposeDriver.active_transfers`: the waiting and ongoing
   listings, read only), waited for at most `gate_timeout_s`, else closed (`transfers_active`): a replication could
   dial a link mid-sequence;
4. `restart_link` on **every** provider, the restored one and the crashed source included, one after another with no
   fetch in between, so one settle covers them all (`ComposeDriver.restart_link`; each result is a word, and a
   `badrpc_timeout` is never retried); before each later restart the listings are read once, not waited for, and an
   active transfer stops the sequence there, closed, after the settle;
5. one settle of `heal_settle_s` (180 s, provisional) with no probe, no fetch and no run, then the listings read once
   (an active transfer fails it closed), then the session gate of the restored provider, then every ordered pair,
   with fresh probes.

The settle is enforced after every restart that was sent, in both modes, an interrupted sequence and an unverified
container restart included. Each restart is held before it is sent (`hold`, `Recovery.hold`), so a heal interrupted
mid-step leaves the settle held, and the next gate read or link check of that recovery waits until it could have ended.

If a pair still fails, the **container fallback**: the readers of the stuck pairs, one at a time in label order, each
container restarted by the injector's own stop and start (`Recovery.restart_container`: journalled as an injection of
its own before the stop, cleared only once every pair passes), then settled, gated and checked again; never a provider
that is the source of a failing pair, and never the restored provider (that is the outage again).

**The per-reader mode:** `"link_then_container"` (and `"link"`, the same without containers, kept as it costs nothing)
heals each stuck pair's reader in turn: `ping`, a fresh matrix (a reader that is now the source of a failing pair, or
whose pairs pass, is left alone), then no active transfer, `restart_link`, the same enforced settle, no active transfer,
the gate and a fresh matrix; then the container step, enforced the same way.

**Bounds.** At most one `restart_link` and one container restart per provider per restore: the restore journals that
it heals before the first action, so neither a later clear nor another process's recovery heals the same injection
again. At most `ROUNDS` passes of per-reader steps, and `[faults] heal_timeout_s` (4,200 s, provisional) in all: no
step starts unless its declared bounds fit in what is left (`fault_settings.heal_all_links_reserve`,
`heal_link_reserve`, `heal_container_reserve`), and each wait for transfers or the gate is cut to what is left. The
link check's own time is outside the reserve, so the heal can end one check's time past the bound.

**Fail closed.** A step after which a pair that passed before it fails stops the heal at once (`heal_broke_pair`). A
gate that does not pass stops it (`sessions_not_ready`), and so does a transfer still active (`transfers_active`) and a
container restart that is not verified (`heal_restart_failed`). A heal that ends with a pair failing is `links_stuck`.
Only a matrix with every pair passing is `healed`, and only then is the restore verified; until then the injector holds
the federation exclusively, so no run is admitted.

**The record** (`HealResult.record`) holds the mode, the outcome and why, the matrix before the heal and after it, the
source every failing pair shares (`shared_source`: the crashed provider), the settle window's start and end, and every
step: the provider by label (`all` for the transfer readings and the all-pairs check), the step (`ping`,
`restart_link`, `transfers`, `check`, `container_restart`), its result word, its start and end in UTC, and, where it has
them, the settle, the gate's reading and the matrix after it. Nothing a command printed is kept.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from expcore_onedata.compose import DriverError
from expcore_onedata.fault_matrix import LinkMatrix, PairState, label_order, unreadable

__all__ = ["ROUNDS", "HealResult", "LinkHeal", "LinkMatrix", "PairState", "unreadable"]

ROUNDS = 2


@dataclass
class HealResult:
    """How a heal ended: `how` is `healed`, `links_stuck`, `heal_broke_pair`, `sessions_not_ready`,
    `transfers_active` or `heal_restart_failed`; `record` goes into the injector's snapshot."""

    how: str
    record: dict[str, Any] = field(default_factory=dict)

    @property
    def healed(self) -> bool:
        return self.how == "healed"


def _utc() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


Step = tuple[str | None, LinkMatrix, str]  # the word to stop with (None to go on), the matrix after it, and why


class LinkHeal:
    """One heal of a restore's stuck links (see the module docstring).

    `services` maps a provider's label to its service, and `restored` is the label of the provider the restore brought
    back. `matrix()` reads every pair; `gate(service, timeout_s)` passes the session gate for a provider and returns its
    reading (`ok` among its keys); `transfers()` counts the active transfer listings (None when it cannot know);
    `restart_container(label, service, budget_s)` restarts a reader's container, verified, its waits ending within
    `budget_s` (`ok`, `how`, `serves_after_s`).
    `command_s` bounds one driver command, `settle_s` is the settle, and `link_s`, `all_links_s` and `container_s` are
    the declared bounds of a ping with a link restart, of the whole all-links sequence and of a container restart."""

    def __init__(
        self,
        *,
        mode: str,
        driver: Any,
        services: Mapping[str, str],
        restored: str,
        bound_s: float,
        gate_s: float,
        poll_s: float,
        command_s: float,
        settle_s: float,
        link_s: float,
        all_links_s: float,
        container_s: float,
        matrix: Callable[[], LinkMatrix],
        gate: Callable[[str, float], dict[str, Any]],
        transfers: Callable[[], int | None],
        restart_container: Callable[[str, str, float], dict[str, Any]],
        hold: Callable[[float], None],
        clock: Callable[[], float],
        sleep: Callable[[float], None],
        utc: Callable[[], str] = _utc,
    ) -> None:
        self._mode, self._driver, self._services, self._restored = mode, driver, dict(services), restored
        self._bound_s, self._gate_s, self._poll_s, self._settle_s = bound_s, gate_s, poll_s, settle_s
        self._link_cost, self._restart_cost = link_s, link_s - 2 * command_s  # what is left once a ping is done
        self._all_links_cost, self._container_cost = all_links_s, container_s
        self._margin_s = 3 * command_s  # past a container restart's budget: a start in flight and one reading
        self._matrix, self._gate, self._transfers, self._restart = matrix, gate, transfers, restart_container
        self._hold = hold  # marks that nothing may read through the links for that many seconds from now
        self._clock, self._sleep, self._utc = clock, sleep, utc
        self._actions: list[dict[str, Any]] = []
        self._skipped: list[dict[str, str]] = []
        self._window: dict[str, Any] | None = None
        self._linked: set[str] = set()  # providers whose link was restarted, or whose node gave no pong
        self._restarted: set[str] = set()  # providers whose container was restarted, or that may never be
        self._started = 0.0

    # --- readings and records ---------------------------------------------------------------------------------------

    def _read(self) -> LinkMatrix:
        try:
            return self._matrix()
        except Exception as error:  # a check that cannot answer is a failed one, never a pass
            return unreadable(error)

    def _left(self) -> float:
        return self._bound_s - (self._clock() - self._started)

    def _fits(self, cost: float) -> bool:
        """Whether a step whose declared bounds are `cost` seconds ends within `heal_timeout_s`."""
        return cost <= self._left()

    def _gate_within(self, service: str) -> dict[str, Any]:
        """The gate after a step, waiting at most `gate_timeout_s` and never past `heal_timeout_s`."""
        return self._gate(service, max(0.0, min(self._gate_s, self._left())))

    def _record(self, label: str, action: str, result: str, start: str, **extra: Any) -> None:
        entry = {"provider": label, "action": action, "result": result, "start_utc": start, "end_utc": self._utc()}
        after = extra.pop("after", None)
        self._actions.append({**entry, **extra, **({} if after is None else {"matrix_after": after.to_json()})})

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
            "window": self._window,
            "actions": list(self._actions),
            "skipped": list(self._skipped),
            "tried": tried,
        }
        return HealResult(how, record)

    # --- the steps --------------------------------------------------------------------------------------------------

    def _ping(self, label: str, service: str) -> bool:
        start = self._utc()
        try:
            pong = self._driver.ping(service) is True
        except DriverError:
            pong = False
        self._record(label, "ping", "pong" if pong else "no_pong", start)
        return pong

    def _sent(self, label: str, service: str) -> str:
        """Send one link restart, the settle held first, so that even a restart interrupted mid-command leaves no read
        before its settle could end (`hold`), and again from its return; never issued twice to a provider."""
        self._linked.add(label)
        self._hold(self._restart_cost)  # its lookup, its RPC and the settle: the longest it could need
        try:
            result = str(self._driver.restart_link(service))
        except DriverError:
            result = "not_run"
        except Exception:
            # sent, perhaps: the settle runs here, under the admission still held, before anything unwinds and another
            # recovery (the health check's, which knows nothing of the hold) may read through the link
            self._settle()
            raise
        self._hold(self._settle_s)
        return result

    def _restart_link(self, label: str, service: str) -> str:
        """One link restart of the all-links sequence, recorded by its result word."""
        start = self._utc()
        result = self._sent(label, service)
        self._record(label, "restart_link", result, start)
        return result

    def _no_transfers(self, phase: str, *, wait: bool = True) -> bool:
        """Whether no transfer is active in the probe space or a study space; record the reading. Before a restart
        (`wait`), it polls every `poll_s` for at most `gate_timeout_s` (cut to what is left) until none is; once a
        restart has been issued (`wait=False`: the next restart of a sequence, the end of a settle), one reading
        decides, since a transfer active then may already have fetched through a restarting link. False when one is
        active, the listings cannot be read, or the driver cannot tell."""
        start, began = self._utc(), self._clock()
        bound = max(0.0, min(self._gate_s, self._left())) if wait else 0.0
        while True:
            try:
                active = self._transfers()
            except Exception:  # a listing that cannot be read shows no quiet: fail closed
                active = -1
            if not wait or active is None or active == 0 or self._clock() - began >= bound:
                break
            self._sleep(max(0.0, min(self._poll_s, bound - (self._clock() - began))))
        # a driver that cannot tell (None) shows no quiet, and neither does a quiet reading seen past the bound
        quiet = active == 0 and (not wait or self._clock() - began <= bound)
        result = "unknown" if active is None else ("quiet" if quiet else "active")
        waited = round(self._clock() - began, 3)
        self._record("all", "transfers", result, start, phase=phase, active=active, waited_s=waited)
        return quiet

    def _settle(self) -> float:
        """The settle after a restart: no probe, no fetch and no run touches any link meanwhile."""
        self._sleep(self._settle_s)
        return self._settle_s

    def _checked(self, label: str, action: str, before: LinkMatrix, after: LinkMatrix) -> Step:
        """The verdict on the matrix after a step: unreadable, a pair broken, or go on."""
        if not after.pairs:
            return "links_stuck", after, f"the link check after the {action} on {label} could not run"
        broken = after.broken_since(before)
        if broken:
            shown = ", ".join(f"{source}->{reader}" for reader, source in broken)
            return "heal_broke_pair", after, f"the {action} on {label} broke {shown}, which passed before it"
        return None, after, ""

    def _act(self, label: str, service: str, action: str, before: LinkMatrix, cost: float) -> Step:
        """One reader's link or container restart, enforced as the all-links sequence's is: no active transfer before
        it, then the settle with no fetch at all, no active transfer at its end, then its gate and a fresh matrix.
        `cost` is the step's declared bounds, checked again once the transfers are quiet."""
        if not self._no_transfers("before"):
            return "transfers_active", before, f"a transfer stayed active before the {action} on {label}"
        if not self._fits(cost):
            return "links_stuck", before, f"heal_timeout_s leaves no time for the {action} on {label}"
        start, extra = self._utc(), {}
        if action == "restart_link":
            result = self._sent(label, service)
        else:  # its waits end by what its reserve leaves after the margin and the settle: never past the bound
            budget = max(0.0, self._left() - self._margin_s - self._settle_s)
            self._hold(self._container_cost)  # held before the stop: an interrupted restart still leaves no read early
            try:
                done = self._restart(label, service, budget)
            except Exception:
                self._settle()  # as for a link restart: settled here, before the admission unwinds (`_sent`)
                raise
            self._hold(self._settle_s)
            result, extra = str(done.get("how")), {"serves_after_s": done.get("serves_after_s")}
        settled = self._settle()  # enforced after every restart sent, a failed or unverified one included
        if action != "restart_link" and done.get("ok") is not True:
            self._record(label, action, result, start, settle_s=settled, **extra)
            return "heal_restart_failed", before, f"the container of {label} was not restored ({result})"
        if not self._no_transfers("after", wait=False):
            self._record(label, action, result, start, settle_s=settled, **extra)
            return "transfers_active", before, f"a transfer was active at the end of the {action}'s settle on {label}"
        gate = self._gate_within(service)
        if gate.get("ok") is not True:
            self._record(label, action, result, start, settle_s=settled, gate=gate, **extra)
            return "sessions_not_ready", before, f"the session gate for {label} did not pass after its {action}"
        after = self._read()
        self._record(label, action, result, start, settle_s=settled, gate=gate, after=after, **extra)
        return self._checked(label, action, before, after)

    def _all_links(self, before: LinkMatrix) -> Step:
        """Ping every provider, wait for no transfer, restart every link in quick succession, settle with no fetch,
        wait for no transfer, pass the restored provider's gate and read every pair."""
        labels = sorted(self._services, key=label_order)
        paired = {provider for pair in before.pairs for provider in (pair.reader, pair.source)}
        uncovered = sorted(paired - set(labels), key=label_order)
        if uncovered:  # a partial sequence leaves pairs stale: every provider of a pair must be restartable
            shown = ", ".join(uncovered)
            return "links_stuck", before, f"no service is declared for {shown}, so not every link can be restarted"
        if not self._fits(self._all_links_cost):
            return "links_stuck", before, "heal_timeout_s leaves no time for the all-links sequence"
        for label in labels:
            if not self._ping(label, self._services[label]):
                return "links_stuck", before, f"{label} answered no pong, so not every link can be restarted"
        if not self._no_transfers("before"):
            return "transfers_active", before, "a transfer stayed active before the all-links sequence"
        pinged = len(labels) * (self._link_cost - self._restart_cost)  # the pings' share of the reserve, now spent
        if not self._fits(self._all_links_cost - pinged):
            return "links_stuck", before, "heal_timeout_s leaves no time for the all-links restarts"
        opened, interrupted = self._utc(), None
        for index, label in enumerate(labels):  # one after another, nothing fetched in between
            if index and not self._no_transfers("between", wait=False):  # a transfer could dial a restarted link
                interrupted = f"a transfer became active during the all-links sequence, before {label}'s restart"
                break
            self._restart_link(label, self._services[label])
        settled = self._settle()  # enforced once any restart was sent, an interrupted sequence included
        self._window = {"start_utc": opened, "end_utc": self._utc(), "settle_s": settled}
        if interrupted is not None:
            return "transfers_active", before, interrupted
        if not self._no_transfers("after", wait=False):
            return "transfers_active", before, "a transfer was active at the end of the all-links window"
        restored = self._services.get(self._restored)
        gate = self._gate_within(restored) if restored is not None else {"ok": True, "how": "none"}
        start = self._utc()
        if gate.get("ok") is not True:
            self._record("all", "check", "not_run", start, gate=gate)
            return "sessions_not_ready", before, "the restored provider's session gate did not pass after the restarts"
        after = self._read()
        self._record("all", "check", "passed" if after.ok else "failing", start, gate=gate, after=after)
        return self._checked("every provider", "all-links sequence", before, after)

    def _heal_reader(self, label: str, current: LinkMatrix) -> Step:
        """The per-reader steps: ping and link restart (unless done), then the container when declared and still
        failing."""
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
                stop, current, why = self._act(label, service, "restart_link", current, self._restart_cost)
                if stop is not None:
                    return stop, current, why
            elif self._mode == "link":
                return "links_stuck", current, f"{label} answered no pong, so its link cannot be restarted"
        still = label in current.stuck_readers() and label not in current.sources()
        if still and self._mode != "link" and label not in self._restarted:
            self._restarted.add(label)  # one container restart per provider per restore, or none
            if label == self._restored:
                self._skipped.append({"provider": label, "why": "the restored provider's container is not restarted"})
                return None, current, ""
            if not self._fits(self._container_cost):
                return "links_stuck", current, "heal_timeout_s leaves no time for a container restart"
            return self._act(label, service, "container_restart", current, self._container_cost)
        return None, current, ""

    def _readers(self, current: LinkMatrix, before: LinkMatrix) -> HealResult | LinkMatrix:
        """At most `ROUNDS` passes of per-reader steps, each taking its next reader from the latest matrix; the end
        when a step stops, else the matrix after the passes."""
        for _ in range(ROUNDS):
            if current.ok:
                break
            seen: set[str] = set()  # the readers this pass has taken, each once
            while True:
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
        return current

    def run(self) -> HealResult:
        """Heal what a fresh matrix shows stuck; return how it ended and its record."""
        self._started = self._clock()
        current = before = self._read()
        if current.ok:
            return self._end("healed", current, before)
        if self._mode == "all_links":
            stop, current, why = self._all_links(before)
            if stop is not None:
                return self._end(stop, current, before, why)
        ended = self._readers(current, before)
        if isinstance(ended, HealResult):
            return ended
        if ended.ok:
            return self._end("healed", ended, before)
        return self._end("links_stuck", ended, before, "a pair still fails after every step the bounds allow")
