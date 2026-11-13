# Provenance: new
"""The day-0 outage check's provider outage, made by the fault injector (`expcore_onedata.faults`, O10, O11, O28).

The check takes no provider down by itself. It builds the platform's `FaultInjector` for the space's data provider and
reads what the injector did from its snapshot, so the check has the injector's guarantees and no copy of them:

- **Sanction (O10).** The injector needs `sanctioned = true` in `[faults]` and `sanctioned=True` from its caller; the
  check gives the caller's half only once `EXPCORE_LIVE_MUTATE=1` and `EXPCORE_LIVE_FAULTS=1` are both set.
- **Journal and recovery (O11).** The injection is journalled before the stop; an injection an earlier process left open
  is cleared and verified before this one starts, and one that cannot be fails the start. The check runs that recovery
  (`recover`) before it prepares anything (#178), since a provider an earlier process left stopped fails every read and
  write of the preparation; what it settled is `recovered`, and one it cannot settle stops the run there. Such a run
  took nothing down and still leaves a provider that may be down, so `restored` tells it from a start that failed
  cleanly (`earlier_injection_open`, not cleared, naming each provider left open by its label).
- **Verified restore (O11).** A restore counts only when the service runs with an identity other than the one read
  before the stop and the provider then serves (its driver's `serves`, within `ready_timeout_s` of `[faults]`; the
  wait is `ready_after_s`); readiness alone is not one, and a provider that does not serve in time is `not_serving`,
  unverified. A stop whose outcome is unknown (its command timed out, or the process died with the journal saying
  `stopping`) is settled by waiting for the service to show down before it is started again, and one never seen down
  stays unverified.
- **The federation held (O11).** The check holds the federation's lock exclusively around a whole space's scenario
  (`outage.py`); the injector takes that admission as its own and names its target in the holder record.

**The outage is the injector's `running` (#27).** `down` takes the provider down for the body of an `async with`
through `FaultInjector.running`: the start runs in a worker thread whose future is kept, so a cancellation waits for it
(a thread cannot be stopped) instead of leaving it stopping the provider while nothing restores it, and the clear runs
in `finally`, shielded. The check never awaits the injector's start or clear in a bare `asyncio.to_thread`.

`down` and `restored` turn the injector's outcome into the check's facts: whether the provider was seen down, and
whether it is back (`cleared`: nothing is left down) and restored (`verified`: back with a changed identity, and
serving), with the injector's own word for how (`restored`, `found_restored`, `never_down`, `identity_unchanged`,
`not_running`, `not_serving`, `start_failed`, `restore_unverified`, `stop_unsettled`), or `earlier_injection_open`: the
start failed in settling an injection an earlier run left open (the injector's `recovered` step, not `ok`), or with no
such step and an injection open in the journal, or a journal that cannot be read. `not_injected` is only a start that
failed before anything was journalled, with nothing open. The provider is named by its label and an identity by its
digest, as the injector names them (O12).
"""

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Mapping
from typing import Any

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.fault_journal import InjectionJournal
from expcore_onedata.faults import FaultInjector

RESTORED = ("restored", "found_restored")  # the injector's ways of a verified restore
LEFT_OPEN = "earlier_injection_open"  # a start's recovery of an earlier injection failed or left it open
_WORD = re.compile(r"[A-Za-z_]{1,40}")  # how a settling ended, as the injector or its journal words it


def _last(steps: list[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    found = [step for step in steps if step.get("step") == name]
    return found[-1] if found else None


def _word(how: Any, otherwise: str) -> str:
    """How a settling ended when it is a plain word, else `otherwise`: the journal is a file, nothing else is kept."""
    return how if isinstance(how, str) and _WORD.fullmatch(how) else otherwise


class Outage:
    """One provider taken down through the injector for the body of `down` and brought back as it ends."""

    def __init__(self, injector: FaultInjector, run_key: str, journal: InjectionJournal) -> None:
        """`journal` is the injector's own (`[faults] journal`), read only after a start that failed with nothing
        taken down, to see whether it left an injection open (`left_open`)."""
        self._injector, self._identity, self._journal = injector, {"run_key": run_key, "attempt": 1}, journal

    async def recover(self) -> list[dict[str, Any]]:
        """Settle every injection an earlier run left open, before the check prepares anything (#178): preparing reads
        and writes through the data provider, which a process that died with it stopped leaves unanswering, so the
        recovery cannot wait for the start. Return the injections still open, each as `{"target": label, "how":
        word}`: none when nothing is, and `recovered` then tells what was settled. A recovery the injector cannot
        finish, a lock that is stuck, a journal that cannot be read and an injection a live injector holds down are
        each left open, since the provider may be down.

        The recovery runs in a worker thread whose future is kept, so a cancellation waits for it (shielded, at most
        the injector's `cancel_wait_s`) instead of leaving it restarting a provider while the check cleans up."""
        recovering = asyncio.ensure_future(asyncio.to_thread(self._injector.recover))
        try:
            try:
                held = await asyncio.shield(recovering)
            except asyncio.CancelledError:
                await asyncio.wait({recovering}, timeout=self._injector.cancel_wait_s)
                if recovering.done() and not recovering.cancelled():
                    recovering.exception()  # read here: what the thread raised is never an unread error
                raise
        except Exception as error:  # fail closed, as a start's failure does: the provider may be down (O11)
            how = "left_open" if isinstance(error, InfraError) else _word(type(error).__name__, "left_open")
            # the injector's failed `recovered` step, else the journal, tells which injection is open
            return self.left_open(self._injector.snapshot()["steps"]) or [{"target": None, "how": how}]
        return [{"target": target, "how": "left_open"} for target in held]

    def recovered(self) -> list[dict[str, Any]]:
        """The injections of an earlier run that `recover` settled, each as `{"target", "how", "ready_after_s"}` in the
        injector's words (`restored`, `found_restored`): the provider was found back, or restarted under a new identity
        and serving."""
        steps = [step for step in self._injector.snapshot()["steps"] if step.get("step") == "recovered" and step["ok"]]
        return [
            {
                "target": step["target"],
                "how": _word(step.get("how"), "restored"),
                "ready_after_s": step.get("ready_after_s"),
            }
            for step in steps
        ]

    def left_open_facts(self, left: list[dict[str, Any]]) -> dict[str, Any]:
        """The check's `restore` facts for an injection found open and not settled (`LEFT_OPEN`): not cleared."""
        steps = [step["step"] for step in self._injector.snapshot()["steps"]]
        return {"cleared": False, "verified": False, "how": LEFT_OPEN, "left_open": left, "journal_steps": steps}

    @contextlib.asynccontextmanager
    async def down(self) -> AsyncIterator[dict[str, Any]]:
        """The provider journalled, stopped and seen down for the body, and cleared as it ends (`running`). Yields the
        start's facts: `started` and `seen_down`, or, when the start failed, `started` false with the injector's
        reason, which names only the label (the injector has then tried to bring the provider back, and cleared)."""
        entered = False
        try:
            async with self._injector.running(self._identity):
                entered = True
                yield {"started": True, "seen_down": True, **self._injected()}
                return
        except InfraError as error:
            if entered:
                raise
            reason = error.detail
        except ConfigError as error:  # refused before anything was stopped; its message names the file, so not kept
            if entered:
                raise
            reason = f"the injector refused to start ({type(error).__name__})"
        yield {"started": False, "reason": reason, **self._injected()}

    def _injected(self) -> dict[str, Any]:
        step = _last(self._injector.snapshot()["steps"], "injected")
        if step is None:
            return {"stop_sent": False}
        facts = {"stop_sent": True, "identity_before_sha256": step.get("identity_before_sha256")}
        if not step.get("ok"):
            facts["seen_down"] = bool(step.get("seen_down"))
        return facts

    def restored(self) -> dict[str, Any]:
        """How the restore went, read from the injector's snapshot once `down` has ended (its clear settled the
        injection and verified the identity and the serving). With nothing stopped, nothing needed restoring:
        `not_injected`, unless the start failed in settling an injection an earlier run left open, or left one open
        (`earlier_injection_open`, with `left_open`): a provider may still be down, so it is not cleared."""
        snapshot = self._injector.snapshot()
        cleared, verified = _last(snapshot["steps"], "cleared"), _last(snapshot["steps"], "verified")
        if cleared is None:
            left = self.left_open(snapshot["steps"])
            if left:
                return self.left_open_facts(left)
            return {"cleared": True, "verified": False, "how": "not_injected"}
        facts: dict[str, Any] = {
            "cleared": bool(cleared.get("ok")),
            "verified": bool(verified and verified.get("ok")),
            "how": cleared.get("how"),
            "identity_changed": bool(verified and verified.get("identity_changed")),
        }
        if verified and verified.get("identity_after_sha256"):
            facts["identity_after_sha256"] = verified["identity_after_sha256"]
        if verified and verified.get("ready_after_s") is not None:
            facts["ready_after_s"] = verified["ready_after_s"]  # from the identity change to serving
        facts["journal_steps"] = [step["step"] for step in snapshot["steps"]]
        return facts

    def left_open(self, steps: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
        """The injections a start that took nothing down left open, each as `{"target": label, "how": word}`.

        The injector's own word comes first: its recovery of an injection an earlier run left open records each one it
        failed to settle (`recovered`, not `ok`, with the label and how it ended). A start can also fail with no such
        step and leave one open all the same (an injection a live injector holds down, a journal that cannot be read),
        so without one the journal is read: each open injection there is left open (`how` is its `clear_failed`, else
        `left_open`), and a journal that cannot be read cannot show that nothing is open, so it is told as one
        (`target` None, `journal_unreadable`). A start that failed cleanly leaves the journal with nothing open."""
        failed = [step for step in steps if step.get("step") == "recovered" and not step.get("ok")]
        if failed:
            return [{"target": step.get("target"), "how": _word(step.get("how"), "left_open")} for step in failed]
        try:
            entries = self._journal.open_entries()
        except (ValueError, OSError):  # a line number or an OS error's path is not kept (O12)
            return [{"target": None, "how": "journal_unreadable"}]
        return [{"target": entry["target"], "how": _word(entry.get("clear_failed"), "left_open")} for entry in entries]


__all__ = ["LEFT_OPEN", "RESTORED", "Outage"]
