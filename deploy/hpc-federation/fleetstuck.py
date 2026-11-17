"""A stuck shard moves to a spare federation through the fleet alone (O68; README.md, "A stuck shard"; DESIGN.md,
section 1, "A fleet"). The fleet's two parts of it:

**`step-stop --shard L`** (`stop`) ends the step the fleet launched for L, by the ids the fleet recorded for it and
nothing else: the srun client on the login node, by the pid and start tick its launch recorded, and the Slurm step it
runs, by its exact `<job>.<n>` id. The id is not known when the client is spawned, so it is read now: the federation's
state names its jobs, the fleet gave the step one name (`shards.step_name`), and `Slurm.steps_named` lists every step of
that name in those jobs. The name is not the fleet's alone (`shards.py lines` and `resume-line` give a step run by hand
the same name), so the id is cancelled only when exactly one step carries the name and the recorded client is still
alive: a live client is what vouches that the step listed is the one it runs. With the client gone and a step of the
name listed, or with two steps of the name, nothing is cancelled and the operator is told the ids to look at. Nothing
is signalled or cancelled unless every one of these holds: the shard is held by a federation of the fleet whose root
and state are the ones its up made (`fleetrecord.identity`); `shards.toml` is the one the record gives and names that
federation's root; the job the step lands in is still named as the federation's up named it (Slurm reuses ids); and the
pid still holds the process recorded (`Runner.client_state`: its start tick). A pid that holds another process now
refuses the whole stop: the client ended, so nothing of the fleet's is left to signal, and the step is for the
operator to look at. The step is cancelled through the federation's launcher (`Launcher.cancel`, which refuses a step
of any other job), then the client is ended, verified (`Runner.end_client`: SIGTERM, then SIGKILL, each only while the
process is still the recorded one), as `down` does. Slurm lists a cancelled step until its KillWait ends; the stop does
not wait for it, and `step --resume` refuses while the step is listed. The stop is written into the launch's entry of
the fleet's record before it acts and again with what happened (`stopped`: the step's id, whether it was cancelled and
the client ended).

**`step --resume` over a killed step's mark** (`over_proof`): a step killed outright leaves its running mark
(`<results>/<label>/running.json`), and a step on another federation may take it over only with its nonce as `--over`
(`shards.claim_running`). The fleet passes it exactly as `shards.py resume-line` does, and only after that command's own
proof (`shardresume.check_no_live_step`): no step of this user's carries the shard's step name, and the step the mark
names is no longer listed in its job or anywhere. The proof is asked through the runner of every federation that held or
ran the shard. A mark that cannot be read as the shard's, or whose step still runs, refuses that shard and launches it
nowhere; a shard with no mark carries no `--over`.
"""

import os
import pwd
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence

import fleetplan
import hpclaunch
import hpcstate
import shardresume
import shards
from fleetrecord import FleetError, Record, identity, member_of, root_of, shard_of
from hpclaunch import Launcher, StateStore
from hpcslurm import ALIVE, ANOTHER, Client, Slurm, SlurmError

ERRORS = (SlurmError, OSError)


def over_proof(plan: shards.Plan, label: str, runners: Sequence[Any], user: Optional[str] = None) -> Optional[str]:
    """The nonce of the shard's running mark, for `--over`, once `shardresume.check_no_live_step` shows that no step of
    the shard runs and the step the mark names is gone (asked through each of `runners`, which must agree on the mark);
    None when the shard has no mark. FleetError, saying why, for a step that still runs, a mark that cannot be read as
    the shard's, or a lookup that failed: never a nonce without the proof."""
    shard = next((s for s in plan.shards if s.label == label), None)
    if shard is None:
        raise FleetError(f"the fleet's manifest has no shard {label}")
    try:
        name = shards.step_name(shards.federation(shard), shard)
    except shards.ShardsError as error:
        raise FleetError(str(error)) from None
    who = user or pwd.getpwuid(os.getuid()).pw_name

    def refuse(why: str) -> None:
        raise FleetError(why)

    found = set()
    try:
        for runner in runners:
            found.add(shardresume.check_no_live_step(shards, Slurm(runner), ERRORS, plan, shard, name, who, refuse))
    except ERRORS as error:
        raise FleetError(f"its steps could not be looked up ({type(error).__name__}): no --over is carried") from None
    if len(found) > 1:
        raise FleetError("its running mark changed while it was read: no --over is carried")
    return found.pop() if found else None


def _target(directory: Path, record: Dict[str, Any], label: str) -> Dict[str, Any]:
    """What `stop` acts on, every part of it from the fleet's record or checked against it: the shard's holder, the
    launch (its index in the shard's launches, its client), the federation's job and the step's name."""
    shard = shard_of(record, label)
    holder = shard["member"]
    if holder is None:
        raise FleetError(f"{label} is unassigned: no federation holds it, so no step of the fleet's runs for it")
    mine = [i for i, launch in enumerate(shard["launches"]) if launch["member"] == holder and not launch.get("failed")]
    if not mine:
        raise FleetError(f"the fleet launched no step of {label} on {holder}: nothing recorded to stop")
    launch = shard["launches"][mine[-1]]
    if launch.get("pid") is None or launch.get("start") is None:
        raise FleetError(f"no client of {label}'s latest launch was recorded (its spawn was cut short), so none can be "
                         "shown to be its own: nothing signalled or cancelled")  # fmt: skip
    reason, _ = identity(directory, member_of(record, holder))
    if reason is not None:
        raise FleetError(f"{reason}: nothing signalled or cancelled")
    planned = next((s for s in fleetplan.current_plan(record, directory).shards if s.label == label), None)
    if planned is None or planned.root != root_of(directory, holder):
        raise FleetError(f"{fleetplan.MANIFEST} gives {label} another root than {holder}'s: run plan again; nothing "
                         "signalled or cancelled")  # fmt: skip
    try:
        fed = shards.federation(planned)
    except shards.ShardsError as error:
        raise FleetError(f"{error}: nothing signalled or cancelled") from None
    return {"holder": holder, "index": mine[-1], "client": Client(launch["pid"], launch["start"]), "fed": fed,
            "name": shards.step_name(fed, planned), "root": root_of(directory, holder)}  # fmt: skip


def stop(directory: Path, store: Record, label: str, runner_of: Callable[[str], Any],
         say: Callable[[str], None]) -> int:  # fmt: skip
    """Stop shard `label`'s step as the module's docstring says; the exit status (0: nothing of it runs now or was
    cancelled and ended, 1: the step's cancel or the client's end failed). FleetError, before anything is signalled or
    cancelled, for every refusal."""
    target = _target(directory, store.read(), label)
    holder, client, fed, name = target["holder"], target["client"], target["fed"], target["name"]
    runner = runner_of(holder)
    state = runner.client_state(client)
    if state == ANOTHER:
        raise FleetError(f"{label}'s recorded client (pid {client.pid}) is another process now: its start tick is not "
                         "the recorded one, so the client ended and its pid was reused. Nothing signalled and nothing "
                         "cancelled; look at its step in job "
                         f"{fed['listed']} with squeue and cancel it by hand if it still runs")  # fmt: skip
    slurm = Slurm(runner)
    store_of_state = StateStore(target["root"])
    launcher = Launcher(slurm, store_of_state, None)  # `cancel` reads the state file alone
    try:
        job = slurm.job(fed["job"])
        found = []  # every step of the name in every job the federation's state names (none in a job Slurm lost)
        for held in () if job is None else hpclaunch.listings(store_of_state.read()):
            found += slurm.steps_named(held, name)
    except (*ERRORS, hpcstate.StateError, ValueError) as error:
        raise FleetError(f"job {fed['job']} could not be looked up ({type(error).__name__}): nothing signalled or "
                         "cancelled") from None  # fmt: skip
    if job is not None and job.get("JobName") != fed["name"]:
        raise FleetError(f"job {fed['job']} now names another job than {holder}'s: never stepped into; nothing "
                         "signalled or cancelled")  # fmt: skip
    if len(found) > 1:  # the name is no key: a step started by hand carries it too
        raise FleetError(f"{len(found)} steps carry {label}'s step name ({', '.join(found)}) and the fleet cannot tell "
                         "which is its launch's: nothing signalled or cancelled; cancel the stuck one "
                         "by hand")  # fmt: skip
    step = found[0] if found else None
    if state != ALIVE and step is None:
        say(f"{label} on {holder}: nothing to stop: its client has ended and no step of it is listed")
        return 0
    if state != ALIVE:  # only a live recorded client vouches that a step of the name is the fleet's launch
        raise FleetError(f"{label}'s recorded client has ended, so it vouches for no step, and step {step} carries its "
                         "step name but may be one the fleet did not launch (a line run by hand has the same name): "
                         "nothing cancelled; cancel it by hand if it is the stuck one")  # fmt: skip
    stopped: Dict[str, Any] = {"step": step, "cancelled": False, "ended": False}  # the client is alive here
    index = target["index"]

    def record(current: Dict[str, Any]) -> None:
        shard_of(current, label)["launches"][index]["stopped"] = dict(stopped)

    store.update(record)  # before it acts, as every action of the fleet is recorded
    problems = []
    if step is not None:
        try:
            launcher.cancel(step)
            stopped["cancelled"] = True
        except (*ERRORS, hpcstate.StateError) as error:
            problems.append(f"step {step} not cancelled ({error}): it may still run in job {fed['job']}")
    stopped["ended"] = bool(runner.end_client(client))
    if not stopped["ended"]:
        problems.append(f"client pid {client.pid} did not end")
    store.update(record)
    parts = [] if step is None else [f"step {step} " + ("cancelled" if stopped["cancelled"] else "not cancelled")]
    parts.append("client " + ("ended" if stopped["ended"] else "not ended"))
    say(f"{label} on {holder}: stopped: {'; '.join(parts)}")
    if stopped["cancelled"]:
        say(f"{label}: Slurm lists a cancelled step until its KillWait ends; `step --resume` refuses until it is gone")
    for problem in problems:
        say(f"{label}: {problem}")
    return 1 if problems else 0
