"""The O97 live proof on a real HPC node: a cell blocked reading a stopped owner's file is freed at its bound, the run
goes on as the agent's, and a block that no injection of the run explains stays infrastructure. A script, not a test:
the node has no pytest. Run it from a private 0700 directory, once, under `python -I -B`, in a step of the federation's
agent job, with the federation up and its relay serving:

    python -I -B tests/live/fuse_abort_apptainer.py PLATFORM_TOML OUT_DIR [--gap-s S] [--no-second-cycle]

`PLATFORM_TOML` is the federation's own platform file, as `up` rendered it: a sanctioned `[faults]` of the hpc backend,
`[principals.data_path]`, `[sandbox]` with its Apptainer pins and a mount level, and a space whose data provider is not
the agent's local one (the target). The plan is `e2e/fuse-abort-helios-proof-plan.private.md`, and the pass list is
`tests/support/fuse_proof.py` (P1 to P12). In order:

1. **Seed** through the platform: a directory, a file written through the target alone (the local provider holds none
   of it) and a local file. **K**, the control sandbox (direct, no injection), reads the local file.
2. **P12**: **N**, an unregistered sandbox (direct, outside any attempt), reads the owner-only file while the driver's
   own injector holds the target down: the read blocks, the bound kills it, the survivor is aborted, and the verdict is
   infrastructure (`no_injection_in_effect`); N's next cell is refused as `cell_unkillable`. The owner is restored. K
   reads again.
3. The **gap**: the session drain and three minutes more, so the next outage's peers are not in their backoff.
4. **S**, the registered attempt, through the campaign (`python -m expcore run`) of a minimal study written into
   OUT_DIR from `tests/support/fuse_proof_study/`: the platform's launch and the run's own injector inside the attempt.
   K reads a third time.
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
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
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
GAP_S = 500.0  # session_drain_s (320 s) and three minutes more


def wait_seen(files: Any, provider: str, file_id: str, timeout_s: float = SEEN_S) -> bool:
    """Until `provider` knows the entry; whether it did within `timeout_s`."""
    until = time.monotonic() + timeout_s
    while time.monotonic() < until:
        if files.seen(provider, file_id):
            return True
        time.sleep(2.0)
    return files.seen(provider, file_id)


def sha_of(stdout: str) -> str | None:
    """The SHA-256 a local-read cell printed, None when it printed none."""
    line = next((x for x in stdout.splitlines() if x.startswith("sha256 ")), None)
    return line.split()[1] if line else None


def mount_facts(box: Any) -> dict[str, Any]:
    """What a direct sandbox's metadata says of its mount: its remounts, and whether any generation was aborted."""
    mount = (box.metadata() or {}).get("mount") or {}
    generations = mount.get("generations") or []
    return {"remounts": mount.get("remounts"), "aborted": any(g.get("abort") for g in generations)}


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
    if platform is not None:
        shutil.copyfile(platform, study / "platform.toml")
    fills = {"@TARGET@": target, "@BOUND_S@": f"{bound_s:g}", "@TIME_CAP_S@": str(time_cap_s), "@PLATFORMS@": platforms}
    for name in ("study.toml", "campaign.toml"):
        text = (TEMPLATE / f"{name}.in").read_text(encoding="utf-8")
        for key, value in fills.items():
            text = text.replace(key, value)
        (study / name).write_text(text, encoding="utf-8")
    (study / "fuse_proof.json").write_text(json.dumps(settings, indent=1), encoding="utf-8")
    write_stamp(study, STAMP, load_study(study).scope)
    return study


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


def negative(world: Any, injector: Any, box: Any, owner_path: str, bound_s: float) -> dict[str, Any]:
    """N's cell under the driver's own outage (P12): what its deadline and its next cell were."""
    result: dict[str, Any] = {}
    done = box.exec(FP.owner_read(owner_path), bound_s)
    deadlines = (box.metadata() or {}).get("cell_deadlines") or []
    result["deadline"] = deadlines[-1] if deadlines else None
    result["restore_pending"] = bool(getattr(done, "restore_pending", False))
    try:
        box.exec("print('next')", 30)
        result["next_cell"] = "ran"
    except InfraError as refused:
        result["next_cell"] = refused.kind
    return result


def teardown(world: Any, files: Any, boxes: dict[str, Any]) -> list[str]:
    """Stop the direct sandboxes, count the proof's FUSE mounts left and remove the seeded files; what went wrong."""
    found: list[str] = []
    free = getattr(world, "free", None)
    for name, box in boxes.items():
        if box is None:
            continue
        if free is not None:
            free(f"fuse-proof-{name}")
        try:
            box.stop()
        except Exception as error:
            found.append(f"the {name} sandbox's stop raised {type(error).__name__}")
    left = world.mounts_left()
    if left:
        found.append(f"{left} FUSE mount(s) of the proof are left")
    removed = files.remove_all()
    if removed:
        found.append(f"{len(removed)} seeded file(s) could not be removed")
    return found


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
    facts: dict[str, Any] = {"bound_s": bound_s, "target": world.target, "second_cycle": second_cycle, "times": {}}
    began = time.monotonic()

    def mark(name: str) -> None:
        facts["times"][name] = round(time.monotonic() - began, 1)

    files = world.files()
    local, owner_provider = world.local_name, world.provider_of(world.target)
    folder = "fuse-proof" if settings_world == "fake" else f"fuse-proof-{uuid.uuid4().hex[:8]}"
    root = files.root(local, world.space_id)
    top = files.make(local, root, folder, "DIR")
    seeded = wait_seen(files, owner_provider, top)
    owner = files.make(owner_provider, top, "owner-only.bin", "REG")
    files.write(owner_provider, owner, 0, OWNER_BYTES)
    seeded = seeded and wait_seen(files, local, owner)
    here = files.make(local, top, "local.bin", "REG")
    files.write(local, here, 0, LOCAL_BYTES)
    seeded = seeded and wait_seen(files, owner_provider, here)
    space_root = f"{world.mount_path}/{world.space_name}"
    owner_path, local_path = f"{space_root}/{folder}/owner-only.bin", f"{space_root}/{folder}/local.bin"
    world.owner_path = owner_path
    facts.update(seeded=seeded, owner_rest=f"{folder}/owner-only.bin")
    facts["local_sha256"] = hashlib.sha256(LOCAL_BYTES if settings_world != "fake" else _fake_local()).hexdigest()
    mark("seeded")
    control = unregistered = None
    try:
        control = world.direct("fuse-proof-control")
        reads: list[str | None] = [sha_of(control.exec(FP.local_read(local_path, space_root), 60).stdout)]
        mark("control_up")

        unregistered = world.direct("fuse-proof-negative")
        injector = world.injector(world.target)
        with injector.session({"run_key": "fuse-proof-negative-outage", "attempt": 1}):
            mark("negative_outage")
            facts["negative"] = negative(world, injector, unregistered, owner_path, bound_s)
            reads.append(sha_of(control.exec(FP.local_read(local_path, space_root), 60).stdout))
        mark("negative_restored")
        facts["negative_problems"] = FP.negative_problems(facts["negative"])
        facts["negative"]["deadline"] = None  # its record is checked above; the facts keep the verdict only
        sleep(gap_s)
        mark("gap_done")

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
        facts["registered"] = FP.record_facts(record) if record is not None else {}
        facts["record_valid"], facts["replayed"] = record_checks(text)
        reads.append(sha_of(control.exec(FP.local_read(local_path, space_root), 60).stdout))
        facts["control"] = {"reads": reads, **mount_facts(control)}
        facts["control_problems"] = FP.control_problems(facts["control"])
    except BaseException as error:  # a cut (the file-count watcher's SIGINT) or a failure: the teardown still runs
        facts["cut"] = type(error).__name__
        raise
    finally:
        facts["teardown_problems"] = teardown(world, files, {"control": control, "negative": unregistered})
        facts["scans"] = "not_run"  # the facts are scanned on the host they are pulled to
        mark("done")
        cut = [f"the proof was cut ({facts['cut']}): nothing after it was shown"] if "cut" in facts else []
        facts["problems"] = FP.problems(facts) + cut
        (out / "facts.json").write_text(json.dumps(facts, indent=1, default=str), encoding="utf-8")
    return facts


def _fake_local() -> bytes:
    import fuse_proof_fake

    return fuse_proof_fake.LOCAL_BYTES


def live_campaign(study: Path) -> int:
    """The campaign in a process of its own, on this interpreter, as a study's run is made. A SIGINT or SIGTERM to the
    driver (the file-count watcher's cut, or the step's end) is passed on to it as SIGINT and its end is awaited: its
    injector clears and its sandbox stops on their own way out, never killed under them. The driver then tears down
    its own sandboxes and files (`run_proof`)."""
    child = subprocess.Popen([sys.executable, "-I", "-B", "-m", "expcore", "run", str(study)])
    asked: list[int] = []

    def forward(signum: int, frame: Any) -> None:
        asked.append(signum)
        with contextlib.suppress(ProcessLookupError):
            child.send_signal(signal.SIGINT)

    previous = {sig: signal.signal(sig, forward) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
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
