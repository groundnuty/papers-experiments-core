# Provenance: new
"""The outage check's steps after the restore, and the agent's tokens' end (O28, issue #108).

`AfterRestore` is part of `outage_run.Run`, kept apart for its size: once the injector has cleared the injection, the
restore is checked (cleared, verified, the target answering REST again within the settle timeout) and the reads of the
outage are made again until each is served or the settle timeout, which must return what was written; the transfers
scheduled during the outage are read once more. A step that uses the agent's tokens (a read through the mount, a tool
call) is made only while they surely hold (`AgentPlane.tokens_until`); once they have ended it is not made, and the
first such step is the check's own failure, never the provider's.
"""

import asyncio
from collections.abc import Callable
from typing import Any

from expcore_onedata.day0.outage_agent import AgentPlane
from expcore_onedata.day0.outage_budget import DOWN_PROBE_S
from expcore_onedata.day0.outage_cells import KINDS, Cells, ScratchFile
from expcore_onedata.day0.outage_fault import LEFT_OPEN, RESTORED
from expcore_onedata.day0.outage_tools import Tools
from expcore_onedata.day0.registry import Context


class AfterRestore:
    """The checks after the restore, on the run's own state (the attributes below are `outage_run.Run`'s)."""

    ctx: Context
    plane: AgentPlane
    cells: Cells
    tools: Tools | None
    files: dict[tuple[str, str], ScratchFile]
    directory_id: str
    bound_s: float
    facts: dict[str, Any]
    problems: list[str]
    tokens_ended: str | None
    expected: dict[str, int]
    mount_cells: Callable[..., dict[str, Any]]

    async def after_restore(self, went_down: bool) -> None:
        """Check the restore: cleared and verified by the injector, the target answering, and the reads returning
        what was written. A provider never seen down needs only to be found as it was (`never_down`). A recovery of an
        earlier injection that failed or left it open (`earlier_injection_open`, before anything was prepared) or a
        start that did is a failure that names the provider by its position, and `outage.provider_outage` runs no
        later space after it."""
        restore = self.facts["restore"]
        if restore["how"] == "not_injected":
            return
        if restore["how"] == LEFT_OPEN:  # nothing of this space was stopped, but a provider may still be down
            left = ", ".join(
                f"{row['target'] or 'the injection journal'} ({row['how']})" for row in restore["left_open"]
            )
            self.problems.append(
                "the injector could not settle an injection an earlier run left open, so nothing was prepared, no "
                f"provider was taken down and one may still be down: {left}"
            )
            return
        if not restore["cleared"]:
            self.problems.append(f"the provider could not be brought back ({restore['how']})")
            return
        if went_down and restore["how"] not in RESTORED:
            self.problems.append(f"the provider could not be restored and verified ({restore['how']})")
            return
        if not await asyncio.to_thread(self.wait_for_target):
            self.problems.append("the provider ran again but did not answer REST within the settle timeout")
            return
        if not went_down:
            return
        after = await asyncio.to_thread(self.rereads)
        if self.tools is not None and self.tokens_hold("the transfers' reading after the restore"):
            after["transfers"] = {kind: await self.tools.transfer_now(f"knowledge/{kind}") for kind in KINDS}
        self.facts["after"] = after
        reads = {name: row for name, row in after.items() if name != "transfers" and not row.get("tokens_ended")}
        wrong = [name for name, row in reads.items() if row["outcome"] != "served"]
        if wrong:
            self.problems.append(f"after the restore {len(wrong)} read(s) did not return what was written")

    def rereads(self) -> dict[str, Any]:
        """The reads again after the restore, each until it is served or the settle timeout."""
        files = self.files
        after: dict[str, Any] = {
            "owner_only": self.reread(lambda: self.cells.read(files[("rest", "owner_only")])),
            "replicated": self.reread(lambda: self.cells.read(files[("rest", "replicated")])),
            "listing": self.reread(lambda: self.cells.listing(self.directory_id, self.expected)),
        }
        if self.plane.mount_path is not None:
            after.update(self.mount_rereads())
        return after

    def mount_rereads(self) -> dict[str, Any]:
        """The mount's two files again after the restore, from one cell per try, until both are served or the settle
        timeout (`mount_owner_only`, `mount_replicated`). The cell reads both, and each must return what was written: a
        replicated file that differs or fails after the restore fails the check, as the owner-only one does (#178)."""
        latest: dict[str, dict[str, Any]] = {}

        def both() -> dict[str, Any]:
            cells = self.mount_cells("mount", listing=False)
            latest.update({kind: cells[kind] or {"outcome": None} for kind in KINDS})
            return next((row for row in latest.values() if row["outcome"] != "served"), latest["owner_only"])

        row = self.reread(both, tokens="the re-read through the mount")
        if row.get("tokens_ended"):  # neither was read: the tokens' end is the check's own failure
            return {"mount_owner_only": row, "mount_replicated": row}
        return {f"mount_{kind}": {**latest[kind], "attempts": row["attempts"]} for kind in KINDS}

    def wait_for_target(self) -> bool:
        """Poll the target's REST until it answers, within the settle timeout; record how long it took."""
        settings = self.ctx.fault_settings
        assert settings is not None
        began, tries = self.ctx.clock(), 0
        while True:
            tries += 1
            if self.cells.target_answers(min(self.bound_s, DOWN_PROBE_S))["answers"]:
                elapsed = round(self.ctx.clock() - began, 3)
                self.facts["restore"].update(rest_answers_again=True, rest_answered_after_s=elapsed, polls=tries)
                return True
            if self.ctx.clock() - began >= settings.settle_timeout_s:
                self.facts["restore"].update(rest_answers_again=False, polls=tries)
                return False
            self.ctx.sleep(settings.poll_s)

    def reread(self, read: Callable[[], dict[str, Any]], *, tokens: str | None = None) -> dict[str, Any]:
        """Read again until the read is served, within the settle timeout (a provider is slow to take up its peers).
        A read made with the agent's tokens (`tokens` names it) is not made once they have ended: it is then
        `tokens_ended`, the check's own failure, never the provider's."""
        settings = self.ctx.fault_settings
        assert settings is not None
        began, tries = self.ctx.clock(), 0
        while True:
            if tokens is not None and not self.tokens_hold(tokens):
                return {"outcome": None, "tokens_ended": True, "attempts": tries}
            tries += 1
            row = read()
            if row["outcome"] == "served" or self.ctx.clock() - began >= settings.settle_timeout_s:
                return {**row, "attempts": tries}
            self.ctx.sleep(settings.poll_s)

    def tokens_hold(self, step: str) -> bool:
        """Whether the agent's tokens surely still hold for a step that uses them (`AgentPlane.tokens_until`). When
        they have ended the step is not made, and the first such step is the check's own failure, named: a step the
        tokens could not make says nothing about the provider."""
        until = self.plane.tokens_until
        if until is None or self.ctx.clock() < until:
            return True
        if self.tokens_ended is None:
            self.tokens_ended = step
            self.problems.append(
                f"the agent's tokens had ended before {step}, so it was not made (the check's own failure: the "
                "lifetime they were minted for did not cover the scenario)"
            )
        return False


__all__ = ["AfterRestore"]
