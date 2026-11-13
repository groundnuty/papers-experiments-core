# Provenance: new
"""The preallocated mode of a progressive ingest: each planned file at its final size from the start, with only a
declared prefix written at the owner, the rest written in parts, and the reset completing the file in place (#107,
O36). `ingest.py` drives it; `ingestbase.Baseline` holds the journal, the id records and the removal it builds on.

**The platform's facts.** A range nobody wrote reads as zeros, at its full length, through the REST content endpoint
(a 206) and through the mount, and the block map holds only written ranges (measured on 10 and 11 Nov,
`deploy/compose-federation/day0/2026-11-10-federation-facts.md` and `2026-11-11-federation-facts-mount.md`). An empty
write at an offset past the end sets the size and writes no block, as that measurement made it; op-worker f118ed9
drops the zero-size block (read_write_req.erl:58-86, replica_updater.erl:187-192, fslogic_blocks.erl:125-126). A read
of the gap starts no transfer, since no provider holds it (replica_finder.erl:53-89); through the mount it costs
oneclient about seven `SynchronizeBlock` round trips before it returns zeros (its source, fsLogic.cc:952-1001, not
measured). A write of content without an offset truncates the file to 0 first (file_content_rest_handler.erl:224-228),
so none is ever sent (`client.TruncatingWrite`).

**The start state** of each file, in the plan's order. The complete file is removed: its staged copy is checked
first and the intent journalled, and the start waits until every provider answers its old id absent, since 25.0
deletes asynchronously and a name used again at once was measured to lose files (#29, F7). Then a new file is made
under the same name and **extended first**: created, then the empty write at its final size, then the prefix written
at offset 0. Extending first means the namespace shows the final size from the first moment the file holds a byte,
and no later write can change it, since every one lies inside it; writing the prefix first would show the file at the
prefix's size until the extension. The only moment the size is not final is between the creation and the extension,
two calls of the instrument's start, before the agent's first turn, and the start state is confirmed (below) before the
schedule can begin. The 25.0 build has no other way to take a file the owner holds whole to a prefix: REST truncates
only to 0, and the owner's copy is never evicted (O09). So **the start recreates the file and the reset never deletes
it**; each new id is recorded with the old one.

**Every state is confirmed** before the next step, as an arrival is (#175), in this mode on the block map as well:
every provider that must see the file shows it at its final size (`consistency.wait_visible`), the hash read back of
everything written is the plan's, and, as each of those providers' `/distribution` shows it on consecutive polls, the
data provider holds exactly the written ranges at `virtualSize` the final size, and no provider holds a byte beyond
them. A byte beyond them is refused at once, never waited out. Before each part the data provider's block map must be
exactly the ranges written so far, so a part out of order, or a file another writer changed, fails closed with nothing
written.

**The reset completes each file in place and never removes it**: the ranges the data provider's block map does not
hold are written from the staged source, checked first, at their offsets; the whole file is then read back by its
hash, and written whole again at its offsets when that differs (a written range damaged). It is verified by the hash,
the data provider's block map covering it and every provider that must see it showing it at its size. A file the
ingest did not make is left as it is when its bytes are its staged source's (the dataset's own, before a first run)
and refused otherwise; a name removed by a crashed start is written back from the staged source once every provider
answers the old id absent. So a crash at any point ends with the complete file after the next start.
"""

from dataclasses import dataclass
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError
from expcore_onedata.ingestbase import FAILURES, Baseline, plain
from expcore_onedata.ingestplan import PlannedFile, Step
from expcore_onedata.placement import Range, merged, provider_blocks, uncovered
from expcore_onedata.provision import Entry, NotPlaced, ProvisionError
from expcore_onedata.readretry import ONE_ATTEMPT
from expcore_onedata.streamed import Digest


@dataclass(frozen=True)
class Layout:
    """A file's distribution as one provider shows it: the data provider's `virtualSize` (None when unreadable) and
    each provider's merged blocks, by name."""

    virtual_size: int | None
    blocks: dict[str, tuple[Range, ...]]


def written_ranges(end: int) -> tuple[Range, ...]:
    """The ranges a preallocated file holds once everything before `end` is written: `[0, end)`, or none."""
    return ((0, end),) if end > 0 else ()


class SparseBaseline(Baseline):
    """The planned files of a preallocated plan: their start state, their parts and their completion, each verified
    and every id recorded."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.written: dict[str, int] = {}  # label -> the end of what the ingest has written and confirmed

    # -- the block maps ------------------------------------------------------------------------------------------------
    def layout(self, file_id: str, reader: str) -> Layout:
        """The file's distribution through `reader`, once; a PlatformError when it cannot be read or lists a provider
        of the space as unknown."""
        answer = self._p.client.get(f"/data/{plain(file_id)}/distribution", provider=reader, retry=ONE_ATTEMPT)
        try:
            blocks = {p: tuple(merged(provider_blocks(answer, self._p.provider_id(p)))) for p in self.providers}
        except ValueError as error:
            raise PlatformError(f"the block map is unreadable ({error})") from None
        entry = answer["distributionPerProvider"][self._p.provider_id(self.holder)]
        virtual = entry.get("virtualSize") if isinstance(entry, dict) else None
        exact = isinstance(virtual, int) and not isinstance(virtual, bool)
        return Layout(virtual if exact else None, blocks)

    def _beyond(self, label: str, layout: Layout, end: int, reader: str) -> None:
        """Refuse at once a provider that holds a byte outside `[0, end)`: no wait makes that the plan's state."""
        for provider, blocks in layout.blocks.items():
            if uncovered(written_ranges(end), blocks):
                raise NotPlaced(
                    f"{self._p.where}{label}: {self._p.client.provider_label(provider)} holds bytes beyond the written "
                    f"ranges, as {self._p.client.provider_label(reader)} shows it"
                )

    def _exact(self, planned: PlannedFile, layouts: dict[str, Layout], end: int) -> bool:
        for reader, layout in layouts.items():
            self._beyond(planned.label, layout, end, reader)
            if layout.virtual_size != planned.size or layout.blocks[self.holder] != written_ranges(end):
                return False
        return True

    def confirm_state(self, file_id: str, planned: PlannedFile, end: int) -> dict[str, Any]:
        """Confirm the file holds exactly `[0, end)` at its final size: every provider that must see it shows that
        size, and, through each of them on consecutive polls, the data provider holds exactly those ranges and no
        provider a byte beyond them. Return the seconds each took, by position."""
        clock = self._p.clock
        visible = {}
        for provider in self.visible_on:
            if provider != self.holder:
                visible[self._p.client.provider_label(provider)] = consistency.wait_visible(
                    self._p.client,
                    provider,
                    plain(file_id),
                    size=planned.size,
                    clock=clock,
                    sleep=self._p.sleep,
                    retry=self._p.retry,
                )
        readers = [self.holder, *(p for p in self.visible_on if p != self.holder)]
        began, settings = clock(), self._p.settings
        shown = consistency.converge(
            lambda: {reader: self.layout(file_id, reader) for reader in readers},
            lambda layouts: self._exact(planned, layouts, end),
            timeout_s=settings.settle_timeout_s,
            poll_s=settings.poll_s,
            consecutive=settings.consecutive_polls,
            clock=clock,
            sleep=self._p.sleep,
        )
        if not shown.converged:
            raise NotPlaced(
                f"{self._p.where}{planned.label}: the block maps did not show exactly the written ranges at the file's "
                "final size within the bound"
            )
        return {
            "size": planned.size,
            "blocks": [list(r) for r in written_ranges(end)],
            "visible_s": visible,
            "map_s": round(clock() - began, 3),
        }

    def _check_prefix(self, file_id: str, planned: PlannedFile, end: int, sha256: str) -> None:
        """The bytes before `end`, read back through the data provider, have the plan's hash."""
        if end and self._p.read_file(file_id, size=planned.size, stop=end) != Digest(end, sha256):
            raise ProvisionError(f"{self._p.where}{planned.label}: the bytes written do not have the plan's hash")

    # -- the start state -----------------------------------------------------------------------------------------------
    def _preallocate(self, planned: PlannedFile, old: str) -> str:
        """Make the file again at its final size, its id recorded as soon as it exists, a failed extension's too."""
        made_before = self._made_at(planned.path)
        try:
            file_id = self._p.preallocate(planned.path, planned.size)
        except FAILURES:
            for new in self._made_at(planned.path):
                if new not in made_before:
                    self.record_id(planned.label, "created", "start", old, new, action="preallocated")
            raise
        self.record_id(planned.label, "created", "start", old, file_id, action="preallocated")
        return file_id

    def start_state(self) -> dict[str, dict[str, Any]]:
        """Take each planned file from complete to its prefix at its final size, confirmed; return each file's old id,
        new id and confirmed state by label."""
        states = {}
        for planned in self._plan.files:
            found = self._p.resolve(planned.path)
            self.remove(planned, found, "start")
            file_id = self._preallocate(planned, found.file_id)
            if planned.prefix:
                self._p.write_range(file_id, planned.source, 0, planned.prefix)
            self._check_prefix(file_id, planned, planned.prefix, planned.prefix_sha256)
            confirmed = self.confirm_state(file_id, planned, planned.prefix)
            self.written[planned.label] = planned.prefix
            states[planned.label] = {"baseline_id": found.file_id, "file_id": file_id, **confirmed}
        return states

    # -- the parts -----------------------------------------------------------------------------------------------------
    def write_part(self, planned: PlannedFile, step: Step, file_id: str) -> Digest:
        """Write one part at its offsets, once the file holds exactly what came before it; return what was sent. A part
        that does not start where the file's written ranges end is refused with nothing written."""
        if self.written.get(planned.label) != step.start:
            raise ProvisionError(
                f"{self._p.where}{planned.label}: part {step.part} is out of order; nothing was written"
            )
        before = self.layout(file_id, self.holder)
        self._beyond(planned.label, before, step.start, self.holder)
        if before.virtual_size != planned.size or before.blocks[self.holder] != written_ranges(step.start):
            raise ProvisionError(
                f"{self._p.where}{planned.label}: before part {step.part} the data provider's block map is not the "
                "plan's ranges at the file's final size; nothing was written"
            )
        return self._p.write_range(file_id, planned.source, step.start, step.stop)

    def check_part(self, planned: PlannedFile, step: Step, file_id: str) -> None:
        """The bytes before the part's end read back with the plan's hash."""
        self._check_prefix(file_id, planned, step.stop, step.sha256)

    def confirm_part(self, planned: PlannedFile, step: Step, file_id: str) -> dict[str, Any]:
        """Confirm the file after a part, and count it as written."""
        confirmed = self.confirm_state(file_id, planned, step.stop)
        self.written[planned.label] = step.stop
        return confirmed

    # -- the reset -----------------------------------------------------------------------------------------------------
    def restore_file(self, planned: PlannedFile, phase: str) -> dict[str, Any]:
        """Bring one file back to its complete baseline without removing it, verified; return what was done and its id.
        A name a crashed start removed is written back once every provider answers the old id absent."""
        before = self.entry(planned.path)
        if before is None:
            if planned.label in self.last_id:
                self.await_absent(planned.label, self.last_id[planned.label])
            return self._write_back(planned, phase, None)
        if before.kind != "REG" or before.size > planned.size:
            raise ProvisionError(
                f"{self._p.where}{planned.label}: the file at its path is not one its staged source can complete; "
                "nothing was written"
            )
        if not self._made_by_ingest(before):
            return self._keep(planned, phase, before)
        self.check_source(planned)
        action = self._complete(planned, before)
        self.record_id(planned.label, "restored", phase, before.file_id, before.file_id, action=action)
        self.confirm(before.file_id, planned.size)
        return {"action": action, "file_id": before.file_id}

    def _keep(self, planned: PlannedFile, phase: str, before: Entry) -> dict[str, Any]:
        """A file the ingest did not make is the dataset's own: kept when its bytes are its staged source's, refused
        otherwise, with nothing written either way."""
        whole = Digest(planned.size, planned.sha256)
        if before.size != planned.size or self._p.read_file(before.file_id, size=planned.size) != whole:
            raise ProvisionError(
                f"{self._p.where}{planned.label}: a file at its path is neither the ingest's nor its staged source; "
                "nothing was written"
            )
        self.record_id(planned.label, "restored", phase, before.file_id, before.file_id, action="kept")
        self.confirm(before.file_id, planned.size)
        return {"action": "kept", "file_id": before.file_id}

    def _complete(self, planned: PlannedFile, before: Entry) -> str:
        """Write what the data provider does not hold, then the whole file again if its hash still differs; return
        `kept`, `completed` or `rewritten`."""
        whole = Digest(planned.size, planned.sha256)
        held = self._p.blocks(before.file_id, self.holder, retry=self._p.retry)
        gaps = uncovered(held, [(0, planned.size)]) if before.size == planned.size else [(0, planned.size)]
        for offset, length in gaps:
            self._p.write_range(before.file_id, planned.source, offset, offset + length)
        if self._p.read_file(before.file_id, size=planned.size) == whole:
            return "completed" if gaps else "kept"
        self._p.write_range(before.file_id, planned.source, 0, planned.size)
        if self._p.read_file(before.file_id, size=planned.size) != whole:
            raise ProvisionError(f"{self._p.where}{planned.label}: the completed file is not its staged source")
        return "rewritten"


__all__ = ["Layout", "SparseBaseline", "written_ranges"]
