# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/campaign/healthgate.py, blob d11e173f034051b52302d1d23168ac34c6f19f2a
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""The health gate: health that cannot be read is unhealthy, and an outage holds all dispatch (C11).

A pre-run health check (a `HealthCheck` the platform supplies) runs before every attempt. One that fails, raises, times
out or answers something that is not a `Health` counts as a failed check, never as a crash and never as a pass. The
first failed check **closes the gate**: no run starts until the dispatcher's own check, which it runs itself every
`recheck_s` seconds, passes. **One attempt is charged per outage**, to the run that found it; any other run whose check
fails while the gate is closed, or that finds it closed, is not charged and not recorded, and stays pending.

Each outage is one record in the store's meta `health_outages`, written when the gate closes, at every check and when
it opens, so a restart in the outage restores it (`restarts` counts those) and the status lists it from the same record:

    {"start_utc", "end_utc": null while open, "how": null | "check_passed", "checks": checks made by the dispatcher
     itself, the passing one included, "restarts": restarts survived while open, "last_check_utc", "first_detail",
     "last_detail"}

**A gate state that cannot be read at a restart opens closed**: whether the platform is up is then unknown, and the
first passing check ends it. Nothing is charged for a restart.

Post-run checks. A run whose post-run check fails **while the gate is closed at the instant the check returns** (it
began after the gate closed, or was in flight as it closed) is retried without the attempt counting: it is kept with its
tokens as an outage-discarded attempt, and the outage's record counts it (`discarded`, `discarded_input_tokens`,
`discarded_output_tokens`, `discarded_usage_unknown`). The definition is by order, with no clock window. A failed
post-run check that returns while the gate is still open does not close it; the next pre-run check that fails does.
"""

import contextlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, NamedTuple, Protocol

from expcore.campaign.store import Store

META = "health_outages"
HOW_PASSED = "check_passed"  # the dispatcher's own health check passed: dispatch resumed by itself
KEEP = 200  # the newest this many outages are kept
GO, HOLD, FOUND = "go", "hold", "found"  # what a pre-run check lets a run do
UNREADABLE_DETAIL = "the stored outage record was unreadable"


class Health(NamedTuple):
    """One reading of the platform: whether it is healthy, and in what words."""

    ok: bool
    detail: str


class HealthCheck(Protocol):
    """What the platform supplies: a quick look at whether runs can start. It may raise; that reads as unhealthy."""

    def __call__(self) -> Health:
        """Return the platform's health."""
        ...


def read_health(check: Callable[[], Any]) -> Health:
    """Run a check and return what it said. One that raises, or answers something other than a `Health` or an
    (ok, detail) pair with a real boolean, is a failed check, never a crash and never a pass."""
    try:
        answer = check()
    except Exception as error:
        return Health(False, f"health check raised {type(error).__name__}")
    if isinstance(answer, tuple | list) and len(answer) == 2 and isinstance(answer[0], bool):
        return Health(answer[0], str(answer[1]))
    return Health(False, f"health check answered {str(answer)[:80]!r}, not (ok, detail)")


def parse(raw: str | None) -> list[dict[str, Any]] | None:
    """Return the recorded outages, oldest first; [] when none was ever recorded; None when the value cannot be read
    (unknown, never 'none')."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        outages = data["outages"]
        if not isinstance(outages, list) or not all(
            isinstance(o, dict) and isinstance(o.get("start_utc"), str) for o in outages
        ):
            raise ValueError("not a list of outages")
    except (ValueError, KeyError, TypeError):
        return None
    return outages


def dump(outages: list[dict[str, Any]]) -> str:
    """Return the record of the newest outages as the text the store keeps."""
    return json.dumps({"outages": outages[-KEEP:]}, sort_keys=True)


def fact(raw: str | None) -> dict[str, Any]:
    """Return the status fact: readable, the outages, and the open one. An unreadable record lists none, never []."""
    outages = parse(raw)
    if outages is None:
        return {"readable": False, "outages": None, "open": None}
    return {
        "readable": True,
        "outages": outages,
        "open": next((o for o in reversed(outages) if o.get("end_utc") is None), None),
    }


def alarms(state: dict[str, Any], every_s: float) -> list[str]:
    """Return the status alarm for the gate being closed now, or for its record being unreadable."""
    if not state["readable"]:
        return [
            "HEALTH GATE: the outage record in the store is unreadable, so whether the gate is closed is unknown "
            "(a restart treats it as closed until a check passes)"
        ]
    outage = state["open"]
    if outage is None:
        return []
    last = outage.get("last_detail") or outage.get("first_detail") or "no detail"
    return [
        f"HEALTH GATE CLOSED since {outage['start_utc']}: no run starts; the dispatcher re-checks every {every_s:g} s "
        f"and resumes by itself on the first pass ({outage.get('checks', 0)} check(s) so far, last "
        f"{outage.get('last_check_utc') or 'not yet'}: {str(last)[:160]}); one attempt was charged, to the run that "
        "found it"
    ]


@dataclass(frozen=True)
class PreRun:
    """What a pre-run check lets the run do: `go`, `hold` (stay pending, charge nothing) or `found` (this run found a
    new outage and is charged its one attempt)."""

    verdict: str
    detail: str


class HealthGate:
    """The gate: its state in the store, its check, and the thread that re-checks while it is closed."""

    def __init__(
        self,
        store: Store,
        check: HealthCheck,
        clock: Callable[[], str],
        *,
        recheck_s: float = 30.0,
        timeout_s: float = 60.0,
    ):
        if recheck_s <= 0 or timeout_s <= 0:
            raise ValueError("recheck_s and timeout_s must be positive")
        self.store, self.check, self.clock = store, check, clock
        self.recheck_s, self.timeout_s = recheck_s, timeout_s
        self._open: dict[str, Any] | None = None  # the open outage's record; None: the gate is open
        self._outages: list[dict[str, Any]] = []
        self._lock = threading.Lock()  # "who found it" and the record's writes are one step each
        self._wake = threading.Event()  # set when the gate closes
        self._call: dict[str, Any] | None = None  # the check in flight: one at a time
        self._thread: threading.Thread | None = None
        self.check_now = False  # a gate restored by a restart is checked at once, not after a period

    # --- state ----------------------------------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        """Tell whether the gate is closed now."""
        return self._open is not None

    def load(self) -> None:
        """Restore the outage list at a start. A gate closed when the last process ended is closed again and checked at
        once; a record that cannot be read reads as closed too, until a check passes. Nothing is charged for it."""
        now = self.clock()
        raw = self.store.get_meta(META)
        outages = parse(raw)
        if outages is None:
            self.store.event(now, "health_gate_record_unreadable", None, f"{META} cannot be read: the gate is closed")
            outages = [
                {
                    "start_utc": now,
                    "end_utc": None,
                    "how": None,
                    "checks": 0,
                    "restarts": 0,
                    "last_check_utc": None,
                    "first_detail": UNREADABLE_DETAIL,
                    "last_detail": UNREADABLE_DETAIL,
                }
            ]
        self._outages = outages
        open_one = next((o for o in reversed(outages) if o.get("end_utc") is None), None)
        if open_one is not None:
            open_one["restarts"] = int(open_one.get("restarts", 0)) + 1
            self._open, self.check_now = open_one, True
            self.store.put_meta(META, dump(self._outages), now)
            self.store.event(
                now,
                "health_gate_restored",
                None,
                f"the gate was closed (since {open_one['start_utc']}) when the last process ended: it stays closed and is "
                "checked at once; no attempt is charged for the restart",
            )

    def _close(self, run_key: str, detail: str) -> bool:
        """Close the gate for a failed check. True: this run found the outage and is its one attempt."""
        with self._lock:
            if self._open is not None:
                return False
            now = self.clock()
            record = {
                "start_utc": now,
                "end_utc": None,
                "how": None,
                "checks": 0,
                "restarts": 0,
                "last_check_utc": None,
                "first_detail": detail[:200],
                "last_detail": detail[:200],
            }
            self._open = record
            self._outages.append(record)
            self.store.put_meta(META, dump(self._outages), now)
            self.store.event(
                now,
                "health_gate_closed",
                None,
                f"{run_key}: the pre-run health check failed ({detail[:200]}): no run starts until the dispatcher's own "
                f"check passes (every {self.recheck_s:g} s); this run is charged one attempt, no other is",
            )
        self._wake.set()
        return True

    # --- the checks of a run ----------------------------------------------------------------------------------------

    def _read_at_gate(self) -> tuple[Health, dict[str, Any] | None]:
        """Run a check and take the gate's state the instant it returns: the open outage's record or None.

        The check runs outside the lock, since it may take as long as its timeouts. The gate is read with the answer in
        hand, and the run is classified from that read, not from one made after other statements.
        """
        health = read_health(self.check)
        with self._lock:
            return health, self._open

    def before_run(self, run_key: str) -> PreRun:
        """Check before an attempt. A closed gate holds the run without a check; a passing check lets it go unless the
        gate closed while the check was out; a failing check closes the gate and charges this run, or, if another run
        closed it first, holds this one."""
        if self._open is not None:
            return PreRun(HOLD, "the gate is closed")
        health, closed = self._read_at_gate()
        if closed is not None:
            return PreRun(HOLD, "the gate closed while this check was out")
        if health.ok:
            return PreRun(GO, health.detail)
        if not self._close(run_key, health.detail):
            return PreRun(HOLD, "another run's failed check closed the gate first")
        return PreRun(FOUND, health.detail)

    def after_run(self) -> tuple[Health, dict[str, Any] | None]:
        """Check after an attempt: the health, and the outage record if the gate was closed when the check returned."""
        return self._read_at_gate()

    def note_discard(self, outage: dict[str, Any], input_tokens: int | None, output_tokens: int | None) -> None:
        """Count a post-run attempt the outage discarded, and its tokens (counted apart where unknown, never as zero)."""
        with self._lock:
            outage["discarded"] = int(outage.get("discarded", 0)) + 1
            if input_tokens is None and output_tokens is None:
                outage["discarded_usage_unknown"] = int(outage.get("discarded_usage_unknown", 0)) + 1
            outage["discarded_input_tokens"] = int(outage.get("discarded_input_tokens", 0)) + int(input_tokens or 0)
            outage["discarded_output_tokens"] = int(outage.get("discarded_output_tokens", 0)) + int(output_tokens or 0)
            self.store.put_meta(META, dump(self._outages), self.clock())

    # --- the dispatcher's own check while the gate is closed ------------------------------------------------------

    def _result(self, health: Health) -> bool:
        """Record one check of the dispatcher's own; return True when it opened the gate."""
        with self._lock:
            record = self._open
            if record is None:
                return False
            now = self.clock()
            record["checks"] = int(record.get("checks", 0)) + 1
            record["last_check_utc"], record["last_detail"] = now, health.detail[:200]
            if health.ok:
                record["end_utc"], record["how"] = now, HOW_PASSED
                self._open = None
            self.store.put_meta(META, dump(self._outages), now)
            if health.ok:
                survived = f" and survived {record['restarts']} restart(s)" if record.get("restarts") else ""
                self.store.event(
                    now,
                    "health_gate_opened",
                    None,
                    f"the dispatcher's own health check passed ({health.detail[:200]}) after {record['checks']} "
                    f"check(s); the outage began {record['start_utc']}{survived}: dispatch resumes",
                )
        return health.ok

    def _timed(self, stop: threading.Event | None) -> Health:
        """One check of the dispatcher's own, with a timeout: one that does not answer in `timeout_s` reads as down and
        its late answer is thrown away. While it is out no second one starts."""
        previous = self._call
        if previous is not None and not previous["done"].is_set():
            return Health(False, "the previous health check has still not answered")
        box: dict[str, Any] = {"done": threading.Event()}

        def go() -> None:
            try:
                box["health"] = read_health(self.check)
            finally:
                box["done"].set()

        self._call = box
        threading.Thread(target=go, daemon=True, name="health-gate-check").start()
        waited = 0.0
        while not box["done"].wait(0.02):  # in slices, so the end of the run is seen at once
            waited += 0.02
            if stop is not None and stop.is_set():
                return Health(False, "the campaign is ending")
            if waited >= self.timeout_s:
                return Health(False, f"the health check did not answer within {self.timeout_s:g} s: counted as down")
        return box["health"]

    def poll(self, stop: threading.Event | None = None) -> bool:
        """Run the dispatcher's own check once, if the gate is closed; return True when it opened the gate."""
        if self._open is None:
            return False
        health = self._timed(stop)
        if stop is not None and stop.is_set():
            return False
        return self._result(health)

    def run_loop(self, stop: threading.Event, on_open: Callable[[], None] = lambda: None) -> None:
        """While the gate is closed, check every `recheck_s` (at once after a restart); the first pass opens it. An
        error in one round is an event, never the end of the loop."""
        while not stop.is_set():
            try:
                if self._open is None:
                    self._wake.wait(1.0)
                    self._wake.clear()
                    continue
                wait = 0.0 if self.check_now else self.recheck_s
                self.check_now = False
                if stop.wait(wait):
                    break
                if self.poll(stop):
                    on_open()
            except Exception as error:
                with contextlib.suppress(Exception):  # the store may be gone: the campaign is ending
                    self.store.event(
                        self.clock(),
                        "health_gate_error",
                        None,
                        f"{type(error).__name__}: {error}"[:300] + "; the gate stays as it was and is tried again",
                    )
                stop.wait(1.0)

    def start(self, stop: threading.Event, on_open: Callable[[], None] = lambda: None) -> None:
        """Start the thread that re-checks while the gate is closed."""
        self._wake.clear()
        self._thread = threading.Thread(target=self.run_loop, args=(stop, on_open), daemon=True, name="health-gate")
        self._thread.start()

    def join(self, timeout: float = 30.0) -> None:
        """Wait for the thread to end; the caller has set the stop event."""
        if self._thread is not None:
            self._thread.join(timeout=timeout)
