"""O24: a harness step before the agent's loop, and its requests through the data gate, kept apart from the agent's.

A study's executor checks the sandbox as the agent's user before its loop starts (`exec(..., record=False)`), then
marks where the agent's run begins (`mark_run_start`): the gate writes the mark in its own log, naming every request
it had begun and not yet logged, and the count of the bytes served (O22) takes only the answers after it, with those
before it counted apart. Every line here is one the gate writes, with invented values; the sandbox runs over the
in-memory host, and the gate's own mark is its real code.
"""

import json
import signal
import time
from types import SimpleNamespace

import pytest
from sandbox_support import make_spec, start_options

from expcore_onedata.sandbox import gate, gatelog, served
from expcore_onedata.sandbox.api import SandboxError
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox
from expcore_onedata.sandbox.served import ServedBytes

STOPPED = {
    "signalled": True,
    "exit_code": 0,
    "output_read": True,
    "log_write_failed": False,
    "teardown_incomplete": False,
}
MARKED = {"gate": True, "signalled": True, "confirmed": True}  # what the sandbox's mark found
FILE, LOOKED = "fileidalpha", "fileidbravo"  # invented file ids
FOLDER, CHECKED = "diridfolder", "diridchecked"  # invented directory ids
TABLES = ("files", "files_total", "files_cut", "listings", "listings_total", "listings_cut")
DATA = "/api/v3/oneprovider/data/"


def answer(path: str, size: int, body: str, conn: str, request: int) -> dict:
    """The gate's line of one answer of the platform's, the `conn_request`-th on the client connection `conn`."""
    return {
        "method": "GET",
        "path": path,
        "status": 200,
        "body": body,
        "body_bytes": size,
        "conn": conn,
        "conn_request": request,
    }


CHECK = [  # the harness's check, answered and logged before the mark: a lookup and a listing
    answer(f"{DATA}{CHECKED}", 40, "other", "1-1", 1),
    answer(f"{DATA}{CHECKED}/children", 300, "listing", "1-1", 2),
]
LATE = answer(f"{DATA}{LOOKED}/content", 25, "content", "1-2", 1)  # begun before the mark, its line written after it
MARK = {"event": "run_start", "connections": 1, "before": [["1-2", 1]]}  # as the gate writes it, naming LATE
RUN = [  # the agent's own answers, after the mark
    answer(f"{DATA}{FILE}/content", 1000, "content", "2-1", 1),
    answer(f"{DATA}{FOLDER}/children", 200, "listing", "2-1", 2),
    answer(f"{DATA}{FILE}", 90, "other", "2-1", 3),
]
END = {"event": "teardown_complete", "connections": 0}
BEFORE = {  # what CHECK and LATE served
    "requests": 3,
    "served_bytes": 365,
    "content_bytes": 25,
    "listing_bytes": 300,
    "other_bytes": 40,
    "gate_answer_bytes": 0,
}


def log(*records) -> list[str]:
    return [json.dumps(r) for r in records]


def start(world, level="via-gate") -> DockerSandbox:
    return DockerSandbox.start(world.host, world.s, make_spec(world, level), **start_options(world))


def gate_log(sbx):
    return sbx.dir / "gatelog" / "requests.jsonl"


def append_log(sbx, *records) -> None:
    with open(gate_log(sbx), "a", encoding="utf-8") as handle:
        handle.write("".join(json.dumps(r) + "\n" for r in records))


def the_gate_marks(world, sbx, monkeypatch, live=None) -> None:
    """The gate's SIGUSR1, as the fake host delivers it: the real gate's mark, written to this sandbox's gate log while
    the connections in `live` are open."""
    monkeypatch.setattr(gate, "CFG", {"log": str(gate_log(sbx))})
    monkeypatch.setattr(gate, "_LIVE", dict(live or {}))
    world.host.signal_handlers["USR1"] = lambda container: gate.mark_run_start()


def test_o24_a_harness_step_runs_as_the_agent_and_is_recorded_apart_from_the_agents_cells(world):
    """O24: `exec(..., record=False)` runs the code in the agent's container as before, and records it as a harness
    step with its exit status, never as a cell of the agent's: the cells count none, the agent's first cell is still
    cell 1, and a refusal of the gate during the step is the step's, never handed to that cell."""
    sbx = start(world)
    seen = []

    def checks(container, argv, stdin):
        seen.append((container.name, argv[0], stdin))
        if stdin == "check()":
            append_log(sbx, {"event": "request", "gate_refusal": "redirect"})
            return CmdResult(3, "", "")
        return CmdResult(0, "1\n", "")

    world.host.exec_handler = checks
    step = sbx.exec("check()", 30, record=False)
    assert (step.exit_code, step.gate_refusals) == (3, ("redirect",))
    assert seen == [(sbx.names.agent, "timeout", "check()")]  # the agent's container, under the backstop's timeout
    meta = sbx.metadata()
    assert meta["cells"] == 0
    assert [{k: v for k, v in s.items() if k != "duration_s"} for s in meta["harness_steps"]] == [
        {
            "step": 1,
            "status": "ran",
            "exit_code": 3,
            "timed_out": False,
            "gate_refusals": ["redirect"],
            "earlyoom_kills": [],
        }
    ]
    cell = sbx.exec("print(1)", 30)
    assert cell.gate_refusals == ()
    assert [c["cell"] for c in sbx._cells.cells] == [1]
    assert sbx.metadata()["cells"] == 1
    assert len(sbx.metadata()["harness_steps"]) == 1
    sbx.stop()


def test_o24_a_memory_kill_under_a_harness_step_is_never_put_on_the_agents_first_cell(world):
    """O24: the memory counters a harness step raised are its own: the agent's first cell takes its baseline after the
    step, so a kill the step met is not a kill of that cell (O17)."""
    sbx = start(world, "isolated")
    agent = world.host.containers[sbx.names.agent]
    events = next(path for path in world.host.files if agent.id in path and path.endswith("/memory.events"))

    def killed_under_the_step(container, argv, stdin):
        world.host.files[events] = "low 0\nhigh 0\nmax 0\noom 1\noom_kill 1\n"
        return CmdResult(137, "", "")

    world.host.exec_handler = killed_under_the_step
    sbx.exec("check()", 30, record=False)
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(0, "", "")
    cell = sbx.exec("print(1)", 30)
    assert (cell.kill, cell.timed_out) == (None, False)
    assert "cell_kills" not in sbx.metadata()
    sbx.stop()


def test_o24_a_host_kill_under_a_harness_step_is_the_steps_never_the_agents_first_cell(world):
    """O24: a kill by the host's out-of-memory killer of a process of the step is read once the step ends and recorded
    on the step; the agent's first cell, which the killer never touched, is no kill of the host's."""
    sbx = start(world, "isolated")
    agent = world.host.containers[sbx.names.agent]
    victim = agent.pid + 100  # a process of the step's, in the agent's container

    def killed_by_the_host(container, argv, stdin):
        world.host.add_cgroup(agent, [agent.pid, victim])
        sbx._cells.sample_pids()  # what the sampler's thread sees while the step runs
        world.host.journal = (
            f'{time.time():.3f} h earlyoom[1]: sending SIGTERM to process {victim} uid {world.s.agent_uid} "python": '
            "oom_score 1, oom_score_adj 2, VmRSS 3 MiB\n"
        )
        return CmdResult(143, "", "")

    world.host.exec_handler = killed_by_the_host
    sbx.exec("check()", 30, record=False)
    world.host.add_cgroup(agent, [agent.pid])  # the step's process is gone
    world.host.exec_handler = lambda container, argv, stdin: CmdResult(0, "", "")
    cell = sbx.exec("print(1)", 30)
    assert (cell.kill, cell.timed_out) == (None, False)
    assert "cell_kills" not in sbx.metadata()
    [step] = sbx.metadata()["harness_steps"]
    assert [kill["pid"] for kill in step["earlyoom_kills"]] == [victim]
    sbx.stop()


def test_o24_a_mark_is_refused_once_the_agents_first_cell_has_begun_and_while_a_step_runs(world, monkeypatch):
    """O24: a cell of the agent's has begun when its `exec` is entered, not when it ends: a mark during the agent's
    first cell would set that cell's reads apart, while the count reads as known, so it is refused. So is a mark while
    a harness step still runs, whose later requests would count as the agent's."""
    sbx = start(world)
    the_gate_marks(world, sbx, monkeypatch)
    refused = []

    def marks_meanwhile(container, argv, stdin):
        try:
            sbx.mark_run_start()
        except SandboxError as error:
            refused.append((stdin, str(error)))
        return CmdResult(0, "", "")

    world.host.exec_handler = marks_meanwhile
    sbx.exec("check()", 30, record=False)
    sbx.exec("print(1)", 30)
    assert [stdin for stdin, _ in refused] == ["check()", "print(1)"]
    assert "run_start" not in sbx.metadata()
    assert not [c for c in world.host.calls if c[0] == "signalled"]
    sbx.stop()


def test_o24_a_harness_step_is_refused_once_the_run_has_started(world, monkeypatch):
    """O24: a harness step runs before the agent's run only. After the mark its requests would count as the agent's,
    so a read log naming a file only the harness read would check out as served; after one of the agent's cells has
    begun, it would take that cell's refusals and kills. Both are refused, and nothing runs."""
    marked, begun = start(world), start(world)
    the_gate_marks(world, marked, monkeypatch)
    ran = []
    world.host.exec_handler = lambda container, argv, stdin: (ran.append(stdin), CmdResult(0, "", ""))[1]
    marked.mark_run_start()
    with pytest.raises(SandboxError, match="before the agent's run"):
        marked.exec("check()", 30, record=False)
    marked.stop()
    begun.exec("print(1)", 30)
    with pytest.raises(SandboxError, match="before the agent's run"):
        begun.exec("check()", 30, record=False)
    assert ran == ["print(1)"]
    assert "harness_steps" not in begun.metadata()
    begun.stop()


def test_o24_answers_before_the_mark_are_counted_apart_and_never_in_the_runs_tables():
    """O24: with the mark, the run's counts and tables hold only the answers after it, and the answers before it are
    counted apart (`before_run_start`), so nothing is hidden; an answer begun before the mark and logged after it is
    one of those, by the request the mark names. A lookup reads only the run's rows: a file or directory the harness
    alone read was not served to the agent."""
    got = gatelog.served(log(*CHECK, MARK, LATE, *RUN, END), STOPPED, run_start=MARKED)
    alone = gatelog.served(log(*RUN, END), STOPPED)
    for field in (*gatelog.SERVED_FIELDS, *TABLES):
        assert got[field] == alone[field], field
    assert got["served_bytes"] == 1290
    assert (got["before_run_start"], got["unknown"]) == (BEFORE, [])
    assert served.lookup_file(got, LOOKED).state == "not_served"
    assert served.lookup_bytes(got, LOOKED, 0, 25).state == "not_served"
    assert served.lookup_directory(got, CHECKED).state == "not_served"
    assert served.lookup_file(got, FILE).state == "served"


def test_o24_without_a_mark_every_answer_is_the_runs_and_the_snapshot_says_no_mark_was_set(world, tmp_path):
    """O24: a run whose start was never marked is counted as before: every answer is the run's, nothing is counted
    apart (`before_run_start` None), and the snapshot says no mark was set."""
    got = gatelog.served(log(*CHECK, LATE, *RUN, END), STOPPED)
    assert (got["served_bytes"], got["before_run_start"], got["unknown"]) == (1655, None, [])
    assert served.lookup_directory(got, CHECKED).state == "served"
    sbx = start(world)
    append_log(sbx, *CHECK, LATE, *RUN, END)
    sbx.collect(tmp_path / "evidence")
    snapshot = ServedBytes(sbx).finish()
    assert (snapshot["run_start_marked"], snapshot["before_run_start"]) == (False, None)
    assert snapshot["served_bytes"] == 1655
    assert "run_start" not in sbx.metadata()
    sbx.stop()


@pytest.mark.parametrize(
    ("lines", "run_start", "reasons"),
    [
        (log(*CHECK, LATE, *RUN, END), MARKED, ["run_start_unconfirmed"]),
        (log(*CHECK, MARK, LATE, *RUN, END), {**MARKED, "confirmed": False}, ["run_start_unconfirmed"]),
        (
            log(*CHECK, MARK, LATE, *RUN, END),
            {"gate": True, "signalled": False, "confirmed": False},
            ["run_start_unconfirmed"],
        ),
        (
            [*log(*CHECK), '{"event": "run_start", "conn', *log(LATE, *RUN, END)],
            MARKED,
            ["gate_log_malformed", "run_start_unconfirmed"],
        ),
        (log(*CHECK, MARK, LATE, MARK, *RUN, END), MARKED, ["run_start_unconfirmed"]),
        (log(*CHECK, LATE, *RUN, END, MARK), MARKED, ["run_start_unconfirmed"]),
        (log(*CHECK, MARK | {"before": [["1-2"]]}, LATE, *RUN, END), MARKED, ["gate_log_malformed"]),
        (log(*CHECK, MARK, LATE, *RUN, END), None, ["gate_log_malformed"]),
        (log(*CHECK, MARK, LATE, *RUN), MARKED, ["gate_teardown_unconfirmed"]),
        (
            log(*CHECK, MARK, LATE, *RUN, {"event": "conn_close", "ended": "teardown", "in_request": True}, END),
            MARKED,
            ["request_in_flight_at_stop"],
        ),
    ],
    ids=[
        "mark-line-lost",
        "not-seen-before-the-loop",
        "never-signalled",
        "mark-line-cut",
        "two-marks",
        "mark-after-teardown",
        "mark-without-its-requests",
        "mark-nobody-set",
        "no-teardown-mark",
        "in-flight-at-stop",
    ],
)
def test_o24_a_mark_the_log_does_not_confirm_leaves_the_count_unknown(lines, run_start, reasons):
    """O24: a mark the sandbox set must be in the log, once, before the gate's teardown mark, and must have been read
    back before the agent's loop started; a mark line lost or cut, one never seen, two of them or one after the
    teardown make every count unknown, never "everything after" taken for the run. So does a mark line nobody set, or
    one that does not name its requests. The rules that were there hold as before."""
    got = gatelog.served(lines, STOPPED, run_start=run_start)
    assert got["unknown"] == reasons
    assert all(got[field] is None for field in (*gatelog.SERVED_FIELDS, *TABLES, "before_run_start"))


def test_o24_the_gate_writes_the_mark_and_the_collection_counts_only_what_came_after_it(world, tmp_path, monkeypatch):
    """O24: the executor's check runs as a harness step, then `mark_run_start` signals the gate, which writes the mark
    in its own log, naming the request it had answered and not yet logged; the sandbox reads the mark back before it
    returns. The collection then counts the agent's answers alone, the check's apart, and the instrument records both
    and the mark, whose note the metadata keeps."""
    sbx = start(world)
    world.host.exec_handler = lambda container, argv, stdin: (append_log(sbx, *CHECK), CmdResult(0, "", ""))[1]
    assert sbx.exec("check()", 30, record=False).exit_code == 0
    answering = SimpleNamespace(_conn="1-2", _served=1, _last_logged=0)  # answered its request, its line not written
    the_gate_marks(world, sbx, monkeypatch, live={"1-2": answering})
    note = sbx.mark_run_start()
    assert note == MARKED
    assert ("signalled", sbx.names.gate, "USR1") in world.host.calls
    assert world.host.containers[sbx.names.gate].running  # the mark does not stop the gate
    append_log(sbx, LATE, *RUN, END)
    instrument = ServedBytes(sbx)
    sbx.collect(tmp_path / "evidence")
    snapshot = instrument.finish()
    assert (snapshot["unknown"], snapshot["run_start_marked"]) == ([], True)
    assert (snapshot["served_bytes"], snapshot["before_run_start"]) == (1290, BEFORE)
    assert set(snapshot["files"]) == {FILE}
    assert served.lookup_directory(snapshot, CHECKED).state == "not_served"
    assert sbx.metadata()["run_start"] == MARKED
    assert sbx.metadata()["cells"] == 0
    sbx.stop()


def test_o24_a_mark_the_gate_never_logged_is_unconfirmed_and_the_runs_count_unknown(world, tmp_path):
    """O24: a gate that took the signal and wrote no mark (its log write failed, say) leaves the mark unconfirmed: the
    sandbox says so when it returns, and the run's count is unknown, never every answer taken for the run's."""
    sbx = start(world)
    world.host.signal_handlers["USR1"] = lambda container: None
    assert sbx.mark_run_start() == {"gate": True, "signalled": True, "confirmed": False}
    append_log(sbx, *CHECK, LATE, *RUN, END)
    sbx.collect(tmp_path / "evidence")
    got = sbx.served_bytes()
    assert (got["unknown"], got["run_start_marked"], got["served_bytes"]) == (["run_start_unconfirmed"], True, None)
    sbx.stop()


def test_o24_the_run_start_is_marked_once_before_the_agents_first_cell_and_before_the_collection(
    world, tmp_path, monkeypatch
):
    """O24: a mark after one of the agent's cells would set that cell's reads apart from the run's, and one after the
    collection would come after the count: both are refused, and so is a second mark."""
    sbx = start(world)
    the_gate_marks(world, sbx, monkeypatch)
    sbx.mark_run_start()
    with pytest.raises(SandboxError, match="already marked"):
        sbx.mark_run_start()
    sbx.stop()
    with pytest.raises(SandboxError, match="not running"):
        sbx.mark_run_start()
    late = start(world)
    late.exec("print(1)", 30)
    with pytest.raises(SandboxError, match="first cell"):
        late.mark_run_start()
    late.stop()
    collected = start(world)
    collected.collect(tmp_path / "evidence")
    with pytest.raises(SandboxError, match="collection"):
        collected.mark_run_start()
    collected.stop()


def test_o24_without_a_gate_the_mark_is_recorded_and_nothing_is_signalled(world):
    """O24: a mount or no data path has no gate to mark: the mark is recorded, nothing is signalled, a mount's count
    stays unknown and no data path served nothing before the mark either."""
    zero = dict.fromkeys(BEFORE, 0)
    for level, before in (("mounted", None), ("isolated", zero)):
        sbx = start(world, level)
        assert sbx.mark_run_start() == {"gate": False}
        got = sbx.served_bytes()
        assert (got["run_start_marked"], got["before_run_start"]) == (True, before)
        assert not [c for c in world.host.calls if c[0] == "signalled"]
        sbx.stop()


def test_o24_the_gates_mark_names_each_request_begun_and_not_yet_logged(tmp_path, monkeypatch):
    """O24: the gate reads its open connections and writes one mark: a request a connection has begun and not yet
    logged (its line comes after the mark) is named in `before`, and one whose line is written is not."""
    log_file = tmp_path / "requests.jsonl"
    log_file.touch()
    monkeypatch.setattr(gate, "CFG", {"log": str(log_file)})
    monkeypatch.setattr(
        gate,
        "_LIVE",
        {
            "1-1": SimpleNamespace(_conn="1-1", _served=3, _last_logged=2),  # answering its third request
            "1-2": SimpleNamespace(_conn="1-2", _served=1, _last_logged=1),  # every line written
        },
    )
    gate.mark_run_start()
    [line] = [json.loads(x) for x in log_file.read_text(encoding="utf-8").splitlines()]
    assert (line["event"], line["connections"], line["before"]) == (gatelog.RUN_START, 2, [["1-1", 3]])


def test_o24_the_gate_takes_its_mark_signal_before_it_says_it_is_ready(tmp_path, monkeypatch):
    """O24: the gate handles SIGUSR1 from before it says it is ready, so a mark sent to a gate the sandbox has seen
    ready is written. Unhandled, the signal would end a process, and the container's PID 1, the gate, would ignore it:
    either way no mark."""

    class Server:
        request_queue_size = 256

        def __init__(self, address, handler):
            self.socket = None

    config, token_file = tmp_path / "gate.json", tmp_path / "gate-token"
    token_file.write_text("tok-invented-0123456789", encoding="utf-8")  # the gate reads its token from a file
    document = {"log": str(tmp_path / "r.jsonl"), "hosts": {}, "listen_port": 0, "token_file": str(token_file)}
    config.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["gate.py", str(config)])
    monkeypatch.setattr(gate, "CFG", {})
    monkeypatch.setattr(gate, "GateServer", Server)
    monkeypatch.setattr(gate, "_tls_context", lambda: SimpleNamespace(wrap_socket=lambda sock, **options: sock))
    monkeypatch.setattr(gate, "serve", lambda httpd: None)
    said = []

    def printed(*words, **options):  # what the gate printed, with its handler of SIGUSR1 at that moment
        said.append((words, signal.getsignal(signal.SIGUSR1)))

    monkeypatch.setattr(gate, "print", printed, raising=False)
    previous = signal.getsignal(signal.SIGUSR1)
    try:
        gate.main()
    finally:
        signal.signal(signal.SIGUSR1, previous)
    assert said == [(("gate ready",), gate._on_run_start)]
