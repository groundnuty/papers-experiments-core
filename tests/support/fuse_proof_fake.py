"""The dry run's world for the O97 proof: the same calls as `fuse_proof_world.LiveWorld`, over fake nodes.

Every sandbox is the real `ApptainerSandbox` on a `ReaderNode` of its own, with its own fusectl tree. The federation is
one flag, `owner_down`, which the fake injector sets and clears. A cell is answered by its first line
(`fuse_proof.OWNER_MARK`, `LOCAL_MARK`): the owner-only read blocks in the kernel while the owner is down, as a read of
a stopped provider's file does, with the node's `/proc` showing it in `read(2)` on the run's FUSE device; the abort's
fusectl write frees it, as the kernel does. The injector holds and releases the target through the real registry
(`fault_owner.hold` and `release`, keyed by `fault_owner.attempt_key()`), so the sandbox's `held_reader` and the
attribution are the core's own. The token is invented; nothing leaves the process.

`CURRENT` is the world the minimal study's plugin uses in the dry run: the driver's `run_proof` sets it before the
campaign runs in the same process.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import threading
from collections.abc import AsyncIterator, Iterator, Mapping
from pathlib import Path
from typing import Any

import fuse_proof as FP
from apptainer_support import make_settings, make_spec, options, pinned_bundle
from fake_mount_node import fusectl_tree
from fake_survivor_proc import DATA_DEVICE, DATA_MOUNT, MOUNTS, syscall_text
from reader_node import ReaderNode

from expcore.records.injections import FAULT_NAMESPACE
from expcore_onedata import fault_owner
from expcore_onedata.sandbox import fusectl as FC
from expcore_onedata.sandbox.apptainer import hostmount as HM
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox
from expcore_onedata.sandbox.hostops import CmdResult

CURRENT: Any = None
TARGET = "providers[1]"
SPACE = "orchard"
HOLDERS = {SPACE: TARGET, "meadow": "providers[2]"}
LOCAL_BYTES = b"the local file of the dry run\n"


class FakeFederation:
    """What the fake injector changes and the fake nodes read: whether the owner is down."""

    def __init__(self) -> None:
        self.owner_down = False
        self.lock = threading.Lock()


class FakeInjector:
    """The instrument `FaultInjector` is, as far as the proof uses it: `running` (the executor's), `session` (the
    driver's), `finish` and `snapshot`; the hold through the real registry, under the attempt's token."""

    namespace = FAULT_NAMESPACE

    def __init__(self, federation: FakeFederation, target: str) -> None:
        self.federation, self.target = federation, target
        self._run: dict[str, Any] | None = None
        self._attempt: Any = None
        self._injected = False

    def start(self, identity: Mapping[str, Any]) -> None:
        self._run = {"run_key": identity["run_key"], "attempt": identity["attempt"]}
        self._attempt = fault_owner.attempt_key()
        with self.federation.lock:
            self.federation.owner_down = True
        self._injected = True
        fault_owner.hold(self._run["run_key"], self.target, self, attempt=self._attempt)

    def clear(self) -> bool:
        if self._run is not None:
            fault_owner.release(self._run["run_key"], self.target, attempt=self._attempt)
        with self.federation.lock:
            self.federation.owner_down = False
        return True

    @contextlib.asynccontextmanager
    async def running(self, identity: Mapping[str, Any]) -> AsyncIterator[FakeInjector]:
        import asyncio

        await asyncio.to_thread(self.start, identity)
        try:
            yield self
        finally:
            await asyncio.to_thread(self.clear)

    @contextlib.contextmanager
    def session(self, identity: Mapping[str, Any]) -> Iterator[FakeInjector]:
        self.start(identity)
        try:
            yield self
        finally:
            self.clear()

    def snapshot(self) -> dict[str, Any]:
        injection = {"target": self.target, "injected": self._injected, "verified": True}
        return {
            "ok": self._injected,
            **(self._run or {"run_key": None, "attempt": None}),
            "kind": "provider_outage",
            "target": self.target,
            "injections": [injection] if self._injected else [],
            "steps": [],
            "restore_verified": True,
        }

    def finish(self) -> dict[str, Any]:
        self.clear()
        return self.snapshot()


class FakeFiles:
    """The platform's REST file calls, as the driver makes them: every call answers, nothing is written anywhere."""

    def __init__(self) -> None:
        self.created: list[str] = []

    def root(self, provider: str, space_id: str | None = None) -> str:
        return "root"

    def make(self, provider: str, parent: str, name: str, kind: str) -> str:
        self.created.append(name)
        return f"{parent}/{name}"

    def seen(self, provider: str, file_id: str) -> bool:
        return True

    def write(self, provider: str, file_id: str, offset: int, data: bytes) -> None:
        return None

    def remove_all(self) -> list[str]:
        removed, self.created = list(self.created), []
        return removed[:0]  # every one removed: none left


@dataclasses.dataclass
class Boxed:
    """One fake sandbox: its node, the sandbox, its fusectl root."""

    node: ReaderNode
    box: ApptainerSandbox | None
    root: Path


class FakeWorld:
    """The world the dry run passes for `LiveWorld` (the module's docstring)."""

    def __init__(self, base: Path) -> None:
        self.base = base
        self.federation = FakeFederation()
        self.level = "mounted"
        self.target = TARGET
        self.mount_path = "/data"
        self.space_name = SPACE
        self.space_id = "space-id"
        self.local_name = "local"
        self.owner_path: str | None = None
        self._boxes: dict[str, Boxed] = {}
        self._count = 0
        self._lock = threading.Lock()

    # --- the fusectl write, patched by the dry run: it frees the reader of the node whose root it was made under
    def install(self, patch: Any) -> None:
        real = FC.write_abort

        def write(root: Path, connection: str) -> None:
            real(root, connection)
            for held in list(self._boxes.values()):
                if held.root == Path(root):
                    held.node.free("fusectl")
            waiting = Path(root) / connection / "waiting"
            if waiting.exists():  # the kernel ends every request on the connection: none is left waiting
                waiting.write_text("0\n", encoding="ascii")

        patch.setattr(FC, "write_abort", write)

    def _node(self, name: str) -> Boxed:
        with self._lock:
            self._count += 1
            where = self.base / f"box-{self._count}"
        where.mkdir(parents=True)
        root = where / "fusectl"
        root.mkdir()
        node = ReaderNode()
        held = Boxed(node, None, root)
        node.cell_handler = lambda cell: self._answer(held, cell)
        self._boxes[name] = held
        return held

    def _start(self, held: Boxed, spec: Any) -> ApptainerSandbox:
        settings = make_settings(held.root.parent, oneclient_bundle=pinned_bundle(held.root.parent / "files"))
        factory = HM.host_mount_factory(mountinfo=held.node.mountinfo, fusectl_root=held.root)
        fusectl_tree(held.root, fusectl_device(held.node), waiting=1)
        held.box = ApptainerSandbox.start(held.node, settings, spec, **options(mount_helper=factory))
        return held.box

    def _answer(self, held: Boxed, cell: Any) -> Any:
        program = cell.program
        if FP.OWNER_MARK in program or FP.LOCAL_MARK in program:
            held.node.cell()  # the guard's watcher proves the agent's cell, as a real watcher does
        if FP.OWNER_MARK in program:
            with self.federation.lock:
                down = self.federation.owner_down
            if down:
                return self._stuck(held, cell)
            return CmdResult(0, "read 4096\n")
        if FP.LOCAL_MARK in program:
            return CmdResult(0, f"listed 2\nsha256 {hashlib.sha256(LOCAL_BYTES).hexdigest()}\n")
        return held.node.default_cell(cell)

    def _stuck(self, held: Boxed, cell: Any) -> Any:
        """The owner-only read waits in the kernel on the run's live FUSE connection, until the abort frees it."""
        node = held.node
        node.reader = cell.procs[1]
        device = (held.box._mount.connection() or {}).get("device") if held.box is not None else None
        table = MOUNTS.replace(DATA_DEVICE, device or DATA_DEVICE)
        process = node.survivor_proc.process(node.reader.pid, start=node.reader.start, mounts=table)
        process.task(node.reader.pid, syscall=syscall_text(0, 3, 0x7F10, 0x1000), wchan="request_wait_answer")
        process.fd(3, self.owner_path or f"{self.mount_path}/{SPACE}/fuse-proof/owner-only.bin", mnt_id=DATA_MOUNT)
        fusectl_tree(held.root, device or fusectl_device(node), waiting=1)
        return node.stuck(cell)

    # --- the campaign's path
    @contextlib.asynccontextmanager
    async def launch(self, run_key: str, ceiling_s: float) -> AsyncIterator[ApptainerSandbox]:
        """What `SandboxPlatform.launch` gives the run: the spec's held-down set read through `held_reader`, made here,
        inside the attempt, as the platform's `_spec` makes it."""
        import asyncio

        held = self._node(run_key)
        spec = dataclasses.replace(
            make_spec(self.level), run_key=run_key, held_down=fault_owner.held_reader(run_key), space_holders=HOLDERS
        )
        box = await asyncio.to_thread(self._start, held, spec)
        try:
            yield box
        finally:
            held.node.free("teardown")
            await asyncio.to_thread(box.stop, force=True)

    def injector(self, target: str) -> FakeInjector:
        return FakeInjector(self.federation, target)

    def fingerprint(self) -> dict[str, Any]:
        return {}

    # --- outside the campaign
    def direct(self, run_key: str) -> ApptainerSandbox:
        held = self._node(run_key)
        spec = dataclasses.replace(make_spec(self.level), run_key=run_key, space_holders=HOLDERS)
        return self._start(held, spec)

    def free(self, run_key: str) -> None:
        """Let a direct sandbox's reader go before its stop (the kernel's doing at a teardown)."""
        held = self._boxes.get(run_key)
        if held is not None:
            held.node.free("teardown")

    def files(self) -> FakeFiles:
        return FakeFiles()

    def provider_of(self, label: str) -> str:
        return label

    def mounts_left(self) -> int:
        """FUSE mounts left on the fake nodes once every sandbox stopped."""
        from expcore_onedata.sandbox.hostops import fuse_mounts

        return sum(len(fuse_mounts(held.node.mountinfo())) for held in self._boxes.values())

    def close(self) -> None:
        return None


def fusectl_device(node: ReaderNode) -> str:
    """The device the node's FUSE mount will come up on, from the fake's own mount table."""
    from fake_mount_node import FUSE_DEV

    return FUSE_DEV


__all__ = ["CURRENT", "HOLDERS", "LOCAL_BYTES", "SPACE", "TARGET", "FakeWorld"]
