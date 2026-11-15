"""The HPC run's evidence rule (deploy/hpc-federation/run/evidence.py).

The last live run's driver printed `"token_in_log": False` as a literal, so its gate-log scan never ran: a false pass.
The rule that follows: every step writes raw evidence to files, and the run's summary is derived from those files by
one function. A summary item whose file is missing, empty or unparsable fails; no item can be built without a parsed
file; the verdict is PASS only when every item parsed and every item's own check passed.

`good_evidence` below writes a complete, passing evidence directory (names invented), and each test breaks one thing.
It is also the schema the run's steps write (run/README.md).
"""

import copy
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

RUN = Path(__file__).resolve().parents[2] / "deploy" / "hpc-federation" / "run"
BUNDLE = RUN.parent / "bundle"
PROVIDERS = tuple(f"P{n}" for n in (1, 2, 3))
SERVICES = ("Z", *PROVIDERS)
READERS = PROVIDERS[1:]  # they read what the first provider wrote
SIZE = 1 << 20
SHA = hashlib.sha256(b"the run's file").hexdigest()
NONCE = "0123456789abcdef0123456789abcdef"
SPACE, ROOT, FILE = "space-id", "root-id", "file-id"
WHENS = ("after onepanel's Couchbase setup", "after the rotation")
SENT = ("GET /settings/web (administrator)", 200), ("GET /settings/web (anonymous)", 401)
RUN_ID, JOB = "fedcba9876543210fedcba9876543210", "4242424"  # the run's identity (run.json); the job is invented
SCANNED_FILES = {
    "gate-requests": "sandbox/gate-requests.jsonl",
    "gate-stdout": "sandbox/gate-stdout.txt",
    "gate-stderr": "sandbox/gate-stderr.txt",
    "agent-stdout": "sandbox/agent.json",
    "agent-stderr": "sandbox/agent.err",
    "egress-log": "sandbox/egress.jsonl",
}


def load_evidence():
    spec = importlib.util.spec_from_file_location("hpcop_run_evidence", RUN / "evidence.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def logscan(counts, rc=None):
    doc = {"log_lines": 120, "log_bytes": 9000, "log_sha256": SHA, "counts": counts}
    if rc is not None:
        doc["rc"] = rc
    return doc


def guard_lines():
    rows = [("cb_watch", "start of the service")] + [("cb_guard", when) for when in WHENS]
    return [
        {"check": check, "when": when, "request": request, "status": status, "expected": status}
        for check, when in rows
        for request, status in SENT
    ]


COLLECTOR_UID = 1000


def egress_text(nonce=NONCE, destinations=(("127.0.0.1", 41234, "tcp"), ("127.0.0.1", 41234, "tcp"))):
    """A collector's log in egress-collector.py's own format: start, one record per destination, end."""
    lines = [{"event": "start", "format": "hpcop-egress/1", "nonce": nonce, "pid": 4, "uid": COLLECTOR_UID}]
    for address, port, kind in destinations:
        family = "inet6" if ":" in address else "inet"
        rec = {"op": "connect", "pid": 5, "fd": 3, "time": "1.000000000", "type": kind, "family": family}
        rec.update({"address": address, "port": port, "result": None, "dropped": 0})
        lines.append({"event": "record", "peer": {"pid": 5, "uid": COLLECTOR_UID}, "rec": rec})
    lines.append({"event": "end", "records": len(destinations), "malformed": 0, "unwritten": 0})
    stamped = [dict(line, seq=n, wall=1794700000.0 + n, mono=100.0 + n) for n, line in enumerate(lines)]
    return "\n".join(json.dumps(line) for line in stamped) + "\n"


def egress_rows():
    return [json.loads(line) for line in egress_text().splitlines()]


def rows_text(rows):
    return "".join(json.dumps(r) + "\n" for r in rows)


API = "/api/v3/oneprovider"


def gate_request(method, path, decision, status, body):
    return {"method": method, "path": API + path, "decision": decision, "status": status, "body": body}


def gate_text():
    """The gate's request log (gate.py): each request with its method, path, the gate's decision and the status."""
    rows = [
        gate_request("POST", "/lookup-file-id/run-space", "allow", 200, "lookup"),
        gate_request("GET", "/data/r/children", "allow", 200, "listing"),
        gate_request("GET", "/data/f/content", "allow", 200, "content"),
        gate_request("PUT", "/data/f/content", "deny", 403, "gate"),
        {"event": "teardown_complete", "connections": 0},
    ]
    return rows_text(rows)


def reach():
    ports = {
        "8443": "open",
        "6665": "open",
        "9443": "refused",
        "8091": "refused",
        "11210": "refused",
        "4369": "refused",
    }
    return {role: {p: {"connect": c} for p, c in ports.items()} for role in SERVICES}


def agent():
    return {
        "uid": 1000,
        "gate_lookup": {"status": 200, "file_id": ROOT, "file_id_expected": ROOT},
        "gate_listing": {"status": 200, "entries": 1},
        "gate_read": {"status": 200, "bytes": SIZE, "sha256_read": SHA, "sha256_expected": SHA},
        "gate_write_refused": {"status": 403},
        "gate_strips_client_token": {"probe_status": 200, "control_status": 200},
        "direct_no_token": {"space_status": 401, "content_status": 401},
        "reach": reach(),
        "internet_443": {"connect": "open", "ms": 21},
        "couchbase_login_attempts": {},
        "epmd_open_roles": [],
        "env_token_like": [],
        "cell_files": ["agent.py", "ca.pem", "hosts.txt"],
    }


def write_out():
    return {"space_id": SPACE, "root_id": ROOT, "file_id": FILE, "bytes": SIZE, "sha256": SHA, "sha256_read_back": SHA}


def read_out():
    return {"space_id": SPACE, "file_id": FILE, "read_status": 200, "sha256_read": SHA, "sha256_expected": SHA}


def good_files():
    """Every evidence file of a passing run: relative path -> a dict (one JSON object) or a str (JSON lines)."""
    counts = {"verdict_ok": 1, "ok_lines": 200, "fail_lines": 0, "error_lines": 0, "finish_failed": 0}
    counts.update({"guard_passed": 2, "defaults_ok": 1, "port_check_ok": 1, "live_hash_ok": 1})
    files = {}
    for role in SERVICES:
        files[f"{role}/up.json"] = logscan(counts)
        files[f"{role}/teardown.json"] = logscan({"service_stopped": 1, "workdir_removed": 1, "errors": 0}, rc=0)
        files[f"{role}/service-end.json"] = logscan({"stopped_at_request": 1, "exited_by_itself": 0})
        files[f"{role}/service-step.rc.json"] = {"name": "service_step", "rc": 0}
        for n, line in enumerate(guard_lines()):
            files[f"{role}/cb-guard-{n:03d}.json"] = line
    for role in PROVIDERS:
        files[f"{role}/registration.json"] = {"mint_rc": 0, "deliver_rc": 0}
        files[f"roundtrip/support-{role}.out.json"] = {"space_id": SPACE, "onboard_rc": 0}
        files[f"roundtrip/support-{role}.rc.json"] = {"name": "support", "rc": 0}
    for role in READERS:
        files[f"roundtrip/read-{role}.out.json"] = read_out()
        files[f"roundtrip/read-{role}.rc.json"] = {"name": "read", "rc": 0}
    online = [{"name": f"p{n}", "online": True} for n in range(3)]
    files["providers/online.out.json"] = {"providers": online, "online": 3}
    files["providers/online.rc.json"] = {"name": "online", "rc": 0}
    files["roundtrip/prepare.json"] = {"rc": 0, "stdout_bytes": 900}
    files["roundtrip/write.out.json"] = write_out()
    files["roundtrip/write.rc.json"] = {"name": "write", "rc": 0}
    files["sandbox/step.rc.json"] = {"name": "sandbox_step", "rc": 0}
    files["sandbox/agent.rc.json"] = {"name": "agent", "rc": 0}
    files["sandbox/agent.json"] = agent()
    files["sandbox/gate-requests.jsonl"] = gate_text()
    files["sandbox/gate-stdout.txt"] = "gate ready\n"
    files["sandbox/gate-stderr.txt"] = "gate: teardown\n"
    files["sandbox/agent.err"] = "agent: done\n"
    files["sandbox/gate-end.json"] = {"name": "gate", "rc": 0}
    files["sandbox/egress.jsonl"] = egress_text()
    files["sandbox/egress-collector.json"] = {"nonce": NONCE, "ready_lines": 1, "rc": 0}
    files["sandbox/canary.json"] = {"address": "127.0.0.1", "port": 41234, "rc": 0, "result": "open"}
    files["sandbox/node.json"] = {"tcp_listeners_total": 125, "tcp_listeners_ours": 1}
    files["teardown/steps-left.json"] = {"name": "steps_left", "rc": 0, "lines": ["batch"]}
    for role in (*SERVICES, "S"):
        zero = dict.fromkeys(("leftover_tmp", "tmp_top", "jobtmp", "listeners", "procs_other", "ps_rc", "ss_rc"), 0)
        files[f"teardown/nodecheck-{role}.json"] = dict(zero, role=role)
    files["release/scancel.rc.json"] = {"name": "scancel", "rc": 0}
    files["release/squeue-final.json"] = {"name": "squeue_final", "rc": 0, "lines": ["777 other-study-job"]}
    files["run.json"] = {"run_id": RUN_ID, "job": JOB, "started": "2026-11-15T00:00:00Z"}
    files.update(http_files())
    for name, rel in SCANNED_FILES.items():
        files[f"sandbox/token-scan-{name}.json"] = scan_doc(name, as_bytes(files[rel]))
    return files


def http_files():
    """The helpers' HTTP call logs: the zone's (prepare and the online check) and one per provider's work directory."""
    call = {"method": "GET", "path": "/spaces/s", "status": 200, "body_bytes": 2, "body_sha256": SHA}
    files = {"providers/http-zone.jsonl": json.dumps(call) + "\n"}
    for name in ("zone", "Ab1", "Cd2", "Ef3"):
        files[f"roundtrip/http-{name}.jsonl"] = json.dumps(call) + "\n"
    return files


def as_bytes(value):
    """A fixture file's bytes, as write_files writes it."""
    return (value if isinstance(value, str) else json.dumps(value)).encode("utf-8")


def scan_doc(name, content):
    """What sandbox.sh's scan() records of a clean scan of `content`."""
    shape = {"name": name, "pattern_bytes": 160, "pattern_lines": 1, "pattern_blank_lines": 0}
    digest = hashlib.sha256(content).hexdigest()
    return dict(shape, scanned_bytes=len(content), scanned_sha256=digest, grep_rc=1, matches=0)


def write_files(root, files):
    """Write evidence files; a scanned file a test rewrites gets its scan re-stamped, as the step would have scanned
    what it wrote (a test that means to break that binding writes the file itself)."""
    for rel, value in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
    for name, rel in SCANNED_FILES.items():
        scan = root / "sandbox" / f"token-scan-{name}.json"
        if rel in files and f"sandbox/token-scan-{name}.json" not in files and scan.exists():
            content = (root / rel).read_bytes()
            doc = json.loads(scan.read_text(encoding="utf-8"))
            doc.update(scanned_bytes=len(content), scanned_sha256=hashlib.sha256(content).hexdigest())
            scan.write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture(scope="module")
def evidence():
    return load_evidence()


@pytest.fixture
def run_dir(tmp_path):
    write_files(tmp_path, good_files())
    return tmp_path


def failed(summary):
    return {item["name"] for item in summary["items"] if not item["passed"]}


def item(summary, name):
    return next(i for i in summary["items"] if i["name"] == name)


def counted(summary):
    detail = item(summary, "release_squeue")["detail"]
    return {key: detail[key] for key in ("jobs_listed", "ours")}


def change(run_dir, rel, edit):
    """Rewrite one evidence file: `edit` gets its parsed object (or text) and returns the new value."""
    path = run_dir / rel
    value = path.read_text(encoding="utf-8")
    parsed = value if rel.endswith(".jsonl") else json.loads(value)
    write_files(run_dir, {rel: edit(copy.deepcopy(parsed))})


def test_a_complete_good_evidence_directory_passes(evidence, run_dir):
    summary = evidence.summary(run_dir)
    assert failed(summary) == set(), [(i["name"], i["error"]) for i in summary["items"] if not i["passed"]]
    assert summary["verdict"] == "PASS"
    names = [i["name"] for i in summary["items"]]
    assert len(names) == len(set(names)) > 35
    assert {"gate_token_scan", "release_squeue", "gate_read", "egress", "Z_couchbase_guard"} <= set(names)


def test_every_item_names_the_evidence_files_it_was_parsed_from(evidence, run_dir):
    for entry in evidence.summary(run_dir)["items"]:
        assert entry["files"], entry["name"]
        for found in entry["files"]:
            content = (run_dir / found["path"]).read_bytes()
            assert found["sha256"] == hashlib.sha256(content).hexdigest()
            assert found["bytes"] == len(content)


@pytest.mark.parametrize("rel", sorted(good_files()))
def test_an_item_whose_evidence_file_is_missing_fails_the_run(evidence, run_dir, rel):
    """O52: a summary item is derived from its saved evidence; without the file it fails, never passes."""
    (run_dir / rel).unlink()
    summary = evidence.summary(run_dir)
    assert summary["verdict"] == "FAIL"
    assert failed(summary)


@pytest.mark.parametrize("rel", sorted(good_files()))
def test_an_item_whose_evidence_file_is_empty_fails_the_run(evidence, run_dir, rel):
    (run_dir / rel).write_text("", encoding="utf-8")
    summary = evidence.summary(run_dir)
    assert summary["verdict"] == "FAIL"
    assert failed(summary)


@pytest.mark.parametrize("rel", sorted(good_files()))
def test_an_item_whose_evidence_file_is_unparsable_fails_the_run(evidence, run_dir, rel):
    (run_dir / rel).write_text("{this is not json\n", encoding="utf-8")
    summary = evidence.summary(run_dir)
    assert summary["verdict"] == "FAIL"
    assert failed(summary)


@pytest.mark.parametrize("rel", ["sandbox/agent.json", "roundtrip/write.out.json", "providers/online.out.json"])
def test_an_evidence_file_that_is_not_one_json_object_fails(evidence, run_dir, rel):
    (run_dir / rel).write_text("[1, 2]", encoding="utf-8")
    assert evidence.summary(run_dir)["verdict"] == "FAIL"


def test_a_missing_evidence_directory_fails_every_item(evidence, tmp_path):
    summary = evidence.summary(tmp_path / "nothing-here")
    assert summary["verdict"] == "FAIL"
    assert failed(summary) == {i["name"] for i in summary["items"]}


def test_a_token_count_above_zero_fails_the_run(evidence, run_dir):
    """O52: the gate-log token scan is a count read from its file, and a count above 0 fails the run."""
    change(run_dir, "sandbox/token-scan-gate-requests.json", lambda d: dict(d, grep_rc=0, matches=2))
    summary = evidence.summary(run_dir)
    assert summary["verdict"] == "FAIL"
    assert failed(summary) == {"gate_token_scan"}
    assert item(summary, "gate_token_scan")["detail"]["matches"] == {"gate-requests": 2}


def test_a_scan_that_errored_fails_the_run(evidence, run_dir):
    change(run_dir, "sandbox/token-scan-gate-stderr.json", lambda d: dict(d, grep_rc=2, matches=None))
    assert failed(evidence.summary(run_dir)) == {"gate_token_scan"}


def test_a_scan_with_an_empty_pattern_fails_the_run(evidence, run_dir):
    """An empty pattern file matches nothing: its zero counts would be another false pass."""
    change(run_dir, "sandbox/token-scan-gate-requests.json", lambda d: dict(d, pattern_bytes=0, pattern_lines=0))
    assert failed(evidence.summary(run_dir)) == {"gate_token_scan"}


def test_a_scan_with_a_blank_pattern_line_fails_the_run(evidence, run_dir):
    """A blank line in a pattern file matches every line, so a count would mean nothing."""
    change(run_dir, "sandbox/token-scan-agent-stdout.json", lambda d: dict(d, pattern_blank_lines=1))
    assert failed(evidence.summary(run_dir)) == {"gate_token_scan"}


def test_a_zero_count_that_contradicts_the_scans_exit_status_fails(evidence, run_dir):
    change(run_dir, "sandbox/token-scan-agent-stdout.json", lambda d: dict(d, grep_rc=0, matches=0))
    assert failed(evidence.summary(run_dir)) == {"gate_token_scan"}


def test_an_empty_gate_log_does_not_count_as_a_scan_of_the_gates_log(evidence, run_dir):
    change(run_dir, "sandbox/token-scan-gate-requests.json", lambda d: dict(d, scanned_bytes=0))
    assert failed(evidence.summary(run_dir)) == {"gate_token_scan"}


def test_an_hpcop_job_left_in_squeue_fails_the_run(evidence, run_dir):
    lines = ["777 other-study-job", "778 hpcop-standing-1"]
    change(run_dir, "release/squeue-final.json", lambda d: dict(d, lines=lines))
    summary = evidence.summary(run_dir)
    assert summary["verdict"] == "FAIL"
    assert failed(summary) == {"release_squeue"}
    assert counted(summary) == {"jobs_listed": 2, "ours": 1}


def test_another_jobs_of_the_same_user_is_counted_but_not_as_ours(evidence, run_dir):
    change(run_dir, "release/squeue-final.json", lambda d: dict(d, lines=["777 other-study-job", "779 other-two"]))
    summary = evidence.summary(run_dir)
    assert item(summary, "release_squeue")["passed"] is True
    assert counted(summary) == {"jobs_listed": 2, "ours": 0}


def test_an_empty_queue_is_a_pass_when_squeue_succeeded(evidence, run_dir):
    change(run_dir, "release/squeue-final.json", lambda d: dict(d, lines=[]))
    assert counted(evidence.summary(run_dir)) == {"jobs_listed": 0, "ours": 0}
    assert failed(evidence.summary(run_dir)) == set()


def test_an_empty_queue_from_a_failed_squeue_is_not_a_pass(evidence, run_dir):
    change(run_dir, "release/squeue-final.json", lambda d: dict(d, lines=[], rc=1))
    assert failed(evidence.summary(run_dir)) == {"release_squeue"}


def test_a_read_sha256_that_differs_from_the_expected_one_fails_the_run(evidence, run_dir):
    other = hashlib.sha256(b"another body").hexdigest()
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, gate_read=dict(d["gate_read"], sha256_read=other)))
    summary = evidence.summary(run_dir)
    assert summary["verdict"] == "FAIL"
    assert failed(summary) == {"gate_read"}
    detail = item(summary, "gate_read")["detail"]
    assert detail["sha256_read"] == other
    assert detail["sha256_expected"] == SHA


def test_a_gate_read_that_hashed_nothing_fails_even_if_the_two_strings_agree(evidence, run_dir):
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, gate_read=dict(d["gate_read"], sha256_read=None)))
    assert failed(evidence.summary(run_dir)) == {"gate_read"}


def test_a_provider_read_that_differs_from_what_was_written_fails_the_run(evidence, run_dir):
    other = hashlib.sha256(b"torn").hexdigest()
    change(run_dir, "roundtrip/read-P2.out.json", lambda d: dict(d, sha256_read=other, sha256_expected=other))
    assert failed(evidence.summary(run_dir)) == {"read_P2"}


def test_a_gate_expected_sha256_that_is_not_the_writers_fails_the_run(evidence, run_dir):
    other = hashlib.sha256(b"another file").hexdigest()
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, gate_read=dict(d["gate_read"], sha256_read=other)))
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, gate_read=dict(d["gate_read"], sha256_expected=other)))
    assert failed(evidence.summary(run_dir)) == {"gate_read"}


def test_a_gate_write_that_was_not_refused_fails_the_run(evidence, run_dir):
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, gate_write_refused={"status": 204}))
    assert failed(evidence.summary(run_dir)) == {"gate_write_refused"}


def test_a_gate_write_that_ended_in_a_transport_error_is_not_a_refusal(evidence, run_dir):
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, gate_write_refused={"status": "error: timeout"}))
    assert failed(evidence.summary(run_dir)) == {"gate_write_refused"}


def test_a_client_token_that_reached_the_platform_fails_the_run(evidence, run_dir):
    probe = {"probe_status": 401, "control_status": 200}
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, gate_strips_client_token=probe))
    assert failed(evidence.summary(run_dir)) == {"gate_strips_client_token"}


def test_a_listing_through_the_gate_that_listed_nothing_fails_the_run(evidence, run_dir):
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, gate_listing={"status": 200, "entries": 0}))
    assert failed(evidence.summary(run_dir)) == {"gate_listing"}


def test_a_lookup_that_returned_another_file_id_fails_the_run(evidence, run_dir):
    lookup = {"status": 200, "file_id": "another", "file_id_expected": ROOT}
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, gate_lookup=lookup))
    assert failed(evidence.summary(run_dir)) == {"gate_lookup"}


def test_a_direct_request_without_a_token_that_was_served_fails_the_run(evidence, run_dir):
    change(
        run_dir, "sandbox/agent.json", lambda d: dict(d, direct_no_token={"space_status": 200, "content_status": 401})
    )
    assert failed(evidence.summary(run_dir)) == {"direct_no_token"}


@pytest.mark.parametrize("port", ["8091", "11210", "9443", "4369"])
def test_an_internal_port_that_the_cell_reached_fails_the_run(evidence, run_dir, port):
    def reached(doc):
        doc["reach"][PROVIDERS[1]][port] = {"connect": "open", "ms": 3}
        return doc

    change(run_dir, "sandbox/agent.json", reached)
    assert failed(evidence.summary(run_dir)) == {"reach_matrix"}


def test_a_reach_record_that_lacks_a_host_or_port_fails_the_run(evidence, run_dir):
    def lacking(doc):
        del doc["reach"][PROVIDERS[2]]["11210"]
        return doc

    change(run_dir, "sandbox/agent.json", lacking)
    assert failed(evidence.summary(run_dir)) == {"reach_matrix"}


def test_a_closed_public_port_makes_the_reach_check_vacuous_and_fails_it(evidence, run_dir):
    """If 8443 is closed too, the closed internal ports say nothing about what the cell could have reached."""

    def closed(doc):
        doc["reach"]["Z"]["8443"] = {"connect": "timeout"}
        return doc

    change(run_dir, "sandbox/agent.json", closed)
    assert failed(evidence.summary(run_dir)) == {"reach_matrix"}


def test_the_internet_being_reachable_is_a_fact_not_a_failure(evidence, run_dir):
    summary = evidence.summary(run_dir)
    assert item(summary, "fact_internet_443")["kind"] == "fact"
    assert item(summary, "fact_internet_443")["detail"]["connect"] == "open"
    assert summary["verdict"] == "PASS"


def test_a_token_like_variable_in_the_cell_fails_the_run(evidence, run_dir):
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, env_token_like=["X_TOKEN"]))
    assert failed(evidence.summary(run_dir)) == {"cell_clean"}


def test_a_file_in_the_cell_directory_beyond_the_three_fails_the_run(evidence, run_dir):
    files = ["agent.py", "ca.pem", "ca.srl", "hosts.txt"]
    change(run_dir, "sandbox/agent.json", lambda d: dict(d, cell_files=files))
    assert failed(evidence.summary(run_dir)) == {"cell_clean"}


@pytest.mark.parametrize("tamper", ["anonymous served", "wrong expected", "missing when", "status differs"])
def test_a_couchbase_guard_status_that_is_not_the_expected_one_fails_the_run(evidence, run_dir, tamper):
    rel = "P1/cb-guard-001.json"  # the in-container guard's anonymous request
    if tamper == "anonymous served":
        change(run_dir, rel, lambda d: dict(d, status=200))
    elif tamper == "wrong expected":
        change(run_dir, rel, lambda d: dict(d, status=200, expected=200))
    elif tamper == "status differs":
        change(run_dir, rel, lambda d: dict(d, status=500))
    else:
        (run_dir / rel).unlink()
    summary = evidence.summary(run_dir)
    assert failed(summary) == {"P1_couchbase_guard"}
    assert summary["verdict"] == "FAIL"


def test_a_guard_stage_that_never_ran_fails_the_run(evidence, run_dir):
    for n, line in enumerate(guard_lines()):
        if line["when"] == "after the rotation":
            (run_dir / f"Z/cb-guard-{n:03d}.json").unlink()
    assert failed(evidence.summary(run_dir)) == {"Z_couchbase_guard"}


def test_a_service_log_with_a_failure_line_fails_that_service_only(evidence, run_dir):
    change(run_dir, "P2/up.json", lambda d: dict(d, counts=dict(d["counts"], fail_lines=1)))
    assert failed(evidence.summary(run_dir)) == {"P2_up"}


def test_a_service_log_without_its_verdict_line_fails(evidence, run_dir):
    change(run_dir, "Z/up.json", lambda d: dict(d, counts=dict(d["counts"], verdict_ok=0)))
    assert failed(evidence.summary(run_dir)) == {"Z_up"}


def test_a_service_that_exited_by_itself_fails_its_teardown_item(evidence, run_dir):
    counts = {"stopped_at_request": 0, "exited_by_itself": 1}
    change(run_dir, "P3/service-end.json", lambda d: dict(d, counts=counts))
    assert failed(evidence.summary(run_dir)) == {"P3_teardown"}


def test_a_service_step_with_a_failing_exit_status_fails_its_teardown_item(evidence, run_dir):
    change(run_dir, "Z/service-step.rc.json", lambda d: dict(d, rc=143))
    assert failed(evidence.summary(run_dir)) == {"Z_teardown"}


def test_a_step_left_besides_the_batch_step_fails_the_run(evidence, run_dir):
    change(run_dir, "teardown/steps-left.json", lambda d: dict(d, lines=["batch", "extern", "zone-service"]))
    assert failed(evidence.summary(run_dir)) == {"steps_left"}


@pytest.mark.parametrize("field", ["leftover_tmp", "tmp_top", "jobtmp", "listeners", "procs_other"])
def test_a_leftover_on_a_node_fails_the_run(evidence, run_dir, field):
    change(run_dir, "teardown/nodecheck-S.json", lambda d: dict(d, **{field: 1}))
    assert failed(evidence.summary(run_dir)) == {"nodes_clean"}


@pytest.mark.parametrize("field", ["ps_rc", "ss_rc"])
def test_a_node_check_whose_listing_command_failed_fails_the_run(evidence, run_dir, field):
    """O52: a process or listener count from a command that failed is no count of zero."""
    change(run_dir, "teardown/nodecheck-P2.json", lambda d: dict(d, **{field: 1}))
    assert failed(evidence.summary(run_dir)) == {"nodes_clean"}


def test_the_agents_node_must_be_among_the_nodes_checked(evidence, run_dir):
    (run_dir / "teardown/nodecheck-S.json").unlink()
    assert failed(evidence.summary(run_dir)) == {"nodes_clean"}


def test_the_gate_log_without_its_teardown_line_fails_the_run(evidence, run_dir):
    rows = [json.loads(line) for line in gate_text().splitlines()][:-1]
    write_files(run_dir, {"sandbox/gate-requests.jsonl": "".join(json.dumps(r) + "\n" for r in rows)})
    assert failed(evidence.summary(run_dir)) == {"gate_log"}


def test_a_write_that_the_gate_forwarded_fails_the_run(evidence, run_dir):
    rows = [json.loads(line) for line in gate_text().splitlines()]
    rows[3]["status"] = 204
    write_files(run_dir, {"sandbox/gate-requests.jsonl": "".join(json.dumps(r) + "\n" for r in rows)})
    assert failed(evidence.summary(run_dir)) == {"gate_log"}


def test_a_post_that_is_not_a_lookup_and_was_forwarded_fails_the_run(evidence, run_dir):
    """O52: only the policy's two lookups are POSTs that read; a POST that creates a file is a write."""
    rows = [json.loads(line) for line in gate_text().splitlines()]
    rows.insert(1, gate_request("POST", "/data/r/children", "allow", 201, "other"))
    write_files(run_dir, {"sandbox/gate-requests.jsonl": rows_text(rows)})
    assert failed(evidence.summary(run_dir)) == {"gate_log"}


def test_a_write_the_gate_allowed_fails_the_run_even_when_the_platform_refused_it(evidence, run_dir):
    """O52: the gate's own refusal is the check; a 4xx from upstream means the write went out."""
    rows = [json.loads(line) for line in gate_text().splitlines()]
    rows[3]["decision"] = "allow"
    write_files(run_dir, {"sandbox/gate-requests.jsonl": rows_text(rows)})
    assert failed(evidence.summary(run_dir)) == {"gate_log"}


def test_an_egress_log_of_another_launch_fails_the_run(evidence, run_dir):
    write_files(run_dir, {"sandbox/egress.jsonl": egress_text(nonce="f" * 32)})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_an_egress_log_with_a_gap_in_its_sequence_fails_the_run(evidence, run_dir):
    rows = [json.loads(line) for line in egress_text().splitlines()]
    rows[2]["seq"] = 5
    write_files(run_dir, {"sandbox/egress.jsonl": "".join(json.dumps(r) + "\n" for r in rows)})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_an_egress_log_that_does_not_end_with_its_end_line_fails_the_run(evidence, run_dir):
    text = "".join(egress_text().splitlines(keepends=True)[:-1])
    write_files(run_dir, {"sandbox/egress.jsonl": text})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_an_egress_log_with_no_records_fails_the_run(evidence, run_dir):
    """The cell connects to the gate at least: a log of none says the shim did not load, not that nothing left."""
    write_files(run_dir, {"sandbox/egress.jsonl": egress_text(destinations=())})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_an_egress_log_that_dropped_datagrams_fails_the_run(evidence, run_dir):
    rows = [json.loads(line) for line in egress_text().splitlines()]
    rows[-1]["unwritten"] = 3
    write_files(run_dir, {"sandbox/egress.jsonl": "".join(json.dumps(r) + "\n" for r in rows)})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_an_egress_record_whose_sender_dropped_records_fails_the_run(evidence, run_dir):
    """O52: the shim counts the records it could not deliver in its next one; the log then misses destinations."""
    rows = egress_rows()
    rows[2]["rec"]["dropped"] = 2
    write_files(run_dir, {"sandbox/egress.jsonl": rows_text(rows)})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_an_egress_record_from_another_uid_than_the_collectors_fails_the_run(evidence, run_dir):
    """O52: the kernel attests each sender's uid; a record from another uid is no record of this cell's."""
    rows = egress_rows()
    rows[2]["peer"]["uid"] = COLLECTOR_UID + 1
    write_files(run_dir, {"sandbox/egress.jsonl": rows_text(rows)})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_an_egress_end_line_whose_malformed_count_is_not_the_logs_fails_the_run(evidence, run_dir):
    """O52: the end line's counts are the lines before it, malformed ones included."""
    rows = egress_rows()
    rows[-1]["malformed"] = 1
    write_files(run_dir, {"sandbox/egress.jsonl": rows_text(rows)})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def renumbered(rows):
    """The collector's numbering and clock readings, redone after a line is inserted."""
    return [dict(row, seq=n, mono=100.0 + n) for n, row in enumerate(rows)]


EGRESS_DAMAGE = {
    "the clock goes back": lambda rows: rows[2].update(mono=rows[1]["mono"] - 1) or rows,
    "an event of no known kind": lambda rows: renumbered([*rows[:2], {"event": "note"}, *rows[2:]]),
    "another format": lambda rows: rows[0].update(format="hpcop-egress/0") or rows,
    "an operation the shim has not": lambda rows: rows[2]["rec"].update(op="bind") or rows,
    "a record without its time": lambda rows: rows[2]["rec"].pop("time") and rows,
    "an address of another family": lambda rows: rows[2]["rec"].update(family="inet6") or rows,
    "an attempt with a result": lambda rows: rows[2]["rec"].update(result=0) or rows,
    "an address that is not text": lambda rows: rows[2]["rec"].update(address=2130706433) or rows,
    "a start line without the collector's uid": lambda rows: rows[0].pop("uid") and rows,
}


@pytest.mark.parametrize("damage", sorted(EGRESS_DAMAGE))
def test_an_egress_log_the_core_reads_as_damaged_fails_the_run(evidence, run_dir, damage):
    """O52: the summary reads the collector's log as expcore.egress does; a log the core would call unknown never
    passes here."""
    write_files(run_dir, {"sandbox/egress.jsonl": rows_text(EGRESS_DAMAGE[damage](egress_rows()))})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_a_collector_that_exited_badly_fails_the_run(evidence, run_dir):
    change(run_dir, "sandbox/egress-collector.json", lambda d: dict(d, rc=1))
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_the_egress_item_counts_the_distinct_destinations_and_names_no_address(evidence, run_dir):
    """O52: the summary is what gets pasted; a destination's address stays in the log, for the core to classify."""
    seen = (("127.0.0.1", 41234, "tcp"), ("127.0.0.1", 41234, "tcp"), ("192.0.2.9", 443, "tcp"), ("::1", 53, "udp"))
    write_files(run_dir, {"sandbox/egress.jsonl": egress_text(destinations=seen)})
    summary = evidence.summary(run_dir)
    detail = item(summary, "egress")["detail"]
    assert detail["destinations"] == {"distinct": 3, "loopback": 2, "other": 1}
    assert detail["records"] == 4
    assert "192.0.2.9" not in json.dumps(summary)


# --- the run's identity, the scans' binding to what they scanned, the HTTP call logs ---


def test_the_summary_names_the_runs_identity_and_never_the_allocations_job(evidence, run_dir):
    """O52: run.json, made by the run's first step in an empty directory, says which run the evidence is of."""
    summary = evidence.summary(run_dir)
    detail = item(summary, "run_identity")["detail"]
    assert (detail["run_id"], detail["started"]) == (RUN_ID, "2026-11-15T00:00:00Z")
    assert JOB not in json.dumps(summary)


@pytest.mark.parametrize("bad", [{"run_id": "not-a-nonce"}, {"job": ""}, {"started": ""}, {"run_id": 5}])
def test_a_run_identity_that_is_not_one_fails_the_run(evidence, run_dir, bad):
    change(run_dir, "run.json", lambda d: dict(d, **bad))
    assert failed(evidence.summary(run_dir)) == {"run_identity"}


@pytest.mark.parametrize("name", sorted(SCANNED_FILES))
def test_a_scanned_file_that_changed_after_its_scan_fails_the_token_scan(evidence, run_dir, name):
    """O52: a count holds for the bytes it was taken of; a file replaced after its scan (with a token, say) voids it."""
    path = run_dir / SCANNED_FILES[name]
    path.write_bytes(path.read_bytes() + b"\n")  # still parses: only the scan's binding breaks
    assert failed(evidence.summary(run_dir)) == {"gate_token_scan"}


@pytest.mark.parametrize("name", ["gate-stdout", "gate-stderr", "agent-stderr"])
def test_a_scanned_file_removed_after_its_scan_fails_the_token_scan(evidence, run_dir, name):
    """O52: the logs no item parses are read by the scan's item, so a missing one is never a clean scan."""
    (run_dir / SCANNED_FILES[name]).unlink()
    assert failed(evidence.summary(run_dir)) == {"gate_token_scan"}


def test_a_missing_http_call_log_fails_the_run(evidence, run_dir):
    """O52: a provider's call log is raw evidence of the round trip; without it the run does not pass."""
    (run_dir / "roundtrip" / "http-Ef3.jsonl").unlink()
    assert failed(evidence.summary(run_dir)) == {"fact_http_calls"}


def test_an_item_cannot_be_built_outside_the_one_constructor(evidence):
    for args in [("x", (), True, True, {}, None), ("x", ("a.json",), True, True, {}, None)]:
        with pytest.raises(TypeError):
            evidence.Item(*args)
    with pytest.raises(TypeError):
        evidence.Item(object(), "x", ("a.json",), True, True, {}, None)


def test_the_one_constructor_refuses_an_item_with_no_evidence_file(evidence, tmp_path):
    with pytest.raises(ValueError, match="must name the evidence file"):
        evidence.build_item(tmp_path, "x", "check", (), lambda docs: evidence.verdict(True))


def test_a_parser_that_returns_a_bare_boolean_is_refused(evidence, run_dir):
    built = evidence.build_item(run_dir, "x", "check", (("w", "roundtrip/write.out.json"),), lambda docs: True)
    assert built.passed is False
    assert built.parsed is False
    assert "verdict" in built.error


def test_a_parser_is_never_called_for_a_file_that_did_not_parse(evidence, tmp_path):
    def parser(docs):
        raise AssertionError("called with no parsed file")

    built = evidence.build_item(tmp_path, "x", "check", (("w", "roundtrip/write.out.json"),), parser)
    assert built.passed is False
    assert built.parsed is False
    assert "missing" in built.error


def test_a_parser_that_raises_marks_the_item_failed_never_passed(evidence, run_dir):
    def parser(docs):
        return evidence.verdict(docs["w"]["no-such-key"] == 1)

    built = evidence.build_item(run_dir, "x", "check", (("w", "roundtrip/write.out.json"),), parser)
    assert built.passed is False
    assert built.parsed is False


def test_a_write_whose_read_back_differs_from_what_was_written_fails_the_run(evidence, run_dir):
    """The verdict is a function of the parsed file: the same parser, a file that disagrees with itself, a failure."""
    change(run_dir, "roundtrip/write.out.json", lambda d: dict(d, sha256_read_back="0" * 64))
    assert failed(evidence.summary(run_dir)) >= {"write_P1"}


def test_every_item_of_the_summary_is_built_by_the_catalog_through_the_constructor(evidence, run_dir):
    names = [name for name, *_ in evidence.catalog()]
    assert names == [i["name"] for i in evidence.summary(run_dir)["items"]]


def test_the_command_line_prints_the_summary_and_exits_0_on_pass(run_dir):
    done = subprocess.run(
        [sys.executable, "-I", str(RUN / "evidence.py"), str(run_dir)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["verdict"] == "PASS"


def test_the_command_line_exits_1_on_fail(run_dir):
    (run_dir / "sandbox/agent.json").unlink()
    done = subprocess.run(
        [sys.executable, "-I", str(RUN / "evidence.py"), str(run_dir)], capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 1
    assert json.loads(done.stdout)["verdict"] == "FAIL"


def test_the_summary_names_no_host_and_carries_no_evidence_directory_path(evidence, run_dir):
    text = json.dumps(evidence.summary(run_dir))
    assert str(run_dir) not in text


# --- the Couchbase guard's status codes, as data (the evidence the batch jobs' logs carry) ----------------------------

GUARD_TESTS = Path(__file__).with_name("test_hpc_couchbase_guard.py")
EVIDENCE_LINE = re.compile(r"^hpcop-evidence (\{.*\})$", re.M)


def load_guard_tests():
    """The guard's own tests, loaded by path for their stand-in Couchbase helpers and fixtures."""
    spec = importlib.util.spec_from_file_location("hpcop_guard_tests", GUARD_TESTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard_tests = load_guard_tests()
launch, processes = guard_tests.launch, guard_tests.processes


def evidence_lines(text):
    return [json.loads(m.group(1)) for m in EVIDENCE_LINE.finditer(text)]


def watch(launch, processes, **fake):
    """The guard against the stand-in, with onepanel's setup played: its exit status and every line it printed."""
    secrets_dir, user, password = launch
    port = guard_tests.free_port()
    guard = guard_tests.start_guard(processes, secrets_dir, port)
    guard_tests.start_fake(processes, port, **fake)
    guard_tests.onepanel_setup(port, user, password)
    return guard_tests.verdict(guard)


def test_the_guard_prints_its_two_status_codes_as_data_when_it_passes(launch, processes):
    status, out = watch(launch, processes)
    assert status == 0, out
    assert evidence_lines(out) == [
        {
            "check": "cb_watch",
            "when": "start of the service",
            "request": "GET /settings/web (administrator)",
            "status": 200,
            "expected": 200,
        },
        {
            "check": "cb_watch",
            "when": "start of the service",
            "request": "GET /settings/web (anonymous)",
            "status": 401,
            "expected": 401,
        },
    ]


def test_the_guard_prints_the_status_that_failed_it(launch, processes):
    status, out = watch(launch, processes, anon_admin=True)
    assert status == 2, out
    anonymous = [e for e in evidence_lines(out) if e["request"].endswith("(anonymous)")]
    assert [(e["status"], e["expected"]) for e in anonymous] == [(200, 401)]


def test_the_guards_evidence_lines_hold_no_secret(launch, processes):
    _, user, password = launch
    _, out = watch(launch, processes)
    for line in EVIDENCE_LINE.findall(out):
        assert user not in line
        assert password not in line
        assert "127.0.0.1" not in line


def bash(script, *args):
    full = f'source "{BUNDLE}/lib.sh"; {script}'
    return subprocess.run(["bash", "-c", full, "lib", *args], capture_output=True, text=True, timeout=30, check=False)


STUBS = (
    'curl_auth_file() { :; }; jsonget() { printf "%s" "${STUB_WHO:-cb1}"; }\n'
    'http() { if [[ " $* " == *" -K "* ]]; then printf "%s" "${STUB_ADMIN:-200}"; '
    'else printf "%s" "${STUB_ANON:-401}"; fi; }\n'
)


def cb_guard(tmp_path, **stubs):
    env = " ".join(f"{k}={v}" for k, v in stubs.items())
    return bash(f'{STUBS}{env} cb_guard "{tmp_path}" cb1 pw "after the rotation"')


def test_cb_guard_prints_one_evidence_line_per_request_on_stdout(tmp_path):
    done = cb_guard(tmp_path)
    assert done.returncode == 0, done.stderr
    assert evidence_lines(done.stdout) == [
        {
            "check": "cb_guard",
            "when": "after the rotation",
            "request": "GET /settings/web (administrator)",
            "status": 200,
            "expected": 200,
        },
        {
            "check": "cb_guard",
            "when": "after the rotation",
            "request": "GET /settings/web (anonymous)",
            "status": 401,
            "expected": 401,
        },
    ]


def test_cb_guard_prints_the_anonymous_status_before_it_fails_on_it(tmp_path):
    done = cb_guard(tmp_path, STUB_ANON=200)
    assert done.returncode != 0
    anonymous = [e for e in evidence_lines(done.stdout) if e["request"].endswith("(anonymous)")]
    assert [(e["status"], e["expected"]) for e in anonymous] == [(200, 401)]


def test_cb_guard_prints_the_administrators_status_before_it_fails_on_it(tmp_path):
    done = cb_guard(tmp_path, STUB_ADMIN=401)
    assert done.returncode != 0
    assert [(e["request"], e["status"], e["expected"]) for e in evidence_lines(done.stdout)] == [
        ("GET /settings/web (administrator)", 401, 200)
    ]


def test_cb_evidence_refuses_a_field_that_is_not_in_the_shape_of_a_guard_line():
    for args in [
        ('a "quoted" when', "GET /x", "200", "200"),
        ("when", "GET /x", "abc", "200"),
        ("w", "GET /x", "20", "1"),
    ]:
        done = bash('cb_evidence "$@"', *args)
        assert done.returncode != 0, args
        assert not done.stdout, args


def test_the_job_log_carries_only_well_formed_evidence_lines_from_run_log(tmp_path):
    good = (
        'hpcop-evidence {"check": "cb_watch", "when": "start of the service", '
        '"request": "GET /settings/web (anonymous)", "status": 401, "expected": 401}'
    )
    log = tmp_path / "run.log"
    log.write_text(f"noise\n{good}\nhpcop-evidence not json at all but long enough\nhpcop-evidence {{}}x\nmore\n")
    done = bash(f'guard_evidence "{log}"')
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == [good]


GUARD_PASSED = "hpcop: Couchbase guard passed: this launch's administrator answers GET /settings/web"


def wait_ready_output(tmp_path, run_log):
    """What lib.sh's wait_ready, run for real on a work directory whose service is ready, prints into the job log."""
    work = tmp_path / "w"
    (work / "rootfs" / "root").mkdir(parents=True)
    (work / "rootfs" / "root" / "service-ready.lock").touch()
    (work / "run.log").write_text(run_log, encoding="utf-8")
    done = bash(f'wait_ready "{work}" 1 2>&1')
    return done.returncode, done.stdout


def test_a_ready_start_copies_the_guards_evidence_lines_into_the_job_log(tmp_path):
    first = (
        'hpcop-evidence {"check": "cb_watch", "when": "start of the service", '
        '"request": "GET /settings/web (administrator)", "status": 200, "expected": 200}'
    )
    run_log = f"noise\n{first}\n{GUARD_PASSED}\nCongratulations! New deployment\n"
    status, out = wait_ready_output(tmp_path, run_log)
    assert status == 0, out
    assert out.splitlines()[0] == first
    assert "ok: deployment finished; Couchbase guard passed: this launch's administrator answers" in out


def test_a_failed_guard_still_copies_its_evidence_lines_into_the_job_log_before_the_start_fails(tmp_path):
    anonymous = (
        'hpcop-evidence {"check": "cb_watch", "when": "start of the service", '
        '"request": "GET /settings/web (anonymous)", "status": 200, "expected": 401}'
    )
    run_log = f"{anonymous}\nhpcop: Couchbase guard FAILED: an anonymous request was answered\n"
    status, out = wait_ready_output(tmp_path, run_log)
    assert status != 0
    assert out.splitlines()[0] == anonymous
    assert "Couchbase guard FAILED" in out


def rows_of(text):
    return [json.loads(line) for line in text.splitlines()]


def text_of(rows):
    """The rows as the collector numbers them: seq and its clock reading redone in order."""
    return "".join(json.dumps(dict(row, seq=n, mono=100.0 + n)) + "\n" for n, row in enumerate(rows))


def test_an_egress_log_whose_first_record_is_not_a_connect_to_the_canary_endpoint_fails_the_run(evidence, run_dir):
    """The shim may never have loaded: a log that opens with another destination says nothing of the canary."""
    other = egress_text(destinations=(("192.0.2.9", 443, "tcp"), ("127.0.0.1", 41234, "tcp")))
    write_files(run_dir, {"sandbox/egress.jsonl": other})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_an_egress_log_whose_canary_connect_is_on_another_port_than_the_saved_endpoint_fails_the_run(evidence, run_dir):
    change(run_dir, "sandbox/canary.json", lambda d: dict(d, port=41235))
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_an_egress_log_whose_first_record_is_not_a_connect_fails_the_run(evidence, run_dir):
    rows = rows_of(egress_text())
    rows[1]["rec"]["op"] = "sendto"
    write_files(run_dir, {"sandbox/egress.jsonl": text_of(rows)})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_an_egress_log_with_a_malformed_line_before_the_canary_record_fails_the_run(evidence, run_dir):
    rows = rows_of(egress_text())
    rows.insert(1, {"event": "malformed", "peer": {"pid": 5, "uid": 1000}, "bytes": 3, "sha256": SHA})
    write_files(run_dir, {"sandbox/egress.jsonl": text_of(rows)})
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_a_canary_that_did_not_succeed_in_the_cell_fails_the_run(evidence, run_dir):
    change(run_dir, "sandbox/canary.json", lambda d: dict(d, rc=1))
    assert failed(evidence.summary(run_dir)) == {"egress"}
    change(run_dir, "sandbox/canary.json", lambda d: dict(d, rc=0, result="error: PermissionError"))
    assert failed(evidence.summary(run_dir)) == {"egress"}


def test_the_egress_item_names_the_canary_file_among_its_evidence(evidence, run_dir):
    assert "sandbox/canary.json" in item(evidence.summary(run_dir), "egress")["evidence"]


def test_an_egress_log_with_a_malformed_line_after_the_canary_record_fails_the_run(evidence, run_dir):
    """The core reads a log with a malformed line as unknown, so the summary must not pass it either."""
    rows = rows_of(egress_text())
    rows.insert(2, {"event": "malformed", "peer": {"pid": 5, "uid": 1000}, "bytes": 3, "sha256": SHA})
    rows[-1]["malformed"] = 1
    write_files(run_dir, {"sandbox/egress.jsonl": text_of(rows)})
    summary = evidence.summary(run_dir)
    assert failed(summary) == {"egress"}
    assert item(summary, "egress")["detail"]["failing"] == ["no malformed datagram"]
    assert item(summary, "egress")["detail"]["malformed"] == 1
