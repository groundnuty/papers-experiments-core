"""C65: the fleet's read of a member's fault relay (deploy/hpc-federation/fleetobserve.py) ages the heartbeat against a
clock read when that member's heartbeat is read, not one taken before the walk, and a heartbeat newer than the reading
is live. A walk over many members' slow site reads takes minutes, and with one reading taken at its start every member
read after the first 30 s had a live heartbeat newer than that reading and read as a relay gone."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from control_site_support import FakeSite, Job, observed
from hpc_fleet_support import fleet_modules

SLOW_READ_S = 60.0  # what the site takes to answer one job read, on the clock the controller's host keeps


@pytest.fixture(scope="module")
def deploy():
    with fleet_modules() as found:
        import fleetcontrol
        import fleetobserve
        import fleetrecord
        import hpcslurm
        import hpcstate

        yield SimpleNamespace(fleet=found.fleet, fleetcontrol=fleetcontrol, fleetobserve=fleetobserve,
                              fleetrecord=fleetrecord, hpcslurm=hpcslurm, hpcstate=hpcstate)  # fmt: skip


def member(site: FakeSite, n: int = 1) -> Path:
    """Member fed-N, its up recorded its relay serving, its one held job running; the path of the relay's heartbeat."""
    job = Job("RUNNING", submitted=site.now - 4000, started=site.now - 3600, ends=site.now + 4 * 3600)
    site.add_member(f"fed-{n}", jobs={"allocation": (str(40 + n), job)}, relay="here")
    return site.fleet / f"fed-{n}" / "root" / "fault-relay" / "heartbeat.json"


def beat_at(path: Path, wall) -> None:
    """The relay's heartbeat as written at `wall` on the login node's clock."""
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "wall": wall}), encoding="utf-8")


def slow_site(site: FakeSite, monkeypatch, read_s: float = SLOW_READ_S) -> None:
    """Every job read the site answers takes `read_s` of the clock the observe command reads."""
    answer = site.run

    def run(argv: list, timeout: float):
        done = answer(argv, timeout)
        if argv[:3] == ["scontrol", "show", "job"]:
            site.now += read_s
        return done

    monkeypatch.setattr(site, "run", run)


def replay(site: FakeSite, monkeypatch, read_s: float, ages: list) -> dict:
    """A walk over members fed-1..fed-N, one job read of `read_s` each: member k is read at T + k * read_s, and its
    relay's heartbeat is `ages[k - 1]` s old at that read. The members as `observe` printed them, by label."""
    start = site.now
    slow_site(site, monkeypatch, read_s)
    for n, age in enumerate(ages, 1):
        beat_at(member(site, n), start + n * read_s - age)
    members = {view["label"]: view for view in observed(site)["members"]}
    assert site.now == start + len(ages) * read_s  # every member's job was read, and the clock moved with each
    return members


def test_c65_a_member_read_a_minute_into_the_walk_with_a_heartbeat_5_s_old_at_its_read_is_not_gone(
    deploy, tmp_path, monkeypatch
):
    """C65: the walk began at T, the member's job read took 60 s, and its relay beat at T + 55: 5 s old when read."""
    site = FakeSite(tmp_path / "site-a", deploy)
    started = site.now
    beat_at(member(site), started + 55)
    slow_site(site, monkeypatch)
    view = observed(site)["members"][0]
    assert site.now == started + SLOW_READ_S  # the one job read, and the clock moved with it
    assert (view["relay"], view["relay_beat_at"]) == ("here", started + 55)


def test_c65_a_heartbeat_45_s_old_at_a_read_a_minute_into_the_walk_is_gone(deploy, tmp_path, monkeypatch):
    """C65: the walk began at T and the member's relay beat at T + 15: 45 s old when the member is read at T + 60, so
    gone (a reading taken at T would put it 15 s in the future and call it live)."""
    site = FakeSite(tmp_path / "site-a", deploy)
    started = site.now
    beat_at(member(site), started + 15)
    slow_site(site, monkeypatch)
    view = observed(site)["members"][0]
    assert (view["relay"], view["relay_beat_at"]) == ("gone", started + 15)


def test_c65_replay_of_a_tick_over_16_members_in_164_s_reads_every_relay_beating_7_s_ago_as_here(
    deploy, tmp_path, monkeypatch
):
    """C65: the live tick that drained a fleet: 16 members walked over a 164 s read (10.25 s a member), each relay's
    heartbeat about 7 s old at its own read. None is gone (on the previous code every member read more than 37 s into
    the walk, 13 of the 16, was)."""
    site = FakeSite(tmp_path / "site-a", deploy)
    members = replay(site, monkeypatch, 10.25, [7.0] * 16)
    assert {label: view["relay"] for label, view in members.items()} == {f"fed-{n}": "here" for n in range(1, 17)}


def test_c65_replay_of_a_tick_over_11_members_in_206_s_reads_only_the_two_genuinely_stale_relays_as_gone(
    deploy, tmp_path, monkeypatch
):
    """C65: the second live tick: 11 members walked over a 206 s read (18.75 s a member), 9 relays' heartbeats 3 to 7 s
    old at their own read and 2 (fed-3 and fed-8) 151 and 156 s old. Only those two are gone, with their last
    heartbeat (on the previous code the early reading read fed-8 live and the late fresh ones gone)."""
    site = FakeSite(tmp_path / "site-a", deploy)
    ages = [3.0, 4.0, 151.0, 5.0, 6.0, 7.0, 3.0, 156.0, 4.0, 5.0, 6.0]
    start = site.now
    members = replay(site, monkeypatch, 18.75, ages)
    assert {label for label, view in members.items() if view["relay"] == "gone"} == {"fed-3", "fed-8"}
    assert {label: members[label]["relay"] for label in members if label not in ("fed-3", "fed-8")} == {
        f"fed-{n}": "here" for n in (1, 2, 4, 5, 6, 7, 9, 10, 11)
    }
    assert members["fed-3"]["relay_beat_at"] == start + 3 * 18.75 - 151.0
    assert members["fed-8"]["relay_beat_at"] == start + 8 * 18.75 - 156.0


@pytest.mark.parametrize("what", ["no file", "no wall time", "not JSON", "infinite wall time", "NaN wall time"])
def test_c65_a_member_with_no_usable_heartbeat_is_gone(deploy, tmp_path, what):
    """C65: a relay its up recorded serving whose heartbeat is missing, has no wall time (or one that is no time: an
    infinity or a NaN) or cannot be read is gone."""
    site = FakeSite(tmp_path / "site-a", deploy)
    path = member(site)
    if what == "no file":
        path.unlink()
    elif what == "no wall time":
        path.write_text(json.dumps({"format": "relay", "pid": 7001, "start": 99}), encoding="utf-8")
    elif what == "not JSON":
        path.write_text("{", encoding="utf-8")
    else:
        beat_at(path, float("inf") if what.startswith("infinite") else float("nan"))
    view = observed(site)["members"][0]
    assert (view["relay"], view["relay_beat_at"]) == ("gone", None)


@pytest.mark.parametrize("ahead_s", [5.0, 45.0])
def test_c65_a_heartbeat_newer_than_the_reading_is_live(deploy, tmp_path, ahead_s):
    """C65: a heartbeat stamped after the reading (the walk's clock behind it, or clock jitter) is live, however far:
    only one older than the reading by more than the bound is gone."""
    site = FakeSite(tmp_path / "site-a", deploy)
    path = member(site)
    beat_at(path, site.now + ahead_s)
    view = observed(site)["members"][0]
    assert (view["relay"], view["relay_beat_at"]) == ("here", site.now + ahead_s)
