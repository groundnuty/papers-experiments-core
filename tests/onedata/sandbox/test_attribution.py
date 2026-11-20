"""O97: whose block it is. The verdict on the survivors of a cell's deadline is `agent` only when every survivor was
read whole, only in uninterruptible sleep, with every blocking object known, on the data mount and in a space whose
holder the study holds down right now. Everything else is `infrastructure` with the first reason that failed, and no
reading, an empty one or one cut short never reaches `agent` through an `all()` over nothing."""

import copy
import dataclasses

import pytest
from deadline_support import Clock, Processes, escalation, steps

from expcore_onedata.sandbox import attribution as AT
from expcore_onedata.sandbox import deadline

HOLDERS = {"owned": "providers[1]", "shared": "providers[2]"}
HELD = {"providers[1]"}


def obj(space="owned", path="data/file", mount="data") -> dict:
    return {"mount": mount, "space": space, "path": path}


CALL_OF = {"fd": 0, "path": 257, "holdings": -1}  # a `read`, an `openat`, and a task blocked outside any syscall


def task(*objects, state="D", via="fd", cut=False, nr=None) -> dict:
    blocking = {"via": via, "cut": cut, "objects": list(objects) or [obj()]}
    syscall = {"nr": CALL_OF[via] if nr is None else nr, **({"fd": 3} if via == "fd" else {})}
    return {"state": state, "wchan": "request_wait_answer", "syscall": syscall, "blocking": blocking}


def survivor(*tasks, state="D", **fields) -> dict:
    return {"comm": "python3", "state": state, "tasks": list(tasks) or [task()], **fields}


def reading(*survivors, cut=0) -> dict:
    """What `diagnose` answers: the uncapped `reading` the verdict is made on, and the capped `survivors` kept."""
    held = list(survivors) or [survivor()]
    return {"reading": held, "survivors": held, "survivors_cut": cut}


def ask(diagnostics, held=HELD):
    return AT.attribute(diagnostics, held, HOLDERS)


# ------------------------------------------------------------------------------------------------------ the agent's
def test_o97_a_task_blocked_on_a_file_of_the_held_down_holder_is_the_agents():
    """O97: one survivor, one task in D, blocked on a file in a space whose holder is held down: `agent`, naming the
    held-down label as the target and the space's holder."""
    assert ask(reading()) == {
        "verdict": "agent",
        "why": AT.BLOCKED_ON_HOLDER,
        "target": "providers[1]",
        "space_holder": "providers[1]",
    }


@pytest.mark.parametrize("via", AT.VIAS)
def test_o97_a_blocking_object_found_by_fd_by_path_or_by_the_holdings_rule_counts_the_same(via):
    """O97: the way the reader found what a task waits on (the fd of a read call, the path of an open, or every FUSE
    object the process holds) does not change the verdict: each object must be in a held-down space."""
    assert ask(reading(survivor(task(obj(), obj(path="data/other"), via=via))))["verdict"] == "agent"


def test_o97_a_zombie_leader_with_a_task_in_d_is_read_by_its_task():
    """O97: a reader in a non-main thread leaves a zombie leader and a task in D: the process is read by its task."""
    assert ask(reading(survivor(task(), state="Z")))["verdict"] == "agent"


def test_o97_every_survivor_is_judged_and_all_must_be_the_agents():
    """O97: two processes blocked on the held-down holder's files are the agent's; with one of them blocked on a space
    the study does not hold down, the cell is infrastructure."""
    both = reading(survivor(), survivor(task(obj(path="data/b"))))
    assert ask(both)["verdict"] == "agent"
    mixed = reading(survivor(), survivor(task(obj(space="shared"))))
    assert ask(mixed) == {
        "verdict": "infrastructure",
        "why": AT.SPACE_NOT_HELD_DOWN,
        "target": "providers[1]",
        "space_holder": "providers[2]",
    }


def test_o97_the_verdict_is_made_on_the_whole_reading_never_on_the_capped_survivors():
    """O97: the record keeps at most 8 processes (`survivors_cut`); the verdict is made on the `reading`, which holds
    them all, so a cap on what is shown never makes a verdict: a survivor the capped copy left out decides it."""
    capped = {**reading(survivor(), cut=1), "reading": [survivor(), survivor(task(obj(space="shared")))]}
    assert ask(capped)["why"] == AT.SPACE_NOT_HELD_DOWN
    only_capped = {"survivors": [survivor()], "survivors_cut": 0}
    assert ask(only_capped)["why"] == AT.UNKNOWN  # the capped copy alone is no reading


# ---------------------------------------------------------------------------------------- everything else fails closed
def test_o97_no_injection_in_effect_is_infrastructure_whatever_the_reading_says():
    """O97: nothing held down now (no injector, or one not yet verified or already clearing): `infrastructure`, so a
    wedge in a run with no outage is never the agent's."""
    assert ask(reading(), held=set()) == {
        "verdict": "infrastructure",
        "why": AT.NO_INJECTION,
        "target": None,
        "space_holder": None,
    }


def test_o97_the_held_down_set_is_read_when_the_verdict_is_asked_for_not_when_the_sandbox_started():
    """O97: `attributer` takes the set as a callable and asks it each time: the same reading is the agent's while the
    provider is held down and infrastructure once it is not."""
    held = {"set": {"providers[1]"}}
    attribute = AT.attributer(lambda: held["set"], HOLDERS)
    assert attribute(reading())["verdict"] == "agent"
    held["set"] = set()
    assert attribute(reading())["why"] == AT.NO_INJECTION


def test_o97_a_block_in_a_space_of_another_holder_is_infrastructure_naming_that_holder():
    """O97: a wedge between two providers that are up: the space's holder is not held down."""
    verdict = ask(reading(survivor(task(obj(space="shared")))))
    assert (verdict["verdict"], verdict["why"], verdict["space_holder"]) == (
        "infrastructure",
        AT.SPACE_NOT_HELD_DOWN,
        "providers[2]",
    )


@pytest.mark.parametrize("space", ["nobody_knows", None, 7], ids=["an unmapped space", "the mount's root", "no name"])
def test_o97_a_space_with_no_known_holder_is_never_taken_to_be_held_down(space):
    """O97: a space the holder map does not name, the mount's root and a malformed name each have no holder: not held
    down, `space_holder` None."""
    verdict = ask(reading(survivor(task(obj(space=space)))))
    assert (verdict["why"], verdict["space_holder"]) == (AT.SPACE_NOT_HELD_DOWN, None)


@pytest.mark.parametrize("mount", ["work", "tmp", "shm", "input", "other", None])
def test_o97_a_block_off_the_data_mount_is_infrastructure(mount):
    """O97: a wait on the agent's work directory or anything but the data mount is not the manipulation's."""
    assert ask(reading(survivor(task(obj(mount=mount)))))["why"] == AT.NOT_DATA_MOUNT


def test_o97_one_object_in_another_space_among_the_holdings_makes_it_infrastructure():
    """O97: the holdings rule is fail-closed: a process that holds a file of the held-down holder and a file of another
    mount is not attributed."""
    holdings = task(obj(), obj(mount="work", space=None), via="holdings")
    assert ask(reading(survivor(holdings)))["why"] == AT.NOT_DATA_MOUNT


@pytest.mark.parametrize("nr", [21, 89, 80, 137, 191, 59, 0, 257, None, "x", True])
def test_o97_the_holdings_rule_is_sound_only_for_a_task_blocked_outside_any_syscall(nr):
    """O97: a task blocked in a call the lists do not name (`access` 21, `readlink` 89, `chdir` 80, `statfs` 137,
    `getxattr` 191, `execve` 59) may wait on a path it does not hold, in another space, by a link wedge: what the
    process holds says nothing of it. Any `syscall` but `nr: -1` with `via: "holdings"` is `blocking_object_unknown`."""
    holdings = task(obj(), via="holdings", nr=nr)
    if nr is None:
        holdings["syscall"] = "unreadable"
    assert ask(reading(survivor(holdings)))["why"] == AT.UNKNOWN


def test_o97_a_task_blocked_in_a_call_on_another_space_while_it_holds_a_held_down_file_is_not_the_agents():
    """O97: the agent holds an fd on a file of the held-down space and blocks in `access` on a file of another space:
    read by its holdings it would be the agent's, which the ruling forbids. It is infrastructure."""
    task_in_access = task(obj(), via="holdings", nr=21)
    assert ask(reading(survivor(task_in_access)))["verdict"] == "infrastructure"


@pytest.mark.parametrize(
    ("via", "nr"),
    [("fd", 21), ("fd", 257), ("fd", -1), ("path", 0), ("path", 21), ("path", -1), ("fd", None), ("path", "x")],
)
def test_o97_an_object_resolved_by_fd_or_path_is_believed_only_for_a_call_of_its_list(via, nr):
    """O97: a reader that took an fd for a call that has none, or a path for a call that has none, is not believed."""
    blocked = task(obj(), via=via, nr=nr)
    if nr is None:
        blocked["syscall"] = "running"
    assert ask(reading(survivor(blocked)))["why"] == AT.UNKNOWN


@pytest.mark.parametrize(
    ("via", "nr"), [("fd", n) for n in sorted(AT.FD_CALLS)] + [("path", n) for n in sorted(AT.PATH_CALLS)]
)
def test_o97_every_call_of_the_lists_resolves_its_object(via, nr):
    assert ask(reading(survivor(task(obj(), via=via, nr=nr))))["verdict"] == "agent"


@pytest.mark.parametrize("state", ["R", "S", "Z", "T", None], ids=lambda s: f"state {s}")
def test_o97_a_survivor_not_in_d_is_infrastructure(state):
    """O97: a survivor running, sleeping interruptibly or a pure zombie waits on no FUSE request: not an outage's."""
    assert ask(reading(survivor(task(state=state), state=state)))["why"] == AT.NOT_IN_D


def test_o97_a_process_with_a_task_in_d_and_another_that_is_not_is_infrastructure():
    """O97: a task still running past the kill cannot be accounted for by a wait the outage explains."""
    assert ask(reading(survivor(task(), task(state="S"))))["why"] == AT.NOT_IN_D


def test_o97_a_process_with_no_task_is_infrastructure():
    """O97: no non-zombie task at all is nothing in D (never an `all()` over nothing)."""
    assert ask(reading(survivor(state="D", tasks=[])))["why"] == AT.NOT_IN_D


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda t: t.update(blocking=None), id="no blocking object read"),
        pytest.param(lambda t: t["blocking"].update(objects=[]), id="no object"),
        pytest.param(lambda t: t["blocking"].update(objects=["x"]), id="an object that is no mapping"),
        pytest.param(lambda t: t["blocking"].update(cut=True), id="the objects were cut"),
        pytest.param(lambda t: t["blocking"].update(via="guess"), id="an unknown way of finding it"),
        pytest.param(lambda t: t.pop("blocking"), id="no key"),
    ],
)
def test_o97_a_task_whose_blocking_object_is_not_known_is_infrastructure(mutate):
    """O97: an unknown fd, an unreadable `syscall` and a cut list each end in `blocking_object_unknown`."""
    flawed = copy.deepcopy(reading())
    mutate(flawed["survivors"][0]["tasks"][0])
    assert ask(flawed)["why"] == AT.UNKNOWN


@pytest.mark.parametrize(
    "diagnostics",
    [
        pytest.param(None, id="no reading at all"),
        pytest.param({}, id="an empty reading"),
        pytest.param({"reading": []}, id="no survivors"),
        pytest.param({"reading": "x"}, id="a reading that is no list"),
        pytest.param({"reading": ["x"]}, id="a survivor that is no mapping"),
        pytest.param({"survivors": [survivor()]}, id="only the capped copy"),
        pytest.param(reading(survivor(tasks_cut=1)), id="tasks the reader cut"),
        pytest.param(reading({"comm": "x", "state": "D"}), id="a survivor with no tasks read"),
        pytest.param(reading(survivor(tasks=[3])), id="a task that is no mapping"),
        pytest.param([survivor()], id="a diagnosis that is a list"),
    ],
)
def test_o97_no_reading_an_empty_one_or_one_cut_short_is_never_the_agents(diagnostics):
    """O97: the verdict is never made on nothing: each of these is `infrastructure`, `blocking_object_unknown`, with the
    one provider held down named as the target."""
    verdict = ask(diagnostics)
    assert (verdict["verdict"], verdict["why"], verdict["target"]) == ("infrastructure", AT.UNKNOWN, "providers[1]")


def test_o97_a_verdict_has_the_keys_the_record_keeps_and_a_reason_of_the_list():
    """O97: every verdict has exactly the keys `deadline.ATTRIBUTION_KEYS` keeps, a reason of `REASONS`, and only the
    agent's has `blocked_on_held_down_holder`."""
    cases = [reading(), None, reading(survivor(task(obj(space="shared")))), reading(survivor(task(state="S")))]
    for diagnostics in cases:
        verdict = ask(diagnostics)
        assert set(verdict) == set(deadline.ATTRIBUTION_KEYS)
        assert verdict["why"] in AT.REASONS
        assert (verdict["verdict"] == "agent") is (verdict["why"] == AT.BLOCKED_ON_HOLDER)


# --------------------------------------------------------------------------------------------- through the deadline
class Diagnosing(Processes):
    def __init__(self, *args, found, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.found = found

    def diagnose(self, left, timeout_s):
        """What a reader answers: the reading holds the `pid` and the `start` of each process it was given."""
        held = [{**r, "pid": p["pid"], "start": p["start"]} for r, p in zip(self.found["reading"], left, strict=True)]
        return {**self.found, "reading": held}


@pytest.mark.parametrize(
    ("held", "restore", "why"),
    [
        pytest.param({"providers[1]"}, "pending", AT.BLOCKED_ON_HOLDER, id="the holder is held down"),
        pytest.param(set(), None, AT.NO_INJECTION, id="nothing is held down"),
    ],
)
def test_o97_the_attributer_drives_the_deadline_to_a_pending_restore_only_for_the_agent(held, restore, why):
    """O97: through `enforce`, with a tracker that diagnoses and an abort that frees the survivor: the attributer's
    verdict, read at the `survived` step, is what leaves the restore pending, and the record carries the verdict."""
    clock = Clock()
    procs = Diagnosing({12: "reader"}, wedged=(12,), found=reading())
    esc, _ = escalation(clock, abort=lambda _: procs.release() or {"ok": True})
    esc = dataclasses.replace(esc, restorable=True, attribute=AT.attributer(lambda: held, HOLDERS))
    record = deadline.enforce(procs, esc)
    assert record.get("restore") == restore
    assert record["attribution"]["why"] == why
    assert steps(record).index("survived") < steps(record).index("abort")
