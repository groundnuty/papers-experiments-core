"""fleetidentity.py: what the elastic controller reads for a federation's join and for a lost lease's spend (#462 step
6; README.md, "The elastic fleet's worker"). `fleetcontrol.py control` builds both from its sites file's `settings` (the
fleet's shard settings, `fleetplan.read_settings`).

- `identity_reader(settings, sites)`: `(site label, member) -> (campaign fingerprint's SHA-256, environment identity)`
  or None. Only for a member this fleet's up made and holds, up and linked (`fleetrecord.identity`): the study is
  copied into a private directory with the federation's own `platform.toml` beside it, as a shard step places it, and
  the manifest's Python (`-I -B`, an environment of the allowlist and a home of its own) asks the core for what a start
  there pins (`expcore.study.pipeline.campaign_identity`): the fingerprint (hashed as the lease's `campaign_sha256` is)
  and the environment identity a lease store pins, whose digest the ledger pins at its first join. A read that fails is
  None (the join waits and is decided again), never a guess; a read that worked is kept for the member.
- `locator(evidence)`: `(site, Settle) -> (out/, source)` or None, for `fleetexecute.dead_store_reader`: the lost
  lease's store as a place `lease-<id>/` holds it, `study/out/` with the lease's `spend.jsonl` beside `study/`
  (`PLACES`): in the federation's channel on the site's shared file system (`root`, offered only while the decision
  shows the member's root, that is while it still holds the jobs the federation joined with) or in the evidence the
  controller collected from the member (`pulled`). Local transport only: those directories are read here.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Tuple

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
PLACES = "places"  # in a federation's channel, and in a member's collected evidence: <PLACES>/lease-<id:012d>/
PROGRAM = (
    "import json, sys\n"
    "from expcore.campaign.manifest import fingerprint_sha256\n"
    "from expcore.study.pipeline import campaign_identity\n"
    "fingerprint, environment = campaign_identity(sys.argv[1])\n"
    "print(json.dumps({'fingerprint': fingerprint_sha256(fingerprint), 'environment': environment}, sort_keys=True))\n"
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
) -> Optional[Tuple[str, Mapping[str, str]]]:  # fmt: skip
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
        said = json.loads(done.stdout.strip().splitlines()[-1])
        digest, environment = said["fingerprint"], said["environment"]
        if not isinstance(digest, str) or not isinstance(environment, dict):
            return None
        return digest, {str(k): str(v) for k, v in environment.items()}
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return None
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def identity_reader(
    settings_path: Path, sites: Mapping[str, Any], runner: Callable[..., Any] = subprocess.run
) -> Callable[[str, str], Optional[Tuple[str, Mapping[str, str]]]]:  # fmt: skip
    """The controller's identity hook over the sites' fleets (`read_identity`); a member's read that worked is kept."""
    kept: dict = {}

    def read(site: str, member: str) -> Optional[Tuple[str, Mapping[str, str]]]:
        found = kept.get((site, member))
        if found is None and site in sites:
            found = read_identity(sites[site].fleet, member, settings_path, runner)
            if found is not None:
                kept[(site, member)] = found
        return found

    return read


def locator(evidence: Path) -> Callable[[Any, Any], Optional[Tuple[Path, str]]]:
    """The spend reader's `locate` (module docstring): where a lost lease's store is, or None."""

    def locate(site: Any, action: Any) -> Optional[Tuple[Path, str]]:
        if site is None:
            return None
        from expcore.campaign.elastic import channel as ch

        name = f"lease-{action.lease_id:012d}"
        candidates = []
        if action.root is not None and site.channel is not None:
            candidates.append((ch.federation_dir(str(site.channel), action.label) / PLACES / name, "root"))
        pulled = Path(evidence) / f"{site.label}-{action.member}"
        candidates.append((pulled / PLACES / name, "pulled"))
        for place, source in candidates:
            out = place / "study" / "out"
            if (out / "state.sqlite").is_file():
                return out, source
        return None

    return locate
