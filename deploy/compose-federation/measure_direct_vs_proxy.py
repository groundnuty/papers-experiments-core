"""Replay the same read sequences through the sandbox's mount under proxy I/O and under direct I/O, on the same files
and from the same starting block state, and compare what each read places on the local provider (#223, O40).

    uv run python deploy/compose-federation/measure_direct_vs_proxy.py --sequences FILE --pool CIDR --out DIR \\
        --storage ID [--storage-path PATH] [--platform PATH] [--oneclient-image REF] [--rounds N] [--control] \\
        [--limit N]

A study whose placement model was validated under proxy I/O keeps proxy I/O, unless direct I/O gives the same placed
bytes per read sequence (#223): estimates made under proxy would not transfer otherwise. This is the comparison that
decides it: the same files, the same reads, and before every pass the same starting state (the local provider holds
none of each file, the holder all of it), compared read sequence by read sequence and read by read, never only in
total.

Run it outside any sandbox, after `federation.py up`. It starts the sandbox's containers, writes files into the study
space as the admin, evicts them from the local provider between passes and deletes them at the end. `--sequences` is
a `read-sequences/1` file, as the mount sweep reads (`measure_mount_sweep.py`); `--pool` a private IPv4 range the host
routes nowhere; `--out` a new directory of the caller's. `--storage` is the id of the local provider's storage: its
panel lists it (`GET /api/v3/onepanel/provider/storages`), and a file's `GET /data/{id}/distribution` on that provider
names it among the keys of `distributionPerStorageBackend`. `--storage-path` is the directory that holds it on this
host, by default the Compose layout's `<federation root>/<local provider>/storage` beside the platform file.

1. Fixtures, as the sweep makes them: one fresh file per sequence file and per replay mode (`buffered`, `pread`),
   written through the holder (the first supporting provider other than the local one), the local provider holding
   none of it; and one check file written through the local provider, which holds it in full, for the direct pass's
   start check (`[sandbox.mount] direct_check_file`, never one of the fixtures).
2. Passes: in round r, proxy then direct when r is even, direct then proxy when it is odd, so an order effect is not
   taken for a mode's. `--control` adds a second proxy pass to each round, compared with the first: the noise floor
   of proxy against proxy, without which a difference between the modes cannot be judged.
3. Each pass: every fixture evicted from the local provider and shown held by the holder alone; a fresh sandbox (a new
   oneclient process, so nothing is cached) with the pass's `io_mode`; for direct, the storage mapped read-only into
   the sidecar and the start's check, whose facts are logged; each file's reads replayed by one resident process,
   settled after each read on the local provider's block map, as the sweep does; for direct, the sidecar's log read
   again after the reads (a read refused for permissions goes through the provider, and only the log says so); the
   sandbox stopped.
4. The comparison (`compare`, `verdict`): for each file, each read's gained ranges under the one pass against the
   other's, and the ranges placed at the end; a file is the same only when both agree. A file whose replay is not
   complete in both passes (a failed read, a block map that did not settle, a replay that did not end, a pass that did
   not start, a direct pass whose log shows a read through the provider or cannot be read) is incomplete, never the
   same, and every file of the measurement is compared in every round, even when neither pass has it.

Every event is one JSON line in `measure.jsonl`, and the verdict is `verdict.json`, which says whether the measurement
ran to its end (`complete`): offsets, lengths, bytes, counts, seconds and tags, never a token, an address, a name or a
path. Every file is deleted at the end.
"""

import argparse
import dataclasses
import hashlib
import json
import os
import posixpath
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tests" / "support"))  # the live mount harness's helpers

from measure_mount_sweep import (  # noqa: E402 -- the sweep's replayer and fixtures, imported by plain name
    LAUNCH,
    REPLAYER,
    Log,
    cell,
    load_sequences,
    make_fixtures,
    settled,
    wait_for,
)
from mount_live import (  # noqa: E402
    AGENT_IMAGE,
    LEASE_KEY,
    ONECLIENT_IMAGE,
    SpaceFiles,
    derive_platform,
    federation_network,
    sandbox_table,
)

from expcore_onedata import consistency  # noqa: E402
from expcore_onedata.client import OnezoneClient  # noqa: E402
from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.placement import held_bytes, merged, uncovered  # noqa: E402
from expcore_onedata.probes import PlatformProbes  # noqa: E402
from expcore_onedata.provision import Provisioner, resolve_provider_ids  # noqa: E402
from expcore_onedata.rangemap import load_placement  # noqa: E402
from expcore_onedata.sandbox import hostops  # noqa: E402
from expcore_onedata.sandbox.api import DataPathMode, Endpoint, SandboxError, SandboxSpec  # noqa: E402
from expcore_onedata.sandbox.hostops import DockerHost  # noqa: E402
from expcore_onedata.sandbox.oneclient_args import STORAGE_ID, host_path_problem  # noqa: E402
from expcore_onedata.sandbox.sandbox import DockerSandbox  # noqa: E402
from expcore_onedata.tokens import TokenIssuer, data_path_spec  # noqa: E402

MODES = ("proxy", "direct")
CEILING_S = 1800.0  # the mount token's lifetime, as the live mount test mints it; one token per pass
VISIBLE_TIMEOUT_S = 180.0
CHECK_BYTES = 64 * 1024 + 3  # the direct start check's file: small, held in full by the local provider
DEFAULT_PLATFORM = Path("/mnt/volume2/expcore-federation/platform.toml")
NOT_ANSWERED = ("error", "no_answer_after_s")  # a read record with either did not read


def pass_plan(rounds: int, *, control: bool) -> list[dict[str, Any]]:
    """The passes in order: each round runs both modes, proxy first in an even round and direct first in an odd one,
    and with `control` a second proxy pass after them."""
    plan = []
    for r in range(rounds):
        for mode in MODES if r % 2 == 0 else tuple(reversed(MODES)):
            plan.append({"round": r, "mode": mode, "label": mode})
        if control:
            plan.append({"round": r, "mode": "proxy", "label": "proxy-control"})
    return plan


def _incomplete(base: Mapping[str, Any] | None, other: Mapping[str, Any] | None) -> str | None:
    """Why one file's two replays cannot be compared, or None when both are complete."""
    if base is None or other is None:
        return "missing in a pass"
    if base.get("not_direct") or other.get("not_direct"):
        return "a direct pass did not read directly, or could not be shown to"
    if len(base["reads"]) != len(other["reads"]):
        return "read counts differ"
    if not (base.get("ended") and other.get("ended")):
        return "a replay did not end"
    reads = [*base["reads"], *other["reads"]]
    if any(key in read for read in reads for key in NOT_ANSWERED):
        return "a read failed or gave no answer"
    if not all(read.get("settled") for read in reads):
        return "a block map did not settle"
    return None


def _ranges(value: Sequence[Sequence[int]]) -> list[tuple[int, int]]:
    return merged((int(offset), int(length)) for offset, length in value)


def compare(
    base: Mapping[str, Mapping[str, Any]], other: Mapping[str, Mapping[str, Any]], tags: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Each file's replay under one pass (`base`) against the other's, by its tag: every read whose gained ranges
    differ, with the bytes each gained and the bytes only one of them gained, and the same for the ranges placed at the
    end. `tags` are the measurement's files, each compared even when neither pass has it (both failed to start), so a
    file never vanishes from the verdict. `same` is None for a file that is incomplete (`incomplete` says why), else
    whether no read differed and the placement at the end is the same."""
    out = []
    for tag in sorted(set(tags) | set(base) | set(other)):
        b, o = base.get(tag), other.get(tag)
        why = _incomplete(b, o)
        found: dict[str, Any] = {"file": tag, "same": None, "incomplete": why}
        if why is None:
            differing = []
            for i, (rb, ro) in enumerate(zip(b["reads"], o["reads"], strict=True)):
                gb, go = _ranges(rb["gained"]), _ranges(ro["gained"])
                if gb != go:
                    differing.append({
                        "i": i, "base_bytes": held_bytes(gb), "other_bytes": held_bytes(go),
                        "only_base_bytes": held_bytes(uncovered(go, gb)),
                        "only_other_bytes": held_bytes(uncovered(gb, go)),
                    })  # fmt: skip
            pb, po = _ranges(b["placed"]), _ranges(o["placed"])
            found.update(
                same=not differing and pb == po, reads_compared=len(b["reads"]), differing_reads=differing,
                placed_same=pb == po,
                placed_base_bytes=held_bytes(pb), placed_other_bytes=held_bytes(po),
                placed_only_base_bytes=held_bytes(uncovered(po, pb)),
                placed_only_other_bytes=held_bytes(uncovered(pb, po)),
            )  # fmt: skip
        out.append(found)
    return out


def verdict(comparisons: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Whether the passes placed the same bytes per read sequence: True only when every file compared and none
    differed, False once one differed, None (unknown, never yes) when nothing differed but a file did not compare."""
    same = sum(1 for c in comparisons if c["same"] is True)
    differing = sum(1 for c in comparisons if c["same"] is False)
    incomplete = sum(1 for c in comparisons if c["same"] is None)
    answer = False if differing else (True if comparisons and not incomplete else None)
    return {
        "files": len(comparisons),
        "same": same,
        "differing": differing,
        "incomplete": incomplete,
        "same_placed_bytes_per_read_sequence": answer,
    }


def storage_of(storage_id: str, path: str | None, platform: Path, local: str) -> tuple[str, str]:
    """The local provider's storage as `[sandbox.mount.storages]` maps it: its id, and the directory that holds it on
    this host (by default the Compose layout's, `<root>/<local>/storage`, the root being the platform file's
    directory)."""
    if not STORAGE_ID.fullmatch(storage_id):
        raise SystemExit("--storage: a storage id is letters, digits, '.', '_' and '-' (no ':')")
    where = path if path is not None else str(platform.resolve().parent / local / "storage")
    if host_path_problem(where):
        raise SystemExit("--storage-path: one absolute, normalised directory path, not the root, without a comma")
    return storage_id, posixpath.normpath(where)


def mount_keys(mode: str, storage: tuple[str, str], check_file: str) -> dict[str, Any]:
    """The `[sandbox.mount]` keys of a pass beside its path: the mode, and for direct the storage and the check file."""
    if mode != "direct":
        return {"io_mode": mode}
    return {"io_mode": "direct", "storages": {storage[0]: storage[1]}, "direct_check_file": check_file}


def replay_one(ctx: dict, sandbox: DockerSandbox, fixture: dict, label: str, log: Log) -> dict[str, Any]:
    """One file's sequence replayed by one resident process in the agent, settled after each read (the sweep's
    replayer); returns each read's gained ranges and the ranges the local provider holds at the end."""
    view, local, file_id, mode = ctx["view"], ctx["local"], fixture["file_id"], fixture["mode"]
    reads, tag = fixture["reads"], f"{mode}-{fixture['id']}"
    stem = f"/work/{label}-{tag}"
    probes = PlatformProbes(ctx["client"], view, {tag: fixture["space_path"]}, ctx["providers"])
    probes.start({})
    before = merged(view.blocks(file_id, local))
    launched = cell(
        sandbox,
        LAUNCH.format(child=REPLAYER, mode=mode, path=fixture["inside"], prog=f"{stem}.jsonl", ack=f"{stem}.ack",
                      reads=json.dumps(reads), err=f"{stem}.err"),
        60,
    )  # fmt: skip
    work = sandbox.dir / "work"
    progress = work / f"{label}-{tag}.jsonl"
    common = {"pass": label, "round": ctx["round"], "file": tag}
    log("opened", **common, launched="pid" in launched, **(wait_for(progress, -1) or {"no_answer": True}))
    out: list[dict[str, Any]] = []
    for i, (offset, length) in enumerate(reads):
        rec = wait_for(progress, i)
        if rec is None:
            out.append({"i": i, "gained": [], "no_answer_after_s": True, "settled": False})
            log("read", **common, **out[-1], offset=offset, length=length)
            break
        after, settle_s, ok = settled(view, file_id, local)
        gained = merged(uncovered(before, after))
        out.append({"i": i, "gained": [list(g) for g in gained], "settled": ok,
                    **{k: v for k, v in rec.items() if k in ("bytes", "error", "seconds")}})  # fmt: skip
        log("read", **common, **out[-1], offset=offset, length=length, gained_bytes=held_bytes(gained),
            settle_s=settle_s)  # fmt: skip
        before = after
        ack = ctx["host"].docker(
            "exec",
            "-u",
            str(ctx["agent_uid"]),
            sandbox.names.agent,
            "touch",
            f"{stem}.ack.{i}",
            check=False,
            timeout=30,
        )
        if ack.returncode != 0:
            log("ack_failed", **common, i=i)
            break
    ended = wait_for(progress, "end") is not None
    time.sleep(5.0)
    late = merged(view.blocks(file_id, local))
    snapshot = probes.finish()
    log("replay", **common, ended=ended, reads=len(out), placed_bytes=held_bytes(late),
        late_gain_bytes=held_bytes(uncovered(before, late)), probes_ok=snapshot.get("ok"),
        on_demand=snapshot.get("on_demand"), transfer_records=len(snapshot.get("transfers") or []))  # fmt: skip
    return {"reads": out, "placed": [list(r) for r in late], "ended": ended and len(out) == len(reads)}


def reset_fixtures(ctx: dict, fixtures: list[dict], label: str, log: Log) -> None:
    """Every fixture evicted from the local provider and shown held by the holder alone, at its size: the starting
    state of every pass. A fixture in any other state stops the measurement."""
    view, local, owner = ctx["view"], ctx["local"], ctx["owner"]
    evicted = 0
    for f in fixtures:
        if held_bytes(merged(view.blocks(f["file_id"], local))):
            view.evict(f["space_path"], local)
            evicted += 1
    state = [
        (held_bytes(merged(view.blocks(f["file_id"], local))), held_bytes(merged(view.blocks(f["file_id"], owner))))
        for f in fixtures
    ]
    log("reset", **{"pass": label, "round": ctx["round"]}, evicted=evicted,
        receiver_bytes=sum(r for r, _ in state), holder_short=sum(1 for (_, h), f in zip(state, fixtures, strict=True)
                                                                  if h != f["size"]))  # fmt: skip
    if any(r or h != f["size"] for (r, h), f in zip(state, fixtures, strict=True)):
        raise RuntimeError("a fixture is not held by the holder alone before a pass")


def run_pass(ctx: dict, entry: dict, fixtures: list[dict], log: Log) -> dict[str, dict] | None:
    """One pass: the starting state, a fresh sandbox in the pass's mode, every fixture replayed, the sandbox stopped.
    None when the sandbox did not start (its files are then missing in the comparison)."""
    label, config = entry["label"], ctx["configs"][entry["mode"]]
    ctx["round"] = entry["round"]
    reset_fixtures(ctx, fixtures, label, log)
    token = TokenIssuer(ctx["client"], config).mint(
        data_path_spec(config.space.id, "oneclient"), config.principals.data_path, ceiling_s=CEILING_S
    )
    spec = SandboxSpec(
        run_key=f"{ctx['run']}-{label}-{entry['round']}", mode=DataPathMode.MOUNT, token=token,
        zone=Endpoint.from_url(config.zone.url), provider=Endpoint.from_url(config.local_provider.url),
        lease_key=LEASE_KEY,
    )  # fmt: skip
    began = time.monotonic()
    try:
        sandbox = DockerSandbox.start(ctx["host"], config.sandbox, spec)
    except SandboxError as error:
        log("start_failed", **{"pass": label}, round=entry["round"], error=str(error)[:400],
            seconds=round(time.monotonic() - began, 1))  # fmt: skip
        return None
    try:
        mount = sandbox.metadata()["mount"]
        log("started", **{"pass": label}, round=entry["round"], seconds=round(time.monotonic() - began, 1),
            io_mode=mount["io_mode"], options=mount["options"], direct=mount.get("direct"))  # fmt: skip
        results = {f"{f['mode']}-{f['id']}": replay_one(ctx, sandbox, f, label, log) for f in fixtures}
        errors = sandbox.sidecar_errors() or {}
        log("sidecar", **{"pass": label}, round=entry["round"], total=errors.get("total"),
            by_kind=errors.get("by_kind"), signatures=errors.get("signatures"))  # fmt: skip
        if entry["mode"] == "direct":  # a read refused for permissions went through the provider: not a direct pass
            lines = sandbox.direct_io_lines()
            log("direct_after_reads", **{"pass": label}, round=entry["round"], lines=lines)
            if lines is None or lines:
                results = {tag: {**result, "not_direct": lines or ["log unread"]} for tag, result in results.items()}
        return results
    finally:
        try:
            sandbox.stop()
            log("stopped", **{"pass": label}, round=entry["round"], ok=True)
        except SandboxError as error:
            log("stopped", **{"pass": label}, round=entry["round"], ok=False, error=str(error)[:300])


def make_check_file(ctx: dict, files: SpaceFiles, log: Log) -> str:
    """The direct start check's file: written through the local provider, shown held there in full; its path under
    the mount (`<space>/<run>/<name>`)."""
    local, view = ctx["local"], ctx["view"]
    name = f"check-{uuid.uuid4().hex[:8]}.bin"
    file_id = files.make(local, ctx["top"], name, "REG")
    files.write(local, file_id, 0, os.urandom(CHECK_BYTES))
    held = consistency.converge(
        lambda: held_bytes(merged(view.blocks(file_id, local))), lambda n: n == CHECK_BYTES,
        timeout_s=VISIBLE_TIMEOUT_S, poll_s=2.0,
    )  # fmt: skip
    log("check_file", size=CHECK_BYTES, held_in_full=held.converged)
    if not held.converged:
        raise RuntimeError("the check file is not held in full by the local provider")
    return f"{ctx['space']}/{ctx['run']}/{name}"


def derived(args: argparse.Namespace, images: dict, mode: str, storage: tuple[str, str], check: str | None) -> Any:
    """The platform of one mode's passes, derived from the federation's own into a directory of its own under `--out`
    (`mount_live.derive_platform`), with its secrets."""
    facts = json.loads((args.platform.parent / "facts.json").read_text(encoding="utf-8"))
    local = load_platform(args.platform, require_secrets=False).support.local
    sdir = args.out / mode
    sdir.mkdir()
    table = sandbox_table(
        work_root=sdir / "work", images=images, pool=args.pool, network=federation_network(facts), local=local,
        local_address=facts["providers"][local]["domain_on_project_network"], **mount_keys(mode, storage, check or ""),
    )  # fmt: skip
    return load_platform(derive_platform(args.platform, sdir, table))


def measure(args: argparse.Namespace, sequences: list[dict], images: dict, log: Log) -> None:
    proxy = derived(args, images, "proxy", ("", ""), None)
    local = proxy.support.local
    storage = storage_of(args.storage, args.storage_path, args.platform, local)
    owner = next(p for p in proxy.support.providers if p != local)
    client = OnezoneClient(proxy)
    placement = load_placement(proxy.file, proxy).settings
    view = Provisioner(client, proxy, dataclasses.replace(placement, holder=owner),
                       provider_ids=resolve_provider_ids(proxy), log=args.out / "never-written.jsonl")  # fmt: skip
    files = SpaceFiles(proxy)
    run = f"expcore-direct-{uuid.uuid4().hex[:8]}"
    host = DockerHost(sudo_command=proxy.sandbox.sudo)
    ctx: dict[str, Any] = {"setting": "direct-vs-proxy", "local": local, "owner": owner, "run": run, "view": view,
                           "client": client, "providers": list(proxy.support.providers), "host": host,
                           "space": proxy.space.name, "limit": args.limit or None, "round": None,
                           "mount": proxy.sandbox.mount.path, "agent_uid": proxy.sandbox.agent_uid}  # fmt: skip
    comparisons: dict[str, list] = {}
    finished = False
    try:
        ctx["top"] = files.make(local, files.root(local), run, "DIR")
        seen = consistency.converge(
            lambda: files.seen(owner, ctx["top"]), bool, timeout_s=VISIBLE_TIMEOUT_S, poll_s=2.0
        )
        if not seen.converged:
            raise RuntimeError("the run's directory did not reach the holder")
        check = make_check_file(ctx, files, log)
        ctx["configs"] = {"proxy": proxy, "direct": derived(args, images, "direct", storage, check)}
        fixtures = make_fixtures(ctx, files, sequences, log)
        passes: dict[tuple[int, str], dict | None] = {}
        for entry in pass_plan(args.rounds, control=args.control):
            passes[(entry["round"], entry["label"])] = run_pass(ctx, entry, fixtures, log)
        tags = [f"{f['mode']}-{f['id']}" for f in fixtures]  # every file, even of a round whose passes both failed
        for r in range(args.rounds):
            pairs = [("proxy", "direct")] + ([("proxy", "proxy-control")] if args.control else [])
            for a, b in pairs:
                found = compare(passes[(r, a)] or {}, passes[(r, b)] or {}, tags)
                comparisons.setdefault(f"{a}~{b}", []).extend(found)
                for c in found:
                    log("compare", round=r, pair=f"{a}~{b}", **c)
        finished = True
    finally:
        # a measurement that stopped early has no verdict: `complete` false, whatever the pairs compared so far say
        result = {"complete": finished, **{pair: verdict(found) for pair, found in comparisons.items()}}
        (args.out / "verdict.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        log("verdict", **result)
        log("cleanup", failures=files.remove_all())
        files.close()
        client.close()
        left = [host.docker(*cmd, check=False, timeout=30).stdout.split()
                for cmd in (("ps", "-a", "-q", "--filter", "name=fedsbx"),
                            ("network", "ls", "-q", "--filter", "name=fedsbx"))]  # fmt: skip
        fuse = [m for m in hostops.fuse_mounts(host.read_text("/proc/self/mountinfo"))
                if m["mountpoint"].startswith(str(args.out))]  # fmt: skip
        log("left", containers=len(left[0]), networks=len(left[1]), fuse_mounts=len(fuse))


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sequences", type=Path, required=True, help="a read-sequences/1 file")
    parser.add_argument("--pool", required=True, help="a private IPv4 range the host routes nowhere")
    parser.add_argument("--out", type=Path, required=True, help="a new directory for every file the measurement writes")
    parser.add_argument("--storage", required=True, help="the id of the local provider's storage")
    parser.add_argument("--storage-path", help="the directory that holds that storage on this host")
    parser.add_argument("--platform", type=Path, default=DEFAULT_PLATFORM, help="the federation's own platform.toml")
    parser.add_argument("--oneclient-image", default=ONECLIENT_IMAGE, help="a oneclient image on the host")
    parser.add_argument("--rounds", type=int, default=1, help="rounds of the two passes, alternating which goes first")
    parser.add_argument("--control", action="store_true", help="a second proxy pass per round: the noise floor")
    parser.add_argument("--limit", type=int, default=0, help="replay only the first N reads of each file (a smoke run)")
    args = parser.parse_args(argv)
    if args.rounds < 1:
        raise SystemExit("--rounds: at least 1")
    sequences = load_sequences(args.sequences)
    args.out.mkdir(parents=True)
    log = Log(args.out / "measure.jsonl")
    log("input", sha256=hashlib.sha256(args.sequences.read_bytes()).hexdigest(), rounds=args.rounds,
        control=args.control, files=[{"id": s["id"], "size": s["size"], "reads": len(s["reads"])}
                                     for s in sequences])  # fmt: skip
    host = DockerHost()
    images = {"agent": (AGENT_IMAGE, host.image_id(AGENT_IMAGE)),
              "oneclient": (args.oneclient_image, host.image_id(args.oneclient_image))}  # fmt: skip
    measure(args, sequences, images, log)


if __name__ == "__main__":
    main()
