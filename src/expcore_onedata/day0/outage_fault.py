# Provenance: new
"""The day-0 outage check's provider outage, made by the fault injector (`expcore_onedata.faults`, O10, O11, O28).

The check takes no provider down by itself. It builds the platform's `FaultInjector` for the space's data provider and
reads what the injector did from its snapshot, so the check has the injector's guarantees and no copy of them:

- **Sanction (O10).** The injector needs `sanctioned = true` in `[faults]` and `sanctioned=True` from its caller; the
  check gives the caller's half only once `EXPCORE_LIVE_MUTATE=1` and `EXPCORE_LIVE_FAULTS=1` are both set.
- **Journal and recovery (O11).** The injection is journalled before the stop; an injection an earlier process left open
  is cleared and verified before this one starts, and one that cannot be fails the start.
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
`not_running`, `not_serving`, `start_failed`, `restore_unverified`, `stop_unsettled`). The provider is named by its
label and an identity by its digest, as the injector names them (O12).
"""

import contextlib
from collections.abc import AsyncIterator, Mapping
from typing import Any

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.faults import FaultInjector

RESTORED = ("restored", "found_restored")  # the injector's ways of a verified restore


def _last(steps: list[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    found = [step for step in steps if step.get("step") == name]
    return found[-1] if found else None


class Outage:
    """One provider taken down through the injector for the body of `down` and brought back as it ends."""

    def __init__(self, injector: FaultInjector, run_key: str) -> None:
        self._injector, self._identity = injector, {"run_key": run_key, "attempt": 1}

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
        `not_injected`."""
        snapshot = self._injector.snapshot()
        cleared, verified = _last(snapshot["steps"], "cleared"), _last(snapshot["steps"], "verified")
        if cleared is None:
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


__all__ = ["RESTORED", "Outage"]
