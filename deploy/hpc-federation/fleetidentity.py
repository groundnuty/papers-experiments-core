"""fleetidentity.py: what the elastic controller reads for a federation's join and for a lost lease's spend (#462 step
6; README.md, "The elastic fleet's worker"). `fleetcontrol.py control` builds both from its sites file's `settings` (the
fleet's shard settings, `fleetplan.read_settings`).

- `identity_reader(settings, sites)`: `(site label, member) -> (campaign fingerprint's SHA-256, environment identity,
  platform fingerprint's SHA-256)` or None. Only for a member this fleet's up made and holds, up and linked
  (`fleetrecord.identity`): the study is copied into a private directory with the federation's own `platform.toml`
  beside it, as a shard step places it, and the manifest's Python (`-I -B`, an environment of the allowlist and a home
  of its own) asks the core for what a start there pins (`expcore.study.pipeline.campaign_member_identity`): the
  fingerprint (hashed as the lease's `campaign_sha256` is), the environment identity a lease store pins, whose digest
  the ledger pins at its first join, and the SHA-256 of the federation's own platform fingerprint, which a campaign
  that declares its platforms admits or refuses (C50). A read that fails is None (the join waits and is decided
  again), never a guess; a read that worked is kept for the member.
- `locator(evidence)`: `(site, Settle) -> (out/, source)` or None, for `fleetexecute.dead_store_reader`: the lost
  lease's store as a place `lease-<id>/` holds it (`PLACES`): in the federation's channel on the site's shared file
  system (`root`, whatever the member's root: the channel is outside it and outlives the member's down, and at a settle
  the member's job is proven ended, so its journal there is the final one) or, where the place is gone or holds no
  snapshot that verifies (never after a failed call: read again next tick), in the evidence the controller collected
  from the member (`pulled`; at an ssh site its retirement pulled the member's places there before its down,
  `fleetssh.SshSiteTransport.pull_places`). A place holds
  either `study/out/` with the lease's `spend.jsonl` beside `study/` (put there by an operator), or what the worker
  leaves while the lease runs (C46): `snapshots/` and `spend.jsonl`. For those the newest snapshot that verifies
  (`out_snapshot.restore`, each in turn, newest first) is restored into a private directory on this host with the
  journal copied beside it, as the first form is read, and the reader settles from them; the directory is removed at
  the next read and at exit. At a site whose transport is ssh the place is on its login node: its newest snapshot is
  streamed here as a tar (`tar -C <snapshot> -cf - .`, extracted with the `data` filter) and its journal copied, into a
  private 0700 directory removed afterwards, and read as a local place is (each snapshot verified in turn, newest
  first). A call that fails or times out reads nothing this tick: the lease stays charged its grant and is read again
  next tick, never taken for a place with nothing in it.
- `fleetidentity.py read --fleet DIR --member fed-k --settings FILE`: `read_identity` run on a site's login node, its
  one JSON line printed (exit 1 when there is none). The controller runs it there through the transport for an ssh
  site, whose fleet, root and manifest's Python are on the login node and not on this host.
"""

import argparse
import atexit
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
PLACES = "places"  # in a federation's channel, and in a member's collected evidence: <PLACES>/lease-<id:012d>/
PROGRAM = (
    "import json, sys\n"
    "from expcore.campaign.manifest import fingerprint_sha256\n"
    "from expcore.study.pipeline import campaign_member_identity\n"
    "fingerprint, environment, platform = campaign_member_identity(sys.argv[1])\n"
    "print(json.dumps({'fingerprint': fingerprint_sha256(fingerprint), 'environment': environment,\n"
    "                  'platform': platform}, sort_keys=True))\n"
)
TIMEOUT_S = 600.0


def _modules() -> Any:
    for path in (HERE, HERE.parents[1] / "deploy" / "compose-federation"):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import fleetplan
    import fleetrecord

    return fleetplan, fleetrecord


def read_identity(
    fleet: Path, member: str, settings_path: Path, runner: Callable[..., Any] = subprocess.run
) -> Optional[Tuple[str, Mapping[str, str], Optional[str]]]:  # fmt: skip
    """One member's (fingerprint, environment identity), read now; None as the module says."""
    fleetplan, fleetrecord = _modules()
    shards = fleetplan.shards
    try:
        found = fleetrecord.member_of(fleetrecord.Record(fleet).read(), member)
        why, _ = fleetrecord.identity(fleet, found)
        if why is not None or found["status"] != "up":
            return None
        settings = fleetplan.read_settings(settings_path)
        if settings.get("home_files") and shards.home_files_overlap_problem(
            settings["home_files"], Path(settings["study"])
        ):
            return None  # the key tree lies in the study: never copied here (the worker refuses it as well)
        root = fleetrecord.root_of(fleet, member)
        scratch = Path(tempfile.mkdtemp(prefix="expcore-identity-", dir=str(fleet)))
    except (fleetrecord.FleetError, OSError, ValueError, TypeError):  # TypeError: a settings value of the wrong kind
        return None
    try:
        for name in ("home", "tmp", *(f"home/{sub}" for sub in shards.HOME_DIRS)):
            (scratch / name).mkdir(mode=0o700)
        copy = scratch / "study"
        shutil.copytree(settings["study"], copy, symlinks=True)
        if os.path.lexists(copy / shards.PLATFORM) or os.path.lexists(copy / "out"):
            return None  # the step places the federation's platform file there, and a study has no out/ of its own
        shutil.copyfile(root / shards.PLATFORM, copy / shards.PLATFORM)
        env = {"PATH": settings["step_path"], "LC_ALL": "C", "EXPCORE_FED_ROOT_PARENT": str(root.parent),
               "EXPCORE_FED_ROOT": str(root), **shards.home_env(str(scratch / "home")),
               "TMPDIR": str(scratch / "tmp")}  # fmt: skip
        done = runner([settings["python"], "-I", "-B", "-c", PROGRAM, str(copy)], cwd=str(scratch), env=env,
                      capture_output=True, text=True, timeout=TIMEOUT_S, check=False)  # fmt: skip
        if done.returncode != 0:
            return None
        return said_identity(json.loads(done.stdout.strip().splitlines()[-1]))
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def read_identity_remote(
    transport: Any, site: Any, member: str
) -> Optional[Tuple[str, Mapping[str, str], Optional[str]]]:  # fmt: skip
    """One member's identity read on an ssh site's login node (`fleetidentity.py read` there, over the transport); None
    for any failure, a call that cannot be made or that is past its bound included: the join waits."""
    settings = site.shard_settings
    if transport is None or settings is None:
        return None
    argv = site.script("fleetidentity.py", "read", "--fleet", str(site.fleet), "--member", member, "--settings",
                       str(settings))  # fmt: skip
    try:
        code, out, _ = transport.run(site, argv, TIMEOUT_S + 60)  # past the read's own bound there
        found = said_identity(json.loads(out.strip().splitlines()[-1]))
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return None
    return found if code == 0 else None


def said_identity(said: Any) -> Optional[Tuple[str, Mapping[str, str], Optional[str]]]:
    """The (fingerprint, environment identity, platform fingerprint) a read printed, or None when it is not that: the
    platform is the SHA-256 of the federation's own platform fingerprint, which a campaign that declares its platforms
    admits at the join or refuses (C50); None from a core that prints none, which only such a campaign refuses."""
    digest, environment, platform = said["fingerprint"], said["environment"], said.get("platform")
    if not isinstance(digest, str) or not isinstance(environment, dict) or not isinstance(platform, (str, type(None))):
        return None
    return digest, {str(k): str(v) for k, v in environment.items()}, platform


def identity_reader(
    settings_path: Path, sites: Mapping[str, Any], runner: Callable[..., Any] = subprocess.run, transport: Any = None
) -> Callable[[str, str], Optional[Tuple[str, Mapping[str, str], Optional[str]]]]:  # fmt: skip
    """The controller's identity hook over the sites' fleets (`read_identity`, on the login node through `transport`
    for an ssh site); a member's read that worked is kept."""
    kept: dict = {}

    def read(site: str, member: str) -> Optional[Tuple[str, Mapping[str, str], Optional[str]]]:
        found = kept.get((site, member))
        if found is None and site in sites:
            if getattr(sites[site], "remote", False):
                found = read_identity_remote(transport, sites[site], member)
            else:
                found = read_identity(sites[site].fleet, member, settings_path, runner)
            if found is not None:
                kept[(site, member)] = found
        return found

    return read


def locator(evidence: Path, transport: Any = None) -> Callable[[Any, Any], Optional[Tuple[Path, str]]]:
    """The spend reader's `locate` (module docstring): where a lost lease's store is, or None. `transport` reaches an
    ssh site's login node (`fleetexecute.SiteTransport`)."""
    restored: list = []  # the private directory of the last read: removed at the next one and at exit

    def release() -> None:
        while restored:
            shutil.rmtree(restored.pop(), ignore_errors=True)

    atexit.register(release)

    def from_snapshot(place: Path) -> Optional[Path]:
        """The newest snapshot of `place` that verifies, restored as `study/out` beside the place's journal."""
        from expcore.campaign import out_snapshot
        from expcore.campaign.elastic.snapshots import SNAPSHOTS
        from expcore.campaign.run_lease import LEASE_JOURNAL

        try:
            found = out_snapshot.snapshots(place / SNAPSHOTS)
        except OSError:
            return None
        for snapshot in found:  # a snapshot that does not verify is passed over for the one before it
            release()
            private = Path(tempfile.mkdtemp(prefix="expcore-locate-"))
            restored.append(private)
            try:
                out_snapshot.restore(snapshot, private / "study" / "out")
                if (place / LEASE_JOURNAL).is_file():
                    shutil.copyfile(place / LEASE_JOURNAL, private / LEASE_JOURNAL)
            except (out_snapshot.OutSnapshotError, OSError):
                continue
            return private / "study" / "out"
        return None

    def from_remote(site: Any, action: Any) -> Tuple[Optional[Path], bool]:
        """A lost lease's place on an ssh site's login node, fetched into a private directory here and read as a local
        place is: (its `out/`, False) when found; (None, False) when the place is not there (the answer of a listing,
        never a failed call's) or holds no snapshot that verifies; (None, True) for any failure or timeout of a call,
        which is read again next tick and never taken for no place."""
        from fleetssh import SNAPSHOT_NAME

        from expcore.campaign import out_snapshot
        from expcore.campaign.elastic import channel as ch
        from expcore.campaign.elastic.snapshots import SNAPSHOTS
        from expcore.campaign.elastic.transport import Missing
        from expcore.campaign.run_lease import LEASE_JOURNAL

        there = f"{ch.federation_dir(str(site.channel), action.label) / PLACES / ch.place_name(action.lease_id)}"
        private = Path(tempfile.mkdtemp(prefix="expcore-fetch-"))  # 0700, ours
        mirror = private / "place"
        try:
            if not transport.exists(site, "-d", there):
                return None, False
            names = sorted(transport.names(site, f"{there}/{SNAPSHOTS}", SNAPSHOT_NAME), key=lambda n: int(n[5:]),
                           reverse=True)  # fmt: skip
            if not names:
                return None, False
            (mirror / SNAPSHOTS).mkdir(parents=True)
            with contextlib.suppress(Missing):  # a journal not there is read as none; a failed or timed-out call is not
                transport.stream(site, ["cat", "--", f"{there}/{LEASE_JOURNAL}"], mirror / LEASE_JOURNAL)
            for name in names[: out_snapshot.KEEP]:
                tar = private / f"{name}.tar"
                transport.stream(site, ["tar", "-C", f"{there}/{SNAPSHOTS}/{name}", "-cf", "-", "."], tar)
                with tarfile.open(tar, "r:") as archive:
                    archive.extractall(mirror / SNAPSHOTS / name, filter="data")
                tar.unlink()
                found = from_snapshot(mirror)
                if found is not None:
                    return found, False
            return None, False
        except (OSError, tarfile.TarError, ValueError, subprocess.SubprocessError):
            return None, True
        finally:
            shutil.rmtree(private, ignore_errors=True)

    def locate(site: Any, action: Any) -> Optional[Tuple[Path, str]]:
        if site is None:
            return None
        from expcore.campaign.elastic import channel as ch

        name = ch.place_name(action.lease_id)
        candidates = []
        remote = getattr(site, "remote", False)
        # the channel first, whatever the member's root: it is outside the root, outlives the member's down, and at a
        # settle its job is proven ended (and KillWait passed), so its journal is the final one. The pulled copy is
        # read where the place is gone or holds no snapshot that verifies, never after a failed call: it was taken
        # earlier, so a spend written after it would be missed, and a settled charge is final
        if site.channel is not None and remote and transport is not None:
            found, failed = from_remote(site, action)
            if found is not None:
                return found, "root"
            if failed:
                return None
        elif site.channel is not None and not remote:
            place = ch.federation_dir(str(site.channel), action.label) / PLACES / name
            try:
                place.stat()
            except FileNotFoundError:
                pass  # gone
            except OSError:
                return None  # a read that failed
            else:
                candidates.append((place, "root"))
        pulled = Path(evidence) / f"{site.label}-{action.member}"
        candidates.append((pulled / PLACES / name, "pulled"))
        for place, source in candidates:
            out = place / "study" / "out"
            if (out / "state.sqlite").is_file():
                return out, source
            found = from_snapshot(place)
            if found is not None:
                return found, source
        return None

    return locate


def main(argv: Optional[Sequence[str]] = None, runner: Callable[..., Any] = subprocess.run) -> int:
    """`read`: this login node's `read_identity` as one JSON line, for the controller of an ssh site."""
    found = argparse.ArgumentParser(prog="fleetidentity.py", description=__doc__.split("\n\n")[0])
    found.add_argument("command", choices=("read",))
    for key in ("fleet", "member", "settings"):
        found.add_argument(f"--{key}", required=True)
    args = found.parse_args(argv)
    got = read_identity(Path(args.fleet), args.member, Path(args.settings), runner)
    if got is None:
        return 1
    print(json.dumps({"fingerprint": got[0], "environment": dict(got[1]), "platform": got[2]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
