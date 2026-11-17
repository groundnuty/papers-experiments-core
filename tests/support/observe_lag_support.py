"""A fake of the two Slurm reads the HPC federation's observed read makes (`squeue -s`, `sacct`), whose accounting
trails a step's stop by a number of seconds that its own clock counts (O86). Nothing sleeps, and no cluster is used."""

from dataclasses import dataclass

NODE = "node-private-07"  # a node's name as the state holds it: never in a reading
FEDERATION = "fedx-3f9a"
STOP = {"service": "alpha", "stopped": True, "ended": "exit", "seconds": 3.0, "requested_utc": "2026-11-17T02:10:00Z",
        "confirmed_utc": "2026-11-17T02:10:04Z", "node": NODE}  # fmt: skip
NOT_CONFIRMED = {**STOP, "stopped": False, "confirmed_utc": None}
STARTED, ENDED_AT = "2026-11-17T01:00:00", "2026-11-17T02:10:03"


class Clock:
    """The time a wait lives in: `sleep` moves it, and nothing really sleeps."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


@dataclass
class Step:
    """A service's step as the cluster shows it. Running: listed, and a RUNNING row. Stopped: not listed (the driver's
    stop confirmed that), and accounting shows `before` until `lag` seconds after the stop, and CANCELLED from then;
    `listed_for` keeps squeue listing it that long as well (a controller that is behind)."""

    id: str
    name: str
    stopped: bool = False
    lag: float = 0.0
    before: str = "running"  # what accounting holds until `lag`: running, none, completing, unended or ended
    listed_for: float = 0.0


class Cluster:
    """The login node's `squeue -s` and `sacct` for one job, answering by the clock; every command is kept."""

    def __init__(self, clock, *steps, squeue_rc=0, sacct_rc=0):
        self.clock, self.steps, self.squeue_rc, self.sacct_rc = clock, steps, squeue_rc, sacct_rc
        self.calls: list[list[str]] = []

    def run(self, argv, timeout):
        from hpcslurm import Completed

        self.calls.append(list(argv))
        now = self.clock.now - 1000.0
        if argv[0] == "squeue" and "-s" in argv:
            if self.squeue_rc:
                return Completed(self.squeue_rc, "", "slurm_load_jobs error: Unable to contact slurm controller")
            listed = [s for s in self.steps if not s.stopped or now < s.listed_for]
            lines = ["4242.batch batch", "4242.extern extern", *(f"{s.id} {s.name}" for s in listed)]
            return Completed(0, "".join(f"{line}\n" for line in lines), "")
        if argv[0] == "sacct":
            if self.sacct_rc:
                return Completed(self.sacct_rc, "", "sacct: error: slurmdbd unreachable")
            rows = [("4242.batch", "batch", "RUNNING", STARTED, "Unknown", "0:0")]
            rows += [row for s in self.steps if (row := self._row(s, now)) is not None]
            return Completed(0, "".join("|".join(row) + "\n" for row in rows), "")
        raise AssertionError(f"a command that is not a read: {argv}")

    @staticmethod
    def _row(step, now):
        if not step.stopped:
            return (step.id, step.name, "RUNNING", STARTED, "Unknown", "0:0")
        kind = step.before if now < step.lag else "caught up"
        if kind == "none":
            return None
        if kind == "running":
            return (step.id, step.name, "RUNNING", STARTED, "Unknown", "0:0")
        if kind == "completing":
            return (step.id, step.name, "COMPLETING", STARTED, "Unknown", "0:0")
        if kind == "unended":
            return (step.id, step.name, "CANCELLED", STARTED, "Unknown", "0:15")
        return (step.id, step.name, "CANCELLED", STARTED, ENDED_AT, "0:15")  # ended, or caught up


def state_of(**services):
    """A federation's state as `up` leaves it, trimmed to what the read uses: each service as (kind, launch, step id,
    the stops the state records, the service's state)."""
    return {
        "format": "expcore-hpc-federation/1",
        "project": FEDERATION,
        "phase": "linked",
        "site": {},
        "allocation": {"name": "alloc", "job": "4242", "ends": "2026-11-17T20:00:00"},
        "services": {
            name: {"kind": kind, "node": NODE, "launch": launch, "state": at,
                   "step": {"name": f"alloc-{name}-run-{launch}", "id": step}, "stops": stops}
            for name, (kind, launch, step, stops, at) in services.items()
        } | {"agent": {"kind": "sandbox", "node": NODE, "step": None}},
    }  # fmt: skip
