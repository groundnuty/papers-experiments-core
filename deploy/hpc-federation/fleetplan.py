"""Which federation of a fleet runs which shard (`fleet.py plan`), and what `fleet.py step` checks again before each
launch (DESIGN.md, section 1, "A fleet"). The lines are `shards.py`'s own (`load_plan`, `federation`, `step_argv`).

**One campaign.** A federation is given a shard only when its own `up` finished (`linked`: every space's data read back
against its manifest, every link checked), its root and state are the ones this fleet made (`fleetrecord.identity`),
every space of its `platform.toml` declares its data (`data_manifest`, C06), and its platform fingerprint
(`PlatformConfig.fingerprint()`) is the fleet's. The first plan sets that reference, and refuses outright when the
federations' fingerprints differ, naming each federation and the keys that differ, never a value: their shards would
never merge. A later federation of another fingerprint is never used.

**A shard that moves.** The shard count is fixed at the fleet's `up`. A shard whose federation is taken down
(`fleet.py down --member`) is released, and given to a federation only after its fresh `up`, never one that held a shard
before, with the same checks and an equal fingerprint. Its first reset there must be a full one (the coordinator's rule
for moving a shard). What the fleet can check is the federation's platform file, which the shard's step carries into
the study: it moves a shard only to a federation whose `platform.toml` leaves `[placement] reset_shortcut` off (the
default), under which every reset's occupancy and quiet stages take the full reads, the first included; one that turns
it on is refused for the move. It cannot see a study's own request for one full reset (O51), so a study that turns the
shortcut on moves no shard.
"""

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import hpcstate
import shards
import tomllib
from fleetrecord import FleetError, identity, member_of, root_of

MANIFEST = "shards.toml"  # in the fleet's directory: what `shards.py collect --manifest` reads at the end
SETTINGS = {
    "study",
    "python",
    "core_src",
    "results",
    "tmpfs",
    "step_path",
    "cpus",
    "memory",
}  # shards.py's keys: no argv
OPTIONAL = (
    "snapshot_every_s",
    "pre_step",
    "pre_step_timeout_s",
    "post_run",
    "post_run_timeout_s",
    "expect_render_identity",
    "pass_env",
)  # the rest of shards.py's optional keys: the study's own guard, post-run script and expected render identity, and
# the operator's variables its dispatchers are passed (names only: `pin` reads the values)
SCRIPT = Path(__file__).resolve().parent / "shards.py"


def read_settings(path: Path) -> Dict[str, Any]:
    """The shard manifest's settings: `shards.py`'s keys but `[[shards]]`, which the fleet writes. shards.py checks the
    values when the fleet's manifest is loaded; here each must be a string or a number."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise FleetError(f"the shard settings could not be read ({type(error).__name__})") from None
    if not set(SETTINGS) <= set(data) <= set(SETTINGS) | set(OPTIONAL):
        keys = ", ".join(sorted(SETTINGS))
        raise FleetError(f"the shard settings take exactly: {keys}, and perhaps {', '.join(OPTIONAL)}; "
                         "no [[shards]] (the fleet writes them)")  # fmt: skip
    for key, value in data.items():
        if key == "pass_env":  # a list of names, by shards.py's rule: a secret-looking or the step's own is refused
            try:
                shards.pass_env_names(value)
            except shards.ShardsError as error:
                raise FleetError(str(error)) from None
        elif isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise FleetError(f"the shard setting {key} must be a string or a number")
    return data


def _toml(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (str, list)) else repr(value)


def manifest_text(settings: Mapping[str, Any], entries: List[Tuple[str, str]]) -> str:
    """`shards.toml` for `shards.py`: the settings, then one `[[shards]]` per shard (its label, its federation's
    root), in the campaign's order."""
    lines = ["# written by fleet.py plan: one shard per federation of the fleet"]
    lines += [f"{key} = {_toml(settings[key])}" for key in sorted(settings)]
    for label, root in entries:
        lines += ["", "[[shards]]", f"label = {_toml(label)}", f"root = {_toml(root)}"]
    return "\n".join(lines) + "\n"


def platform_fingerprint(root: Path) -> Dict[str, Any]:
    """The platform's half of the campaign fingerprint, as a study's plugin takes it from this platform file."""
    from expcore_onedata.config import ConfigError, load_platform

    try:
        return load_platform(root / shards.PLATFORM, require_secrets=False).fingerprint()
    except (ConfigError, OSError, ValueError) as error:
        raise FleetError(f"its {shards.PLATFORM} could not be loaded ({type(error).__name__})") from None


def sha256(fingerprint: Mapping[str, Any]) -> str:
    from expcore.campaign.manifest import fingerprint_sha256

    return fingerprint_sha256(fingerprint)


def differing(one: Mapping[str, Any], other: Mapping[str, Any]) -> List[str]:
    """The fingerprint's keys whose values differ, by their canonical JSON (a recorded one went through JSON): named,
    never shown."""

    def canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    return sorted(key for key in set(one) | set(other) if canonical(one.get(key)) != canonical(other.get(key)))


def shortcut_on(root: Path) -> bool:
    """Whether a federation's `platform.toml` turns `[placement] reset_shortcut` on (unreadable counts as on)."""
    try:
        data = tomllib.loads((root / shards.PLATFORM).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return True
    placement = data.get("placement")
    return isinstance(placement, Mapping) and placement.get("reset_shortcut", False) is not False


STUDY = "study.toml"  # in the study's root: its conditions are what `injection` reads


def declares(condition: Mapping[str, Any], kind: str) -> bool:
    """Whether a study's condition names `kind` among its `infrastructure_is_treatment` (a name, or a `{kind, target}`
    table), as `expcore.campaign.factors` reads them."""
    entries = condition.get("infrastructure_is_treatment")
    return isinstance(entries, list) and any(
        e == kind or (isinstance(e, Mapping) and e.get("kind") == kind) for e in entries
    )


def injection(study: Optional[Path], root: Path) -> Optional[str]:
    """Why this campaign injects faults on the federation at `root`, so that its fault relay must serve; None when
    nothing in it does. One of: the study's `study.toml` declares a condition whose treatment is a provider outage
    (`expcore.errors.PROVIDER_OUTAGE`), or the federation's `platform.toml` sanctions injection (`[faults] sanctioned =
    true`, the configuration's half of the injector's sanction). Both are read as files, nothing of the study is run. A
    shard's runs are a share of every condition (the partition is by pair, C28), so a campaign that has one outage
    condition injects in every shard, whatever tasks the condition names. A `study.toml` that cannot be read counts as
    injecting (fail closed); one that does not exist counts as none, and the campaign check, which runs the study's own
    plugin before any shard is assigned, refuses a study with no study file."""
    from expcore.errors import PROVIDER_OUTAGE

    if study is not None:
        try:
            data = tomllib.loads((study / STUDY).read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {}
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return "the study's study.toml could not be read, so an outage condition cannot be ruled out"
        conditions = data.get("conditions")
        if isinstance(conditions, Mapping) and any(
            isinstance(c, Mapping) and declares(c, PROVIDER_OUTAGE) for c in conditions.values()
        ):
            return "the study declares an outage condition"
    try:
        faults = tomllib.loads((root / shards.PLATFORM).read_text(encoding="utf-8")).get("faults")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        faults = None  # `undeclared_spaces` refuses a platform file that cannot be read, before this is asked
    if isinstance(faults, Mapping) and faults.get("sanctioned") is True:
        return "its platform.toml sanctions fault injection ([faults] sanctioned = true)"
    return None


def relay_gap(label: str, state: Mapping[str, Any], study: Optional[Path], root: Path) -> Optional[str]:
    """Why a federation cannot run a shard of this campaign for want of a fault relay, None when it can: the campaign
    injects faults (`injection`) and the state file `up` wrote does not say `serving` (`hpcstate.relay_of`: `none` for
    a core without the relay, `unrecorded` for a state with neither). A federation without a serving relay can neither
    inject nor restore. The relay's state is read from the state file, never from the up log, which is for the
    operator."""
    why = injection(study, root)
    relay = hpcstate.relay_of(dict(state))
    if why is None or relay == "serving":
        return None
    how = (
        "none: its core has no relay"
        if relay == "none"
        else "not recorded: its state file has no relay record, as one an older up wrote, or an up that never got "
        "there, has not"
    )
    return (f"{label}: its fault relay is not serving ({how}), and this campaign injects faults ({why}): a federation "
            "without a serving relay can neither inject nor restore")  # fmt: skip


def readiness(directory: Path, member: Mapping[str, Any],
              study: Optional[Path] = None) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:  # fmt: skip
    """(why the federation cannot run a shard now, None), or (None, its platform fingerprint). `study` is the study's
    root, which says whether the campaign injects faults and so needs the federation's relay."""
    label = member["label"]
    if member["status"] != "up":
        return f"{label}: its up has not finished (it is {member['status']})", None
    reason, _ = identity(directory, member)
    if reason is not None:
        return reason, None
    root = root_of(directory, label)
    try:
        state = hpcstate.load(root)
    except hpcstate.StateError as error:
        return f"{label}: {error}", None
    if state is None or state.get("phase") != shards.LINKED:
        return f"{label}: its federation is not linked (phase {(state or {}).get('phase')})", None
    missing = shards.undeclared_spaces(label, root)
    if missing is not None:
        return f"{label}: a space's data is not declared (data_manifest, C06): {missing}", None
    gap = relay_gap(label, state, study, root)
    if gap is not None:
        return gap, None
    try:
        return None, platform_fingerprint(root)
    except FleetError as error:
        return f"{label}: {error}", None


def assignments(record: Mapping[str, Any], directory: Path,
                study: Optional[Path] = None) -> Tuple[Dict[str, str], Dict[str, Any], List[str]]:  # fmt: skip
    """The new assignments ({shard: federation}), the reference fingerprint and what is said; FleetError refuses the
    whole plan: a federation holding a shard that is no longer ready or no longer of the fleet's fingerprint, the first
    plan's federations of differing fingerprints, or none ready. A campaign that injects faults (`injection`, read from
    `study`) is given only to federations whose fault relay serves."""
    said: List[str] = []
    holders = {s["member"]: s["label"] for s in record["shards"] if s["member"] is not None}
    found: Dict[str, Dict[str, Any]] = {}
    for member in record["members"]:
        label = member["label"]
        if label not in holders and (member["status"] != "up" or member["held"]):
            continue  # neither holding a shard nor fresh
        reason, fingerprint = readiness(directory, member, study)
        if reason is not None and label in holders:
            raise FleetError(f"shard {holders[label]}'s federation is not ready: {reason}; `fleet.py down --member "
                             f"{label}` frees the shard for a fresh federation")  # fmt: skip
        if reason is not None:
            said.append(f"not used: {reason}")
        else:
            found[label] = fingerprint
    reference = (record.get("reference") or {}).get("fingerprint")
    if reference is None:  # the first plan: every federation ready must be of one fingerprint
        groups: Dict[str, List[str]] = {}
        for label, fingerprint in found.items():
            groups.setdefault(sha256(fingerprint), []).append(label)
        if len(groups) > 1:
            first, *rest = groups.values()
            keys = sorted({k for labels in rest for k in differing(found[first[0]], found[labels[0]])})
            raise FleetError(
                "the federations' platform fingerprints differ, so their shards would not be one campaign: "
                + "; ".join(", ".join(labels) for labels in groups.values())
                + f" (differing: {', '.join(keys)}); nothing was assigned"
            )
        if not found:
            why = "; ".join(line.removeprefix("not used: ") for line in said)
            raise FleetError("no federation of the fleet is ready to run a shard; nothing was assigned"
                             + (f" ({why})" if why else ""))  # fmt: skip
        reference = next(iter(found.values()))
    free = []
    for label, fingerprint in found.items():
        if sha256(fingerprint) == sha256(reference):  # by its canonical JSON: the reference was recorded as JSON
            free += [] if label in holders else [label]
        elif label in holders:
            raise FleetError(f"{label}'s platform fingerprint is no longer the fleet's (differing: "
                             f"{', '.join(differing(reference, fingerprint))}); nothing was assigned")  # fmt: skip
        else:
            said.append(f"not used: {label}: its platform fingerprint differs from the fleet's (differing: "
                        f"{', '.join(differing(reference, fingerprint))})")  # fmt: skip
    new: Dict[str, str] = {}
    unassigned = [s for s in record["shards"] if s["member"] is None]
    for shard in unassigned:  # shard-k on fed-k, the federation its fleet's up made for it, when that one is ready
        mine = "fed-" + shard["label"].split("-", 1)[1]
        if shard["last"] is None and mine in free:
            new[shard["label"]] = mine
            free.remove(mine)
    for shard in unassigned:  # any other: the next fresh federation, a moved shard only where its first reset is full
        if shard["label"] in new:
            continue
        for label in list(free):
            if shard["last"] is not None and shortcut_on(root_of(directory, label)):
                said.append(f"{shard['label']}: not moved to {label}: its platform.toml turns [placement] "
                            "reset_shortcut on, so the moved shard's first reset there is not shown full")  # fmt: skip
                continue
            new[shard["label"]] = label
            free.remove(label)
            break
        else:
            said.append(f"{shard['label']}: unassigned: no fresh federation is ready for it (`fleet.py add 1`, then "
                        "plan again)")  # fmt: skip
    return new, reference, said


def entries(record: Mapping[str, Any], directory: Path, new: Mapping[str, str]) -> List[Tuple[str, str]]:
    """Each shard's label and root for `shards.toml`: its federation's; an unassigned one's the federation it last had,
    or the one its fleet's up made for it (never launched: `shards.py` refuses a federation that is not linked)."""
    found = []
    for shard in record["shards"]:
        label = new.get(shard["label"]) or shard["member"] or shard["last"] or "fed-" + shard["label"].split("-")[1]
        found.append((shard["label"], str(root_of(directory, label))))
    return found


def write_whole(path: Path, text: str) -> None:
    draft = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(draft, path)


def load(path: Path) -> shards.Plan:
    try:
        return shards.load_plan(path)
    except shards.ShardsError as error:
        raise FleetError(f"the fleet's shard manifest: {error}") from None


def require_core(plan: shards.Plan) -> None:
    """Refuse a plan whose interpreter imports a core that `expcore run` would refuse to start on: its commit unknown
    or its install stale (C06). The fleet builds its lines with `shards.step_argv` and never goes through
    `shards.py plan`, which makes this check, so it asks the same: the manifest's `python`, as the steps will run."""
    try:
        refusal = shards.core_refusal(shards.identify_core(plan))
    except shards.ShardsError as error:
        raise FleetError(str(error)) from None
    if refusal:
        raise FleetError(refusal)


def check_campaign(plan: shards.Plan, labels: Sequence[str]) -> None:
    """The study's campaign fingerprint, taken beside each of these shards' federations' `platform.toml`, is one
    (`shards.check_one_campaign`, O63): FleetError otherwise. A study's plugin may fingerprint a setting that the
    platform's own fingerprint (`platform_fingerprint`) leaves out, and the shards would never merge."""
    try:
        shards.check_one_campaign(plan, labels)
    except shards.ShardsError as error:
        raise FleetError(str(error)) from None


def pin(plan: shards.Plan, environ: Optional[Mapping[str, str]] = None) -> shards.Plan:
    """The plan as `shards.py plan` and `lines` pin it before they print a line: the study's scripts hashed as the
    study holds them now, and each declared variable's value read from `environ` (the operator's, by default the
    process's): an unset variable or a value that may not ride on a line refuses it, naming the variable."""
    try:
        return shards.pin_pass_env(shards.pinned(plan), environ)
    except shards.ShardsError as error:
        raise FleetError(str(error)) from None


def check_first_values(plan: shards.Plan, label: str, environ: Optional[Mapping[str, str]] = None) -> None:
    """A shard's declared variables keep the values its first step passed, on whichever federation a later step runs
    (`shards.check_pass_env_origin`, in the results directory every shard shares): the values `environ` gives now (the
    operator's, as `pin` reads them) must be those, else `FleetError` naming the variable and never a value. Nothing is
    compared for a shard with no first step recorded. `launches` asks before it makes a line to launch: `plan`'s lines
    are first steps' and launch nothing."""
    (shard,) = [s for s in plan.shards if s.label == label]
    spec = {"index": shard.index, "count": shard.count, "label": shard.label}
    try:
        shards.check_pass_env_origin(plan.results, spec, dict(pin(plan, environ).pass_env_pins or ()))
    except shards.ShardsError as error:
        raise FleetError(str(error)) from None


def line_of(plan: shards.Plan, label: str, resume: bool, root: Optional[Path] = None,
            environ: Optional[Mapping[str, str]] = None) -> List[str]:  # fmt: skip
    """The login node's srun line of one shard (`shards.step_argv`) on the federation the manifest gives it, which must
    be `root` when one is given (the federation the fleet's record gives it, and checked). The plan is pinned here
    (`pin`): a line is never made from a plan that has not hashed the study's scripts and read the declared
    variables."""
    return target_of(plan, label, resume, root, environ)[0]


def target_of(plan: shards.Plan, label: str, resume: bool, root: Optional[Path] = None,
              environ: Optional[Mapping[str, str]] = None) -> Tuple[List[str], Dict[str, Any]]:  # fmt: skip
    """`line_of`'s line, and the job it lands in as the state names it (`shards.federation`: its id and name)."""
    (shard,) = [s for s in plan.shards if s.label == label]
    if root is not None and shard.root != root:
        raise FleetError(f"{MANIFEST} gives shard {label} another root than the fleet's record: run plan again")
    plan = pin(plan, environ)
    try:
        fed = shards.federation(shard)
        argv = shards.step_argv(plan, shard, fed, SCRIPT)
    except shards.ShardsError as error:
        raise FleetError(str(error)) from None
    return argv + (["--resume"] if resume else []), fed


@dataclass(frozen=True)
class Launch:
    """One shard's step to launch: on which federation, its line, the job it lands in (its id and the name its
    federation's up gave it), and whether it resumes."""

    shard: str
    member: str
    argv: List[str]
    resume: bool
    note: str
    job: str
    name: str


def launches(record: Mapping[str, Any], directory: Path, only: Optional[str], resume: bool,
             running: Callable[[Mapping[str, Any]], Optional[bool]],
             environ: Optional[Mapping[str, str]] = None) -> Tuple[List[Launch], List[str], List[str]]:  # fmt: skip
    """The steps `fleet.py step` launches, what it skips (said), and what it refuses, each checked again just before:
    the federation still ready, still this fleet's, still of the fleet's fingerprint, and a moved shard's resets there
    full reads. A shard already launched on its federation is launched again only with `resume`, and never while its
    client runs (`running`: True, False, or None when it cannot be told). `fleet.py step` then asks Slurm of each
    launch's job before it spawns. `environ` is the operator's: each line pins the declared variables from it, as it
    holds them now."""
    plan = load(directory / MANIFEST)
    settings, written = record.get("template"), (directory / MANIFEST).read_text(encoding="utf-8")
    if settings is None or written != manifest_text(settings, entries(record, directory, {})):
        raise FleetError(f"the fleet's {MANIFEST} is not the one its record gives (edited, or a plan cut short): run "
                         "plan again; nothing was launched")  # fmt: skip
    reference = (record.get("reference") or {}).get("sha256")
    found: List[Launch] = []
    said: List[str] = []
    refused: List[str] = []
    for shard in record["shards"]:
        label, holder = shard["label"], shard["member"]
        if (only is not None and label != only) or holder is None:
            if only == label:
                refused.append(f"{label}: unassigned: plan gives it a federation first")
            continue
        started = [launch for launch in shard["launches"] if not launch.get("failed")]  # a spawn that failed: none
        here = [launch for launch in started if launch["member"] == holder]
        if here and running(here[-1]) is not False:
            said.append(f"{label}: its step on {holder} still runs, or cannot be shown gone: not launched again")
            continue
        if here and not resume:
            said.append(f"{label}: its step on {holder} has ended; `fleet.py step --shard {label} --resume` relaunches")
            continue
        reason, fingerprint = readiness(directory, member_of(record, holder), plan.study)
        if reason is None and sha256(fingerprint) != reference:
            reason = f"{holder}: its platform fingerprint is no longer the fleet's"
        if reason is None and shard["moved"] and shortcut_on(root_of(directory, holder)):
            reason = f"{holder}: its platform.toml turns [placement] reset_shortcut on, so the moved shard's resets "
            reason += "there are not all full reads"
        if reason is not None:
            refused.append(f"{label}: not launched: {reason}")
            continue
        again = resume or bool(started)  # it ran before, here or elsewhere: its results go on
        note = ""
        if shard["moved"] and not here:
            note = (f"moved from {shard['last']}: {holder}'s platform.toml leaves [placement] reset_shortcut off, so "
                    "every reset there takes the full reads, its first included")  # fmt: skip
        check_first_values(plan, label, environ)  # a later step of the shard carries the values its first one did
        argv, fed = target_of(plan, label, again, root_of(directory, holder), environ)
        found.append(Launch(label, holder, argv, again, note, str(fed["job"]), str(fed["name"])))
    if found:  # a step that launches nothing reads no core; one that would launch asks first, as the plan did
        require_core(plan)
    return found, said, refused
