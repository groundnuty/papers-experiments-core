"""`up` retries a service's launch once, and only when it is proved that nothing of the service ran (O57, live 17 Nov
2026): twice on the site, srun's fan-out on the login node could not find the nodes' addresses and every provider's
step was accounted CANCELLED with exit 0:116 within seconds, its run log empty (the bundle's first write never
happened). `up` now relaunches such a service ONCE, when BOTH proofs hold: (a) the step, by the exact id srun's client
names, is in sacct as exactly CANCELLED 0:116 and the client's log shows the task launch failing for want of an
address; (b) its run log is there and empty. Each retry is recorded with its proofs, never a host name. The provider
launches are also staggered. Over the fake Slurm of hpc_fed_support: every node name and step id here is invented."""

import importlib
import json
import re
from pathlib import Path

import pytest
from hpc_fed_support import LAYOUT, SITE, FakeSlurm, agent_site, hpc_on_path

PROVIDERS = ["aspen", "larch", "rowan", "alder", "hazel"]
NODE = re.compile(r"node[0-9]{2}")  # the fake's node names: never in a retry's record or in what `up` says
LIVE_CLIENT = (
    "srun: error: _fwd_tree_get_addr: can't find address for host node01, check slurm.conf\n"
    "srun: error: Task launch for StepId=4242.7 failed on node node01: Can't find an address, check slurm.conf\n"
    "srun: error: Application launch failed: Can't find an address, check slurm.conf\n"
    "srun: Job step aborted\n"
)


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        yield modules


@pytest.fixture
def proof(hpc):
    return importlib.import_module("hpclaunchproof")


@pytest.fixture
def site(tmp_path):
    images, logs = tmp_path / "images", tmp_path / "logs"
    images.mkdir()
    logs.mkdir()
    for name in ("onezone.tar", "oneprovider.tar"):
        (images / name).write_bytes(b"tar")
        (images / f"{name}.provenance").write_text("sha256\n", encoding="utf-8")
    (tmp_path / "fed").mkdir()
    return {**SITE, **agent_site(tmp_path / "agent"), "SH": str(images), "L": str(logs)}


class Rig:
    """A driver over the fake Slurm, the core's phase recorded instead of run; what it says is kept."""

    def __init__(self, hpc, tmp_path, site, fake=None):
        self.hpc = hpc
        self.fake = fake or FakeSlurm()
        self.root = tmp_path / "fed" / "root"
        self.site = site
        self.driver = hpc.cli.make_driver(LAYOUT, str(self.root), runner=self.fake)
        self.said = []
        self.slept = []
        self.driver.say = self.said.append
        self.driver.sleep = self.slept.append
        self.driver.poll_s = 0.0
        self.driver.slurm.sleep = lambda s: None
        self.driver.slurm.poll_s = 0.0
        self.driver.core = lambda layout, owner, values, environ=None: "core: the link check passed"
        self.driver.relay_available = lambda: False

    def up(self):
        return self.driver.up(self.hpc.settings.site_from_environment(self.site))

    def state(self):
        return self.driver.store.read()

    def launches(self, service):
        """The steps launched for a service, in order."""
        return [s for s in self.fake.spawned if re.search(rf"-{service}-run-[0-9]+$", s.name)]


def retries(rig, service):
    return rig.state()["services"][service].get("launch_retries", [])


def rows_of(rig):
    """The accounting rows the fake holds, by step name: (id, state, exit)."""
    found = {}
    for rows in rig.fake.step_rows.values():
        for row in rows:
            ident, name, state, code = row.split("|")
            found[name] = (ident, state, code)
    return found


def never_names_a_node(*texts):
    for text in texts:
        assert not NODE.search(text), f"a node's name in: {text}"


# -- the retry --------------------------------------------------------------------------------------------------------


def test_o57_a_provider_that_failed_at_launch_with_an_empty_log_is_launched_again_once_and_up_finishes(
    hpc, tmp_path, site
):
    """O57: a provider's first launch fails at launch (CANCELLED 0:116, an empty run log, the client names the failed
    task launch): up launches it again, once, finishes, and records the retry with its proofs."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{}]}))
    out = rig.up()
    assert out.startswith("core: the link check passed")
    state = rig.state()
    assert state["phase"] == "linked"
    larch = state["services"]["larch"]
    assert (larch["launch"], larch["state"]) == (2, "running")
    assert larch["step"]["name"].endswith("-larch-run-2")
    first = f"{state['allocation']['name']}-larch-run-1"
    ident, accounted, code = rows_of(rig)[first]
    assert retries(rig, "larch") == [
        {
            "launch": 1,
            "step": first,
            "step_id": ident,
            "sacct_state": "CANCELLED",
            "sacct_exit": "0:116",
            "run_log_bytes": 0,
            "failure": "task-launch-no-address",
        }
    ]
    assert (accounted, code) == ("CANCELLED", "0:116")
    assert [len(rig.launches(p)) for p in PROVIDERS] == [1, 2, 1, 1, 1]
    assert len(rig.launches("zone")) == 1
    assert {first, larch["step"]["name"]} <= set(state["clients"])  # down ends both clients
    assert all(not retries(rig, p) for p in ("zone", "aspen", "rowan", "alder", "hazel"))
    never_names_a_node(json.dumps(retries(rig, "larch")), "\n".join(rig.said), out)
    for word in ("larch", "run-1", "retried"):  # the retry is in the up's evidence
        assert word in out


def test_o57_every_provider_that_failed_at_launch_is_retried_by_the_id_its_client_names(hpc, tmp_path, site):
    """O57: all five providers fail their first launch, as live. By the time up waits for the later ones their steps
    were never listed, so no id is recorded in the state: each one's id comes from its client's log, and each is
    retried once, its sacct row found by that exact id."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={p: [{}] for p in PROVIDERS}))
    rig.up()
    state = rig.state()
    assert state["phase"] == "linked"
    ids = []
    for provider in PROVIDERS:
        (entry,) = retries(rig, provider)
        assert entry["step"] == f"{state['allocation']['name']}-{provider}-run-1"
        assert entry["step_id"] == rows_of(rig)[entry["step"]][0]
        ids.append(entry["step_id"])
        assert state["services"][provider]["launch"] == 2
        assert state["services"][provider]["state"] == "running"
    assert len(set(ids)) == 5


def test_o57_the_zone_that_failed_at_launch_is_retried_once_too(hpc, tmp_path, site):
    """O57: the retry is every service's, the zone's included."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"zone": [{}]}))
    rig.up()
    assert rig.state()["phase"] == "linked"
    assert len(rig.launches("zone")) == 2
    (entry,) = retries(rig, "zone")
    assert entry["launch"] == 1
    assert entry["step"].endswith("-zone-run-1")


def vanish_after_first_listing(fake, suffix):
    """The step of the name that ends in `suffix` leaves squeue -s once squeue has listed it once: live, the first
    provider's step was listed for about 12 s before it ended."""
    real = fake.run

    def run(argv, timeout):
        done = real(argv, timeout)
        if argv[0] == "squeue" and "-s" in argv:
            for name in [n for n in fake.steps if n.endswith(suffix)]:
                fake.steps.pop(name)
        return done

    fake.run = run


def test_o57_a_step_listed_before_it_ended_is_retried_when_the_id_the_state_holds_is_the_clients(hpc, tmp_path, site):
    """O57: where up saw the step listed it holds the step's id in the state: the retry's proofs then read that id and
    the client's agree (live, the first provider's step was listed for about 12 s)."""
    fake = FakeSlurm(launch_failures={"larch": [{"listed": True}]})
    vanish_after_first_listing(fake, "-larch-run-1")
    rig = Rig(hpc, tmp_path, site, fake)
    held = []
    real_proof = rig.driver.launch_proof
    rig.driver.launch_proof = lambda service: (
        held.append(rig.state()["services"][service]["step"]["id"]),
        real_proof(service),
    )[1]
    rig.up()
    (entry,) = retries(rig, "larch")
    assert held == [entry["step_id"]]  # the state held the id, and it is the one in the proofs
    assert len(rig.launches("larch")) == 2


def test_o57_an_id_the_state_holds_that_the_client_does_not_name_is_no_proof(hpc, tmp_path, site):
    """O57: the state holds the id up saw listed; srun's client and sacct agree on another: which step this launch
    was is not shown, so it is never launched again."""
    fake = FakeSlurm(launch_failures={"larch": [{"listed": True, "client_id": "4242.77", "row_id": "4242.77"}]})
    vanish_after_first_listing(fake, "-larch-run-1")
    rig = Rig(hpc, tmp_path, site, fake)
    with pytest.raises(RuntimeError, match="larch's step ended before its log said"):
        rig.up()
    assert len(rig.launches("larch")) == 1
    assert retries(rig, "larch") == []


def test_o57_down_after_a_retried_up_tears_down_the_second_launch_and_ends_both_clients(hpc, tmp_path, site):
    """O57: a federation whose provider was launched twice is torn down like any other: teardown on each service's
    own node (the second launch's step and log are the recorded ones), every step gone, the held job cancelled by its
    id, the failed launch's client ended too, and the root removed."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{}]}))
    rig.up()
    rig.fake.sruns.clear()
    rig.driver.down()
    teardowns = [s for s in rig.fake.sruns if s.command[1:2] and s.command[1].endswith("teardown.sh")]
    assert [s.node for s in teardowns] == [f"node{i:02d}" for i in (2, 3, 4, 5, 6, 1)]
    assert rig.fake.cancelled == ["4242"]
    assert len(rig.fake.ended_clients) == 7  # the six services' clients and the failed first launch's
    assert not rig.root.exists()


# -- the backoff before the retry ------------------------------------------------------------------------------------


def test_o57_the_retry_waits_the_declared_backoff_before_it_launches_again_and_it_is_recorded(hpc, tmp_path, site):
    """O57: the nodes resolved again a minute after the live failure, so the retry waits `launch_retry_backoff_s`
    (60 s unless changed) after the failure was proved and before the relaunch: exactly one pause between the two
    launches of the service, none before its first, nothing recorded as retried until the pause is over; the bound is
    recorded in the state file."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{}]}))
    assert rig.driver.launch_retry_backoff_s == 60.0
    rig.driver.launch_retry_backoff_s = 7.5
    events, held = [], []
    real_launch = rig.driver.launch
    rig.driver.launch = lambda service, argv: (events.append(("launch", service)), real_launch(service, argv))[1]
    real_sleep = rig.driver.sleep

    def sleep(seconds):
        events.append(("sleep", seconds))
        if seconds == 7.5:
            held.append(retries(rig, "larch"))
        real_sleep(seconds)

    rig.driver.sleep = sleep
    rig.up()
    mine = [e for e in events if e in (("launch", "larch"), ("sleep", 7.5))]
    assert mine == [("launch", "larch"), ("sleep", 7.5), ("launch", "larch")]
    assert held == [[]]  # the retry is written once the pause is over, with the run log seen empty again
    assert len(retries(rig, "larch")) == 1
    assert rig.state()["launch"]["retry_backoff_s"] == 7.5


def test_o57_a_run_log_that_gains_a_byte_during_the_backoff_cancels_the_retry(hpc, tmp_path, site):
    """O57: proof (b) is read again after the backoff, immediately before the relaunch: a log that holds a single byte
    by then means the service may have started, so it is never launched again, nothing is recorded as retried, and
    up fails as it always did."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{}]}))
    real_sleep = rig.driver.sleep

    def sleep(seconds):
        if seconds == rig.driver.launch_retry_backoff_s:
            Path(rig.state()["services"]["larch"]["log"]).write_text("x", encoding="utf-8")
        real_sleep(seconds)

    rig.driver.sleep = sleep
    with pytest.raises(RuntimeError, match="larch's step ended before its log said"):
        rig.up()
    assert len(rig.launches("larch")) == 1
    assert retries(rig, "larch") == []


def test_o57_a_failure_that_is_no_proof_waits_no_backoff(hpc, tmp_path, site):
    """O57: the backoff is for a launch proved failed: one that is not (a log with content) fails up at once."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{"log": "x"}]}))
    with pytest.raises(RuntimeError, match="larch's step ended before its log said"):
        rig.up()
    assert rig.driver.launch_retry_backoff_s not in rig.slept


# -- what is never retried -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("held", ["x", "hpcop: work directory /tmp/hpcop-op.larxyz\n"])
def test_o57_a_launch_whose_run_log_holds_a_single_byte_is_never_launched_again(hpc, tmp_path, site, held):
    """O57: the sacct row and the client's log say a launch failure, but the run log holds content: the service may
    have started, so it is never launched again, and up fails as it did before."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{"log": held}]}))
    with pytest.raises(RuntimeError, match="larch's step ended before its log said"):
        rig.up()
    assert len(rig.launches("larch")) == 1
    assert retries(rig, "larch") == []
    assert rig.state()["services"]["larch"]["state"] == "failed"


def test_o57_a_launch_with_no_run_log_at_all_is_never_launched_again(hpc, tmp_path, site):
    """O57: an empty log is proved by a file of zero bytes; a log that is not there proves nothing."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{"no_log": True}]}))
    with pytest.raises(RuntimeError, match="larch's step ended before its log said"):
        rig.up()
    assert len(rig.launches("larch")) == 1
    assert retries(rig, "larch") == []


def test_o57_a_second_failure_of_the_same_service_stops_up_as_before(hpc, tmp_path, site):
    """O57: one retry only: the second launch failing the same way stops up with the message it always had, the
    service failed, one retry recorded, and no third launch."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{}, {}]}))
    with pytest.raises(RuntimeError, match="larch's step ended before its log said"):
        rig.up()
    assert len(rig.launches("larch")) == 2
    assert len(retries(rig, "larch")) == 1
    state = rig.state()
    assert state["services"]["larch"]["state"] == "failed"
    assert state["phase"] != "linked"


def test_o57_a_retry_the_state_already_records_is_never_followed_by_another(hpc, tmp_path, site):
    """O57: whether a service was retried is read from the state file, not from this process: a record of a retry,
    made by an earlier up in the same state, ends the proofs' use for that service."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{}]}))
    real = rig.driver.launch

    def seeded(service, argv):
        real(service, argv)
        if service == "larch":
            rig.driver.set(lambda s: s["services"]["larch"].update(launch_retries=[{"launch": 0}]))

    rig.driver.launch = seeded
    with pytest.raises(RuntimeError, match="larch's step ended before its log said"):
        rig.up()
    assert len(rig.launches("larch")) == 1


def refusal_of_sacct(argv):
    from hpcslurm import Completed

    return Completed(1, "", "sacct: error: slurm_persist_conn_open: connection refused\n")


def silence_of_sacct(argv):
    from hpcslurm import Completed

    return Completed(0, "", "")


def garbage_of_sacct(argv):
    from hpcslurm import Completed

    return Completed(0, "this is not a row\n|||\n", "")


def timeout_of_sacct(argv):
    from hpcslurm import SlurmError

    raise SlurmError("sacct did not answer in 60 s")


@pytest.mark.parametrize("answer", [refusal_of_sacct, silence_of_sacct, garbage_of_sacct, timeout_of_sacct])
def test_o57_a_step_that_sacct_cannot_show_is_never_launched_again_and_up_fails_with_its_own_error(
    hpc, tmp_path, site, answer
):
    """O57: no accounting row, no proof: sacct failing, answering nothing, answering what is no row, or not answering
    leaves up failing exactly as before, with its own message, never sacct's."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{}]}, sacct_steps=answer))
    with pytest.raises(RuntimeError) as failure:
        rig.up()
    assert "larch's step ended before its log said" in str(failure.value)
    assert "sacct" not in str(failure.value)
    assert len(rig.launches("larch")) == 1
    assert retries(rig, "larch") == []


@pytest.mark.parametrize(
    "how",
    [
        {"state": "CANCELLED by 1000", "exit": "0:9"},  # a step scancelled by the user, as the live probe read one
        {"state": "CANCELLED by 1000", "exit": "0:116"},
        {"state": "CANCELLED", "exit": "0:9"},
        {"state": "CANCELLED", "exit": "0:15"},
        {"state": "FAILED", "exit": "1:0"},
        {"state": "TIMEOUT", "exit": "0:0"},
        {"state": "COMPLETED", "exit": "0:0"},
        {"state": "NODE_FAIL", "exit": "0:116"},
        {"no_row": True},
        {"row_id": "4242.99"},  # a row of that name, under another step id than the one the client names
        {"client_id": "4242.98"},  # the client names a step the accounting row is not of
    ],
    ids=lambda how: json.dumps(how, sort_keys=True),
)
def test_o57_only_the_exact_step_accounted_cancelled_with_exit_0_116_is_launched_again(hpc, tmp_path, site, how):
    """O57: proof (a) is exact: the state is CANCELLED and nothing more (a step a user cancelled reads `CANCELLED by
    <uid>`), the exit code 0:116, and the row is the one of the step id the client's own log names, with the step's
    name. Anything else leaves up failing as before, and the service launched once."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [how]}))
    with pytest.raises(RuntimeError, match="larch's step ended before its log said"):
        rig.up()
    assert len(rig.launches("larch")) == 1
    assert retries(rig, "larch") == []


@pytest.mark.parametrize(
    "how",
    [
        {"client": ""},
        {"client": "srun: error: something else failed\nsrun: Job step aborted\n"},
        {"reason": "Socket timed out on send/recv operation"},  # the request may have reached the node: no proof
        {"reason": "Connection timed out"},
        {
            "client": (
                "srun: error: Task launch for StepId=4242.1 failed on node node01: Can't find an address, check x\n"
                "srun: error: Task launch for StepId=4242.2 failed on node node01: Can't find an address, check x\n"
            )
        },  # two steps named: which one failed is not shown
    ],
    ids=["empty", "other-error", "socket-timeout", "connection-timeout", "two-steps"],
)
def test_o57_a_client_log_that_does_not_show_the_address_failure_of_the_step_is_no_proof(hpc, tmp_path, site, how):
    """O57: proof (a) needs srun's client to show the task launch failing for want of an address, for one step."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [how]}))
    with pytest.raises(RuntimeError, match="larch's step ended before its log said"):
        rig.up()
    assert len(rig.launches("larch")) == 1
    assert retries(rig, "larch") == []


def test_o57_a_service_whose_wait_ran_out_is_never_launched_again(hpc, tmp_path, site):
    """O57: only a step gone before it said anything is a candidate: a service that is still listed when its bound
    runs out (it may be running) is never launched again, whatever sacct says of an earlier step of the name."""
    fake = FakeSlurm()
    real = fake.spawn

    def silent(argv, client_log):
        client = real(argv, client_log)
        srun = fake.sruns[-1]
        if srun.name.endswith("-larch-run-1"):
            Path(srun.output).write_text("", encoding="utf-8")  # listed, alive, and silent
        return client

    fake.spawn = silent
    rig = Rig(hpc, tmp_path, site, fake)
    ticks = iter(range(0, 10_000_000, 100))
    rig.driver.clock = lambda: float(next(ticks))
    with pytest.raises(RuntimeError, match="larch did not say"):
        rig.up()
    assert len(rig.launches("larch")) == 1
    assert retries(rig, "larch") == []


def test_o57_a_service_that_logged_a_failure_line_is_never_launched_again(hpc, tmp_path, site):
    """O57: a log that says FINISH FAILED holds content: it is a failure the service reported, not a launch that
    never happened, whatever else is shown."""
    rig = Rig(hpc, tmp_path, site, FakeSlurm(launch_failures={"larch": [{"log": "FINISH FAILED: x\n"}]}))
    with pytest.raises(RuntimeError, match="larch failed to start"):
        rig.up()
    assert len(rig.launches("larch")) == 1
    assert retries(rig, "larch") == []


# -- the stagger ------------------------------------------------------------------------------------------------------


def test_o57_the_provider_launches_are_staggered_by_the_declared_bound_and_it_is_recorded(hpc, tmp_path, site):
    """O57: the providers are launched one after the other with a pause between consecutive launches (4 pauses for 5
    providers), by a declared bound that is 1 s unless changed; the bound is recorded in the state file."""
    rig = Rig(hpc, tmp_path, site)
    assert rig.driver.launch_stagger_s == 1.0
    rig.driver.launch_stagger_s = 0.25
    rig.up()
    assert rig.slept.count(0.25) == len(PROVIDERS) - 1
    assert rig.state()["launch"] == {"stagger_s": 0.25, "retry_backoff_s": 60.0, "retry_limit": 1}


def test_o57_the_stagger_pauses_come_between_the_provider_launches_not_before_the_first(hpc, tmp_path, site):
    """O57: the pause is between two launches, in the layout's order: never before the first provider's, never
    after the last."""
    rig = Rig(hpc, tmp_path, site)
    rig.driver.launch_stagger_s = 0.25
    events = []
    real_launch = rig.driver.launch
    rig.driver.launch = lambda service, argv: (events.append(("launch", service)), real_launch(service, argv))[1]
    real_sleep = rig.driver.sleep
    rig.driver.sleep = lambda s: (events.append(("sleep", s)), real_sleep(s))[1]
    rig.up()
    launched = [e for e in events if e[0] == "launch" or e == ("sleep", 0.25)]
    expected = [("launch", "zone")]
    for index, provider in enumerate(PROVIDERS):
        expected += ([("sleep", 0.25)] if index else []) + [("launch", provider)]
    assert launched == expected


# -- the proofs, one by one -------------------------------------------------------------------------------------------


def test_o57_the_client_logs_failure_is_read_as_a_step_id_and_a_class_never_a_host(proof):
    """O57: the live client log gives the step id and the class `task-launch-no-address`, and nothing else of it."""
    assert proof.client_failure(LIVE_CLIENT) == ("4242.7", "task-launch-no-address")
    assert proof.client_failure("") is None
    assert proof.client_failure("srun: Job step aborted\n") is None
    assert proof.client_failure(LIVE_CLIENT.replace("Can't find an address", "Socket timed out")) is None
    assert proof.client_failure(LIVE_CLIENT.replace("4242.7", "4242+1.7")) == ("4242+1.7", "task-launch-no-address")


def test_o57_an_accounting_row_is_found_by_its_exact_id_and_name_and_read_raw(proof):
    """O57: the row of the one step id, whose name is the step's; its state and exit code as sacct prints them."""
    rows = (
        "4242.6|a-zone-run-1|RUNNING|0:0\n"
        "4242.7|a-larch-run-1|CANCELLED|0:116\n"
        "4242.8|a-rowan-run-1|CANCELLED by 5|0:9\n"
    )
    assert proof.accounted(rows, "4242.7", "a-larch-run-1") == ("CANCELLED", "0:116")
    assert proof.accounted(rows, "4242.8", "a-rowan-run-1") == ("CANCELLED by 5", "0:9")
    assert proof.accounted(rows, "4242.7", "a-rowan-run-1") is None  # the name is not that id's
    assert proof.accounted(rows, "4242.9", "a-larch-run-1") is None  # no such id
    assert proof.accounted(rows, "4242", "a-larch-run-1") is None
    twice = rows + "4242.7|a-larch-run-1|CANCELLED|0:116\n"
    assert proof.accounted(twice, "4242.7", "a-larch-run-1") is None  # two rows of the id: which is it?
    assert proof.accounted("", "4242.7", "a-larch-run-1") is None
