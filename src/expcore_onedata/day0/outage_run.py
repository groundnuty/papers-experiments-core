# Provenance: new
"""One space's run of the day-0 outage check: the scenario, the measurements, the outage and the restore (O28).

`outage.py` decides whether a space can be run and holds the federation for it; this module is what runs. See
`outage.py` for the scenario, `outage_cells` (REST), `outage_mount` (the mount, through a real sandbox),
`outage_tools` (the agent's tools, through MCP), `outage_agent` (starting those two), `outage_fault` (the injector),
`outage_after` (the steps after the restore) and `outage_budget` (the lifetime of the agent's tokens).

The run is a coroutine because the tool server's client is one; every blocking step (the platform's REST calls and
bounded waits, the injector, the sandbox's cells) runs in a worker thread (`asyncio.to_thread`, which carries the
federation's admission with it), since the platform layer refuses to wait on the thread of a running event loop.
"""

import asyncio
import hashlib
import secrets
from collections.abc import Mapping
from typing import Any

from expcore_onedata import consistency
from expcore_onedata.client import PlatformError
from expcore_onedata.config import StudySpace
from expcore_onedata.day0 import outage_mount
from expcore_onedata.day0.outage_after import AfterRestore
from expcore_onedata.day0.outage_agent import AgentPlane, mount_levels
from expcore_onedata.day0.outage_budget import DOWN_PROBE_S
from expcore_onedata.day0.outage_cells import KINDS, ROLES, Cells, Roles, ScratchFile
from expcore_onedata.day0.outage_fault import Outage
from expcore_onedata.day0.outage_tools import Scrubber, Tools, baseline_gaps
from expcore_onedata.day0.registry import Context, Result, failed, passed, unknown
from expcore_onedata.faults import FaultInjector
from expcore_onedata.placement import held_bytes
from expcore_onedata.provision import ProvisionError
from expcore_onedata.sandbox.api import SandboxError

FILE_BYTES = 16384
BASE_PAIRS = ("baseline", "rest", "knowledge")
MOUNT_PAIRS = ("baseline_mount", "mount")


class Unprepared(RuntimeError):
    """The scenario could not be set up or proved, so nothing was taken down."""


def payload(label: str) -> bytes:
    """Deterministic bytes, different for every file, so a read of another file never has the hash of this one."""
    out, block = bytearray(), 0
    while len(out) < FILE_BYTES:
        out += hashlib.sha256(f"{label}:{block}".encode()).digest()
        block += 1
    return bytes(out[:FILE_BYTES])


def scrubber(ctx: Context, entry: StudySpace) -> Scrubber:
    """Every name and id of the configuration a tool's text may hold, with what the report calls it, replaced before
    the text's digest is taken (O12)."""
    config, pairs = ctx.config, {}
    for name, provider in config.providers.items():
        label = config.provider_label(name)
        pairs.update({provider.url: label, provider.url.split("://", 1)[-1]: label, name: label})
        pairs[ctx.provider_ids.get(name, "")] = label
    pairs.update({config.zone.url: "zone", config.zone.url.split("://", 1)[-1]: "zone"})
    for space in config.spaces:
        pairs.update({space.space.id: space.position, space.space.name: space.position, space.label: space.position})
    return Scrubber(pairs)


class Run(AfterRestore):
    """One space's outage: the scenario's files, the facts gathered, and what went wrong or could not be measured. The
    steps after the restore are `outage_after.AfterRestore`'s."""

    def __init__(self, ctx: Context, entry: StudySpace, roles: Roles, plane: AgentPlane) -> None:
        self.ctx, self.entry, self.roles, self.plane = ctx, entry, roles, plane
        self.provisioner = ctx.provisioner(entry.label)
        self.bound_s = ctx.outage_bound_s
        self.directory = f"day0-outage-{secrets.token_hex(6)}"
        self.directory_id = ""
        self.files: dict[tuple[str, str], ScratchFile] = {}
        self.pairs = [*BASE_PAIRS, *(MOUNT_PAIRS if mount_levels(ctx) else ())]
        self.problems: list[str] = []  # what failed
        self.gaps: list[str] = []  # what could not be measured
        self.tokens_ended: str | None = None  # the first step the agent's tokens had ended before
        self.facts: dict[str, Any] = {"roles": dict(roles.labels), "bound_s": self.bound_s, "bytes": FILE_BYTES}
        self.cells = Cells(
            ctx.client, entry, self.provisioner, roles, bound_s=self.bound_s, clock=ctx.clock, sleep=ctx.sleep
        )
        self.scrub = scrubber(ctx, entry)
        self.tools: Tools | None = None

    # -- the run ------------------------------------------------------------------------------------------------------
    async def main(self) -> None:
        """Start what the agent meets, run the scenario, and stop it again, the restore having come first."""
        async with self.plane.opened():
            self.facts["agent"] = self.plane.facts
            self._plan()
            await self.execute()
        self.problems += self.plane.problems

    def _plan(self) -> None:
        """What of the mount and the knowledge plane can be measured, and why the rest cannot (a gap each)."""
        plane, sandbox_reason = self.plane, self.plane.facts["sandbox"].get("reason", "")
        if not mount_levels(self.ctx):
            self.facts["mount"] = {"declared": False}
        elif plane.mount_path is None:
            self.facts["mount"] = {"declared": True, "measured": False, "reason": sandbox_reason}
            self.gaps.append(f"the platform declares a mount data path and it was not measured ({sandbox_reason})")
        else:
            self.facts["mount"] = {"declared": True, "measured": True}
        if plane.tools is None:
            reason = plane.facts["tool_server"].get("reason", "")
            self.facts["knowledge"] = {"measured": False, "reason": reason}
            self.gaps.append(f"the knowledge plane was not measured ({reason})")
            return
        self.tools = Tools(
            plane.tools, self.roles, self.entry.space.id, self.scrub, bound_s=self.bound_s,
            poll_s=self.provisioner.settings.poll_s, clock=self.ctx.clock, sleep=self.ctx.sleep,
        )  # fmt: skip
        missing = self.tools.missing()
        self.facts["knowledge"] = {"measured": not missing, "missing_tools": missing}
        if missing:
            self.gaps.append(f"the tool server lacks {len(missing)} of the knowledge tools, so they were not measured")

    async def execute(self) -> None:
        """Prepare and baseline, take the target down, measure, restore it, then check the restore and read again."""
        try:
            await asyncio.to_thread(self.prepare)
            await self.agent_baseline()
        except (ProvisionError, PlatformError, consistency.WaitTimeout, KeyError, Unprepared) as error:
            reason = str(error) if isinstance(error, Unprepared) else type(error).__name__
            self.gaps.append(f"nothing was taken down: the scenario could not be set up or proved ({reason})")
            return
        ctx = self.ctx
        assert ctx.fault_settings is not None
        assert ctx.fault_driver is not None
        injector = FaultInjector(
            ctx.config, ctx.fault_settings, self.roles.labels["target"],
            sanctioned=ctx.allow_faults and ctx.allow_mutation,  # the caller's half of the sanction (O10)
            driver=ctx.fault_driver, clock=ctx.clock, sleep=ctx.sleep,
        )  # fmt: skip
        outage = Outage(injector, self.directory)
        went_down = False
        try:
            async with outage.down() as down:  # the injector's `running`: never its start in a bare thread (#27)
                self.facts["outage"] = down
                if not down["started"]:
                    self.gaps.append(f"the provider could not be taken down and seen down ({down['reason']})")
                else:
                    went_down = await self.measured()
        except Exception as error:  # the injector's own failure past its handling: recorded, its restore read below
            self.problems.append(f"the outage raised {type(error).__name__}")
        self.facts["restore"] = outage.restored()
        await self.after_restore(went_down)

    async def measured(self) -> bool:
        """See the target down over REST and measure; True when it was seen down. A measurement that breaks is a
        failure, never an escape past the restore."""
        went_down = False
        try:
            went_down = await asyncio.to_thread(self.seen_down)
            if went_down:
                await self.during()
        except Exception as error:
            self.problems.append(f"a measurement raised {type(error).__name__}")
        return went_down

    # -- before ---------------------------------------------------------------------------------------------------
    def prepare(self) -> None:
        """Make the files, place the replicas, prove who holds what and read the REST baseline."""
        roles, prov = self.roles, self.provisioner
        prov.verify_space()
        for pair in self.pairs:
            for kind in KINDS:
                path = f"{self.directory}/{pair}-{kind}.bin"
                data = payload(f"{pair}:{kind}")
                self.files[(pair, kind)] = ScratchFile(pair, kind, path, prov.put_file(path, data), data)
        self.directory_id = prov.resolve(self.directory).file_id
        prov.await_visible(roles.local, [f.file_id for f in self.files.values()])
        for pair in self.pairs:
            prov.replicate(self.files[(pair, "replicated")].path, roles.mirror)
        for file in self.files.values():
            self.scrub.add(file.file_id, f"file:{file.key}")
        self.scrub.add(self.directory, "scratch")
        self.prove_holdings()
        self.baseline()

    @property
    def expected(self) -> dict[str, int]:
        """The entries of the scratch directory: every file's name and size."""
        return {f.name: len(f.data) for f in self.files.values()}

    def prove_holdings(self) -> None:
        """Read, through the local provider, who holds how much of each file; refuse a scenario that is not as built."""
        roles, rows, wrong = self.roles, {}, []
        for file in self.files.values():
            held = {
                role: held_bytes(self.provisioner.blocks(file.file_id, roles.name(role), reader=roles.local))
                for role in ROLES
            }
            size = len(file.data)
            if held != {"target": size, "mirror": size if file.kind == "replicated" else 0, "local": 0}:
                wrong.append(file.key)
            rows[file.key] = {roles.labels[role]: held[role] for role in ROLES}
        self.facts["held_before"] = rows
        if wrong:
            raise Unprepared(f"{len(wrong)} file(s) are not held as the scenario needs before the outage")

    def baseline(self) -> None:
        """Read the REST baseline pair with everything up, through each provider that may hold it, and list."""
        cells, files, roles = self.cells, self.files, self.roles
        owner_only, replicated = files[("baseline", "owner_only")], files[("baseline", "replicated")]
        rest = {
            "target_owner_only": cells.read(owner_only, roles.target),
            "mirror_replicated": cells.read(replicated, roles.mirror),
            "local_owner_only": cells.read(owner_only),
            "local_replicated": cells.read(replicated),
            "listing": cells.listing(self.directory_id, self.expected),
        }
        self.facts["baseline"] = {"rest": rest}
        short = [name for name, row in rest.items() if row["outcome"] != "served"]
        if short:
            raise Unprepared(f"the baseline did not read back everything it was given ({len(short)} REST cell(s))")

    async def agent_baseline(self) -> None:
        """The mount and the tools with everything up: each must serve, or the outage could say nothing about them."""
        if self.plane.mount_path is not None:
            mount = await asyncio.to_thread(self.mount_cells, "baseline_mount")
            self.facts["baseline"]["mount"] = mount
            short = [name for name, row in mount.items() if row is None or row["outcome"] != "served"]
            if short:
                raise Unprepared(f"the baseline did not read back through the mount ({len(short)} cell(s))")
        if self.tools is not None:
            owner_only = self.files[("baseline", "owner_only")]
            knowledge = {
                "get_file_distribution": await self.tools.distribution(owner_only.key, owner_only.file_id),
                "list_space_providers": await self.tools.providers(),
                "schedule_file_replication": await self.tools.replicate(owner_only.key, owner_only.file_id),
            }
            self.facts["baseline"]["knowledge"] = knowledge
            self.note_unmeasured("baseline", knowledge)
            wrong = baseline_gaps(knowledge)
            if wrong:
                raise Unprepared(f"the knowledge tools did not serve with everything up ({', '.join(wrong)})")

    # -- the mount ------------------------------------------------------------------------------------------------
    def mount_cells(self, pair: str, *, listing: bool = True) -> dict[str, Any]:
        """Read a pair's two files, and list the scratch directory, through the mount, in one cell of the agent's.
        A cell that says nothing of a read leaves it None, and a gap."""
        root = self.plane.mount_path
        ops = [{"key": kind, "op": "read", "path": f"{root}/{self.files[(pair, kind)].path}"} for kind in KINDS]
        if listing:
            ops.append({"key": "listing", "op": "list", "path": f"{root}/{self.directory}"})
        try:
            done = self.plane.sandbox.exec(
                outage_mount.cell(ops, self.bound_s), outage_mount.cell_timeout_s(len(ops), self.bound_s), record=False
            )
            rows = outage_mount.results(done)
        except SandboxError as error:
            rows = f"the sandbox did not run the mount cell ({type(error).__name__})"
        if isinstance(rows, str):
            self.gaps.append(rows)
            rows = {}
        out: dict[str, Any] = {}
        for kind in KINDS:
            file = self.files[(pair, kind)]
            out[kind] = outage_mount.read_facts(rows.get(kind), len(file.data), file.sha256, self.bound_s)
        if listing:
            out["listing"] = outage_mount.listing_facts(rows.get("listing"), list(self.expected), self.bound_s)
        unread = [name for name, row in out.items() if row is None]
        if unread and rows:
            self.gaps.append(f"the mount cell said nothing of {len(unread)} read(s) of {pair}")
        return out

    # -- during ---------------------------------------------------------------------------------------------------
    def seen_down(self) -> bool:
        """Whether the target, its service seen down by the injector, answers no REST call either; a target that
        answers is no observed outage, and a problem."""
        probe = self.cells.target_answers(min(self.bound_s, DOWN_PROBE_S))
        self.facts["outage"].update(rest_answers_while_down=probe["answers"], rest=probe)
        if probe["answers"]:
            self.problems.append(
                "the provider answered REST while its service was seen down, so no outage was observed"
            )
        return not probe["answers"]

    async def during(self) -> None:
        """Measure, through the local provider, the REST path, the mount and the agent's tools: the knowledge pair's
        distribution, the providers and the replications, and then `get_file_distribution` of every file the REST
        path and the mount read, which shows where its bytes are held now (`from_where`)."""
        cells, files = self.cells, self.files

        def rest() -> dict[str, Any]:
            return {
                "replicated": cells.read(files[("rest", "replicated")]),
                "owner_only": cells.read(files[("rest", "owner_only")]),
                "listing": cells.listing(self.directory_id, self.expected),
            }

        during: dict[str, Any] = {"rest": await asyncio.to_thread(rest)}
        self.facts["during"] = during
        if self.plane.mount_path is not None and self.tokens_hold("the reads through the mount during the outage"):
            during["mount"] = await asyncio.to_thread(self.mount_cells, "mount")
        if self.tools is not None and self.tokens_hold("the tools' calls during the outage"):
            tools = self.tools
            knowledge: dict[str, Any] = {"get_file_distribution": {}, "schedule_file_replication": {}}
            for kind in KINDS:
                file = files[("knowledge", kind)]
                knowledge["get_file_distribution"][kind] = await tools.distribution(file.key, file.file_id)
            knowledge["list_space_providers"] = await tools.providers()
            for kind in KINDS:
                file = files[("knowledge", kind)]
                knowledge["schedule_file_replication"][kind] = await tools.replicate(file.key, file.file_id)
            knowledge["from_where"] = {}  # where each file read above is held now, by the agent's own tool
            for pair in ("rest", "mount") if "mount" in during else ("rest",):
                for kind in KINDS:
                    file = files[(pair, kind)]
                    knowledge["from_where"][file.key] = await tools.distribution(file.key, file.file_id)
            during["knowledge"] = knowledge
            self.note_unmeasured("the outage", knowledge)

    def note_unmeasured(self, when: str, knowledge: Mapping[str, Any]) -> None:
        """A gap for each tool cell that was not measured (no such tool, arguments it could not fill, a schedule whose
        answer named no transfer), apart from the missing tools `_plan` already named."""
        reasons: set[str] = set()

        def walk(cell: Any) -> None:
            if isinstance(cell, Mapping):
                if (
                    cell.get("outcome", "") is None
                    and cell.get("reason") != "the condition's tool server has no such tool"
                ):
                    reasons.add(f"{cell.get('tool')}: {cell.get('reason')}")
                if cell.get("transfer_id_read") is False:
                    reasons.add(
                        "schedule_file_replication: its answer named no transfer, so get_transfer was not called"
                    )
                for value in cell.values():
                    walk(value)

        walk(knowledge)
        self.gaps += [f"during {when}, {reason}" for reason in sorted(reasons)]

    # -- the verdict ----------------------------------------------------------------------------------------------
    def verdict(self) -> Result:
        """The check's result: failed for a problem, unknown for a gap, else passed with what was observed."""
        if self.facts.get("cleanup_verified") is not True:
            self.problems.append("the scratch it made was not shown gone (O11)")
        if self.problems:
            return failed("; ".join(self.problems), **self.facts)
        if self.gaps:
            return unknown("; ".join(self.gaps), **self.facts)
        return passed(self.summary(), **self.facts)

    def summary(self) -> str:
        """One line of what the outage did, in the vocabulary of the cells."""
        during = self.facts["during"]
        rest, target = during["rest"], self.roles.labels["target"]
        line = (
            f"{target} was taken down, seen down and restored; through the local provider a file held only there was "
            f"{rest['owner_only']['outcome']} over REST"
        )
        if "mount" in during:
            line += f" and {during['mount']['owner_only']['outcome']} through the mount"
        line += f", a replicated file {rest['replicated']['outcome']}, the listing {rest['listing']['outcome']}"
        if "knowledge" in during:
            distribution = during["knowledge"]["get_file_distribution"]["owner_only"]["outcome"]
            line += f"; get_file_distribution {distribution}"
        return line + "; the scratch was removed"


__all__ = ["BASE_PAIRS", "FILE_BYTES", "KINDS", "MOUNT_PAIRS", "Run", "Unprepared", "payload"]
