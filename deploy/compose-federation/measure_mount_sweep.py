"""Replay recorded read sequences through the sandbox's mount under a declared set of oneclient settings, and record
what the receiver's block map gains for each read.

    uv run python deploy/compose-federation/measure_mount_sweep.py --sequences FILE --pool CIDR --out DIR \\
        [--platform PATH] [--oneclient-image REF] [--settings NAME,...] [--limit N]

Run it outside any sandbox, after `federation.py up`: it starts the sandbox's containers (FUSE and SYS_ADMIN for the
oneclient sidecar only, as the sandbox always does), and writes files into the study space as the admin and deletes
them again. `--sequences` is a `read-sequences/1` file: files by id with their size and their reads as [offset, length].
`--pool` is a private IPv4 range the host routes nowhere, for the sandbox's own networks. `--out` is a directory of
the caller's (it must not exist); every derived platform file, the sandbox's work root and `sweep.jsonl` go there.

For each setting in `SETTINGS` (all, or those `--settings` names), in a fresh directory of the study space:
1. one fresh file per sequence file and per mode, of the file's size, random bytes, new names, written through the
   first supporting provider other than the local one (the holder), and shown held there only: the local provider,
   the one the sidecar dials (the receiver), holds none of it before the replay;
2. a sandbox in mount mode, oneclient started with the setting's arguments;
3. in the agent, the mount's read-ahead as the container sees it (`READAHEAD`), never changed;
4. each file's reads replayed by one resident process in the agent, holding one open file for the whole sequence:
   `buffered` (`open(path, 'rb')`, default buffering, then seek and read) or `pread` (`os.pread` on a bare descriptor),
   each mode on its own files. After each read the replayer waits; the host reads the receiver's block map until two
   reads `SETTLE_POLL_S` apart agree, records what it gained, then lets the next read go. After the last read the map
   is read once more `LATE_S` later. The platform probes run around each replay (attribution, transfer records).

Every event is one JSON line in `sweep.jsonl`: offsets, lengths, bytes and seconds, never a token, an address, a name
or a path. Every file is deleted at the end of its setting.
"""

import argparse
import dataclasses
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "tests" / "support"))  # the live mount harness's helpers

from mount_live import (  # noqa: E402
    AGENT_IMAGE,
    LEASE_KEY,
    ONECLIENT_IMAGE,
    SpaceFiles,
    derive_platform,
    federation_network,
    file_guid,
    mentions,
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
from expcore_onedata.sandbox.containers import ONECLIENT_BIN, SIDECAR_LOG_DIR  # noqa: E402
from expcore_onedata.sandbox.hostops import DockerHost  # noqa: E402
from expcore_onedata.sandbox.sandbox import DockerSandbox  # noqa: E402
from expcore_onedata.tokens import TokenIssuer, data_path_spec  # noqa: E402

# Declared before measuring, spanning "fetch less" to "fetch more"; every one is a oneclient argument the sidecar
# takes through `[sandbox.mount] extra_args`, and none needs more than the sidecar's FUSE and SYS_ADMIN.
SETTINGS: dict[str, list[str]] = {
    "defaults": [],
    "least": [
        "--min-block-prefetch-size=4096",
        "--read-buffer-min-size=4096",
        "--read-buffer-max-size=4096",
        "--read-buffer-prefetch-duration=0",
    ],
    "min-block-4k": ["--min-block-prefetch-size=4096"],
    "more": [
        "--min-block-prefetch-size=4194304",
        "--read-buffer-min-size=4194304",
        "--read-buffer-prefetch-duration=4",
    ],
    "most": ["--seqrd-prefetch-threshold=0", "--rndrd-prefetch-threshold=0", "--rndrd-prefetch-cluster-window=-1"],
}
MODES = ("buffered", "pread")
CEILING_S = 1800.0  # the mount token's lifetime, as the live mount test mints it
SETTLE_POLL_S, SETTLE_TIMEOUT_S, LATE_S = 2.0, 60.0, 5.0
VISIBLE_TIMEOUT_S, READ_WAIT_S = 180.0, 300.0
DEFAULT_PLATFORM = Path("/mnt/volume2/expcore-federation/platform.toml")

READAHEAD = """
import errno, json, os, shutil, subprocess
mount, path = {mount!r}, {path!r}
out = {{}}
rows = [l.split() for l in open("/proc/self/mountinfo")]
line = next((r for r in rows if r[4] == mount), None)
if line:
    sep = line.index("-")
    out.update(mountinfo_dev=line[2], fstype=line[sep + 1], mount_options=line[5], super_options=line[sep + 3])
st = os.stat(path)
out["st_dev"] = "%d:%d" % (os.major(st.st_dev), os.minor(st.st_dev))
out["st_blksize"] = st.st_blksize
bdi = "/sys/class/bdi/" + out["st_dev"]
out["bdi_visible"] = os.path.isdir(bdi)
for name in ("read_ahead_kb", "min_ratio", "max_ratio", "strict_limit"):
    try:
        out[name] = open(os.path.join(bdi, name)).read().strip()
    except OSError as e:
        out[name] = "error " + errno.errorcode.get(e.errno, str(e.errno))
out["read_ahead_kb_access_w"] = os.access(os.path.join(bdi, "read_ahead_kb"), os.W_OK)
sys_line = next((r for r in rows if r[4] == "/sys"), None)
out["sys_mount_options"] = sys_line[5] if sys_line else None
out["cap_eff"] = next(l.split()[1] for l in open("/proc/self/status") if l.startswith("CapEff"))
tool = shutil.which("blockdev")
out["blockdev_present"] = bool(tool)
if tool:
    for label, target in (("file", path), ("mountpoint", mount)):
        r = subprocess.run([tool, "--getra", target], capture_output=True, text=True)
        said = (r.stdout if r.returncode == 0 else r.stderr).replace(path, "<file>").replace(mount, "<mount>").strip()
        out["blockdev_getra_" + label] = {{"exit": r.returncode, "said": said[-160:]}}
print(json.dumps(out))
"""

REPLAYER = r"""
import ctypes, errno, io, json, os, sys, time
mode, path, prog, ack, reads = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], json.loads(sys.argv[5])
def emit(rec):
    with open(prog, "a") as h:
        h.write(json.dumps(rec) + "\n")
t0 = time.perf_counter()
handle = open(path, "rb") if mode == "buffered" else None
fd = handle.fileno() if handle is not None else os.open(path, os.O_RDONLY)
st = os.fstat(fd)
opened = {"i": -1, "open_s": round(time.perf_counter() - t0, 5), "st_blksize": st.st_blksize, "size": st.st_size,
          "handle": type(handle).__name__ if handle is not None else "fd",
          "default_buffer_size": io.DEFAULT_BUFFER_SIZE}
# Whether the file maps shared (FUSE refuses that, ENODEV, for a file opened in direct-I/O mode); nothing is touched.
libc = ctypes.CDLL(None, use_errno=True)
libc.mmap.restype, libc.mmap.argtypes = ctypes.c_void_p, [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                                                          ctypes.c_int, ctypes.c_int, ctypes.c_long]
libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
got = libc.mmap(None, st.st_size, 1, 1, fd, 0)  # PROT_READ, MAP_SHARED
if got in (None, ctypes.c_void_p(-1).value):
    opened["mmap_shared"] = errno.errorcode.get(ctypes.get_errno(), "error")
else:
    opened["mmap_shared"] = "mapped"
    libc.munmap(ctypes.c_void_p(got), st.st_size)
emit(opened)
for i, (offset, length) in enumerate(reads):
    if i:
        while not os.path.exists("%s.%d" % (ack, i - 1)):
            time.sleep(0.005)
    began = time.perf_counter()
    try:
        if handle is not None:
            handle.seek(offset)
            data = handle.read(length)
        else:
            data = os.pread(fd, length, offset)
        emit({"i": i, "bytes": len(data), "seconds": round(time.perf_counter() - began, 5)})
    except OSError as e:
        emit({"i": i, "error": errno.errorcode.get(e.errno, str(e.errno)),
              "seconds": round(time.perf_counter() - began, 5)})
if handle is not None:
    handle.close()
else:
    os.close(fd)
emit({"i": "end"})
"""

LAUNCH = """
import json, subprocess, sys
p = subprocess.Popen([sys.executable, "-c", {child!r}, {mode!r}, {path!r}, {prog!r}, {ack!r}, {reads!r}],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=open({err!r}, "w"),
                     start_new_session=True)
print(json.dumps({{"pid": p.pid}}))
"""


class Log:
    """One JSON line per event, appended to `sweep.jsonl`; a short line per replay on stdout."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __call__(self, kind: str, **fields: Any) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"kind": kind, **fields}, default=str) + "\n")
        if kind not in ("read",):
            print(json.dumps({"kind": kind, **{k: v for k, v in fields.items() if not isinstance(v, (dict, list))}}))
            sys.stdout.flush()


def load_sequences(path: Path) -> list[dict[str, Any]]:
    """The files of a `read-sequences/1` file, each read inside its file; anything else is refused."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "read-sequences/1" or not data.get("files"):
        raise SystemExit("--sequences: not a read-sequences/1 file with files")
    out = []
    for f in data["files"]:
        size, reads = int(f["size_bytes"]), [(int(o), int(n)) for o, n in f["reads"]]
        if any(o < 0 or n <= 0 or o + n > size for o, n in reads):
            raise SystemExit(f"--sequences: file {f['id']} has a read outside its size")
        out.append({"id": str(f["id"]), "size": size, "reads": reads})
    return out


def cell(sandbox: DockerSandbox, code: str, timeout_s: float) -> dict:
    result = sandbox.exec(code, timeout_s)
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"cell_failed": True, "exit_code": result.exit_code, "timed_out": result.timed_out}


def settled(view: Provisioner, file_id: str, provider: str) -> tuple[list, float, bool]:
    """The provider's block map once two reads `SETTLE_POLL_S` apart agree, as the live mount test reads it."""
    began, last = time.monotonic(), None
    while True:
        now = merged(view.blocks(file_id, provider))
        if now == last:
            return now, round(time.monotonic() - began, 1), True
        if time.monotonic() - began > SETTLE_TIMEOUT_S:
            return now, round(time.monotonic() - began, 1), False
        last = now
        time.sleep(SETTLE_POLL_S)


def progress(path: Path, want: Any) -> dict | None:
    """The replayer's record with `i == want`, once its line is complete; None until then."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.split("\n")[:-1]:  # the last piece is empty or a line still being written
        rec = json.loads(line)
        if rec.get("i") == want:
            return rec
    return None


def wait_for(path: Path, want: Any) -> dict | None:
    deadline = time.monotonic() + READ_WAIT_S
    while time.monotonic() < deadline:
        rec = progress(path, want)
        if rec is not None:
            return rec
        time.sleep(0.02)
    return None


def scope_facts(host: DockerHost, sandbox: DockerSandbox) -> dict:
    """What `docker inspect` shows of each container's privileges: the sidecar's FUSE and SYS_ADMIN, none for the
    agent."""
    out = {}
    for role, name in (("sidecar", sandbox.names.sidecar), ("agent", sandbox.names.agent)):
        info = hostops.inspect_from(host.docker("inspect", name, check=False, timeout=30)) or {}
        config = info.get("HostConfig") or {}
        out[role] = {
            "devices": sorted(d.get("PathOnHost", "") for d in config.get("Devices") or []),
            "cap_add": sorted(config.get("CapAdd") or []),
            "cap_drop": sorted(config.get("CapDrop") or []),
            "security_opt": sorted(config.get("SecurityOpt") or []),
            "privileged": bool(config.get("Privileged")),
            "host_binds": len(config.get("Binds") or []),
        }
    return out


def replay(ctx: dict, mode: str, fixture: dict, log: Log) -> None:
    """One file's sequence replayed by one resident process in the agent, settled after every read."""
    sandbox, view, local, file_id = ctx["sandbox"], ctx["view"], ctx["local"], fixture["file_id"]
    reads, tag = fixture["reads"], f"{mode}-{fixture['id']}"
    work = sandbox.dir / "work"
    probes = PlatformProbes(ctx["client"], view, {tag: fixture["space_path"]}, ctx["providers"])
    probes.start({})
    before = merged(view.blocks(file_id, local))
    launched = cell(
        sandbox,
        LAUNCH.format(child=REPLAYER, mode=mode, path=fixture["inside"], prog=f"/work/{tag}.jsonl",
                      ack=f"/work/{tag}.ack", reads=json.dumps(reads), err=f"/work/{tag}.err"),
        60,
    )  # fmt: skip
    opened = wait_for(work / f"{tag}.jsonl", -1)
    common = {"setting": ctx["setting"], "mode": mode, "file": fixture["id"]}
    log("opened", **common, launched="pid" in launched, **(opened or {"no_answer": True}))
    agent, uid = sandbox.names.agent, str(ctx["agent_uid"])
    for i, (offset, length) in enumerate(reads):
        rec = wait_for(work / f"{tag}.jsonl", i)
        if rec is None:
            log("read", **common, i=i, offset=offset, length=length, no_answer_after_s=READ_WAIT_S)
            break
        after, settle_s, ok = settled(view, file_id, local)
        gained = merged(uncovered(before, after))
        log("read", **common, i=i, offset=offset, length=length, **{k: v for k, v in rec.items() if k != "i"},
            gained=[list(g) for g in gained], gained_bytes=held_bytes(gained), held_after=held_bytes(after),
            settle_s=settle_s, settled=ok)  # fmt: skip
        before = after
        host_ack = ctx["host"].docker(
            "exec", "-u", uid, agent, "touch", f"/work/{tag}.ack.{i}", check=False, timeout=30
        )
        if host_ack.returncode != 0:
            log("ack_failed", **common, i=i)
            break
    ended = wait_for(work / f"{tag}.jsonl", "end") is not None
    time.sleep(LATE_S)
    late = merged(view.blocks(file_id, local))
    snapshot = probes.finish()
    ctx["raw"].append({"tag": tag, "probes": snapshot})
    requested = merged((o, n) for o, n in reads)
    log(
        "replay",
        **common,
        ended=ended,
        reads=len(reads),
        requested_bytes=sum(n for _, n in reads),
        distinct_bytes=held_bytes(requested),
        placed_bytes=held_bytes(late),
        placed_beyond_reads=held_bytes(uncovered(requested, late)),
        read_not_placed=held_bytes(uncovered(late, requested)),
        late_gain_bytes=held_bytes(uncovered(before, late)),
        placed_ranges=[list(r) for r in late],
        probes_ok=snapshot.get("ok"),
        pairs=snapshot.get("pairs"),
        on_demand=snapshot.get("on_demand"),
        transfer_records=len(snapshot.get("transfers") or []),
        space_transfers={k: v for k, v in (snapshot.get("space_transfers") or {}).items() if not isinstance(v, list)},
    )


def make_fixtures(ctx: dict, files: SpaceFiles, sequences: list[dict], log: Log) -> list[dict]:
    """Fresh files, one per mode and sequence file, held by the holder only; the receiver holds none of each."""
    local, owner, top, run = ctx["local"], ctx["owner"], ctx["top"], ctx["run"]
    out = []
    for mode in MODES:
        for seq in sequences:
            name = f"{mode[0]}{seq['id'].lower()}-{uuid.uuid4().hex[:8]}.bin"
            file_id = files.make(owner, top, name, "REG")
            files.write(owner, file_id, 0, os.urandom(seq["size"]))
            out.append({"mode": mode, "id": seq["id"], "file_id": file_id, "reads": seq["reads"][: ctx["limit"]],
                        "size": seq["size"], "space_path": f"{run}/{name}",
                        "inside": f"{ctx['mount']}/{ctx['space']}/{run}/{name}"})  # fmt: skip
    view = ctx["view"]
    for f in out:
        done = consistency.converge(
            lambda f=f: _size(view, f["space_path"], local), lambda s, f=f: s == f["size"],
            timeout_s=VISIBLE_TIMEOUT_S, poll_s=2.0,
        )  # fmt: skip
        held = {p: held_bytes(merged(view.blocks(f["file_id"], p))) for p in ctx["providers"]}
        roles = {("receiver" if p == local else "holder" if p == owner else "other"): n for p, n in held.items()}
        log("fixture", setting=ctx["setting"], mode=f["mode"], file=f["id"], size=f["size"],
            visible=done.converged, held=roles)  # fmt: skip
        if not done.converged or roles["receiver"] != 0 or roles["holder"] != f["size"] or roles.get("other", 0):
            raise RuntimeError("a fixture file is not held by the holder alone, at its size")
    return out


def _size(view: Provisioner, path: str, provider: str) -> int | None:
    try:
        return view.resolve(path, provider=provider, wait=False).size
    except KeyError:
        return None


def sidecar_log(host: DockerHost, sandbox: DockerSandbox, spec: SandboxSpec, fixtures: list[dict]) -> dict:
    """oneclient's log files by size and line count, and how many lines name each fixture file; never the text."""
    sidecar, log_dir = sandbox.names.sidecar, SIDECAR_LOG_DIR  # the sandbox's own, bound from the run (O05)
    listed = host.docker("exec", sidecar, "find", log_dir, "-type", "f", "-printf", "%s %p\\n", check=False, timeout=60)
    files, text = [], []
    for line in listed.stdout.splitlines():
        size, _, path = line.partition(" ")
        body = host.docker("exec", sidecar, "cat", path, check=False, timeout=120).stdout
        files.append({"name": os.path.relpath(path, log_dir), "bytes": int(size), "lines": body.count("\n")})
        text.append(body)
    forms = {f"{f['mode']}-{f['id']}": {"id": f["file_id"], "uuid": file_guid(f["file_id"])} for f in fixtures}
    return {"files": files, "lines_naming_each_file": mentions("\n".join(text), forms)}


def help_check(host: DockerHost, sandbox: DockerSandbox, args: list[str]) -> dict:
    shown = host.docker("exec", sandbox.names.sidecar, ONECLIENT_BIN, "--help", check=False, timeout=60)
    return {a.split("=")[0]: (a.split("=")[0] in shown.stdout + shown.stderr) for a in args}


def run_setting(name: str, args: argparse.Namespace, sequences: list[dict], images: dict, log: Log) -> None:
    sdir = args.out / name
    sdir.mkdir()
    facts = json.loads((args.platform.parent / "facts.json").read_text(encoding="utf-8"))
    source = load_platform(args.platform, require_secrets=False)
    local = source.support.local
    table = sandbox_table(
        work_root=sdir / "work", images=images, pool=args.pool, network=federation_network(facts), local=local,
        local_address=facts["providers"][local]["domain_on_project_network"], extra_args=SETTINGS[name],
    )  # fmt: skip
    config = load_platform(derive_platform(args.platform, sdir, table))
    owner = next(p for p in config.support.providers if p != local)
    host = DockerHost(sudo_command=config.sandbox.sudo)
    client = OnezoneClient(config)
    placement = load_placement(config.file, config).settings
    view = Provisioner(client, config, dataclasses.replace(placement, holder=local),
                       provider_ids=resolve_provider_ids(config), log=sdir / "never-written.jsonl")  # fmt: skip
    files = SpaceFiles(config)
    run = f"expcore-sweep-{uuid.uuid4().hex[:8]}"
    ctx: dict[str, Any] = {"setting": name, "local": local, "owner": owner, "run": run, "view": view, "client": client,
                           "providers": list(config.support.providers), "mount": config.sandbox.mount.path,
                           "space": config.space.name, "limit": args.limit or None, "host": host, "raw": [],
                           "agent_uid": config.sandbox.agent_uid}  # fmt: skip
    log("setting", setting=name, oneclient_args=SETTINGS[name])
    try:
        ctx["top"] = files.make(local, files.root(local), run, "DIR")
        seen = consistency.converge(
            lambda: files.seen(owner, ctx["top"]), bool, timeout_s=VISIBLE_TIMEOUT_S, poll_s=2.0
        )
        if not seen.converged:
            raise RuntimeError("the setting's directory did not reach the holder")
        fixtures = make_fixtures(ctx, files, sequences, log)
        client_token = TokenIssuer(client, config).mint(
            data_path_spec(config.space.id, "oneclient"), config.principals.data_path, ceiling_s=CEILING_S
        )
        spec = SandboxSpec(
            run_key=run,
            mode=DataPathMode.MOUNT,
            token=client_token,
            zone=Endpoint.from_url(config.zone.url),
            provider=Endpoint.from_url(config.local_provider.url),
            lease_key=LEASE_KEY,
        )
        began = time.monotonic()
        try:
            sandbox = DockerSandbox.start(host, config.sandbox, spec)
        except SandboxError as error:
            log("start_failed", setting=name, error=str(error)[:300], seconds=round(time.monotonic() - began, 1))
            return
        ctx["sandbox"] = sandbox
        try:
            log("started", setting=name, seconds=round(time.monotonic() - began, 1), scope=scope_facts(host, sandbox),
                options_in_help=help_check(host, sandbox, SETTINGS[name]))  # fmt: skip
            log(
                "readahead",
                setting=name,
                **cell(sandbox, READAHEAD.format(mount=ctx["mount"], path=fixtures[0]["inside"]), 60),
            )
            for fixture in fixtures:
                replay(ctx, fixture["mode"], fixture, log)
            log("sidecar_log", setting=name, **sidecar_log(host, sandbox, spec, fixtures))
        finally:
            try:
                sandbox.stop()
                log("stopped", setting=name, ok=True)
            except SandboxError as error:
                log("stopped", setting=name, ok=False, error=str(error)[:300])
    finally:
        (sdir / "probes.json").write_text(json.dumps(ctx["raw"], default=str), encoding="utf-8")
        log("cleanup", setting=name, failures=files.remove_all())
        files.close()
        client.close()
        left = [
            host.docker(*cmd, check=False, timeout=30).stdout.split()
            for cmd in (
                ("ps", "-a", "-q", "--filter", "name=fedsbx"),
                ("network", "ls", "-q", "--filter", "name=fedsbx"),
            )
        ]
        fuse = [m for m in hostops.fuse_mounts(host.read_text("/proc/self/mountinfo"))
                if m["mountpoint"].startswith(str(sdir))]  # fmt: skip
        log("left", setting=name, containers=len(left[0]), networks=len(left[1]), fuse_mounts=len(fuse))
        if left[0] or left[1] or fuse:
            raise SystemExit("something of the sandbox was left; the sweep stops here")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sequences", type=Path, required=True, help="a read-sequences/1 file")
    parser.add_argument("--pool", required=True, help="a private IPv4 range the host routes nowhere")
    parser.add_argument("--out", type=Path, required=True, help="a new directory for every file the sweep writes")
    parser.add_argument("--platform", type=Path, default=DEFAULT_PLATFORM, help="the federation's own platform.toml")
    parser.add_argument(
        "--oneclient-image", default=ONECLIENT_IMAGE, help="a oneclient image on the host, by name or id"
    )
    parser.add_argument("--settings", default=",".join(SETTINGS), help="settings to run, by name, comma-separated")
    parser.add_argument("--limit", type=int, default=0, help="replay only the first N reads of each file (a smoke run)")
    args = parser.parse_args()
    chosen = [s for s in args.settings.split(",") if s]
    unknown = [s for s in chosen if s not in SETTINGS]
    if unknown:
        raise SystemExit(f"--settings: unknown {unknown}; known {list(SETTINGS)}")
    sequences = load_sequences(args.sequences)
    args.out.mkdir(parents=True)
    log = Log(args.out / "sweep.jsonl")
    log("input", sha256=hashlib.sha256(args.sequences.read_bytes()).hexdigest(),
        files=[{"id": s["id"], "size": s["size"], "reads": len(s["reads"])} for s in sequences])  # fmt: skip
    host = DockerHost()
    images = {"agent": (AGENT_IMAGE, host.image_id(AGENT_IMAGE)),
              "oneclient": (args.oneclient_image, host.image_id(args.oneclient_image))}  # fmt: skip
    for name in chosen:
        run_setting(name, args, sequences, images, log)


if __name__ == "__main__":
    main()
