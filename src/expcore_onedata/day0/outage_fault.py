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

`take_down` and `restore` turn the injector's outcome into the check's facts: whether the provider was seen down, and
whether it is back (`cleared`: nothing is left down) and restored (`verified`: back with a changed identity, and
serving), with the injector's own word for how (`restored`, `found_restored`, `never_down`, `identity_unchanged`,
`not_running`, `not_serving`, `start_failed`, `restore_unverified`, `stop_unsettled`). The provider is named by its
label and an identity by its digest, as the injector names them (O12).
"""

from collections.abc import Mapping
from typing import Any

from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.faults import FaultInjector

RESTORED = ("restored", "found_restored")  # the injector's ways of a verified restore


def _last(steps: list[Mapping[str, Any]], name: str) -> Mapping[str, Any] | None:
    found = [step for step in steps if step.get("step") == name]
    return found[-1] if found else None


class Outage:
    """One provider taken down through the injector and brought back; `restore` never raises."""

    def __init__(self, injector: FaultInjector, run_key: str) -> None:
        self._injector, self._identity = injector, {"run_key": run_key, "attempt": 1}

    def take_down(self) -> dict[str, Any]:
        """Start the injector: the provider journalled, stopped and seen down. On failure the injector has already
        tried to bring it back; `started` is then false, with the injector's reason, which names only the label."""
        try:
            self._injector.start(self._identity)
        except InfraError as error:
            return {"started": False, "reason": error.detail, **self._injected()}
        except ConfigError as error:  # refused before anything was stopped; its message names the file, so not kept
            return {
                "started": False,
                "reason": f"the injector refused to start ({type(error).__name__})",
                **self._injected(),
            }
        return {"started": True, "seen_down": True, **self._injected()}

    def _injected(self) -> dict[str, Any]:
        step = _last(self._injector.snapshot()["steps"], "injected")
        if step is None:
            return {"stop_sent": False}
        facts = {"stop_sent": True, "identity_before_sha256": step.get("identity_before_sha256")}
        if not step.get("ok"):
            facts["seen_down"] = bool(step.get("seen_down"))
        return facts

    def restore(self) -> dict[str, Any]:
        """Clear the injection (the injector settles it and verifies the identity), then read how it went from its
        snapshot. With nothing stopped, nothing needed restoring: `not_injected`."""
        cleared_now = self._injector.clear()
        snapshot = self._injector.snapshot()
        cleared, verified = _last(snapshot["steps"], "cleared"), _last(snapshot["steps"], "verified")
        if cleared is None:
            return {"cleared": cleared_now, "verified": False, "how": "not_injected"}
        facts: dict[str, Any] = {
            "cleared": bool(cleared.get("ok")) and cleared_now,
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
