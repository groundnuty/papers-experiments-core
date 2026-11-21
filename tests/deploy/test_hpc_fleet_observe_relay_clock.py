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


def one_member(site: FakeSite) -> Path:
    """A member whose up recorded its relay serving, its one held job running; the path of the relay's heartbeat."""
    job = Job("RUNNING", submitted=site.now - 4000, started=site.now - 3600, ends=site.now + 4 * 3600)
    site.add_member("fed-1", jobs={"allocation": ("41", job)}, relay="here")
    return site.fleet / "fed-1" / "root" / "fault-relay" / "heartbeat.json"


def beat_at(path: Path, wall: float) -> None:
    """The relay's heartbeat as written at `wall` on the login node's clock."""
    record = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**record, "wall": wall}), encoding="utf-8")


def slow_site(site: FakeSite, monkeypatch) -> None:
    """Every job read the site answers takes SLOW_READ_S of the clock the observe command reads."""
    answer = site.run

    def run(argv: list, timeout: float):
        done = answer(argv, timeout)
        if argv[:3] == ["scontrol", "show", "job"]:
            site.now += SLOW_READ_S
        return done

    monkeypatch.setattr(site, "run", run)


def test_c65_a_member_read_a_minute_into_the_walk_with_a_heartbeat_5_s_old_at_its_read_is_not_gone(
    deploy, tmp_path, monkeypatch
):
    """C65: the walk began at T, the member's job read took 60 s, and its relay beat at T + 55: 5 s old when read."""
    site = FakeSite(tmp_path / "site-a", deploy)
    started = site.now
    path = one_member(site)
    beat_at(path, started + 55)
    slow_site(site, monkeypatch)
    view = observed(site)["members"][0]
    assert site.now == started + SLOW_READ_S  # the one job read, and the clock moved with it
    assert (view["relay"], view["relay_beat_at"]) == ("here", started + 55)


def test_c65_a_heartbeat_45_s_old_at_a_read_a_minute_into_the_walk_is_gone(deploy, tmp_path, monkeypatch):
    """C65: the walk began at T and the member's relay beat at T + 15: 45 s old when the member is read at T + 60, so
    gone (a reading taken at T would put it 15 s in the future and call it live)."""
    site = FakeSite(tmp_path / "site-a", deploy)
    started = site.now
    path = one_member(site)
    beat_at(path, started + 15)
    slow_site(site, monkeypatch)
    view = observed(site)["members"][0]
    assert (view["relay"], view["relay_beat_at"]) == ("gone", started + 15)


@pytest.mark.parametrize("what", ["no file", "no wall time", "not JSON"])
def test_c65_a_member_with_no_usable_heartbeat_is_gone(deploy, tmp_path, what):
    """C65: a relay its up recorded serving whose heartbeat is missing, has no wall time or cannot be read is gone."""
    site = FakeSite(tmp_path / "site-a", deploy)
    path = one_member(site)
    if what == "no file":
        path.unlink()
    elif what == "no wall time":
        path.write_text(json.dumps({"format": "relay", "pid": 7001, "start": 99}), encoding="utf-8")
    else:
        path.write_text("{", encoding="utf-8")
    view = observed(site)["members"][0]
    assert (view["relay"], view["relay_beat_at"]) == ("gone", None)


@pytest.mark.parametrize("ahead_s", [5.0, 45.0])
def test_c65_a_heartbeat_newer_than_the_reading_is_live(deploy, tmp_path, ahead_s):
    """C65: a heartbeat stamped after the reading (the walk's clock behind it, or clock jitter) is live, however far:
    only one older than the reading by more than the bound is gone."""
    site = FakeSite(tmp_path / "site-a", deploy)
    path = one_member(site)
    beat_at(path, site.now + ahead_s)
    view = observed(site)["members"][0]
    assert (view["relay"], view["relay_beat_at"]) == ("here", site.now + ahead_s)
