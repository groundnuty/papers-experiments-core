#!/usr/bin/env python3
"""hpctests.py: run a pytest selection of this repository, at a commit, as one step of a standing Slurm allocation,
and bring its results back (README.md).

    hpctests.py install                         put alloc.sh and the login node's settings in the login directory
    hpctests.py submit [--begin ISO]            submit one allocation of ours (read back, cancelled if not as asked)
    hpctests.py jobs                            our allocations, their ends in UTC, and the renewer's state
    hpctests.py release JOB                     cancel one allocation of ours (the ledger's, under our name)
    hpctests.py renew start --until ISO | stop  the login node's renewal loop (README, "Renewal")
    hpctests.py run [--rev REV] [--cores N] [--mem SIZE] [--time MIN] [--job JOB] -- <pytest arguments>
    hpctests.py clean [--stale MIN | --all]     remove what killed steps left on the allocation's node

Settings come from a local site.env (mode 0600; HPCT_SITE or --site names it; site.env.example lists the keys). The
ssh destination is passed to ssh and never printed. Exit of `run`: pytest's own status when results came back;
64 a setting or an argument; 69 no allocation fits; 70 no results came back; 71 the results failed their SHA-256;
72 the step refused to start; 73 the clone failed; 74 the environment failed; 97 the self-check failed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

import hpctestlib as lib
import hpctestparse as parse

HERE = Path(__file__).resolve().parent
SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=20")
SSH_OPTIONS += ("-o", "ServerAliveCountMax=6", "-T")
GRACE_S = 30.0  # after the end marker: the srun client of a finished step can hang (seen live); it is ended then
WAIT_FOR_CORES_S = 3600  # how long a step may wait for free cores (--exact queues it) before the run gives up


def private_text(path: Path, what: str) -> str:
    """A file only its owner can read (a site file names the cluster), else refused."""
    if path.stat().st_mode & 0o077:
        raise lib.HpcTestsError(f"{what} {path} must be readable by its owner only (chmod 600)")
    return path.read_text(encoding="utf-8")


def load_site(path: Path) -> lib.Site:
    values = lib.parse_env_file(private_text(path, "site.env"))
    host_text = None
    if values.get("HPCT_HOST_FILE"):
        host_text = private_text(Path(values["HPCT_HOST_FILE"]), "HPCT_HOST_FILE")
    return lib.site_from(values, host_text)


class Remote:
    """ssh to the login node (the tests put a fake `ssh` first on PATH)."""

    def __init__(self, site: lib.Site) -> None:
        self.site = site

    def argv(self, command: str) -> list[str]:
        return ["ssh", *SSH_OPTIONS, self.site.host, command]

    def run(self, command: str, stdin: bytes = b"", timeout: float = 180) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(self.argv(command), input=stdin, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise lib.HpcTestsError(f"the login node did not answer within {timeout:.0f} s") from None

    def alloc(self, *words: str) -> subprocess.CompletedProcess[bytes]:
        return self.run(shlex.join(["bash", f"{self.site.login_dir}/alloc.sh", *words]))


def git(repo: Path, *args: str) -> str:
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise lib.HpcTestsError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout.strip()


def build_bundle(repo: Path, sha: str, work: Path) -> Path:
    """The commit's history as a git bundle, with origin/main beside it when the repository has it. It is made in a
    temporary repository that borrows the objects (alternates), so no ref of the repository itself is created."""
    common = Path(git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    ship = work / "ship.git"
    subprocess.run(["git", "init", "-q", "--bare", str(ship)], check=True)
    (ship / "objects" / "info" / "alternates").write_text(f"{common / 'objects'}\n", encoding="utf-8")
    refs = [f"refs/heads/hpct-{sha[:12]}"]
    git(ship, "update-ref", refs[0], sha)
    main = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", "refs/remotes/origin/main"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if main:
        git(ship, "update-ref", "refs/remotes/origin/main", main)
        refs.append("refs/remotes/origin/main")
    bundle = work / "repo.bundle"
    git(ship, "-c", "pack.threads=2", "bundle", "create", "-q", str(bundle), *refs)
    return bundle


def write_payload(work: Path, files: dict[str, bytes], bundle: Path | None) -> tuple[Path, str]:
    """The tar the step reads from stdin: node.sh, its settings, the pytest arguments and the bundle."""
    payload = work / "payload.tar"
    with tarfile.open(payload, "w", format=tarfile.PAX_FORMAT) as tar:
        for name, data in {"node.sh": (HERE / "node.sh").read_bytes(), **files}.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            tar.addfile(info, io.BytesIO(data))
        if bundle is not None:
            tar.add(bundle, arcname="repo.bundle")
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    return payload, digest


def stream_step(remote: Remote, step: lib.Step, payload: Path, bound_s: float, transport: Path) -> parse.Streamed:
    """Run the step with the payload on its stdin, read what it prints, and end ssh once the step has said all."""
    command = lib.remote_command(step.argv())
    reader = parse.StreamParser()
    with payload.open("rb") as stdin, transport.open("wb") as stderr:
        process = subprocess.Popen(
            remote.argv(command),
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=stderr,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        bound = threading.Timer(bound_s, process.kill)
        bound.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                if line.startswith(lib.LOG + " "):
                    print(f"node: {line[len(lib.LOG) + 1 :].rstrip()}", flush=True)
                reader.feed(line)
                if reader.done:
                    break
            try:
                process.wait(GRACE_S)
            except subprocess.TimeoutExpired:
                print("the step has finished but its srun client did not return: ending it", flush=True)
                process.terminate()
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        finally:
            bound.cancel()
            if process.stdout is not None:
                process.stdout.close()
    return reader.result()


def tail(path: Path, lines: int = 20) -> str:
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]) if path.exists() else ""


def report(streamed: parse.Streamed, out: Path, meta: dict[str, object]) -> int:
    """Unpack the results into `out`, print what pytest said, write run.json, and return the run's exit status."""
    if streamed.failure is not None:
        print(f"the step refused to start: {streamed.failure}")
        rc = 72
    elif streamed.archive is None or streamed.rc is None:
        print(
            "no results came back (the step failed, or ssh dropped); transport messages:\n"
            + tail(out / "transport.log")
        )
        print("a killed step can leave its directory on the node: hpctests.py clean --stale 60 removes it")
        rc = 70
    else:
        (out / "results.tgz").write_bytes(streamed.archive)
        with tarfile.open(out / "results.tgz") as tar:
            tar.extractall(out, filter="data")
        rc = streamed.rc
        res = out / "results"
        log = (
            (res / "pytest.log").read_text(encoding="utf-8", errors="replace") if (res / "pytest.log").exists() else ""
        )
        if (res / "junit.xml").exists():
            meta["junit"] = parse.junit_counts((res / "junit.xml").read_bytes())
        meta["summary"] = parse.summary_line(log)
        meta["times"] = dict(x.split("=", 1) for x in tail(res / "times").splitlines() if "=" in x)
        for line in parse.short_summary(log):
            print(f"  {line}")
        for name, code in (("clone.log", 73), ("env.log", 74), ("check.log", 97)):
            if rc == code:
                print(f"{name}:\n{tail(res / name)}")
        print(f"pytest: {meta['summary'] or '(no summary line)'}; exit {rc}; times {meta['times']}")
    meta["rc"] = rc
    (out / "run.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"results: {out}")
    return rc


def listing(remote: Remote) -> tuple[int, list[parse.Job], dict[str, str]]:
    answer = remote.alloc("list")
    if answer.returncode != 0:
        raise lib.HpcTestsError(f"alloc.sh list failed: {answer.stderr.decode(errors='replace').strip()}")
    return parse.parse_listing(answer.stdout.decode(errors="replace"))


def cmd_run(site: lib.Site, remote: Remote, args: argparse.Namespace) -> int:
    repo = Path(args.repo).resolve()
    sha = git(repo, "rev-parse", "--verify", "--end-of-options", f"{args.rev}^{{commit}}")
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        print(f"note: {repo} has uncommitted changes; they are NOT shipped, only commit {sha[:12]} is")
    if not site.uv:
        raise lib.HpcTestsError("site.env gives no HPCT_UV (the uv binary on the nodes)")
    if not 5 <= args.time <= 720 or not 1 <= args.cores <= 64:
        raise lib.HpcTestsError("--time is 5 to 720 minutes and --cores 1 to 64")
    memory = lib.memory_mib(args.mem)
    now, jobs, _ = listing(remote)
    job = parse.pick_job(jobs, now, args.time * 60 + 120, args.job)
    run_id = f"{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}-{sha[:10]}-{secrets.token_hex(2)}"
    out = Path(args.out or site.out or "") / run_id
    if not (args.out or site.out):
        raise lib.HpcTestsError("give --out or HPCT_OUT: the directory the results are written under")
    out.mkdir(parents=True, mode=0o700)
    settings = lib.node_settings(
        sha=sha,
        uv=site.uv,
        uv_version=site.uv_version,
        python=site.python,
        timeout_s=args.time * 60 - 60,
        cores=args.cores,
        keep=args.keep,
    )
    pytest_args = "\0".join(args.pytest).encode() + (b"\0" if args.pytest else b"")
    print(f"run {run_id}: commit {sha[:12]} on job {job.id} ({args.cores} cores, {args.mem}, {args.time} min)")
    work = Path(tempfile.mkdtemp(prefix="hpct-", dir=site.work or None))
    try:
        files = {"settings": settings.encode(), "pytest-args": pytest_args}
        payload, digest = write_payload(work, files, build_bundle(repo, sha, work))
        step = lib.Step(
            job.id,
            f"hpct-{run_id}",
            args.cores,
            memory,
            args.time,
            lib.allowlist(lib.step_environment(site)),
            lib.boot_command(site.node_root, run_id, digest, "run"),
        )
        meta: dict[str, object] = {"run": run_id, "commit": sha, "job": job.id, "cores": args.cores, "mem": args.mem}
        meta["pytest_args"] = args.pytest
        try:
            streamed = stream_step(remote, step, payload, args.time * 60 + WAIT_FOR_CORES_S, out / "transport.log")
        except lib.HpcTestsError as error:
            print(f"the results did not verify: {error}")
            return 71
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return report(streamed, out, meta)


def cmd_clean(site: lib.Site, remote: Remote, args: argparse.Namespace) -> int:
    now, jobs, _ = listing(remote)
    job = parse.pick_job(jobs, now, 600, args.job)
    run_id = f"clean-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(2)}"
    mode = "all" if args.all else str(args.stale)
    work = Path(tempfile.mkdtemp(prefix="hpct-", dir=site.work or None))
    try:
        payload, digest = write_payload(work, {"settings": b""}, None)
        step = lib.Step(
            job.id,
            f"hpct-{run_id}",
            1,
            512,
            10,
            lib.allowlist(lib.step_environment(site)),
            lib.boot_command(site.node_root, run_id, digest, "clean", mode),
        )
        streamed = stream_step(remote, step, payload, 1200, work / "transport.log")
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if streamed.failure:
        print(f"the clean step refused: {streamed.failure}")
        return 72
    return 0


def cmd_install(site: lib.Site, remote: Remote) -> int:
    directory = shlex.quote(site.login_dir)
    files = {"alloc.sh": (HERE / "alloc.sh").read_bytes(), "login.env": lib.login_env(site).encode()}
    for name, data in files.items():
        target = shlex.quote(f"{site.login_dir}/{name}")
        script = (
            f"umask 077 && mkdir -p {directory} && chmod 0700 {directory}"
            f' && [ "$(stat -c %u {directory})" = "$(id -u)" ] && cat > {target}.new && sha256sum {target}.new'
        )
        answer = remote.run(script, stdin=data)
        got = answer.stdout.decode(errors="replace").split()[:1]
        if answer.returncode != 0 or got != [hashlib.sha256(data).hexdigest()]:
            remote.run(f"rm -f {target}.new")
            raise lib.HpcTestsError(f"{name} did not arrive intact in the login directory")
        if remote.run(f"mv -f {target}.new {target}").returncode != 0:
            raise lib.HpcTestsError(f"cannot put {name} in place in the login directory")
        print(f"installed {name} (sha256 {got[0][:12]}) in the login directory {site.login_dir}")
    return 0


def utc(epoch: int | None) -> str:
    return "-" if epoch is None else f"{dt.datetime.fromtimestamp(epoch, dt.UTC):%Y-%m-%dT%H:%M:%SZ}"


def epoch_of(text: str) -> int:
    """An ISO time; one without a zone is read as UTC."""
    when = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return int((when if when.tzinfo else when.replace(tzinfo=dt.UTC)).timestamp())


def cmd_jobs(remote: Remote) -> int:
    now, jobs, renewer = listing(remote)
    print(f"login node clock {utc(now)}")
    for job in jobs:
        left = "" if job.end is None else f" ({(job.end - now) // 60} min left)"
        print(
            f"job {job.id} {job.name} {job.state} start {utc(job.start)} end {utc(job.end)}{left}"
            f" {job.cpus} cores {job.mem}"
        )
    if not jobs:
        print("no allocation of ours is alive")
    beat = renewer.get("heartbeat", "")
    beat_text = utc(int(beat)) if beat.isdigit() else "-"
    until = renewer.get("until", "")
    print(
        f"renewer: {renewer.get('status', 'none')}, heartbeat {beat_text},"
        f" until {utc(int(until)) if until.isdigit() else '-'}"
    )
    running = [j for j in jobs if j.state == "RUNNING" and j.end is not None]
    queued = [j for j in jobs if j.state != "RUNNING"]
    if running and not queued and max(j.end or 0 for j in running) - now < 7200 and renewer.get("status") != "running":
        print("WARNING: the allocation ends within 2 h, no successor is queued and no renewer runs (README, Renewal)")
    return 0


def passthrough(answer: subprocess.CompletedProcess[bytes]) -> int:
    sys.stdout.write(answer.stdout.decode(errors="replace"))
    sys.stderr.write(answer.stderr.decode(errors="replace"))
    return answer.returncode


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="hpctests.py", description=__doc__.split("\n\n")[0])
    top.add_argument("--site", default=os.environ.get("HPCT_SITE", str(HERE / "site.env")))
    sub = top.add_subparsers(dest="command", required=True)
    sub.add_parser("install")
    sub.add_parser("jobs")
    submit = sub.add_parser("submit")
    submit.add_argument("--begin", help="ISO time (UTC unless it has a zone) before which Slurm does not start it")
    release = sub.add_parser("release")
    release.add_argument("job")
    renew = sub.add_parser("renew")
    renew.add_argument("action", choices=["start", "stop", "once"])
    renew.add_argument("--until", help="ISO time (UTC) after which no allocation is submitted (start, once)")
    renew.add_argument("--max", type=int, default=4, help="at most this many submits (start)")
    run = sub.add_parser("run")
    run.add_argument("--repo", default=".", help="the repository or worktree whose commit is shipped")
    run.add_argument("--rev", default="HEAD", help="the commit (only committed work ships)")
    run.add_argument("--cores", type=int, default=4)
    run.add_argument("--mem", default="6G")
    run.add_argument("--time", type=int, default=60, help="the step's time limit in minutes")
    run.add_argument("--job", help="an allocation of ours (default: the running one that ends last)")
    run.add_argument("--out", help="the directory results are written under (default HPCT_OUT)")
    run.add_argument("--keep", action="store_true", help="leave the run's directory on the node (clean removes it)")
    run.add_argument("pytest", nargs="*", help="after --: pytest's arguments (paths, -k, -m ...)")
    clean = sub.add_parser("clean")
    clean.add_argument("--job")
    group = clean.add_mutually_exclusive_group()
    group.add_argument("--stale", type=int, default=60, help="remove dead runs older than this many minutes")
    group.add_argument("--all", action="store_true", help="also the uv cache and Python, when no run is live")
    return top


def dispatch(args: argparse.Namespace) -> int:
    site = load_site(Path(args.site))
    remote = Remote(site)
    if args.command == "install":
        return cmd_install(site, remote)
    if args.command == "jobs":
        return cmd_jobs(remote)
    if args.command == "submit":
        return passthrough(remote.alloc("submit", *(["--begin", str(epoch_of(args.begin))] if args.begin else [])))
    if args.command == "release":
        return passthrough(remote.alloc("release", args.job))
    if args.command == "renew":
        if args.action == "stop":
            return passthrough(remote.alloc("renew-stop"))
        if not args.until:
            raise lib.HpcTestsError("renew start and once need --until")
        words = ["--until", str(epoch_of(args.until)), "--max", str(args.max)]
        if args.action == "once":
            return passthrough(remote.alloc("renew", *words, "--once"))
        return passthrough(remote.alloc("renew-start", *words))
    if args.command == "clean":
        return cmd_clean(site, remote, args)
    return cmd_run(site, remote, args)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return dispatch(args)
    except lib.NoAllocation as error:
        print(f"hpctests: {error}", file=sys.stderr)
        return 69
    except lib.HpcTestsError as error:
        print(f"hpctests: {error}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    sys.exit(main())
