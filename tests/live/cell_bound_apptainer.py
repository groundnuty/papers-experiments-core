"""The per-cell kill (L18) on a real node, over the real Apptainer sandbox: a script, not a test, since the node has no
pytest and a test allocation allows no environment. Run it from a private 0700 directory, once, under `python -I`:

    python -I tests/live/cell_bound_apptainer.py PLATFORM_TOML FACTS_OUT [--control] [--record-out FILE]

`PLATFORM_TOML` is a platform file whose `[sandbox]` table is the study's own (its `[sandbox.apptainer]` pins: the agent
image, the egress shim and collector, each a file with a hash): no data-path level, federation, token, mount, gate or
CA is needed. The sandbox runs the scenario of tests/support/cell_bound_live.py on this node (`LocalNode`): the bound
is 60 s, the agent's first cell starts a child and a detached grandchild and sleeps 180 s. The independent look is
this node's `ps`, filtered for the run's marker. With `--control` a second sandbox then runs the control (a 45 s cap
under the same bound: the loop's cut, not the bound, ends the cell). The facts go to `FACTS_OUT` as JSON, one object
per run (outcomes, counts and seconds only: no token, address, name or path); the exit status is 0 only when every
run's `problems` is empty.

The checkout is this file's own: `src` and `tests/support` beside it are put on the path by hand, since `-I` drops
`PYTHONPATH`, and nothing else is imported from outside it.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for entry in (ROOT / "src", ROOT / "tests" / "support"):
    sys.path.insert(0, str(entry))

from cell_bound_live import CAP_S, CONTROL_CAP_S, problems, run_scenario  # noqa: E402

from expcore_onedata.config import load_platform  # noqa: E402
from expcore_onedata.sandbox.api import DataPathMode, SandboxError, SandboxSpec  # noqa: E402
from expcore_onedata.sandbox.apptainer.node import LocalNode  # noqa: E402
from expcore_onedata.sandbox.apptainer.sandbox import ApptainerSandbox  # noqa: E402

MARKER = "cellbound-live"


def ps_look(node: LocalNode):
    """The independent look: the node's own process list, filtered for the marker (the filter runs here, so the `ps`
    command line never carries it)."""

    def look() -> list[str]:
        listed = node.run(["ps", "-eo", "pid,args"], 30)
        return [line for line in listed.stdout.splitlines() if MARKER in line]

    return look


def attempt(platform: Path, *, cap_s: float, record_out: Path | None) -> dict:
    """One scripted attempt over a sandbox of its own, and its facts with the problems found."""
    config = load_platform(platform, require_secrets=False)
    node = LocalNode()
    spec = SandboxSpec(run_key=f"expcore-live-cell-bound-{int(cap_s)}", mode=DataPathMode.NONE)
    sandbox = ApptainerSandbox.start(node, config.sandbox, spec)
    left: dict = {}
    try:
        facts = run_scenario(sandbox, ps_look(node), MARKER, cap_s=cap_s, record_out=record_out)
    finally:
        try:
            sandbox.stop()
            left["stopped"] = True
        except SandboxError as error:
            left["stop_error"] = type(error).__name__
    left["marked_after_stop"] = len(ps_look(node)())
    facts["after_stop"] = left
    found = problems(facts)
    if left != {"stopped": True, "marked_after_stop": 0}:
        found.append(f"after the stop: {left}")
    facts["problems"] = found
    return facts


def main(argv: list[str]) -> int:
    args = argv[1:]
    control = "--control" in args
    record_out = Path(args[args.index("--record-out") + 1]) if "--record-out" in args else None
    positional = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] != "--record-out")]
    if len(positional) != 2:
        print(__doc__)
        return 2
    platform, facts_out = Path(positional[0]), Path(positional[1])
    runs = {"bound": attempt(platform, cap_s=CAP_S, record_out=record_out)}
    if control:
        runs["control"] = attempt(platform, cap_s=CONTROL_CAP_S, record_out=None)
    facts_out.write_text(json.dumps(runs, indent=1, default=str), encoding="utf-8")
    found = {name: facts["problems"] for name, facts in runs.items()}
    print(json.dumps({"problems": found}))
    return 1 if any(found.values()) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
