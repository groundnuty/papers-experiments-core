"""The O97 live proof on a real HPC node: a cell blocked reading a stopped owner's file is freed at its bound, the run
goes on as the agent's, and a block that no injection of the run explains stays infrastructure. A script, not a test:
the node has no pytest. Run it from a private 0700 directory, once, under `python -I -B`, in a step of the federation's
agent job, with the federation up and its relay serving:

    python -I -B tests/live/fuse_abort_apptainer.py PLATFORM_TOML OUT_DIR [--gap-s S] [--no-second-cycle]

`PLATFORM_TOML` is the federation's own platform file, as `up` rendered it: a sanctioned `[faults]` of the hpc backend,
`[principals.data_path]`, `[sandbox]` with its Apptainer pins and a mount level, and a space whose data provider is not
the agent's local one (the target). The plan is `e2e/fuse-abort-helios-proof-plan.private.md`, and the pass list is
`tests/support/fuse_proof.py` (P1 to P12). In order:

1. **Seed** through the platform: a directory, two files written through the target alone (the local provider holds
   none of either: one for the registered attempt, one for the negative, never shared, since a fetch that ends after
   its restore gives the local provider a copy) and a local file. **K**, the control sandbox (direct, no injection),
   reads the local file. The local provider must hold no byte of the registered file, or the proof stops there.
2. **S**, the registered attempt, through the campaign (`python -m expcore run`) of a minimal study written into
   OUT_DIR from `tests/support/fuse_proof_study/`: the platform's launch and the run's own injector inside the attempt.
   It comes first, so a refusal at the campaign's start costs a minute, not an outage. Its control condition reads the
   local file only. K reads again.
3. The **gap**: the run's clear has waited out the session drain (320 s, past the link heal's three minutes); `--gap-s`
   adds to it.
4. **P12**: **N**, an unregistered sandbox (direct, outside any attempt), reads its own owner-only file while the
   driver's own injector holds the target down: the read blocks, the bound kills it, the survivor is aborted, and the
   verdict is infrastructure (`no_injection_in_effect`); N's next cell is refused as `cell_unkillable`. The owner is
   restored. K reads a third time.
5. K and N are stopped, the seeded files removed, the record validated and replayed, and the facts written to
   `OUT_DIR/facts.json`: outcomes, counts, seconds, labels and hashes; never a token, a host or a configured path.

The exit status is 0 only when `fuse_proof.problems` finds nothing. The facts are scanned with both site-pattern lists
on the host they are pulled to, never on the node. `run_proof` is also what the offline dry run calls, with the fake
world and the campaign in its own process.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / "tests" / "support"
for entry in (ROOT / "src", SUPPORT):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

import fuse_proof as FP  # noqa: E402

from expcore.errors import InfraError  # noqa: E402
from expcore.provenance.codestamp import write_stamp  # noqa: E402
from expcore.records.record import RunRecord, decode  # noqa: E402
from expcore.records.schema import validate  # noqa: E402
from expcore.replay.replay import replay_record  # noqa: E402
from expcore.study.config import load_study  # noqa: E402

TEMPLATE = SUPPORT / "fuse_proof_study"
OWNER_BYTES = bytes(range(256)) * 64  # 16 KiB: one file the target alone holds
LOCAL_BYTES = b"the local file of the O97 proof\n"
SEEN_S = 300.0
STAMP = "f" * 40  # the stamped commit of the minimal study: it is written here, never committed
GAP_S = 60.0  # beyond the session drain the run's clear waits out (320 s): the link heal's 3 min are inside it
REGISTERED_FILE = "owner-registered.bin"  # the registered attempt's owner-only file
NEGATIVE_FILE = "owner-negative.bin"  # the unregistered sandbox's: never the same file


class ProofStopped(Exception):
    """A stop of the proof's own, before anything it could not show: the facts say why, the teardown still runs."""


def wait_seen(files: Any, provider: str, file_id: str, timeout_s: float = SEEN_S) -> bool:
    """Until `provider` knows the entry; whether it did within `timeout_s`."""
    until = time.monotonic() + timeout_s
    while time.monotonic() < until:
        if files.seen(provider, file_id):
            return True
        time.sleep(2.0)
    return files.seen(provider, file_id)


def sha_of(stdout: str) -> str | None:
    """The SHA-256 a local-read cell printed, None when no line is exactly `sha256 <64 hex digits>`: nothing else of a
    cell's output ever reaches the facts."""
    found = re.search(r"^sha256 ([0-9a-f]{64})$", stdout, re.MULTILINE)
    return found.group(1) if found else None


def mount_facts(box: Any) -> dict[str, Any]:
    """What a direct sandbox's metadata says of its mount: its remounts, and whether it is shown untouched by any abort
    (`aborted`: False only for exactly one generation whose own `abort` is recorded as None; None when that is not
    shown)."""
    mount = (box.metadata() or {}).get("mount") or {}
    generations = mount.get("generations")
    aborted: bool | None = None
    if isinstance(generations, list) and generations and all(isinstance(g, dict) and "abort" in g for g in generations):
        aborted = any(g["abort"] for g in generations)
    return {"remounts": mount.get("remounts"), "aborted": aborted}


def write_study(
    out: Path, settings: dict[str, Any], *, target: str, bound_s: float, time_cap_s: int, platform: Path | None
) -> Path:
    """The minimal study, from its template, with the run's settings beside it and stamped (it is never committed).
    Live, the federation's platform file is copied beside it as `platform.toml`, where the Onedata layer reads its
    longest deferral (`[study] platforms`); the dry run's process has the layer's kinds already and lists none."""
    study = out / "study"
    study.mkdir(parents=True)
    for name in ("models.toml", "preregistration.toml", "prompt.txt", "plugin.py"):
        shutil.copyfile(TEMPLATE / name, study / name)
    platforms = '["expcore_onedata.sandbox.kinds"]' if platform is not None else "[]"
    max_defer = max_defer_s_for(platform)
    if platform is not None:
        shutil.copyfile(platform, study / "platform.toml")
        settings = {**settings, "platform": str(study / "platform.toml")}  # the run reads the file its study points at
    fills = {
        "@TARGET@": target,
        "@BOUND_S@": f"{bound_s:g}",
        "@TIME_CAP_S@": str(time_cap_s),
        "@PLATFORMS@": platforms,
        "@MAX_DEFER_S@": f"{max_defer:g}",
    }
    for name in ("study.toml", "campaign.toml"):
        text = (TEMPLATE / f"{name}.in").read_text(encoding="utf-8")
        for key, value in fills.items():
            text = text.replace(key, value)
        (study / name).write_text(text, encoding="utf-8")
    (study / "fuse_proof.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    write_stamp(study, STAMP, load_study(study).scope)
    return study


RECHECK_S, POLL_S, HEALTH_TIMEOUT_S = 5.0, 0.2, 900.0  # the template's [scheduler] values the deferral is counted with


def max_defer_s_for(platform: Path | None) -> float:
    """The campaign's `max_defer_s`: what the platform declares of its health check's longest deferral, read from its
    own file as the core reads it (`[study] platforms`), with the template's waits added as the core adds them
    (`SchedulerConfig.deferral_needed`), and two minutes more; the core's default 3,600 s when that is more."""
    from expcore.campaign.settings import SchedulerConfig

    if platform is None:
        return 3600.0
    from expcore_onedata.sandbox.kinds import declared_deferral_from_config

    bound = declared_deferral_from_config(platform)
    if bound is None:
        return 3600.0
    scheduler = SchedulerConfig(recheck_s=RECHECK_S, poll_interval_s=POLL_S, health_timeout_s=HEALTH_TIMEOUT_S)
    return float(max(3600, math.ceil(scheduler.deferral_needed(bound)[0]) + 120))


def registered_record(study: Path) -> tuple[dict[str, Any] | None, str | None]:
    """The registered attempt's record (the `owner_down` run) and its text, from the campaign's runs."""
    for path in sorted((study / "out" / "runs").rglob("*.json")):
        try:
            text = path.read_text(encoding="utf-8")
            data = decode(text)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and (data.get("factors") or {}).get("outage") == "owner_down":
            return data, text
    return None, None


def record_checks(text: str | None) -> tuple[bool, bool]:
    """Whether the record validates, and whether its replay gives its bytes back."""
    if text is None:
        return False, False
    try:
        valid = validate(decode(text)).startswith("expcore-record/")
    except Exception:
        valid = False
    try:
        replayed = replay_record(RunRecord.from_json(decode(text))).dumps() == text
    except Exception:
        replayed = False
    return valid, replayed


def negative(
    box: Any, owner_path: str, expected: tuple[str, str], bound_s: float, local_held: int | None
) -> dict[str, Any]:
    """N's cell under the driver's own outage (P12): what its deadline and its next cell showed, as a summary
    (`fuse_proof.negative_summary`); the raw deadline, which names the space, never leaves this function."""
    done = box.exec(FP.owner_read(owner_path), bound_s)
    deadlines = (box.metadata() or {}).get("cell_deadlines") or []
    try:
        box.exec("print('next')", 30)
        next_cell = "ran"
    except InfraError as refused:
        next_cell = refused.kind
    pending = bool(getattr(done, "restore_pending", False))
    return FP.negative_summary(
        deadlines[-1] if deadlines else None, expected=expected, restore_pending=pending, next_cell=next_cell,
        local_held_before=local_held,
    )  # fmt: skip


def teardown(world: Any, files: Any, boxes: dict[str, Any], injector: Any = None) -> list[str]:
    """Bring the negative's provider back (a second `clear`, which verifies a restore a cut left unverified and does
    nothing after a verified one), stop the direct sandboxes, count the proof's FUSE mounts left and remove the seeded
    files: each step on its own, so one that fails never skips the others. What went wrong."""
    found: list[str] = []
    with _cuts_deferred():
        if injector is not None:
            try:
                if injector.clear() is False:
                    found.append("the negative's provider restore is not verified")
            except Exception as error:
                found.append(f"the negative's provider restore raised {type(error).__name__}")
        free = getattr(world, "free", None)
        for name, box in boxes.items():
            if box is None:
                continue
            try:
                if free is not None:
                    free(f"fuse-proof-{name}")
                box.stop()
            except Exception as error:
                found.append(f"the {name} sandbox's stop raised {type(error).__name__}")
        try:
            left = world.mounts_left()
            if left:
                found.append(f"{left} FUSE mount(s) of the proof are left")
        except Exception as error:
            found.append(f"the proof's FUSE mounts could not be counted ({type(error).__name__})")
        if files is not None:
            try:
                removed = files.remove_all()
                if removed:
                    found.append(f"{len(removed)} seeded file(s) could not be removed")
            except Exception as error:
                found.append(f"the seeded files' removal raised {type(error).__name__}")
    return found


@contextlib.contextmanager
def _cuts_deferred() -> Iterator[None]:
    """SIGINT and SIGTERM ignored for the body (the teardown), in the main thread, where a handler can be set: a cut
    that lands meanwhile never leaves a sandbox, a mount or a seeded file behind. Elsewhere, nothing changes."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def run_proof(
    world: Any,
    out: Path,
    *,
    bound_s: float,
    time_cap_s: int,
    gap_s: float,
    second_cycle: bool,
    run_campaign: Callable[[Path], int],
    settings_world: str,
    platform: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """The proof's sequence (the module's docstring) over `world`; the facts, with `problems`."""
    out.mkdir(parents=True, exist_ok=True)  # the facts are written there however the proof ends
    facts: dict[str, Any] = {"bound_s": bound_s, "target": world.target, "second_cycle": second_cycle, "times": {}}
    began = time.monotonic()

    def mark(name: str) -> None:
        facts["times"][name] = round(time.monotonic() - began, 1)

    files = control = unregistered = injector = None
    try:
        files = world.files()  # the seeding is inside the teardown's reach: what it made is removed however it ends
        local, owner_provider = world.local_name, world.provider_of(world.target)
        folder = "fuse-proof" if settings_world == "fake" else f"fuse-proof-{uuid.uuid4().hex[:8]}"
        root = files.root(local, world.space_id)
        top = files.make(local, root, folder, "DIR")
        seeded = wait_seen(files, owner_provider, top)
        # one owner-only file per outage: a read of one whose fetch ends after its restore gives the local provider a
        # copy, so the registered read and the negative's never share a file
        owners: dict[str, str] = {}
        for name in (REGISTERED_FILE, NEGATIVE_FILE):
            owners[name] = files.make(owner_provider, top, name, "REG")
            files.write(owner_provider, owners[name], 0, OWNER_BYTES)
            seeded = seeded and wait_seen(files, local, owners[name])
        here = files.make(local, top, "local.bin", "REG")
        files.write(local, here, 0, LOCAL_BYTES)
        seeded = seeded and wait_seen(files, owner_provider, here)
        space_root = f"{world.mount_path}/{world.space_name}"
        path_of = {name: f"{space_root}/{folder}/{name}" for name in owners}
        rest_of = {name: f"{folder}/{name}" for name in owners}
        owner_path, negative_path = path_of[REGISTERED_FILE], path_of[NEGATIVE_FILE]
        local_path = f"{space_root}/{folder}/local.bin"
        local_sha = hashlib.sha256(LOCAL_BYTES if settings_world != "fake" else _fake_local()).hexdigest()
        facts.update(seeded=seeded, owner_rest=rest_of[REGISTERED_FILE], local_sha256=local_sha)
        mark("seeded")
        if not seeded:
            raise ProofStopped("the seeded files were not all seen in time")

        control = world.direct("fuse-proof-control")
        reads: list[str | None] = [sha_of(control.exec(FP.local_read(local_path, space_root), 60).stdout)]
        mark("control_up")

        # the registered read must reach the owner: the local provider holds no byte of its file, or nothing blocks
        held = world.local_held(owners[REGISTERED_FILE])
        facts["local_held_before"] = held
        if held != 0:
            raise ProofStopped(f"the local provider holds {held} byte(s) of the registered file: nothing would block")

        study_settings = {
            "support": str(SUPPORT),
            "world": settings_world,
            "platform": str(platform) if platform is not None else None,
            "target": world.target,
            "owner_path": owner_path,
            "local_path": local_path,
            "root_path": space_root,
            "second_cycle": second_cycle,
            "collected": str(out / "collected"),
        }
        study = write_study(
            out, study_settings, target=world.target, bound_s=bound_s, time_cap_s=time_cap_s, platform=platform
        )
        facts["campaign_exit"] = run_campaign(study)
        mark("registered_done")
        record, text = registered_record(study)
        if record is not None:
            facts["registered"] = FP.record_facts(
                record, expected=(world.space_name, rest_of[REGISTERED_FILE]), bound_s=bound_s, local_sha256=local_sha
            )
        facts["record_valid"], facts["replayed"] = record_checks(text)
        reads.append(sha_of(control.exec(FP.local_read(local_path, space_root), 60).stdout))
        if record is None or facts["campaign_exit"] != 0:  # no second outage after a campaign that failed
            raise ProofStopped(
                f"the campaign exited {facts['campaign_exit']} (registered record: {record is not None})"
            )

        sleep(gap_s)  # the run's clear waited out the session drain already: this only adds to it
        mark("gap_done")
        unregistered = world.direct("fuse-proof-negative")
        negative_held = world.local_held(owners[NEGATIVE_FILE])
        if negative_held != 0:
            raise ProofStopped(f"the local provider holds {negative_held} byte(s) of the negative's file")
        injector = world.injector(world.target)  # kept for the teardown: a cut in its clear is cleared again there
        with injector.session({"run_key": "fuse-proof-negative-outage", "attempt": 1}):
            mark("negative_outage")
            expected = (world.space_name, rest_of[NEGATIVE_FILE])
            facts["negative"] = negative(unregistered, negative_path, expected, bound_s, negative_held)
            reads.append(sha_of(control.exec(FP.local_read(local_path, space_root), 60).stdout))
        mark("negative_restored")
        facts["control"] = {"reads": reads, **mount_facts(control)}
    except ProofStopped as stopped:  # a stop of the proof's own: the teardown runs, and the facts say why
        facts["stopped"] = str(stopped)
    except BaseException as error:  # a cut (the file-count watcher's SIGINT) or a failure: the teardown still runs
        facts["cut"] = type(error).__name__
        raise
    finally:
        facts["teardown_problems"] = teardown(
            world, files, {"control": control, "negative": unregistered}, injector=injector
        )
        mark("done")
        facts["problems"] = FP.problems(facts)
        (out / "facts.json").write_text(json.dumps(facts, indent=1, default=str), encoding="utf-8")
    return facts


def _fake_local() -> bytes:
    import fuse_proof_fake

    return fuse_proof_fake.LOCAL_BYTES


def live_campaign(study: Path) -> int:
    """The campaign in a process of its own, on this interpreter, as a study's run is made. A SIGINT or SIGTERM to the
    driver (the file-count watcher's cut, or the step's end) is passed on to it once, as SIGINT, and its end is awaited:
    its injector clears and its sandbox stops on their own way out, never killed under them. The handler is in place
    before the child starts, so a cut in between is remembered and passed on as soon as there is a child. The driver
    then tears down its own sandboxes and files (`run_proof`)."""
    asked: list[int] = []
    child: subprocess.Popen[bytes] | None = None

    def forward(signum: int, frame: Any) -> None:
        asked.append(signum)
        if child is not None and len(asked) == 1:
            with contextlib.suppress(ProcessLookupError):
                child.send_signal(signal.SIGINT)

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        child = subprocess.Popen([sys.executable, "-I", "-B", "-m", "expcore", "run", str(study)])
        if asked:  # a cut before the child existed
            with contextlib.suppress(ProcessLookupError):
                child.send_signal(signal.SIGINT)
        code = child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    if asked:
        raise KeyboardInterrupt("the campaign was cut by a signal")
    return code


def _interrupted(signum: int, frame: Any) -> None:
    raise KeyboardInterrupt(f"signal {signum}")


def main(argv: list[str]) -> int:
    args = argv[1:]
    second_cycle = "--no-second-cycle" not in args
    gap_s = float(args[args.index("--gap-s") + 1]) if "--gap-s" in args else GAP_S
    positional = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--gap-s")]
    if len(positional) != 2:
        print(__doc__)
        return 2
    platform, out = Path(positional[0]).resolve(), Path(positional[1]).resolve()
    out.mkdir(parents=True, exist_ok=False)
    signal.signal(signal.SIGTERM, _interrupted)  # the step's end: the same way out as a SIGINT, teardown included
    from fuse_proof_world import LiveWorld

    world = LiveWorld(platform)
    try:
        facts = run_proof(
            world, out, bound_s=FP.BOUND_S, time_cap_s=FP.TIME_CAP_S, gap_s=gap_s, second_cycle=second_cycle,
            run_campaign=live_campaign, settings_world="live", platform=platform,
        )  # fmt: skip
    finally:
        world.close()
    print(json.dumps({"problems": facts["problems"], "times": facts["times"]}, indent=1))
    return 0 if not facts["problems"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
