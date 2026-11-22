"""The O97 live proof's shared pieces: what the scripted agent runs, its script, and the pass list as pure functions.

The driver (`tests/live/fuse_abort_apptainer.py`) runs the proof on a node; the minimal study it writes
(`tests/support/fuse_proof_study/`) runs the registered attempt through the campaign; the offline dry run
(`tests/onedata/sandbox/apptainer/test_fuse_proof_dry_run.py`) runs both over the fake node. All three take their cells,
their script and their checks from here, so a live run can differ from the dry run only in the site.

The facts are plain JSON with outcomes, counts, seconds, labels and hashes: never a token, an address, a host or a
configured path. A file is named by its space's label and its path below the space, as the record names it.
`problems(facts)` lists what is wrong with them, P1 to P12 of the plan (`e2e/fuse-abort-helios-proof-plan.private.md`);
an empty list is the proof. A check whose facts are missing is a problem, never a pass ("path not exercised").
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from expcore.mountguard.flags import verdict_of

BOUND_S = 30.0  # the study's limit on one code cell: well under oneclient's 120 s provider timeout
GRACE_S = 5.0  # the sandbox's kill grace, which bounds the escalation (`deadline.margin_s`)
TIME_CAP_S = 900  # the agent's time cap: far above the bound, so the bound is what ends a blocked cell
TOOL = "python"
OWNER_MARK = "# fuse-proof: owner-only read"  # a cell's first line: which cell a fake node's handler is asked for
LOCAL_MARK = "# fuse-proof: local read"
FINAL = "The scripted agent read what it could."
TIMEOUT_TEXT = f"the code ran past its {BOUND_S:g} s limit and was killed"
FD_CALLS = frozenset({0, 17, 19, 295, 327, 217, 5, 74, 326, 275, 16, 40})  # `attribution.FD_CALLS`, x86_64
PATH_CALLS = frozenset({257, 262, 332, 2, 4, 6, 269, 267})  # `attribution.PATH_CALLS`
OUTSIDE_A_SYSCALL = -1
CHECK_COUNT = 12  # the plan's checks, P1 to P12; named by `check`, so no short name is a literal of the code


def check(number: int) -> str:
    """The name of the plan's check `number`: P and the number."""
    return f"P{number}"


CHECKS = tuple(check(n) for n in range(1, CHECK_COUNT + 1))


# ------------------------------------------------------------------------------------------------ the agent's cells
def owner_read(path: str) -> str:
    """A cell that reads the first 4 KiB of the owner-only file, as a study's script reads a data file."""
    return (
        f"{OWNER_MARK}\nwith open({path!r}, 'rb') as stream:\n    data = stream.read(4096)\nprint('read', len(data))\n"
    )


def local_read(path: str, root: str) -> str:
    """A cell that lists the space's root and prints the SHA-256 of the local file: what the next cell after a restore
    must still be able to do."""
    return (
        f"{LOCAL_MARK}\nimport hashlib, os\nprint('listed', len(os.listdir({root!r})))\n"
        f"with open({path!r}, 'rb') as stream:\n    print('sha256', hashlib.sha256(stream.read()).hexdigest())\n"
    )


def script(owner: str, local: str, root: str, *, second_cycle: bool, owner_reads: bool = True) -> list[Any]:
    """The stub's turns: the owner-only read, the local read, with `second_cycle` the owner-only read again (a second
    abort and restore, P6), then the final text. Without `owner_reads` (the control condition, every provider up) the
    local read alone: a read of the owner-only file with its owner up would give the local provider a copy of it, and
    the registered read would then be served locally and never block."""
    if not owner_reads:
        return [[(TOOL, {"code": local_read(local, root)})], FINAL]
    turns: list[Any] = [[(TOOL, {"code": owner_read(owner)})], [(TOOL, {"code": local_read(local, root)})]]
    if second_cycle:
        turns.append([(TOOL, {"code": owner_read(owner)})])
    turns.append(FINAL)
    return turns


# ------------------------------------------------------------------------------------------------ reading a record
def _waits_on(survivor: Mapping[str, Any], rest: str | None) -> bool:
    """Whether a survivor's recorded wait names the file `rest` (its path below its space) on the data mount: the
    survivor's own `waits_on`, since a recorded task keeps no blocking object of its own."""
    waits = [w for w in survivor.get("waits_on") or [] if isinstance(w, Mapping)]
    return rest is not None and any(w.get("mount") == "data" and w.get("path") == rest for w in waits)


def _d_tasks(survivor: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [t for t in survivor.get("tasks") or [] if isinstance(t, Mapping) and t.get("state") == "D"]


def _call_known(task: Mapping[str, Any]) -> bool:
    """Whether a task in D says what it waits in: a wait channel, or a system call's fd, of a call the attribution
    reads (an fd or path call, or a page fault)."""
    syscall = task.get("syscall")
    nr = syscall.get("nr") if isinstance(syscall, Mapping) else None
    listed = nr in FD_CALLS | PATH_CALLS | {OUTSIDE_A_SYSCALL}
    return listed and (bool(task.get("wchan")) or (isinstance(syscall, Mapping) and syscall.get("fd") is not None))


def deadline_facts(deadline: Mapping[str, Any] | None, rest: str | None) -> dict[str, Any] | None:
    """What a call's `cell_deadline` shows, as the checks read it: steps, outcomes and timings, the attribution, and,
    per survivor, whether one survivor in D both names the expected file (`rest`) and says what it waits in. No space
    name, no path and no text of the record is kept: only labels, counts, numbers and booleans."""
    if not isinstance(deadline, Mapping):
        return None
    steps = [s for s in deadline.get("steps") or [] if isinstance(s, Mapping)]
    survived = next((s for s in steps if s.get("step") == "survived"), {})
    abort = next((s for s in steps if s.get("step") == "abort"), {})
    restored = next((s for s in steps if s.get("step") == "restored"), {})
    ended = next((s for s in steps if s.get("step") == "ended"), {})
    survivors = [s for s in deadline.get("survivors") or [] if isinstance(s, Mapping)]
    attribution = deadline.get("attribution") or {}
    return {
        "outcome": deadline.get("outcome"),
        "steps": [s.get("step") for s in steps],
        "survived_left": survived.get("left"),
        "survived_states": list(survived.get("states") or []),
        "abort_ok": abort.get("ok"),
        "abort_method": abort.get("method"),
        "waiting_before": abort.get("waiting_before"),
        "waiting_after": abort.get("waiting_after"),
        "abort_t_s": abort.get("t_s"),
        "ended_t_s": ended.get("t_s"),
        "margin_s": deadline.get("margin_s"),
        "restored_generation": restored.get("generation"),
        "task_states": [t.get("state") for s in survivors for t in s.get("tasks") or [] if isinstance(t, Mapping)],
        # one survivor carries both: a task in D whose call is known, and a wait on the expected file (never one
        # survivor's file joined with another's call)
        "blocked_on_expected": any(_waits_on(s, rest) and any(_call_known(t) for t in _d_tasks(s)) for s in survivors),
        "attribution": {k: attribution.get(k) for k in ("verdict", "why", "target", "space_holder")},
    }


def call_facts(
    call: Mapping[str, Any], *, rest: str | None, bound_s: float, local_sha256: str | None
) -> dict[str, Any]:
    """One tool call of a record, as the checks read it: the two things its text must say, as booleans (that it was
    told as the bound's timeout, and that it printed the local file's digest), never the text itself."""
    text = str(call.get("text") or "")
    return {
        "status": call.get("status"),
        "error_class": call.get("error_class"),
        "timeout_told": f"the code ran past its {bound_s:g} s limit and was killed" in text,
        "local_sha_read": local_sha256 is not None and f"sha256 {local_sha256}" in text,
        "paused_s": (call.get("infra_wait_by_kind") or {}).get("sandbox_lifecycle", 0.0),
        "deadline": deadline_facts(call.get("cell_deadline"), rest),
    }


def record_facts(
    record: Mapping[str, Any], *, rest: str | None, bound_s: float, local_sha256: str | None
) -> dict[str, Any]:
    """What the registered attempt's record shows, as the checks read it (`rest`: the registered file's path below its
    space, which a survivor must be shown to wait on)."""
    outcome = record.get("outcome") or {}
    guard = (record.get("instruments") or {}).get("mount_guard")
    generations = guard.get("generations") if isinstance(guard, Mapping) else None
    return {
        "outcome": outcome.get("name"),
        "infra_kind": outcome.get("infra_kind"),
        "calls": [
            call_facts(c, rest=rest, bound_s=bound_s, local_sha256=local_sha256) for c in record.get("tool_calls") or []
        ],
        "guard_verdict": verdict_of(guard) if isinstance(guard, Mapping) else None,
        "guard_generations": len(generations) if isinstance(generations, list) else (1 if guard else 0),
    }


def negative_summary(
    deadline: Mapping[str, Any] | None, *, rest: str | None, restore_pending: bool, next_cell: str | None,
    local_held_before: int | None,
) -> dict[str, Any]:  # fmt: skip
    """What the unregistered sandbox's cell showed (P12), kept in place of its raw deadline: no name or path."""
    facts = deadline_facts(deadline, rest)
    return {
        "blocked": facts is not None,
        "outcome": None if facts is None else facts["outcome"],
        "abort_ok": None if facts is None else facts["abort_ok"],
        "ended": facts is not None and "ended" in facts["steps"],
        "blocked_on_expected": facts is not None and facts["blocked_on_expected"],
        "verdict": None if facts is None else facts["attribution"]["verdict"],
        "why": None if facts is None else facts["attribution"]["why"],
        "restore_pending": restore_pending,
        "next_cell": next_cell,
        "local_held_before": local_held_before,
    }


# ------------------------------------------------------------------------------------------------ the pass list
def _blocked(deadline: Mapping[str, Any] | None, *, generation: int) -> list[str]:
    """P1 and P2 of one call whose owner-only read blocked: the path fired, the abort freed it, the mount came back."""
    if deadline is None:
        return ["path not exercised: the call has no cell_deadline (the read did not block past the bound)"]
    found = []
    steps = deadline["steps"]
    if not ("kill" in steps and "survived" in steps and "abort" in steps and "ended" in steps):
        found.append(f"the escalation's steps are {steps}, not kill, survived, abort, ended")
    if not (isinstance(deadline["survived_left"], int) and deadline["survived_left"] >= 1):
        found.append("no survivor of the kill was recorded")
    if "D" not in deadline["survived_states"] and "D" not in deadline["task_states"]:
        found.append("no survivor or task was in D")
    if deadline["abort_ok"] is not True or deadline["abort_method"] not in ("fusectl", "daemon_kill"):
        found.append(f"the abort was {deadline['abort_ok']} by {deadline['abort_method']}")
    if deadline["abort_method"] == "fusectl" and not (
        isinstance(deadline["waiting_before"], int)
        and deadline["waiting_before"] >= 1
        and deadline["waiting_after"] == 0
    ):
        found.append(f"waiting went {deadline['waiting_before']} -> {deadline['waiting_after']}, not >= 1 -> 0")
    if not (
        isinstance(deadline["ended_t_s"], int | float)
        and isinstance(deadline["margin_s"], int | float)
        and deadline["ended_t_s"] <= deadline["margin_s"]
    ):
        found.append("the cell did not end within the deadline's margin")
    if deadline["outcome"] != "mount_restored" or deadline["restored_generation"] != generation:
        found.append(
            f"the deadline is {deadline['outcome']} at generation {deadline['restored_generation']}, "
            f"not mount_restored at generation {generation}"
        )
    return found


def _attributed(deadline: Mapping[str, Any] | None, target: str | None) -> list[str]:
    """P11: the verdict is the agent's, on the held-down holder's space."""
    if deadline is None:
        return ["path not exercised: no cell_deadline to read the attribution from"]
    want = {"verdict": "agent", "why": "blocked_on_held_down_holder", "target": target, "space_holder": target}
    return [] if deadline["attribution"] == want else [f"the attribution is {deadline['attribution']}, not {want}"]


def _on_expected(deadline: Mapping[str, Any] | None) -> list[str]:
    """P8: one survivor in D says what it waits in and names the registered file on the data mount."""
    if deadline is None or deadline["blocked_on_expected"]:
        return []
    return ["no survivor in D both names the registered file on the data mount and says what call it waits in"]


def control_problems(result: Mapping[str, Any], local_sha256: str | None = None) -> list[str]:
    """P7 of the control sandbox: three reads, each the local file's own digest, and its mount never touched."""
    found = []
    reads = result.get("reads") or []
    if len(reads) < 3 or len(set(reads)) != 1 or reads[0] is None:
        found.append(f"its local reads were {reads}: not three equal ones")
    elif local_sha256 is not None and reads[0] != local_sha256:
        found.append("its local reads are not the local file's digest")
    if result.get("remounts") != 0:
        found.append(f"its mount was remounted {result.get('remounts')} time(s)")
    if result.get("aborted") not in (None, False):
        found.append("its mount was aborted")
    return found


def negative_problems(summary: Mapping[str, Any]) -> list[str]:
    """P12 of the unregistered sandbox's cell: it blocked on its own file of the held-down provider, the abort freed
    it (`mount_aborted`, every process gone), the verdict is infrastructure (`no_injection_in_effect`), nothing was
    restored, and the next cell is refused as `cell_unkillable`; the local provider held none of its file before."""
    if not summary.get("blocked"):
        return ["path not exercised: the unregistered read did not block past the bound"]
    found = []
    if summary.get("local_held_before") != 0:
        found.append(f"the local provider held {summary.get('local_held_before')} byte(s) of its file before")
    if not summary.get("blocked_on_expected"):
        found.append("no survivor in D named its own owner-only file")
    if (summary.get("verdict"), summary.get("why")) != ("infrastructure", "no_injection_in_effect"):
        found.append(f"its verdict is {summary.get('verdict')}/{summary.get('why')}, not infrastructure/no_injection")
    if summary.get("outcome") != "mount_aborted" or summary.get("abort_ok") is not True or not summary.get("ended"):
        found.append(
            f"its deadline is {summary.get('outcome')} (abort {summary.get('abort_ok')}), not freed by the abort"
        )
    if summary.get("restore_pending"):
        found.append("an unregistered block was left to be restored as the agent's")
    if summary.get("next_cell") != "cell_unkillable":
        found.append(f"its next cell was {summary.get('next_cell')}, not refused as cell_unkillable")
    return found


def problems(facts: Mapping[str, Any]) -> list[str]:
    """What is wrong with the proof's facts (the module's docstring), each prefixed with its check; empty: it passed.
    Every verdict is computed here from the facts, never taken from a list the facts carry."""
    found: list[str] = []

    def add(name: str, items: Sequence[str]) -> None:
        found.extend(f"{name}: {item}" for item in items)

    for key in ("cut", "stopped"):
        if facts.get(key):
            add(check(1), [f"the proof did not run whole ({key})"])
    if facts.get("seeded") is not True:
        add(check(1), ["the seeded files were not all seen by the providers that must see them"])
    if facts.get("local_held_before") != 0:
        add(check(1), [f"the local provider held {facts.get('local_held_before')} byte(s) of the registered file"])
    s = facts.get("registered") or {}
    calls = s.get("calls") or []
    target, sha = facts.get("target"), facts.get("local_sha256")
    first = calls[0] if len(calls) > 0 else None
    second = calls[1] if len(calls) > 1 else None
    third = calls[2] if len(calls) > 2 else None
    if first is None:
        add(check(1), ["path not exercised: the registered attempt made no call"])
    else:
        add(check(1), _blocked(first["deadline"], generation=2))
        if first["error_class"] != "timeout" or not first["timeout_told"]:
            add(check(3), [f"call 1 is {first['status']}/{first['error_class']}, not a timeout told as the bound"])
        if not first["paused_s"] > 0:
            add(check(5), ["the restore did not pause the agent's clock (no sandbox_lifecycle wait on call 1)"])
        add(check(8), _on_expected(first["deadline"]))
        add(check(11), _attributed(first["deadline"], target))
    if facts.get("campaign_exit") != 0:
        add(check(4), [f"the campaign exited {facts.get('campaign_exit')}, not 0"])
    if s.get("outcome") != "finished" or s.get("infra_kind") is not None:
        add(check(4), [f"the attempt is {s.get('outcome')}/{s.get('infra_kind')}, not finished with no infra kind"])
    if facts.get("record_valid") is not True or facts.get("replayed") is not True:
        add(check(4), ["the record does not validate or does not replay"])
    if second is None or second["status"] != "ok" or not second["local_sha_read"]:
        add(check(5), ["call 2 did not read the local file whole after the restore"])
    if facts.get("second_cycle"):
        if third is None:
            add(check(6), ["path not exercised: no third call"])
        else:
            add(check(6), _blocked(third["deadline"], generation=3))
            add(check(6), _on_expected(third["deadline"]))
            add(check(6), _attributed(third["deadline"], target))
    add(check(7), control_problems(facts["control"], sha) if facts.get("control") else ["no control facts"])
    if s.get("guard_verdict") != "clean" or (s.get("guard_generations") or 0) < 2:
        add(check(9), [f"the mount guard is {s.get('guard_verdict')} over {s.get('guard_generations')} generation(s)"])
    add(check(10), list(facts.get("teardown_problems", ["no teardown facts"])))
    add(check(12), negative_problems(facts["negative"]) if facts.get("negative") else ["no negative facts"])
    return found


__all__ = [
    "BOUND_S",
    "CHECKS",
    "CHECK_COUNT",
    "FINAL",
    "GRACE_S",
    "LOCAL_MARK",
    "OWNER_MARK",
    "TIMEOUT_TEXT",
    "TIME_CAP_S",
    "TOOL",
    "call_facts",
    "check",
    "control_problems",
    "deadline_facts",
    "local_read",
    "negative_problems",
    "negative_summary",
    "owner_read",
    "problems",
    "record_facts",
    "script",
]
