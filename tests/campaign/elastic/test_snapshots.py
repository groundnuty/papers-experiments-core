"""C46: when a running lease's `out/` is snapshotted: at an attempt's end and at the cadence, never ending the run
when one fails, and once more after the run."""

from pathlib import Path

from expcore.campaign import out_snapshot
from expcore.campaign.elastic.snapshots import LeaseSnapshots
from expcore.campaign.markers import MARKER_NAME


class Taken:
    """A stand-in for `out_snapshot.take`: what it was asked, and a failure for the first call when asked to."""

    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[bool] = []
        self.fail_first = fail_first

    def __call__(self, out: Path, into: Path, *, scratch: Path, facts: dict, live: bool, now: str) -> Path:
        self.calls.append(live)
        if self.fail_first and len(self.calls) == 1:
            raise out_snapshot.OutSnapshotError("a store that cannot be read")
        return into / "snap-000001"


def _lease_out(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    (out / out_snapshot.STORE).write_bytes(b"a store")
    return out


def _an_attempt_ended(out: Path, number: int) -> None:
    marker = out / "runs" / f"run{number}" / "try1" / MARKER_NAME
    marker.parent.mkdir(parents=True)
    marker.write_text("{}", encoding="utf-8")


def _snapshots(tmp_path: Path, take: Taken, clock: list[float]) -> LeaseSnapshots:
    return LeaseSnapshots(
        _lease_out(tmp_path),
        tmp_path / "into",
        tmp_path / "scratch",
        {},
        every_s=900.0,
        take=take,
        clock=lambda: clock[0],
    )


def test_c46_a_snapshot_is_taken_when_an_attempt_began_or_ended_and_when_the_cadence_passed(tmp_path):
    """C46: a poll that sees the first attempt begin (`runs/` appears: a run shorter than the cadence, lost with its
    federation, must leave a store to read), an attempt's marker, or `every_s` since the last snapshot, takes one; any
    other poll takes none (a snapshot copies the whole `out/` to the shared file system)."""
    take, clock = Taken(), [0.0]
    snapshots = _snapshots(tmp_path, take, clock)
    clock[0] += 30.0
    snapshots.poll()
    assert take.calls == []
    (tmp_path / "out" / "runs").mkdir()
    clock[0] += 30.0
    snapshots.poll()
    assert take.calls == [True]
    clock[0] += 30.0
    snapshots.poll()
    assert take.calls == [True]
    _an_attempt_ended(tmp_path / "out", 1)
    clock[0] += 30.0
    snapshots.poll()
    assert take.calls == [True, True]
    clock[0] += 30.0
    snapshots.poll()
    assert take.calls == [True, True]
    clock[0] += 900.0
    snapshots.poll()
    assert take.calls == [True, True, True]


def test_c46_a_snapshot_that_fails_is_reported_and_never_ends_the_run_and_the_last_one_is_taken_after_it(
    tmp_path, capsys
):
    """C46: a take that raises leaves the last good snapshot and says so; the next one is still taken, and `finish`
    takes the last one of a store its dispatcher has left (read from a private copy, not live)."""
    take, clock = Taken(fail_first=True), [0.0]
    snapshots = _snapshots(tmp_path, take, clock)
    clock[0] += 900.0
    snapshots.poll()
    assert "failed" in capsys.readouterr().err
    clock[0] += 900.0
    snapshots.poll()
    snapshots.finish()
    assert take.calls == [True, True, False]
