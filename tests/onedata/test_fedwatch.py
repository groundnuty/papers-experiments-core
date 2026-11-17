"""The federation's own services, observed over a window, the same on both backends (O86).

Over a fake container set (Compose: `docker compose ps` and `docker inspect` through an injected runner) and over the
documents `deploy/hpc-federation/hpcobserve.py` prints (HPC), with the real fault journal. Nothing runs Docker, Slurm or
a network, and a read of a federation that cannot be read is unknown, never stable.
"""

import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from fault_support import write_faults

from expcore_onedata.client import PlatformError
from expcore_onedata.compose import CommandResult, ComposeDriver
from expcore_onedata.config import load_platform
from expcore_onedata.fault_journal import InjectionJournal
from expcore_onedata.fault_settings import load_faults
from expcore_onedata.fedwatch import FORMAT, ComposeSource, FederationWatch, HpcSource, Member, members_of, watch_for

ME = {"process": "proc-1", "campaign": "campaign-1", "attempt_id": 5}  # the run a window is of
ANOTHER = {"process": "proc-1", "campaign": "campaign-1", "attempt_id": 6}  # an earlier attempt of its campaign
PROCESS_ONLY = {"process": "proc-1"}  # an injection outside any attempt (a tool's, a gate's heal)
ZONE, ALPHA, BETA = "zn-svc", "store-one", "store-two"  # a backend's names for the services: never in a result
MEMBERS = (Member("zone", ZONE, None), Member("providers[0]", ALPHA, "prov-a"), Member("providers[1]", BETA, "prov-b"))
DOC_AT = "2026-11-17T04:00:00Z"


class Clock:
    """Each call is an hour after the last: the window's start, then its end."""

    def __init__(self):
        self.now = 1_794_880_800.0

    def __call__(self):
        self.now += 3600.0
        return self.now


class Client:
    """The platform client's `get`, as far as the liveness read uses it: `/configuration`, of the zone or a provider."""

    def __init__(self, failing=()):
        self.failing = set(failing)  # providers (None for the zone) whose answer fails
        self.calls: list[tuple] = []

    def get(self, path, *, provider=None, timeout_s=None, retry=None, **rest):
        self.calls.append((path, provider, timeout_s))
        assert path == "/configuration"
        assert not rest
        if provider in self.failing:
            raise PlatformError(
                f"GET {'zone' if provider is None else 'providers[0]'} failed: the connection was refused"
            )
        return {"version": "21.02"}


# --- Compose: a fake container set ------------------------------------------------------------------------------------


def container(cid, started="2026-11-17T01:00:00Z", *, running=True, restarts=0, health="healthy", exit_code=0):
    state = {
        "Status": "running" if running else "exited", "Running": running, "Restarting": False, "Paused": False,
        "OOMKilled": False, "ExitCode": exit_code, "StartedAt": started,
        "FinishedAt": "0001-01-01T00:00:00Z" if running else "2026-11-17T03:00:00Z", "Error": "",
    }  # fmt: skip
    if health:
        state["Health"] = {"Status": health}
    return {"Id": cid, "RestartCount": restarts, "State": state}


class Containers:
    """What `docker compose ps` and `docker inspect` answer for a set of services; every command is kept."""

    def __init__(self, **services):
        self.by_service = dict(services)  # service -> the container dict, or None (no container), or an exit code
        self.commands: list[list[str]] = []

    def __call__(self, argv, *, timeout_s):
        self.commands.append(list(argv))
        if "ps" in argv:
            have = self.by_service[argv[-1]]
            if isinstance(have, int):
                return CommandResult(have, "", "docker: could not reach the daemon")
            return CommandResult(0, "" if have is None else have["Id"] + "\n")
        if "inspect" in argv:
            found = [c for c in self.by_service.values() if isinstance(c, dict) and c["Id"] == argv[-1]]
            return CommandResult(0, json.dumps(found))
        raise AssertionError(f"a command that is not a read: {argv}")


def compose_watch(containers, tmp_path, *, client=None, members=MEMBERS, journal=True):
    driver = ComposeDriver(Path("/srv/stack/compose.yaml"), project="stack", runner=containers, probe=lambda s: True)
    where = tmp_path / "fault-journal"
    return FederationWatch(
        members, ComposeSource(driver), client or Client(), journal=where if journal else None, clock=Clock()
    )


def steady():
    return Containers(**{ZONE: container("ctr-z"), ALPHA: container("ctr-a"), BETA: container("ctr-b")})


def journal_of(tmp_path):
    return InjectionJournal(tmp_path / "fault-journal")


def injection(journal, service, *, heal=False, clears="restored", stop=True, owner=None):
    """What the injector writes for one outage (or, with `heal`, one heal's container restart), as faults.py does; with
    an `owner`, the owner token it writes (`liveness.owner()`, O80), else a line written before owners were recorded."""
    entry = uuid.uuid4().hex
    line = {
        "target": "a-target",
        "service": service,
        "identity_sha256": "f" * 64,
        "holder": {"pid": 1, "boot_id": None},
        **({} if owner is None else {"owner": owner}),
    }
    journal.append({"event": "injecting", "id": entry, **line, **({"heal": True} if heal else {})})
    if stop:
        journal.append({"event": "stopping", "id": entry})
        journal.append({"event": "injected", "id": entry})
    if clears:
        journal.append({"event": "cleared", "id": entry, "how": clears})
    return entry


def test_o86_a_stable_window_has_no_restart_and_every_service_live_at_both_ends(tmp_path):
    """O86: no container changed and every service answered /configuration at the start and at the end: stable."""
    containers, client = steady(), Client()
    watch = compose_watch(containers, tmp_path, client=client)
    start = watch.read()
    result = watch.window(start)
    assert result["format"] == FORMAT
    assert result["backend"] == "compose"
    assert result["verdict"] == "stable"
    assert result["findings"] == []
    assert result["unknown"] == []
    assert sorted(result["services"]) == ["providers[0]", "providers[1]", "zone"]
    for seen in result["services"].values():
        assert seen["status"] == "stable"
        assert seen["restarts"] == []
        assert seen["unknown"] == []
        assert seen["start"]["state"] == seen["end"]["state"] == "running"
        assert seen["start"]["live"]["answered"] is True
        assert seen["end"]["live"]["answered"] is True
    assert {call[0] for call in client.calls} == {"/configuration"}
    assert len(client.calls) == 6
    assert {call[1] for call in client.calls} == {None, "prov-a", "prov-b"}


def test_o86_a_restart_the_journal_explains_is_labelled_outage(tmp_path):
    """O86: a provider's container started again inside the window, and the journal holds an injection of that service
    written after the start reading (stop asked, restore cleared): an explained restart, labelled outage."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ALPHA] = container("ctr-a", "2026-11-17T03:00:00Z")
    injection(journal_of(tmp_path), ALPHA)
    result = watch.window(start)
    seen = result["services"]["providers[0]"]
    assert seen["status"] == "restarted"
    assert result["verdict"] == "restarted"
    assert result["findings"] == []
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("outage", True)]
    assert result["services"]["providers[1]"]["status"] == "stable"


def test_o86_a_heals_container_restart_is_labelled_heal(tmp_path):
    """O86: the injection the journal holds carries `heal`: the restart is labelled heal, not outage."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[BETA] = container("ctr-b", "2026-11-17T03:10:00Z")
    injection(journal_of(tmp_path), BETA, heal=True)
    seen = watch.window(start)["services"]["providers[1]"]
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("heal", True)]


def test_o86_a_restart_the_journal_does_not_explain_is_a_finding(tmp_path):
    """O86: a container started again and no journal line names its service: a finding, never silent, and the verdict
    is finding."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ZONE] = container("ctr-z", "2026-11-17T03:30:00Z")
    result = watch.window(start)
    seen = result["services"]["zone"]
    assert seen["status"] == "restarted"
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("finding", False)]
    assert result["verdict"] == "finding"
    assert len(result["findings"]) == 1
    assert "zone" in result["findings"][0]


def test_o86_an_injection_of_another_service_or_from_before_the_window_explains_nothing(tmp_path):
    """O86: journal lines from before the start reading, and an injection of another service, explain nothing."""
    injection(journal_of(tmp_path), ALPHA)  # an earlier window's outage, cleared before this one began
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ALPHA] = container("ctr-a", "2026-11-17T03:00:00Z")
    injection(journal_of(tmp_path), BETA)  # another service's, in the window
    result = watch.window(start)
    assert [r["label"] for r in result["services"]["providers[0]"]["restarts"]] == ["finding"]
    assert result["verdict"] == "finding"


def test_o86_an_injection_that_never_stopped_the_service_explains_no_restart(tmp_path):
    """O86: an `injecting` line with no stop asked, or one cleared `never_down`, explains no restart."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ALPHA] = container("ctr-a", "2026-11-17T03:00:00Z")
    injection(journal_of(tmp_path), ALPHA, stop=False, clears=None)
    injection(journal_of(tmp_path), ALPHA, clears="never_down")
    assert [r["label"] for r in watch.window(start)["services"]["providers[0]"]["restarts"]] == ["finding"]


def test_o86_a_late_cleared_line_does_not_explain_a_restart_made_before_the_window(tmp_path):
    """O86: an injection restarted the service and was verified before the start reading, and only its `cleared` line
    was written after it. A restart in the window is not that one's: a finding."""
    journal = journal_of(tmp_path)
    entry = injection(journal, ALPHA, clears=None)  # stop and restore done; its clear is not written yet
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    journal.append({"event": "cleared", "id": entry, "how": "restored"})
    containers.by_service[ALPHA] = container("ctr-a", "2026-11-17T03:00:00Z")  # a restart nobody journalled
    result = watch.window(start)
    assert [r["label"] for r in result["services"]["providers[0]"]["restarts"]] == ["finding"]
    assert result["verdict"] == "finding"


def test_o86_a_service_down_at_the_start_and_restored_in_the_window_is_explained(tmp_path):
    """O86: an injection's stop came before the window and its restore (a start) inside it: the journal explains it."""
    journal = journal_of(tmp_path)
    entry = injection(journal, ALPHA, clears=None)  # injected, not cleared: the service is down
    containers = steady()
    containers.by_service[ALPHA] = container("ctr-a", running=False, health=None, exit_code=0)
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    assert start["services"]["providers[0]"]["state"] == "not_running"
    containers.by_service[ALPHA] = container("ctr-a", "2026-11-17T03:00:00Z")
    journal.append({"event": "cleared", "id": entry, "how": "restored"})
    seen = watch.window(start)["services"]["providers[0]"]
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("outage", True)]


def test_o86_an_outage_still_open_at_the_end_is_held_and_explained(tmp_path):
    """O86: the injector stopped a provider in the window and has not restored it: `held`, and no finding for it; the
    window's verdict is `held`, not `restarted`: nothing restarted."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ALPHA] = container("ctr-a", running=False, health=None, exit_code=0)
    injection(journal_of(tmp_path), ALPHA, clears=None)
    result = watch.window(start)
    assert result["services"]["providers[0]"]["status"] == "held"
    assert result["services"]["providers[0]"]["restarts"] == []
    assert result["findings"] == []
    assert result["verdict"] == "held"
    assert result["ownership"] == "not_checked"


def test_o86_a_restart_by_dockers_own_policy_is_a_finding_even_with_a_journal_line(tmp_path):
    """O86: RestartCount rose: Docker's restart policy brought the container back, which the injector's stop and start
    never does. A finding, whatever the journal holds."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ALPHA] = container("ctr-a", "2026-11-17T03:00:00Z", restarts=1)
    injection(journal_of(tmp_path), ALPHA)
    seen = watch.window(start)["services"]["providers[0]"]
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("finding", False)]
    assert "RestartCount" in seen["restarts"][0]["reason"]


def test_o86_a_recreated_container_is_a_restart(tmp_path):
    """O86: another container id under the service's name is a restart as much as a changed start time."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[BETA] = container("ctr-b-new")
    assert watch.window(start)["services"]["providers[1]"]["status"] == "restarted"


def test_o86_a_service_not_running_at_the_end_is_down_and_a_finding_unless_an_open_injection_holds_it(tmp_path):
    """O86: a container stopped by the end is `down`; with no open injection of it in the window that is a finding."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ALPHA] = container("ctr-a", running=False, health=None, exit_code=137)
    result = watch.window(start)
    assert result["services"]["providers[0]"]["status"] == "down"
    assert result["verdict"] == "finding"
    assert result["services"]["providers[0]"]["end"]["exit_code"] == 137


def test_o86_a_container_inspection_that_lacks_a_field_is_unknown_never_stable(tmp_path):
    """O86: an inspection without RestartCount or StartedAt cannot show a restart; the parser's defaults are not
    readings, so the service is unknown at both ends and the window is not stable."""
    containers = steady()
    containers.by_service[ALPHA] = {"Id": "ctr-a", "State": {"Status": "running", "Running": True}}
    watch = compose_watch(containers, tmp_path)
    result = watch.window(watch.read())
    seen = result["services"]["providers[0]"]
    assert seen["status"] == "unknown"
    assert any("lacks a field" in reason for reason in seen["unknown"])
    assert result["verdict"] == "unknown"


def test_o86_a_service_with_no_container_is_unknown_not_down(tmp_path):
    """O86: a service `docker compose ps` lists no container for was removed or never made: unknown with the reason,
    and an open injection of it explains nothing (an injection's stop leaves its container)."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ALPHA] = None
    injection(journal_of(tmp_path), ALPHA, clears=None)
    result = watch.window(start)
    seen = result["services"]["providers[0]"]
    assert seen["status"] == "unknown"
    assert any("no container" in reason for reason in seen["unknown"])
    assert result["verdict"] == "unknown"


def test_o86_an_unreadable_container_is_unknown_with_a_reason_never_stable(tmp_path):
    """O86: the daemon cannot be reached for one service at the end: that service is unknown with the reason, the
    window is unknown, and no other verdict hides it."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ALPHA] = 1
    result = watch.window(start)
    seen = result["services"]["providers[0]"]
    assert seen["status"] == "unknown"
    assert seen["end"]["state"] == "unknown"
    assert seen["end"]["running"] is None
    assert any("exit code 1" in reason for reason in seen["unknown"])
    assert result["verdict"] == "unknown"
    assert result["unknown"]
    assert result["services"]["providers[1]"]["status"] == "stable"


def test_o86_no_reading_at_the_start_is_unknown_never_assumed_stable(tmp_path):
    """O86: a window with no start reading, or one of another federation, cannot show a service ran throughout."""
    watch = compose_watch(steady(), tmp_path)
    for start in (None, {}, {"format": "other"}):
        result = watch.window(start)
        assert result["verdict"] == "unknown"
        assert all(s["status"] == "unknown" for s in result["services"].values())


def test_o86_a_liveness_answer_that_fails_is_unknown_with_its_reason(tmp_path):
    """O86: /configuration refused by a provider at the end: unknown, with the reason; the running container does not
    make it healthy."""
    client = Client()
    watch = compose_watch(steady(), tmp_path, client=client)
    start = watch.read()
    client.failing.add("prov-a")
    seen = watch.window(start)["services"]["providers[0]"]
    assert seen["status"] == "unknown"
    assert seen["end"]["live"]["answered"] is None
    assert "connection was refused" in seen["end"]["live"]["reason"]
    assert seen["start"]["live"]["answered"] is True


def test_o86_a_service_not_running_is_not_asked_for_its_configuration(tmp_path):
    """O86: a stopped container is not asked: its liveness is no answer, with the reason, and not an unreadable one."""
    containers = steady()
    containers.by_service[ALPHA] = container("ctr-a", running=False, health=None)
    client = Client()
    start = compose_watch(containers, tmp_path, client=client).read()
    assert start["services"]["providers[0]"]["live"] == {"answered": None, "reason": "the service is not running"}
    assert "prov-a" not in {call[1] for call in client.calls}


@pytest.mark.parametrize("damage", ["garbled", "shrunk"])
def test_o86_a_journal_that_cannot_be_read_explains_nothing(tmp_path, damage):
    """O86: a journal line that is not JSON, or a journal shorter than at the start, leaves a restart a finding with
    the reason: an explanation never rests on a journal that cannot be read."""
    journal = journal_of(tmp_path)
    injection(journal, BETA)
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ALPHA] = container("ctr-a", "2026-11-17T03:00:00Z")
    if damage == "garbled":
        injection(journal, ALPHA)
        with open(journal.path, "a", encoding="utf-8") as handle:
            handle.write("this is not json\n")
    else:
        journal.path.write_text("", encoding="utf-8")
    result = watch.window(start)
    restart = result["services"]["providers[0]"]["restarts"][0]
    assert (restart["label"], restart["explained"]) == ("finding", False)
    assert "journal" in restart["reason"]
    assert result["journal"]["read"] is not True


def test_o86_a_window_with_no_journal_declared_leaves_every_restart_a_finding(tmp_path):
    """O86: no `[faults]` journal: nothing can explain a restart."""
    containers = steady()
    watch = compose_watch(containers, tmp_path, journal=False)
    start = watch.read()
    containers.by_service[ALPHA] = container("ctr-a", "2026-11-17T03:00:00Z")
    result = watch.window(start)
    assert [r["label"] for r in result["services"]["providers[0]"]["restarts"]] == ["finding"]
    assert "no journal" in result["services"]["providers[0]"]["restarts"][0]["reason"]


def test_o86_no_command_writes_and_the_journal_directory_is_left_as_it_was(tmp_path):
    """O86: only `ps` and `inspect` run, and the journal's directory holds the same files, byte for byte, after the
    read: no lock file made, no line added."""
    journal = journal_of(tmp_path)
    injection(journal, ALPHA)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in journal.directory.iterdir()}
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    watch.window(watch.read())
    assert all(("ps" in argv or "inspect" in argv) for argv in containers.commands)
    assert not any(word in argv for argv in containers.commands for word in ("stop", "start", "restart", "exec", "up"))
    assert {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in journal.directory.iterdir()} == before


def test_o86_a_result_names_no_service_and_no_provider(tmp_path):
    """O86 (O12): the result is keyed by `zone` and `providers[i]`; no backend service name, no provider name."""
    containers = steady()
    watch = compose_watch(containers, tmp_path, client=Client(failing={"prov-b"}))
    start = watch.read()
    containers.by_service[ALPHA] = container("ctr-a", "2026-11-17T03:00:00Z")
    text = json.dumps({"start": start, "window": watch.window(start)})
    for name in (ZONE, ALPHA, BETA, "prov-a", "prov-b", "stack", "compose.yaml"):
        assert name not in text


def test_o86_members_of_labels_the_zone_and_each_declared_provider(config):
    """O86: `zone` and `providers[i]` by the provider's position in platform.toml, with the service each is read by."""
    name = next(iter(config.providers))
    members = members_of(config, zone="zn-svc", providers={name: "store-one"})
    assert [(m.label, m.service, m.provider) for m in members] == [
        ("zone", "zn-svc", None),
        (config.provider_label(name), "store-one", name),
    ]
    with pytest.raises(ValueError, match="declared"):
        members_of(config, zone="zn-svc", providers={"not-declared": "x"})


# --- whose an injection is: a run's window counts only its own (#371's owner) -----------------------------------------


def down_target(tmp_path, *, owner, clears=None):
    """The pilot's order: the target is down, held by an injection, when the window opens, and still when it ends."""
    containers = steady()
    journal = journal_of(tmp_path)
    injection(journal, ALPHA, clears=clears, owner=owner)
    containers.by_service[ALPHA] = container("ctr-a", running=False, health=None, exit_code=0)
    watch = compose_watch(containers, tmp_path)
    return watch, watch.read(), containers, journal


def test_o86_a_target_down_at_both_ends_by_the_runs_own_open_injection_is_held_not_restarted(tmp_path):
    """O86: the injection came before the start reading and is still open at the end: the run's own injection holds the
    target down, `held`, explained and no finding; every other service is `stable`; the verdict is `held`."""
    watch, start, _, _ = down_target(tmp_path, owner=ME)
    result = watch.window(start, owner=ME)
    held = result["services"]["providers[0]"]
    assert (held["status"], held["restarts"], held["start"]["state"], held["end"]["state"]) == (
        "held", [], "not_running", "not_running",
    )  # fmt: skip
    assert held["held"]["injections"] == 1
    assert held["held"]["opened"] == "before the window"
    assert "this run owns" in held["held"]["reason"]
    assert "restore outcome" in held["held"]["reason"]
    assert [result["services"][label]["status"] for label in ("zone", "providers[1]")] == ["stable", "stable"]
    assert (result["verdict"], result["findings"], result["ownership"]) == ("held", [], "run")


@pytest.mark.parametrize(
    ("owner", "why"),
    [
        (ANOTHER, "not this run's"),
        (None, "that are not this run's"),
        (PROCESS_ONLY, "that are not this run's"),
    ],
    ids=["another attempt's", "ownerless", "a process alone"],
)
def test_o86_a_down_target_whose_injection_is_not_the_runs_is_a_finding(tmp_path, owner, why):
    """O86: an earlier attempt's leftover, a line written before owners were recorded and an injection outside any
    attempt hold the service down but are not this run's: it is `down` with a finding, never `held`."""
    watch, start, _, _ = down_target(tmp_path, owner=owner)
    result = watch.window(start, owner=ME)
    assert result["services"]["providers[0]"]["status"] == "down"
    assert result["services"]["providers[0]"]["held"] is None
    assert result["verdict"] == "finding"
    assert any(why in finding for finding in result["findings"])


def test_o86_a_run_that_names_no_attempt_owns_no_injection(tmp_path):
    """O86: a window asked of a process alone (no attempt) cannot tell its injection from another's: none is its own."""
    watch, start, _, _ = down_target(tmp_path, owner=PROCESS_ONLY)
    result = watch.window(start, owner=PROCESS_ONLY)
    assert result["ownership"] == "unattributed"
    assert result["services"]["providers[0]"]["status"] == "down"
    assert any("names no attempt" in finding for finding in result["findings"])


@pytest.mark.parametrize(
    ("owner", "label", "verdict"),
    [(ME, "outage", "restarted"), (ANOTHER, "finding", "finding"), (None, "finding", "finding")],
    ids=["the run's own", "another attempt's", "ownerless"],
)
def test_o86_a_restart_by_the_runs_own_injection_is_explained_and_another_attempts_is_a_finding(
    tmp_path, owner, label, verdict
):
    """O86: the same restart, in the window, is explained only when the injection that has a line in it is the run's."""
    containers = steady()
    watch = compose_watch(containers, tmp_path)
    start = watch.read()
    containers.by_service[ALPHA] = container("ctr-a", "2026-11-17T03:00:00Z")
    injection(journal_of(tmp_path), ALPHA, owner=owner)
    result = watch.window(start, owner=ME)
    seen = result["services"]["providers[0]"]
    assert [r["label"] for r in seen["restarts"]] == [label]
    assert result["verdict"] == verdict
    reason = seen["restarts"][0]["reason"]
    assert ("this run's own" in reason) if owner is ME else ("not this run's" in reason or "no journalled" in reason)


def test_o86_only_the_injections_target_may_restart_or_be_down_and_another_service_that_does_is_a_finding(tmp_path):
    """O86: an S3 run's target is held and explained; a second provider that restarted or is down in the same window
    is not the target of any injection of the run: a finding, even inside an injecting run."""
    watch, start, containers, _ = down_target(tmp_path, owner=ME)
    containers.by_service[BETA] = container("ctr-b", "2026-11-17T03:00:00Z")  # restarted, nothing journalled for it
    result = watch.window(start, owner=ME)
    assert result["services"]["providers[0]"]["status"] == "held"
    assert [r["label"] for r in result["services"]["providers[1]"]["restarts"]] == ["finding"]
    assert result["verdict"] == "finding"
    containers.by_service[BETA] = container("ctr-b", running=False, health=None, exit_code=137)
    result = watch.window(start, owner=ME)
    assert result["services"]["providers[1]"]["status"] == "down"
    assert result["verdict"] == "finding"


def test_o86_the_verdict_follows_the_worst_service_finding_unknown_held_restarted_stable(tmp_path):
    """O86: held outranks restarted, unknown outranks held, and a finding outranks them all."""
    watch, start, containers, _ = down_target(tmp_path, owner=ME)
    containers.by_service[BETA] = container("ctr-b-new")
    injection(journal_of(tmp_path), BETA, owner=ME)  # the second service restarted, and its restart is the run's too
    assert watch.window(start, owner=ME)["verdict"] == "held"
    assert watch.window(start, owner=ME)["services"]["providers[1]"]["status"] == "restarted"
    containers.by_service[ZONE] = None  # no container: unknown
    assert watch.window(start, owner=ME)["verdict"] == "unknown"
    containers.by_service[ZONE] = container("ctr-z", running=False, health=None)  # not running: a finding
    assert watch.window(start, owner=ME)["verdict"] == "finding"


def test_o86_a_window_with_no_run_named_counts_any_injection_and_says_so(tmp_path):
    """O86: a batch's window (no `owner`) is as it was: any open injection holds a service down and any one explains a
    restart, and the result says the ownership was not checked."""
    watch, start, _, _ = down_target(tmp_path, owner=ANOTHER)
    result = watch.window(start)
    assert result["services"]["providers[0]"]["status"] == "held"
    assert result["ownership"] == "not_checked"
    assert "whose they are is not checked" in result["services"]["providers[0]"]["held"]["reason"]


# --- HPC: the documents hpcobserve prints ---------------------------------------------------------------------------


def hpc_record(step, *, launch=1, started="2026-11-17T01:00:00", running=True, stops=0, unknown=None, **more):
    stopped = [{"requested_utc": "2026-11-17T02:30:00Z", "confirmed_utc": "2026-11-17T02:30:05Z", "stopped": True}]
    record = {
        "kind": "op", "launch": launch, "step": step, "running": running,
        "state": {True: "RUNNING", False: "COMPLETED", None: None}[running], "started_at": started,
        "ended_at": "2026-11-17T03:00:00" if running is False else None, "exit_code": 0 if running is False else None,
        "state_differs": False, "stops": stopped * stops, "unknown": unknown,
    }  # fmt: skip
    return {**record, **more}


class Cluster:
    """The document `hpcobserve.py` prints, as a runner answers it; a test sets what the next read finds."""

    def __init__(self, **services):
        self.services = dict(services)
        self.returncode, self.raises, self.commands = 0, None, []

    def __call__(self, argv, *, timeout_s):
        self.commands.append(list(argv))
        if self.raises is not None:
            raise self.raises
        doc = {"format": "expcore-hpc-observe/1", "at": DOC_AT, "services": self.services}
        return CommandResult(self.returncode, json.dumps(doc) + "\n", "")


def hpc_steady():
    return Cluster(**{ZONE: hpc_record("4242.3", kind="oz"), ALPHA: hpc_record("4242.5"), BETA: hpc_record("4242.6")})


def hpc_watch(cluster, tmp_path, *, client=None, journal=True):
    source = HpcSource(["python", "-I", "hpcobserve.py", "--root", "/r"], runner=cluster)
    where = tmp_path / "fault-journal"
    return FederationWatch(MEMBERS, source, client or Client(), journal=where if journal else None, clock=Clock())


def test_o86_a_stable_hpc_window_is_stable(tmp_path):
    """O86: on HPC the same: step and start unchanged, every service live at both ends."""
    cluster = hpc_steady()
    watch = hpc_watch(cluster, tmp_path)
    result = watch.window(watch.read())
    assert result["backend"] == "hpc"
    assert result["verdict"] == "stable"
    assert all(s["status"] == "stable" for s in result["services"].values())
    assert cluster.commands[0] == ["python", "-I", "hpcobserve.py", "--root", "/r"]


def test_o86_an_hpc_restart_with_a_journal_line_and_a_recorded_stop_is_explained(tmp_path):
    """O86: a provider's step id and start changed, its launch rose by one, the journal holds its injection, and the
    state records one more stop than at the start: explained, labelled outage."""
    cluster = hpc_steady()
    watch = hpc_watch(cluster, tmp_path)
    start = watch.read()
    cluster.services[ALPHA] = hpc_record("4242.9", launch=2, started="2026-11-17T03:00:00", stops=1)
    injection(journal_of(tmp_path), ALPHA)
    seen = watch.window(start)["services"]["providers[0]"]
    assert seen["status"] == "restarted"
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("outage", True)]
    assert (seen["start"]["step"], seen["end"]["step"], seen["end"]["launch"]) == ("4242.5", "4242.9", 2)


def test_o86_an_hpc_restart_with_a_journal_line_but_no_recorded_stop_is_a_finding(tmp_path):
    """O86: a running service that restarted must have been stopped through the verbs: the state records no stop in the
    window, so the journal line alone does not explain it."""
    cluster = hpc_steady()
    watch = hpc_watch(cluster, tmp_path)
    start = watch.read()
    cluster.services[ALPHA] = hpc_record("4242.9", launch=2, started="2026-11-17T03:00:00", stops=0)
    injection(journal_of(tmp_path), ALPHA)
    seen = watch.window(start)["services"]["providers[0]"]
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("finding", False)]
    assert "stop" in seen["restarts"][0]["reason"]


def test_o86_an_hpc_service_launched_twice_with_one_injection_has_one_unexplained_launch(tmp_path):
    """O86: the launch counter rose by two and the journal accounts for one injection: one restart explained, one a
    finding (the count is checked, not only the change)."""
    cluster = hpc_steady()
    watch = hpc_watch(cluster, tmp_path)
    start = watch.read()
    cluster.services[ALPHA] = hpc_record("4242.11", launch=3, started="2026-11-17T03:40:00", stops=2)
    injection(journal_of(tmp_path), ALPHA)
    labels = [(r["label"], r["explained"]) for r in watch.window(start)["services"]["providers[0]"]["restarts"]]
    assert labels == [("outage", True), ("finding", False)]


def test_o86_an_hpc_service_down_at_the_start_and_started_in_the_window_needs_no_new_stop(tmp_path):
    """O86: an injection's stop came before the window, its restore (a start) inside it: the journal explains it and no
    new stop is owed."""
    cluster = hpc_steady()
    cluster.services[ALPHA] = hpc_record("4242.5", running=False, stops=1)
    watch = hpc_watch(cluster, tmp_path)
    journal = journal_of(tmp_path)
    entry = injection(journal, ALPHA, clears=None)  # open at the start: injected, not cleared
    start = watch.read()
    cluster.services[ALPHA] = hpc_record("4242.9", launch=2, started="2026-11-17T03:00:00", stops=1)
    journal.append({"event": "cleared", "id": entry, "how": "restored"})
    seen = watch.window(start)["services"]["providers[0]"]
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("outage", True)]


def test_o86_an_hpc_restart_the_state_never_saw_is_still_a_restart(tmp_path):
    """O86: the state's launch is unchanged but squeue lists another step: the restart happened outside the driver; it
    is read from the step, and with no journal line it is a finding."""
    cluster = hpc_steady()
    watch = hpc_watch(cluster, tmp_path)
    start = watch.read()
    cluster.services[BETA] = hpc_record("4242.12", launch=1, started="2026-11-17T03:05:00", state_differs=True)
    seen = watch.window(start)["services"]["providers[1]"]
    assert [r["label"] for r in seen["restarts"]] == ["finding"]


def test_o86_a_restart_outside_the_driver_behind_an_injection_is_a_second_restart_and_a_finding(tmp_path):
    """O86: the launch counter rose by one (the injection's restore) and the state also names another step than the one
    squeue lists: the injection explains the launch, not the restart the driver never made."""
    cluster = hpc_steady()
    watch = hpc_watch(cluster, tmp_path)
    start = watch.read()
    cluster.services[ALPHA] = hpc_record("4242.9", launch=2, started="2026-11-17T03:00:00", stops=1, state_differs=True)
    injection(journal_of(tmp_path), ALPHA)
    seen = watch.window(start)["services"]["providers[0]"]
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("outage", True), ("finding", False)]


def test_o86_an_outside_restart_is_counted_though_the_state_already_named_another_step_at_the_start(tmp_path):
    """O86: the state already named another step than the one listed at the start. In the window one injection launched
    a step and a restart outside the driver replaced it: the end still differs from the state, so the injection
    explains one restart and the other is a finding."""
    cluster = hpc_steady()
    cluster.services[ALPHA] = hpc_record("4242.8", launch=1, state_differs=True)
    watch = hpc_watch(cluster, tmp_path)
    start = watch.read()
    cluster.services[ALPHA] = hpc_record(
        "4242.10", launch=2, started="2026-11-17T03:40:00", stops=1, state_differs=True
    )
    injection(journal_of(tmp_path), ALPHA)
    seen = watch.window(start)["services"]["providers[0]"]
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("outage", True), ("finding", False)]


def test_o86_a_launch_counter_that_went_backwards_explains_nothing(tmp_path):
    """O86: the state's launch fell between the readings while the step changed: the restarts cannot be counted, so
    even a journalled injection with a recorded stop leaves it a finding, and the service is unknown."""
    cluster = hpc_steady()
    cluster.services[ALPHA] = hpc_record("4242.8", launch=3)
    watch = hpc_watch(cluster, tmp_path)
    start = watch.read()
    cluster.services[ALPHA] = hpc_record("4242.9", launch=2, started="2026-11-17T03:00:00", stops=1)
    injection(journal_of(tmp_path), ALPHA)
    result = watch.window(start)
    seen = result["services"]["providers[0]"]
    assert [(r["label"], r["explained"]) for r in seen["restarts"]] == [("finding", False)]
    assert seen["status"] == "unknown"
    assert result["verdict"] == "finding"


def test_o86_an_hpc_step_that_slurm_cannot_read_is_unknown_with_its_reason(tmp_path):
    """O86: the document says a service's step could not be read (Slurm down, no accounting): unknown, with the reason
    the deploy side gave; the service is neither running nor stable."""
    cluster = hpc_steady()
    watch = hpc_watch(cluster, tmp_path)
    start = watch.read()
    cluster.services[ALPHA] = hpc_record(
        "4242.5", running=None, started=None, unknown="squeue -s failed with status 1: slurmctld down"
    )
    result = watch.window(start)
    seen = result["services"]["providers[0]"]
    assert seen["status"] == "unknown"
    assert seen["end"]["running"] is None
    assert any("slurmctld" in reason for reason in seen["unknown"])
    assert result["verdict"] == "unknown"


@pytest.mark.parametrize(
    "failure",
    [("returncode", 1), ("raises", OSError("no such file")), ("raises", TimeoutError("late"))],
    ids=["exit 1", "cannot run", "timed out"],
)
def test_o86_an_hpc_read_that_fails_leaves_every_service_unknown(tmp_path, failure):
    """O86: the read command failing, not running or timing out is every service unknown with a reason."""
    cluster = hpc_steady()
    watch = hpc_watch(cluster, tmp_path)
    start = watch.read()
    setattr(cluster, *failure)
    result = watch.window(start)
    assert result["verdict"] == "unknown"
    assert all(s["status"] == "unknown" and s["unknown"] for s in result["services"].values())


def test_o86_an_hpc_document_that_is_not_one_is_unknown(tmp_path):
    """O86: a document with a service missing leaves that service unknown and the others as they are."""
    cluster = hpc_steady()
    watch = hpc_watch(cluster, tmp_path)
    start = watch.read()
    del cluster.services[BETA]
    result = watch.window(start)
    assert result["services"]["providers[1]"]["status"] == "unknown"
    assert result["services"]["providers[0]"]["status"] == "stable"


def test_o86_both_backends_return_the_same_shape(tmp_path):
    """O86: the result has the same keys at every level on Compose and on HPC; only values differ."""

    def shape(value):
        if isinstance(value, dict):
            return {key: shape(item) for key, item in value.items()}
        return "list" if isinstance(value, list) else "value"

    compose = compose_watch(steady(), tmp_path / "c")
    hpc = hpc_watch(hpc_steady(), tmp_path / "h")
    a, b = compose.window(compose.read()), hpc.window(hpc.read())
    assert shape(a["services"]) == shape(b["services"])
    assert set(a) == set(b)
    assert set(a["services"]["zone"]["start"]) == set(b["services"]["zone"]["start"])


def test_o86_watch_for_builds_the_watch_the_faults_table_describes(fake, tmp_path):
    """O86: `watch_for` takes the providers and the journal from `[faults]` and the zone from the caller, and reads a
    Compose federation through Compose and an HPC one through the command it is given."""
    path = write_faults(tmp_path)
    config = load_platform(path)
    settings = load_faults(path, config)
    containers = Containers(
        **{ZONE: container("ctr-z"), "store-one": container("ctr-a"), "store-two": container("ctr-b")}
    )
    watch = watch_for(config, settings, Client(), zone=ZONE, runner=containers)
    result = watch.window(watch.read())
    assert sorted(result["services"]) == ["providers[0]", "providers[1]", "zone"]
    assert result["verdict"] == "stable"
    hpc = replace(settings, backend="hpc")
    with pytest.raises(ValueError, match="hpcobserve"):
        watch_for(config, hpc, Client(), zone=ZONE)
    cluster = Cluster(
        **{ZONE: hpc_record("4242.3", kind="oz"), "store-one": hpc_record("4242.5"), "store-two": hpc_record("4242.6")}
    )
    hpc_watch_of = watch_for(config, hpc, Client(), zone=ZONE, hpc_command=["python", "-I", "x.py"], runner=cluster)
    assert hpc_watch_of.window(hpc_watch_of.read())["verdict"] == "stable"
