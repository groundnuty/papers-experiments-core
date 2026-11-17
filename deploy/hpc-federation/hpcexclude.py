"""The nodes of the account's other live federations, which a federation's services keep off (DESIGN.md, section 1,
"Two federations of one account").

A federation's services run with no network namespace and Couchbase's guard watches the fixed port 8091, so two
federations' services of one account on one node would fail the second one's `up`. Slurm packs jobs onto nodes by cores
and memory, so nothing else keeps them apart. `up` therefore reads the account's live jobs twice, read-only
(`squeue --me`), and every job that carries `JOB_PREFIX` counts as a federation's:

- **before it submits**, the nodes of every such job go to the services' job as `--exclude`; an unreadable read refuses;
- **once its job runs, before any step**, none of its service nodes may hold another such job (two ups that raced);
  one that does is refused and its jobs released, naming the role.

The prefix is every held job's: `hpcop-<project>-<random>`, the agent's own job under "separate" with `-sandbox`
after it, and the older bundle's batch scripts (`hpcop-zone`, `hpcop-provider`). A job is read whole, so another
federation's agent's node is one of its nodes too. Every message here and in its callers names a role, a count
or a job's own name, never a node, and never says what squeue said.
"""

import re
from typing import Dict, Iterable, List, NamedTuple, Sequence, Set, Tuple

from hpcslurm import JOB_LISTED, Slurm, SlurmError

JOB_PREFIX = "hpcop-"
# The read names no state (`-t`): squeue's own default lists every job that holds a node, a suspended one, one staging
# out, and a running one with a stop or a signal pending among them (Slurm's `_filter_job`, read on master and on 23.02;
# its manual says only "pending, running, and completing", which is less than the code does). An explicit list of the
# base state names is narrower than that default, since a base name matches a job's whole state, flags included: it
# would hide a running job that has been sent a stop or a signal. A login shell's `SQUEUE_STATES` cannot narrow the
# default either: `hpcslurm.Runner` runs the call with no `SQUEUE_*` variable.
# `--all`: in every partition, a hidden one included, whatever the environment says (`SQUEUE_ALL` is what a site that
# hides its partition would otherwise rely on); it only widens what the guard sees
LIVE = [
    "squeue",
    "--me",
    "-h",
    "--all",
    "-o",
    "%i %j %T %N",
]  # id, name, state, node list: nothing else of a job is read
STATE = re.compile(r"[A-Z_]{2,32}")
LENIENT = ("PENDING", "COMPLETING")  # states whose node list may be none: not yet placed, or all returned to service
NODE_LIST = re.compile(r"[A-Za-z0-9][A-Za-z0-9._,\[\]-]{0,4095}")  # a compressed host list: `node[01-03],node07`
UNREADABLE = "the account's live federation jobs could not be read"


class LiveJob(NamedTuple):
    """One live job that carries the prefix: its id (a heterogeneous job's leader's), its name and every node it holds,
    the components of a heterogeneous job together."""

    job: str
    name: str
    nodes: Tuple[str, ...]


def live_jobs(slurm: Slurm) -> List[LiveJob]:
    """Every job of the account that squeue lists and whose name carries the prefix, in any state (a completing job may
    still hold its node; a pending one holds none). A job of no federation's is none of its business. Anything that
    cannot be taken apart, in a line of a job that carries the prefix, raises `SlurmError`, so that no caller ever
    acts on a guess; the message says what failed, never what squeue said."""
    try:
        done = slurm.runner.run(LIVE, slurm.command_timeout_s)
    except SlurmError:
        raise SlurmError(f"{UNREADABLE}: the call failed or timed out") from None
    if done.returncode != 0:
        raise SlurmError(f"{UNREADABLE}: squeue exited with status {done.returncode}")
    found: Dict[str, Tuple[str, Set[str]]] = {}
    for line in done.stdout.splitlines():
        words = line.split()
        if len(words) < 2 or not words[1].startswith(JOB_PREFIX):
            continue
        listed = JOB_LISTED.fullmatch(words[0])
        if listed is None or not 3 <= len(words) <= 4 or not STATE.fullmatch(words[2]):
            raise SlurmError("a line of a live federation job is not '<id> <name> <state> [<nodes>]'")
        nodes = _nodes(slurm, words[1], words[2], words[3] if len(words) == 4 else "")
        held = found.setdefault(listed.group(1), (words[1], set()))[1]
        held.update(nodes)
    return [LiveJob(job, name, tuple(sorted(held))) for job, (name, held) in found.items()]


def _nodes(slurm: Slurm, name: str, state: str, listed: str) -> List[str]:
    """The nodes of one row (`name`: the job's own, which a refusal names). A job that runs (or is configuring) holds
    nodes, so a row without a list that can be expanded refuses. A pending job's list, if it has one that can be
    expanded, is kept (the scheduler may have set its nodes aside), and its list that is none (`(null)`, empty) is no
    node; so is a completing job's empty list, since squeue lists only the nodes not yet returned to service and a job
    right after a `down` may have none left. A list that is there and cannot be expanded refuses, for a pending job
    as for any other: the nodes the scheduler set aside for it are nodes the read could not exclude."""
    leniently = state in LENIENT
    if NODE_LIST.fullmatch(listed) is None:
        if leniently and (state != "COMPLETING" or not listed):
            return []
        raise SlurmError("a live federation job that holds nodes lists none")
    try:
        return slurm.hostnames(listed)
    except SlurmError:
        raise SlurmError(f"the node list of live federation job {name} could not be expanded") from None


def nodes_of(jobs: Iterable[LiveJob], own: Sequence[str] = ()) -> List[str]:
    """The nodes of every job but this federation's own (`own`: the ids of the held jobs it submitted, a heterogeneous
    job's by its leader's), sorted."""
    return sorted({node for job in jobs if job.job not in own for node in job.nodes})


AGENT = "agent"  # the agent's node, which is no service's and never runs beside one (README, "The uid is the boundary")


def role(index: int) -> str:
    """The role of the held job's `index`th service node, in its order: the zone, then each provider by its place in
    the layout's `providers` (a name of the layout is never repeated)."""
    return "zone" if index == 0 else f"providers[{index - 1}]"
