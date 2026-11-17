"""The HPC federation's observed read (deploy/hpc-federation/hpcobserve.py, O86): each service's Slurm step read by its
EXACT step id through `squeue -s` and `sacct`, never the state file's word for it, and never with a command that
writes. Over a small fake of the two read commands; no cluster, no node, no step is made."""

import json
from pathlib import Path

import pytest
from hpc_fed_support import hpc_on_path

NODE = "node-private-07"  # a node's name as the state holds it: never in a reading
FEDERATION = "fedx-3f9a"


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import hpcobserve

        yield type("Observe", (), {"slurm": modules.slurm, "observe": hpcobserve})


class Cluster:
    """The login node's `squeue -s` and `sacct` for one job; every command is kept, and only these two are answered."""

    def __init__(
        self,
        listed=(),
        accounting=(),
        squeue_rc=0,
        sacct_rc=0,
        squeue_err="",
        sacct_err="",
        squeue_text=None,
        sacct_text=None,
    ):
        self.squeue_text, self.sacct_text = squeue_text, sacct_text  # an answer as it is printed, when a test gives one
        self.listed = list(listed)  # (step id, name) squeue -s lists
        self.accounting = list(accounting)  # (id, name, state, start, end, exit code) sacct holds
        self.squeue_rc, self.sacct_rc, self.squeue_err, self.sacct_err = squeue_rc, sacct_rc, squeue_err, sacct_err
        self.calls: list[list[str]] = []

    def run(self, argv, timeout):
        from hpcslurm import Completed

        self.calls.append(list(argv))
        if argv[0] == "squeue" and "-s" in argv:
            if self.squeue_rc:
                return Completed(self.squeue_rc, "", self.squeue_err)
            if self.squeue_text is not None:
                return Completed(0, self.squeue_text, "")
            lines = ["4242.batch batch", "4242.extern extern", *(f"{i} {n}" for i, n in self.listed)]
            return Completed(0, "".join(f"{line}\n" for line in lines), "")
        if argv[0] == "sacct":
            if self.sacct_rc:
                return Completed(self.sacct_rc, "", self.sacct_err)
            if self.sacct_text is not None:
                return Completed(0, self.sacct_text, "")
            rows = [("4242.batch", "batch", "RUNNING", "2026-11-17T01:00:00", "Unknown", "0:0"), *self.accounting]
            return Completed(0, "".join("|".join(row) + "\n" for row in rows), "")
        raise AssertionError(f"a command that is not a read: {argv}")


def slurm_of(hpc, cluster):
    return hpc.slurm.Slurm(cluster, command_timeout_s=5)


def row(step, name="svc-run-1", state="RUNNING", start="2026-11-17T02:00:00", end="Unknown", code="0:0"):
    return (step, name, state, start, end, code)


def test_o86_a_listed_step_with_a_running_record_is_running_with_its_start(hpc):
    """O86: squeue lists the exact id and accounting says RUNNING with a start: running, with the record's start."""
    cluster = Cluster(listed=[("4242.7", "svc-run-1")], accounting=[row("4242.7")])
    seen = slurm_of(hpc, cluster).step_observed("4242.7", "svc-run-1")
    assert seen == {
        "running": True,
        "state": "RUNNING",
        "started_at": "2026-11-17T02:00:00",
        "ended_at": None,
        "exit_code": None,
        "unknown": None,
    }


def test_o86_a_step_gone_from_squeue_with_a_terminal_record_has_ended_with_its_exit(hpc):
    """O86: absent from squeue and a terminal accounting row with an end: not running, its end and its exit read (a
    signal's number added to 128, as the driver's own step_status does)."""
    ended = row("4242.7", state="CANCELLED", end="2026-11-17T03:30:00", code="0:15")
    seen = slurm_of(hpc, Cluster(accounting=[ended])).step_observed("4242.7", "svc-run-1")
    assert seen["running"] is False
    assert (seen["state"], seen["ended_at"], seen["exit_code"], seen["unknown"]) == (
        "CANCELLED",
        "2026-11-17T03:30:00",
        143,
        None,
    )
    plain = row("4242.7", state="COMPLETED", end="2026-11-17T03:30:00", code="3:0")
    assert slurm_of(hpc, Cluster(accounting=[plain])).step_observed("4242.7")["exit_code"] == 3


def test_o86_the_exact_id_is_read_never_a_neighbour(hpc):
    """O86: step 4242.1 is not 4242.10, and a step of another name under the service's own is not the step asked
    about: only the row whose id is exactly the one asked for counts."""
    cluster = Cluster(listed=[("4242.10", "svc-run-1")], accounting=[row("4242.10", start="2026-11-17T09:00:00")])
    seen = slurm_of(hpc, cluster).step_observed("4242.1", "svc-run-1")
    assert seen["running"] is None  # neither listed nor recorded: not settled by the neighbour that is
    assert seen["unknown"] is not None
    other = slurm_of(hpc, cluster).step_observed("4242.10", "svc-run-1")
    assert (other["running"], other["started_at"]) == (True, "2026-11-17T09:00:00")


@pytest.mark.parametrize(
    "cluster",
    [
        Cluster(squeue_rc=1, squeue_err="slurm_load_jobs error: Unable to contact slurm controller"),
        Cluster(listed=[("4242.7", "svc-run-1")], sacct_rc=1, sacct_err="sacct: error: slurmdbd unreachable"),
    ],
    ids=["squeue failed", "sacct failed"],
)
def test_o86_a_read_that_fails_is_unknown_with_its_reason_never_running(hpc, cluster):
    """O86: a Slurm command that fails (or that cannot be reached) leaves the step unknown, with a reason that says
    which command: never running, never stopped."""
    seen = slurm_of(hpc, cluster).step_observed("4242.7", "svc-run-1")
    assert seen["running"] is None
    assert seen["state"] is None
    assert seen["unknown"]
    assert "squeue" in seen["unknown"] or "sacct" in seen["unknown"]


@pytest.mark.parametrize(
    "cluster",
    [
        Cluster(listed=[("4242.7", "svc-run-1")], sacct_text="4242.7|svc-run-1|RUNNING\n"),
        Cluster(listed=[("4242.7", "svc-run-1")], sacct_text="garbage that is not a row\n"),
        Cluster(listed=[("4242.7", "svc-run-1")], sacct_text=""),
        Cluster(listed=[("4242.7", "svc-run-1")], sacct_text="|||||\n"),
        Cluster(listed=[("4242.7", "svc-run-1")], sacct_text="x|y|z|u|v|w\n"),
        Cluster(
            listed=[("4242.7", "svc-run-1")], sacct_text="4242.7|svc-run-1|running|2026-11-17T02:00:00|Unknown|0:0\n"
        ),
        Cluster(listed=[("4242.7", "svc-run-1")], sacct_text="4242.7|svc-run-1|RUNNING|soon|Unknown|0:0\n"),
        Cluster(
            listed=[("4242.7", "svc-run-1")], sacct_text="4242.7|svc-run-1|RUNNING|2026-11-17T02:00:00|Unknown|ok\n"
        ),
        Cluster(listed=[("4242.7", "svc-run-1")], accounting=[row("4242.7")], squeue_text="4242.7\n"),
    ],
    ids=[
        "truncated row",
        "not a row",
        "no row at all",
        "six empty fields",
        "six words that are not accounting",
        "a state that is not a word of Slurm's",
        "a start that is no time",
        "an exit code that is none",
        "unreadable squeue row",
    ],
)
def test_o86_an_answer_that_cannot_be_read_is_unknown_never_the_lag_of_accounting(hpc, cluster):
    """O86: a Slurm command that succeeds and prints a row that is not one (cut short, or garbage), or prints nothing
    for a job that has rows, is no answer: the step is unknown, not running with accounting a little behind."""
    seen = slurm_of(hpc, cluster).step_observed("4242.7", "svc-run-1")
    assert seen["running"] is None
    assert seen["unknown"]


def test_o86_a_command_that_times_out_is_unknown(hpc):
    """O86: the Runner raises SlurmError when a command does not answer: unknown, not an exception."""

    class Hung:
        def run(self, argv, timeout):
            raise hpc.slurm.SlurmError(f"{argv[0]} did not answer in {timeout:.0f} s")

    seen = slurm_of(hpc, Hung()).step_observed("4242.7")
    assert seen["running"] is None
    assert "did not answer" in seen["unknown"]


@pytest.mark.parametrize(
    ("cluster", "why"),
    [
        (Cluster(), "no record"),
        (Cluster(accounting=[row("4242.7", state="RUNNING")]), "running"),
        (
            Cluster(listed=[("4242.7", "svc-run-1")], accounting=[row("4242.7", state="COMPLETED", end="2026-11-17")]),
            "ended",
        ),
        (Cluster(listed=[("4242.7", "another-name")], accounting=[row("4242.7")]), "name"),
        (Cluster(accounting=[row("4242.7", state="SUSPENDED")]), "state"),
    ],
    ids=["unlisted with no record", "unlisted but running", "listed but ended", "another name", "unrecognised state"],
)
def test_o86_when_the_two_reads_do_not_settle_it_the_step_is_unknown(hpc, cluster, why):
    """O86: a step neither listed nor recorded, listed while accounting says it ended, absent while accounting says it
    runs, listed under another name, or in a state this read does not know, is unknown: never a guess."""
    seen = slurm_of(hpc, cluster).step_observed("4242.7", "svc-run-1")
    assert seen["running"] is None, why
    assert seen["unknown"], why


def test_o86_a_step_id_that_is_not_one_is_refused_before_any_command(hpc):
    """O86: an id that is not `<job>.<n>` runs no command."""
    cluster = Cluster()
    seen = slurm_of(hpc, cluster).step_observed("4242.7; rm", "svc-run-1")
    assert seen["running"] is None
    assert seen["unknown"]
    assert cluster.calls == []


# --- the federation: the state's services, each read by its step id ---------------------------------------------------


def state_of(**services):
    """A federation's state as `up` leaves it, trimmed to what the read uses, with a node name that must not be kept."""
    return {
        "format": "expcore-hpc-federation/1",
        "project": FEDERATION,
        "phase": "linked",
        "site": {},
        "allocation": {"name": "alloc", "job": "4242", "ends": "2026-11-17T20:00:00"},
        "services": {
            name: {"kind": kind, "node": NODE, "fqdn": f"{NODE}.example.org", "address": "10.9.9.9",
                   "workdir": "/tmp/hpcop-op.Ab12Cd", "launch": launch, "state": "running",
                   "step": {"name": f"alloc-{name}-run-{launch}", "id": step}, "stops": stops}
            for name, (kind, launch, step, stops) in services.items()
        } | {"agent": {"kind": "sandbox", "node": NODE, "step": None}},
    }  # fmt: skip


STOP = {"service": "alpha", "stopped": True, "ended": "exit", "seconds": 3.0, "requested_utc": "2026-11-17T02:10:00Z",
        "confirmed_utc": "2026-11-17T02:10:04Z", "node": NODE}  # fmt: skip


def test_o86_each_service_is_read_by_the_step_id_the_state_names(hpc):
    """O86: for the zone and each provider, the state's step id is read through squeue -s and sacct; the document has
    the service's launch, step, whether it runs, its start, and the stops the state records; and nothing of a node,
    an address or a path."""
    state = state_of(zone=("oz", 1, "4242.3", []), alpha=("op", 2, "4242.5", [STOP]))
    cluster = Cluster(
        listed=[("4242.3", "alloc-zone-run-1"), ("4242.5", "alloc-alpha-run-2")],
        accounting=[
            row("4242.3", "alloc-zone-run-1", start="2026-11-17T01:05:00"),
            row("4242.5", "alloc-alpha-run-2", start="2026-11-17T02:11:00"),
        ],
    )
    doc = hpc.observe.read(state, slurm_of(hpc, cluster), now=lambda: 1_700_000_000.0)
    assert doc["format"] == "expcore-hpc-observe/1"
    assert doc["at"].endswith("Z")
    assert sorted(doc["services"]) == ["alpha", "zone"]  # the agent's node is no service of the federation
    zone, alpha = doc["services"]["zone"], doc["services"]["alpha"]
    assert (zone["kind"], zone["step"], zone["launch"], zone["running"]) == ("oz", "4242.3", 1, True)
    assert (alpha["kind"], alpha["step"], alpha["launch"], alpha["started_at"]) == (
        "op",
        "4242.5",
        2,
        "2026-11-17T02:11:00",
    )
    assert alpha["stops"] == [
        {"requested_utc": "2026-11-17T02:10:00Z", "confirmed_utc": "2026-11-17T02:10:04Z", "stopped": True}
    ]
    assert zone["unknown"] is None
    assert alpha["unknown"] is None
    text = json.dumps(doc)
    for private in (NODE, "10.9.9.9", "hpcop-op", "example.org"):
        assert private not in text
    assert all(call[0] in ("squeue", "sacct") for call in cluster.calls)  # nothing but the two reads


def test_o86_a_service_restarted_outside_the_driver_is_read_as_the_step_that_is_listed(hpc):
    """O86: the state still names the old step, squeue lists the service's name under a new id: the observation is the
    listed step (the state is not an observation), and the difference is said."""
    state = state_of(alpha=("op", 2, "4242.5", []))
    cluster = Cluster(
        listed=[("4242.9", "alloc-alpha-run-2")],
        accounting=[row("4242.9", "alloc-alpha-run-2", start="2026-11-17T04:00:00")],
    )
    alpha = hpc.observe.read(state, slurm_of(hpc, cluster), now=lambda: 0.0)["services"]["alpha"]
    assert (alpha["step"], alpha["running"], alpha["started_at"], alpha["state_differs"]) == (
        "4242.9",
        True,
        "2026-11-17T04:00:00",
        True,
    )


def test_o86_a_service_with_no_step_id_and_none_listed_is_unknown(hpc):
    """O86: a service the state gives no step id and squeue lists no step for has nothing to read: unknown, with the
    reason, never running."""
    state = state_of(alpha=("op", 0, None, []))
    alpha = hpc.observe.read(state, slurm_of(hpc, Cluster()), now=lambda: 0.0)["services"]["alpha"]
    assert alpha["running"] is None
    assert alpha["unknown"]
    assert alpha["step"] is None


def test_o86_a_squeue_that_fails_leaves_every_service_unknown(hpc):
    """O86: the one listing of the job's steps failing makes every service unknown with the reason, not an error."""
    state = state_of(zone=("oz", 1, "4242.3", []), alpha=("op", 2, "4242.5", []))
    cluster = Cluster(squeue_rc=1, squeue_err="slurm_load_jobs error: Unable to contact slurm controller")
    doc = hpc.observe.read(state, slurm_of(hpc, cluster), now=lambda: 0.0)
    assert all(rec["running"] is None and "squeue" in rec["unknown"] for rec in doc["services"].values())


def test_o86_the_command_prints_one_json_line_and_changes_no_file(hpc, tmp_path):
    """O86: `hpcobserve.py --root R` prints one JSON line; it opens the state read-only and leaves it as it was."""
    root = tmp_path / "root"
    root.mkdir()
    state = state_of(alpha=("op", 2, "4242.5", []))
    file = root / "hpc-federation.json"
    file.write_text(json.dumps(state), encoding="utf-8")
    before = (file.read_bytes(), file.stat().st_mtime_ns, sorted(p.name for p in root.iterdir()))
    cluster = Cluster(listed=[("4242.5", "alloc-alpha-run-2")], accounting=[row("4242.5", "alloc-alpha-run-2")])

    class Out:
        text = ""

        def write(self, value):
            self.text += value

        def flush(self):
            pass

    out = Out()
    assert hpc.observe.main(["--root", str(root)], runner=cluster, out=out, err=Out()) == 0
    lines = out.text.splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["services"]["alpha"]["running"] is True
    assert (file.read_bytes(), file.stat().st_mtime_ns, sorted(p.name for p in root.iterdir())) == before


@pytest.mark.parametrize(
    "content", [None, "not json", json.dumps({"format": "other"})], ids=["none", "garbled", "foreign"]
)
def test_o86_a_state_that_cannot_be_read_is_exit_1_and_names_no_path(hpc, tmp_path, content):
    """O86: no state, a state that is not JSON, or one of another format prints nothing on stdout and exits 1, with a
    message that names no path."""
    root = tmp_path / "root-private"
    root.mkdir()
    if content is not None:
        (root / "hpc-federation.json").write_text(content, encoding="utf-8")

    class Sink:
        text = ""

        def write(self, value):
            self.text += value

        def flush(self):
            pass

    out, err = Sink(), Sink()
    assert hpc.observe.main(["--root", str(root)], runner=Cluster(), out=out, err=err) == 1
    assert out.text == ""
    assert err.text
    assert "root-private" not in err.text


def test_o86_the_module_holds_no_command_that_writes(hpc):
    """O86: the read's source names no Slurm verb that changes anything (sbatch, scancel, srun, scontrol update) and no
    state write."""
    source = Path(hpc.observe.__file__).read_text(encoding="utf-8")
    for word in ("sbatch", "scancel", "srun", "scontrol", ".save(", "hpcstate.save", "StateStore", "update("):
        assert word not in source, word
