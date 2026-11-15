"""The HPC run's steps (deploy/hpc-federation/run): what they write is what run/evidence.py reads.

The steps run only on the site, but the files they write are plain: record.py, the shell functions of common.sh and
sandbox.sh, and the Python helpers' outputs are exercised here on invented data, and every file they produce is read
back through the same summary the run uses. A step that wrote a name or a shape the summary does not read would be a
missing item on the site, which fails the run; here it fails the build.
"""

import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
RUN = HERE.parents[1] / "deploy" / "hpc-federation" / "run"
BUNDLE = RUN.parent / "bundle"
SCRIPTS = sorted(RUN.glob("*.sh"))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


base = load(HERE / "test_hpc_run_evidence.py", "hpcop_run_evidence_tests")
evidence = base.load_evidence()
good_files, write_files, failed, item, change = base.good_files, base.write_files, base.failed, base.item, base.change
run_dir = base.run_dir


def record(*args, stdin=""):
    return subprocess.run(
        [sys.executable, "-I", str(RUN / "record.py"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def parsed(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# --- record.py: how a step writes a file ---


def test_record_writes_a_file_0600_in_a_directory_0700(tmp_path):
    target = tmp_path / "step" / "x.rc.json"
    assert record("rc", str(target), "name", "7").returncode == 0
    assert parsed(target) == {"name": "name", "rc": 7}
    assert mode(target) == 0o600
    assert mode(target.parent) == 0o700


def test_record_refuses_an_exit_status_that_is_not_a_number(tmp_path):
    done = record("rc", str(tmp_path / "x.json"), "name", "seven")
    assert done.returncode != 0
    assert not (tmp_path / "x.json").exists()


def test_record_keeps_the_raw_lines_of_a_listing_with_their_exit_status(tmp_path):
    assert record("lines", str(tmp_path / "q.json"), "squeue_final", "0", stdin="1 job-a\n2 hpcop-b\n").returncode == 0
    assert parsed(tmp_path / "q.json") == {"name": "squeue_final", "rc": 0, "lines": ["1 job-a", "2 hpcop-b"]}


def test_record_counts_the_lines_of_a_log_that_match_each_pattern(tmp_path):
    log = tmp_path / "svc.log"
    log.write_text("ok: a\nok: b\nFAIL: c\nnoise\n", encoding="utf-8")
    done = record("logscan", str(tmp_path / "up.json"), str(log), "--rc", "3", "oks=^ok:", "fails=^FAIL")
    assert done.returncode == 0, done.stderr
    doc = parsed(tmp_path / "up.json")
    assert doc["counts"] == {"oks": 2, "fails": 1}
    assert doc["log_lines"] == 4
    assert doc["rc"] == 3
    assert len(doc["log_sha256"]) == 64


def test_record_types_the_values_of_a_key_value_file(tmp_path):
    record("kv", str(tmp_path / "kv.json"), "n=int:5", "s=str:text")
    assert parsed(tmp_path / "kv.json") == {"n": 5, "s": "text"}
    assert record("kv", str(tmp_path / "bad.json"), "n=int:five").returncode != 0


def test_record_reads_one_field_of_a_saved_file(tmp_path):
    record("kv", str(tmp_path / "kv.json"), "n=int:5", "s=str:text")
    assert record("field", str(tmp_path / "kv.json"), "s").stdout == "text\n"
    assert record("field", str(tmp_path / "kv.json"), "missing").returncode != 0


def test_record_makes_a_fresh_nonce_each_time():
    nonces = {record("nonce").stdout.strip() for _ in range(3)}
    assert len(nonces) == 3
    assert all(re.fullmatch(r"[0-9a-f]{32}", n) for n in nonces)


def test_record_keeps_one_file_per_guard_evidence_line_and_a_bad_line_as_text(tmp_path):
    line = 'hpcop-evidence {"check": "cb_guard", "when": "w", "request": "r", "status": 200, "expected": 200}'
    log = tmp_path / "svc.log"
    log.write_text(f"noise\n{line}\nhpcop-evidence not json\n{line}\n", encoding="utf-8")
    done = record("guard-lines", str(log), str(tmp_path))
    assert done.stdout.strip() == "3"
    assert parsed(tmp_path / "cb-guard-000.json")["status"] == 200
    assert (tmp_path / "cb-guard-001.json").read_text(encoding="utf-8").startswith("hpcop-evidence not json")
    assert parsed(tmp_path / "cb-guard-002.json")["status"] == 200


def test_a_guard_line_that_is_not_json_makes_the_guard_item_fail(evidence_run):
    (evidence_run / base.PROVIDERS[0] / "cb-guard-000.json").write_text("hpcop-evidence not json\n", encoding="utf-8")
    assert failed(evidence.summary(evidence_run)) == {"P1_couchbase_guard"}


@pytest.fixture
def evidence_run(run_dir):
    return run_dir


def test_record_scan_keeps_the_shape_of_the_pattern_and_never_its_content(tmp_path):
    secret = "tok-" + "z9Qx" * 8
    pattern, scanned = tmp_path / "pattern", tmp_path / "scanned"
    pattern.write_text(secret + "\n", encoding="utf-8")
    scanned.write_text("a line\nanother\n", encoding="utf-8")
    done = record("scan", str(tmp_path / "scan.json"), "gate-requests", str(pattern), str(scanned), "1", "0")
    assert done.returncode == 0
    doc = parsed(tmp_path / "scan.json")
    assert (doc["pattern_bytes"], doc["pattern_lines"], doc["pattern_blank_lines"]) == (len(secret) + 1, 1, 0)
    assert (doc["scanned_bytes"], doc["grep_rc"], doc["matches"]) == (15, 1, 0)
    assert doc["scanned_sha256"] == __import__("hashlib").sha256(b"a line\nanother\n").hexdigest()
    assert secret not in (tmp_path / "scan.json").read_text(encoding="utf-8")
    assert secret not in done.stdout + done.stderr


def test_record_scan_has_no_count_for_a_scan_that_errored(tmp_path):
    pattern, scanned = tmp_path / "pattern", tmp_path / "scanned"
    pattern.write_text("t\n", encoding="utf-8")
    scanned.write_text("x\n", encoding="utf-8")
    assert record("scan", str(tmp_path / "s.json"), "n", str(pattern), str(scanned), "2", "").returncode == 0
    assert parsed(tmp_path / "s.json")["matches"] is None


# --- sandbox.sh's scan, as the step runs it, read back through the summary ---


SANDBOX = (RUN / "sandbox.sh").read_text(encoding="utf-8")
SCAN_FUNCTION = re.search(r"^scan\(\) \{.*?^\}\n", SANDBOX, re.M | re.S).group(0)
SCAN_FILES = {
    "gate-requests": "gate-requests.jsonl",
    "gate-stdout": "gate-stdout.txt",
    "gate-stderr": "gate-stderr.txt",
    "agent-stdout": "agent.json",
    "agent-stderr": "agent.err",
    "egress-log": "egress.jsonl",
}
TOKEN = "gate-tok-" + "k3Lm" * 10


def scan_all(run_dir, token_text, contents):
    """Run the step's own scan() over each evidence copy in sandbox/, as the step does, with the token file holding
    `token_text`; a file named in `contents` is rewritten first, the others keep the passing fixture's. Return the
    scan's output."""
    (run_dir / "d" / "scan").mkdir(parents=True, exist_ok=True)
    (run_dir / "d" / "scan" / "token").write_text(token_text, encoding="utf-8")
    calls = ""
    for name, filename in SCAN_FILES.items():
        if name in contents:
            (run_dir / "sandbox" / filename).write_text(contents[name], encoding="utf-8")
        calls += f'scan {name} "{run_dir}/sandbox/{filename}"\n'
    script = (
        f'set -euo pipefail; D="{run_dir}/d"; E="{run_dir}"\n'
        f'rec() {{ "{sys.executable}" -I "{RUN}/record.py" "$@"; }}\n'
        f"{SCAN_FUNCTION}{calls}"
    )
    done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout + done.stderr


def test_the_steps_scan_of_clean_logs_passes_through_the_summary(run_dir):
    out = scan_all(run_dir, TOKEN + "\n", {})
    assert failed(evidence.summary(run_dir)) == set()
    assert TOKEN not in out
    assert parsed(run_dir / "sandbox" / "token-scan-gate-requests.json")["grep_rc"] == 1


@pytest.mark.parametrize("name", sorted(SCAN_FILES))
def test_a_token_in_any_scanned_file_fails_the_run_and_is_counted_never_printed(run_dir, name):
    """O52: the step's real grep -c -F -f over each saved log, its count read back by the summary."""
    out = scan_all(run_dir, TOKEN + "\n", {name: f"line\n{TOKEN}\nline\nxx {TOKEN} yy\n"})
    summary = evidence.summary(run_dir)
    assert "gate_token_scan" in failed(summary)  # a JSON evidence file so rewritten fails its own item as well
    assert item(summary, "gate_token_scan")["detail"]["matches"] == {name: 2}
    assert TOKEN not in out
    assert TOKEN not in json.dumps(summary)


def test_the_steps_scan_with_an_empty_token_file_fails_the_run(run_dir):
    """grep -f an empty file matches nothing, so every count would be 0: the summary must not take that for a pass."""
    scan_all(run_dir, "", {"gate-stdout": f"{TOKEN}\n"})
    assert failed(evidence.summary(run_dir)) == {"gate_token_scan"}


def test_the_steps_scan_with_a_blank_line_in_the_token_file_fails_the_run(run_dir):
    scan_all(run_dir, TOKEN + "\n\n", {})
    assert failed(evidence.summary(run_dir)) == {"gate_token_scan"}


def test_the_steps_scan_of_a_file_it_cannot_read_is_an_error_and_fails_the_run(run_dir):
    scan_all(run_dir, TOKEN + "\n", {})
    (run_dir / "sandbox" / "gate-stderr.txt").unlink()
    (run_dir / "sandbox" / "gate-stderr.txt").mkdir()  # a directory: grep exits 2
    calls = f'scan gate-stderr "{run_dir}/sandbox/gate-stderr.txt"\n'
    script = (
        f'set -uo pipefail; D="{run_dir}/d"; E="{run_dir}"; rec() {{ "{sys.executable}" -I "{RUN}/record.py" "$@"; }}\n'
        f"{SCAN_FUNCTION}{calls}"
    )
    subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, check=False)
    doc = parsed(run_dir / "sandbox" / "token-scan-gate-stderr.json")
    assert doc["grep_rc"] == 2
    assert failed(evidence.summary(run_dir)) == {"gate_token_scan"}


def test_the_sandbox_step_scans_exactly_the_files_the_summary_expects():
    scanned = re.findall(r"^scan (\S+) ", SANDBOX, re.M)
    assert sorted(scanned) == sorted(evidence.SCANNED)


def test_the_sandbox_step_scans_the_evidence_copies_the_summary_binds_each_scan_to():
    """O52: each scan is of the file in $E/sandbox that the summary hashes and compares with the scan's record."""
    scanned = dict(re.findall(r'^scan (\S+) "\$E/(sandbox/\S+)"$', SANDBOX, re.M))
    assert scanned == base.SCANNED_FILES == evidence.SCANNED_FILES


def test_the_sandbox_step_keeps_the_token_file_until_after_the_scans_and_checks_it_first():
    first_scan = SANDBOX.index("\nscan gate-requests ")
    last_scan = SANDBOX.index("\nscan egress-log ")
    assert SANDBOX.index('grep -c . "$D/scan/token"') < SANDBOX.index("apptainer exec --containall --cleanenv")
    assert SANDBOX.index('rm -f -- "${D:?}/gate/token"') < SANDBOX.index("apptainer exec --containall --cleanenv")
    assert last_scan < SANDBOX.index('rm -f -- "${D:?}/scan/token"')
    assert SANDBOX.index("agent=0") < first_scan
    assert 'grep -c -F -f "$D/scan/token" -- "$2"' in SANDBOX


def test_the_token_is_only_ever_written_to_its_two_0600_files_and_never_echoed():
    uses = [line.strip() for line in SANDBOX.splitlines() if "$tok" in line and not line.lstrip().startswith("#")]
    assert uses == [
        'IFS= read -r tok; [[ -n $tok ]] || { echo "sandbox: ERROR: no gate token on stdin" >&2; exit 1; }',
        'printf \'%s\\n\' "$tok" > "$D/gate/token"; printf \'%s\\n\' "$tok" > "$D/scan/token"; tok=\'\'',
    ]


# --- the run's scripts as text: what the README promises of every step ---


def commands(path):
    return [line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith("#")]


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_every_srun_of_the_run_passes_no_environment(script):
    for line in commands(script):
        if re.search(r"\bsrun\b", line) and "srun --jobid" in line:
            assert "--export=PATH" in line, line


def test_no_script_or_helper_of_the_run_carries_a_literal_verdict_or_a_site_value():
    """The last run printed "token_in_log": False. Nothing but evidence.py makes a verdict; no address or home path."""
    literal = re.compile(r"token_in_log|\"\w*(pass|ok|clean|verdict|refused)\w*\"\s*:\s*(True|False|true|false)")
    address = re.compile(r"\b(?!127\.0\.0\.1\b|0\.0\.0\.0\b)\d{1,3}(?:\.\d{1,3}){3}\b")
    for path in [*SCRIPTS, *RUN.glob("*.py")]:
        text = path.read_text(encoding="utf-8")
        assert not literal.search(text), path.name
        assert not address.search(text), path.name
        assert "/home/" not in text, path.name
        assert "$HOME" not in text, path.name


def test_the_summary_reads_only_names_that_a_step_writes():
    text = "\n".join(p.read_text(encoding="utf-8") for p in [*SCRIPTS, *RUN.glob("*.py")] if p.name != "evidence.py")
    for _name, _kind, slots, _parser in evidence.catalog():
        for _key, pattern in slots:
            name = pattern.rsplit("/", 1)[-1]
            for fragment in re.split(r"\*|P\d|Z", name):
                assert fragment in text, (pattern, fragment)


# --- common.sh: the logs a service wrote, as the steps record them ---


@pytest.fixture
def site(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir(mode=0o700)
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir(mode=0o700)
    env_file = tmp_path / "site.env"
    values = {
        "HPC_JOB": base.JOB,
        "B": "/b",
        "SH": "/sh",
        "L": str(logs),
        "GATE_DIR": "/g",
        "NODE_Z": "nz",
        "NODE_P1": "n1",
        "NODE_P2": "n2",
        "NODE_P3": "n3",
        "NODE_S": "ns",
        "ADDR_Z": "192.0.2.1",
        "ADDR_P1": "192.0.2.2",
        "ADDR_P2": "192.0.2.3",
        "ADDR_P3": "192.0.2.4",
    }
    env_file.write_text("".join(f"{k}='{v}'\n" for k, v in values.items()), encoding="utf-8")
    env = {"PATH": os.environ["PATH"], "SITE_ENV": str(env_file), "E": str(evidence_dir)}
    write_files(evidence_dir, {"run.json": good_files()["run.json"]})  # a run in progress, of this allocation
    return type("Site", (), {"logs": logs, "E": evidence_dir, "env": env})


def sh(site, script):
    done = subprocess.run(
        ["bash", "-c", f'source "{RUN}/common.sh"; {script}'],
        env=site.env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def sh_status(site, script, **extra):
    """A step's shell, its exit status and output kept: for the refusals."""
    return subprocess.run(
        ["bash", "-c", f'source "{RUN}/common.sh"; {script}'],
        env={**site.env, **extra},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_the_runs_first_step_refuses_an_evidence_directory_that_is_not_empty(site):
    """O52: a run starts in a fresh directory, so no file of an earlier run can stand for a step of this one."""
    done = sh_status(site, "true", STEP_BEGINS_RUN="1")
    assert done.returncode != 0
    assert "not empty" in done.stderr


def test_the_runs_first_step_writes_its_identity_into_an_empty_directory(site):
    (site.E / "run.json").unlink()
    done = sh_status(site, "true", STEP_BEGINS_RUN="1")
    assert done.returncode == 0, done.stderr
    doc = parsed(site.E / "run.json")
    assert re.fullmatch(r"[0-9a-f]{32}", doc["run_id"])
    assert (doc["job"], bool(doc["started"])) == (base.JOB, True)
    assert mode(site.E / "run.json") == 0o600


@pytest.mark.parametrize("state", ["no run.json", "another allocation's"])
def test_a_later_step_refuses_an_evidence_directory_that_is_not_this_allocations_run(site, state):
    """O52: a step adds to its own run's directory only; an earlier allocation's evidence is never added to."""
    if state == "no run.json":
        (site.E / "run.json").unlink()
    else:
        write_files(site.E, {"run.json": dict(good_files()["run.json"], job="999")})
    done = sh_status(site, "true")
    assert done.returncode != 0
    assert "run" in done.stderr


def test_a_steps_evidence_directory_is_made_once_per_run(site):
    """O52: a step that ran already cannot run again into the same run, where its earlier files would mix in."""
    done = sh_status(site, "ev roundtrip; ev roundtrip")
    assert done.returncode != 0
    assert "already" in done.stderr


def test_a_repeated_cell_keeps_the_earlier_attempts_evidence_aside_and_starts_afresh(site):
    """O52: the summary reads only the last attempt's files; an earlier attempt's stay, renamed, as raw evidence."""
    (site.E / "sandbox").mkdir(mode=0o700)
    (site.E / "sandbox" / "agent.json").write_text("{}", encoding="utf-8")
    sh(site, "fresh_step_dir sandbox; fresh_step_dir sandbox")
    assert (site.E / "sandbox.attempt-1" / "agent.json").is_file()
    assert (site.E / "sandbox.attempt-2").is_dir()
    assert list((site.E / "sandbox").iterdir()) == []


def guard_job_log():
    """What a service's job log holds from the guard's two emitters, through lib.sh's own copy of the lines."""
    watch = load(BUNDLE / "hpcop-cb-init.py", "hpcop_cb_init")
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        watch.settled(200, 401)
    run_log = buffer.getvalue()
    for when in base.WHENS:
        script = f'source "{BUNDLE}/lib.sh"; cb_evidence "{when}" "GET /settings/web (administrator)" 200 200; '
        script += f'cb_evidence "{when}" "GET /settings/web (anonymous)" 401 401'
        run_log += subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, check=True).stdout
    return run_log


def service_log(tmp_path, role):
    """A service's batch log as the bundle prints it: the guard's lines are wait_ready's own output."""
    zone = role == "Z"
    run_log = guard_job_log() + base.GUARD_PASSED + "\nCongratulations! New deployment\n"
    status, ready = base.wait_ready_output(tmp_path, run_log)
    assert status == 0, ready
    return (
        "hpcop: work directory /x/hpcop-%s.AbCdEf\n" % ("oz" if zone else "op")
        + "ok: configured %s 'name' on host.example with 2 peer(s); epmd on 127.0.0.1:1\n" % ("oz" if zone else "op")
        + "ok: no shipped Couchbase cookie or memcached password under /volumes\n"
        + ready
        + "ok: every listener of this service off loopback is public (8443), every loopback one owner-checked\n"
        + "ok: live Couchbase cookies and memcached password are not the shipped ones\n"
        + "ok: all default checks passed (live)\n"
        + "hpcop: OK %s 'name' at https://host.example:8443\n" % ("zone" if zone else "provider")
    )


def test_a_service_log_as_the_bundle_prints_it_passes_the_up_and_guard_items(site):
    own = lambda rel: rel.startswith("Z/") and ("up.json" in rel or "cb-guard" in rel)  # noqa: E731
    write_files(site.E, {rel: v for rel, v in good_files().items() if not own(rel)})
    (site.logs / "hpcop-zone.log").write_text(service_log(site.E.parent, "Z"), encoding="utf-8")
    sh(site, "record_up Z")
    summary = evidence.summary(site.E)
    shown = [
        (i["name"], i["error"], i["detail"]) for i in summary["items"] if i["name"] in ("Z_up", "Z_couchbase_guard")
    ]
    assert {"Z_up", "Z_couchbase_guard"}.isdisjoint(failed(summary)), shown
    assert len(list((site.E / "Z").glob("cb-guard-*.json"))) == 6


def test_a_service_log_with_a_fail_line_is_counted_and_fails_its_up_item(site):
    log = site.logs / "hpcop-prov2.log"
    log.write_text(service_log(site.E.parent, base.PROVIDERS[1]) + "FAIL: some check\n", encoding="utf-8")
    sh(site, "record_up P2")
    assert parsed(site.E / base.PROVIDERS[1] / "up.json")["counts"]["fail_lines"] == 1


def test_the_teardown_and_end_counts_match_the_bundles_own_messages(site):
    teardown = (BUNDLE / "teardown.sh").read_text(encoding="utf-8")
    assert 'ok "service stopped"' in teardown
    assert 'ok "work directory $W removed"' in teardown
    counts = dict(
        c.split("=", 1) for c in sh(site, 'printf "%s\\n" "${TEARDOWN_COUNTS[@]}" "${END_COUNTS[@]}"').splitlines()
    )
    assert re.search(counts["service_stopped"], "ok: service stopped")
    assert re.search(counts["workdir_removed"], "ok: work directory /x/hpcop-op.AbCdEf removed")
    for job, word in (("zone.sbatch", "zone"), ("provider.sbatch", "provider")):
        text = (BUNDLE / job).read_text(encoding="utf-8")
        assert f"the {word} service stopped at teardown.sh's request" in text
        assert f"the {word} service exited by itself" in text
        assert re.search(
            counts["stopped_at_request"], f"hpcop: the {word} service stopped at teardown.sh's request (status 143)"
        )
        assert re.search(
            counts["exited_by_itself"], f"hpcop: the {word} service exited by itself (status 0): the job fails"
        )


def test_the_up_counts_match_the_messages_the_bundle_prints():
    texts = {p.name: p.read_text(encoding="utf-8") for p in BUNDLE.iterdir() if p.suffix in (".sh", ".sbatch")}
    assert 'ok "all default checks passed${LIVE:+ (live)}"' in texts["check-defaults.sh"]
    assert '"every listener of this service off loopback is public' in texts["check-defaults.sh"]
    assert '"live Couchbase cookies and memcached password are not the shipped ones"' in texts["check-defaults.sh"]
    assert 'ok "deployment finished; $(grep -m1 -F "$CB_GUARD_PASSED"' in (BUNDLE / "lib.sh").read_text(
        encoding="utf-8"
    )
    assert 'say "OK provider' in texts["finish.sh"]
    assert "OK zone" in texts["finish-zone.sh"]
    assert "FINISH FAILED" in texts["provider.sbatch"]
    assert "FINISH FAILED" in texts["zone.sbatch"]


# --- the Python helpers' outputs, read back through the summary ---


CONTENT = b"a body the cell reads" * 1000
CONTENT_SHA = __import__("hashlib").sha256(CONTENT).hexdigest()


def with_content(run_dir):
    """The round trip's evidence made to agree with CONTENT instead of the shared fixture's file."""
    size = len(CONTENT)
    change(
        run_dir,
        "roundtrip/write.out.json",
        lambda d: dict(d, sha256=CONTENT_SHA, sha256_read_back=CONTENT_SHA, bytes=size),
    )
    for role in base.READERS:
        change(
            run_dir,
            f"roundtrip/read-{role}.out.json",
            lambda d: dict(d, sha256_read=CONTENT_SHA, sha256_expected=CONTENT_SHA),
        )


def test_the_agents_output_on_a_healthy_federation_passes_every_gate_item(run_dir, monkeypatch):
    agent = load(RUN / "agent.py", "hpcop_run_agent")
    root, fid, sid = base.ROOT, "file-id", base.SPACE

    def call(conn, method, path, body=None, headers=None):
        gate = isinstance(conn, agent.Via)
        routes = {
            ("POST", f"{agent.API}/lookup-file-id/run-space", True): (200, json.dumps({"fileId": root}).encode()),
            ("GET", f"{agent.API}/data/{root}/children", True): (
                200,
                json.dumps({"children": [{"name": "f"}]}).encode(),
            ),
            ("GET", f"{agent.API}/data/{fid}/content", True): (200, CONTENT),
            ("PUT", f"{agent.API}/data/{fid}/content", True): (403, b"{}"),
            ("GET", f"{agent.API}/data/{root}", True): (200, b"{}"),
            ("GET", f"{agent.API}/spaces/{sid}", False): (401, b"{}"),
            ("GET", f"{agent.API}/data/{fid}/content", False): (401, b"{}"),
        }
        return routes[(method, path.split("?")[0], gate)]

    def tcp(host, port, timeout=3.0):
        return {"connect": "open", "ms": 2} if port in (8443, 6665) or host == "pypi.org" else {"connect": "refused"}

    monkeypatch.setattr(agent, "call", call)
    monkeypatch.setattr(agent, "tcp", tcp)
    out = {}
    agent.data_path(out, lambda: agent.Via("p3.example", 1, "127.0.0.1"), "run-space", root, fid, CONTENT_SHA)
    agent.direct(out, "p3.example", sid, fid)
    agent.reachability(out, ["z.example", "p1.example", "p2.example", "p3.example"])
    out.update(env_token_like=[], cell_files=["agent.py", "ca.pem", "hosts.txt"], uid=1000)
    with_content(run_dir)
    change(run_dir, "roundtrip/write.out.json", lambda d: dict(d, file_id=fid))
    write_files(run_dir, {"sandbox/agent.json": out})
    summary = evidence.summary(run_dir)
    names = {n for n in failed(summary) if n.startswith(("gate_", "direct", "reach", "cell_", "fact_"))}
    assert names == set(), [(i["name"], i["error"], i["detail"]) for i in summary["items"] if i["name"] in names]
    assert out["gate_read"]["sha256_read"] == out["gate_read"]["sha256_expected"] == CONTENT_SHA
    assert out["gate_strips_client_token"]["probe_status"] == out["gate_strips_client_token"]["control_status"] == 200


def test_the_agent_records_a_refused_listing_route_as_a_failed_listing_not_a_pass(monkeypatch, run_dir):
    agent = load(RUN / "agent.py", "hpcop_run_agent_two")
    monkeypatch.setattr(agent, "call", lambda conn, method, path, body=None, headers=None: (403, b"{}"))
    out = {}
    agent.data_path(out, lambda: agent.Via("p3.example", 1, "127.0.0.1"), "run-space", base.ROOT, "f", CONTENT_SHA)
    assert out["gate_listing"]["status"] == 403
    assert out["gate_listing"]["entries"] == 0
    assert out["gate_lookup"]["file_id"] == ""


def test_the_agent_probes_only_routes_the_gates_policy_lists():
    policy = load(HERE.parents[0] / ".." / "src" / "expcore_onedata" / "sandbox" / "gatepolicy.py", "hpcop_gatepolicy")
    agent = load(RUN / "agent.py", "hpcop_run_agent_three")
    api = agent.API
    for method, path in [
        ("POST", f"{api}/lookup-file-id/run-space"),
        ("GET", f"{api}/data/r1/children"),
        ("GET", f"{api}/data/f1/content"),
        ("GET", f"{api}/data/r1"),
    ]:
        decision = policy.decide("oneprovider", method, path, "limit=10")
        assert decision.allow, (method, path, decision.reason)
    assert not policy.decide("oneprovider", "PUT", f"{api}/data/f1/content").allow
    assert not policy.decide("oneprovider", "GET", f"{api}/spaces/s1").allow


class FakeWorker:
    def __init__(self):
        self.stored = {}

    def call(self, method, path, body=None, headers=None):
        if method == "POST" and "/children" in path:
            return 201, json.dumps({"fileId": "file-id"}).encode()
        if method == "PUT":
            self.stored["file-id"] = body
            return 204, b""
        if path.endswith("/content"):
            return 200, self.stored["file-id"]
        return 200, b"[]"


def test_the_providers_write_and_read_outputs_pass_the_round_trip_items(run_dir, monkeypatch):
    provider = load(RUN / "fed_provider.py", "hpcop_run_provider")
    monkeypatch.setattr(provider, "SIZE", 4096)
    worker = FakeWorker()
    out = io.StringIO()
    with redirect_stdout(out):
        assert provider.write(worker, base.ROOT, {"space_id": base.SPACE}) == 0
    write = json.loads(out.getvalue())
    out = io.StringIO()
    with redirect_stdout(out):
        assert provider.read(worker, "file-id", write["sha256"], {"space_id": base.SPACE}) == 0
    read = json.loads(out.getvalue())
    write_files(run_dir, {"roundtrip/write.out.json": write})
    for role in base.READERS:
        write_files(run_dir, {f"roundtrip/read-{role}.out.json": read})
    names = failed(evidence.summary(run_dir))
    assert not names & {"write_P1", "read_P2", "read_P3", "support_P1"}, names
    assert read["sha256_read"] == read["sha256_expected"] == write["sha256"] == write["sha256_read_back"]


def test_a_provider_read_of_other_bytes_prints_the_two_hashes_and_exits_1(monkeypatch):
    provider = load(RUN / "fed_provider.py", "hpcop_run_provider_two")
    monkeypatch.setattr(provider, "SIZE", 8)
    worker = FakeWorker()
    worker.stored["file-id"] = b"12345678"
    out = io.StringIO()
    with redirect_stdout(out):
        assert provider.read(worker, "file-id", "0" * 64, {"space_id": "s"}) == 1
    result = json.loads(out.getvalue())
    assert result["sha256_read"] != result["sha256_expected"]


def test_the_zones_online_output_passes_the_online_item(run_dir):
    zone_module = load(RUN / "fed_zone.py", "hpcop_run_zone")

    class FakeZone:
        def json(self, method, path, body=None):
            return ({"providers": ["a", "b", "c"]} if path == "/providers" else {"name": "p", "online": True}), None

    out = io.StringIO()
    with redirect_stdout(out):
        assert zone_module.online(FakeZone(), 3, 0) == 0
    write_files(run_dir, {"providers/online.out.json": json.loads(out.getvalue())})
    assert "providers_online" not in failed(evidence.summary(run_dir))


SECRET_BODY = '{"error": {"id": "forbidden", "description": "user secret-value at a site path"}}'


def test_a_zone_call_that_failed_names_its_status_and_error_id_never_its_body(capsys):
    """O52: an error answer may echo an account, a path or a credential; its message keeps the status, the body's
    size and hash, and the error's id."""
    zone_module = load(RUN / "fed_zone.py", "hpcop_run_zone_error")
    zone = object.__new__(zone_module.Zone)
    zone.call = lambda method, path, body=None: (403, SECRET_BODY, None)
    with pytest.raises(SystemExit):
        zone.json("GET", "/user")
    err = capsys.readouterr().err
    assert "HTTP 403" in err
    assert "forbidden" in err
    assert "secret-value" not in err


@pytest.mark.parametrize("failing", ["create", "write"])
def test_a_provider_call_that_failed_names_its_status_never_its_body(capsys, failing):
    """O52: as for the zone's calls: the status, the body's size and hash, the error's id."""
    provider = load(RUN / "fed_provider.py", f"hpcop_run_provider_error_{failing}")

    class Refusing(FakeWorker):
        def call(self, method, path, body=None, headers=None):
            if (method == "POST") == (failing == "create"):
                return 400, SECRET_BODY.encode()
            return super().call(method, path, body, headers)

    with pytest.raises(SystemExit):
        provider.write(Refusing(), "root-id", {"space_id": "s"})
    captured = capsys.readouterr()
    assert "HTTP 400" in captured.err
    assert "secret-value" not in captured.err + captured.out


def onboard_failure(monkeypatch, name, err):
    """The support step's output when onboard.py exits 1 with `err` on its stderr."""
    provider = load(RUN / "fed_provider.py", name)
    done = subprocess.CompletedProcess([], 1, "support HTTP 400; users mapped 0/1\n", err)
    monkeypatch.setattr(provider.subprocess, "run", lambda *a, **k: done)
    with pytest.raises(SystemExit):
        provider.support("/w", "/b", {"space_id": "s", "token": "t"}, {"space_id": "s"})
    return provider


ONBOARD_ERRORS = {
    "one line": f"onboard: FAILED support: HTTP 400 {SECRET_BODY}\n",
    "a body over lines": 'onboard: FAILED support: HTTP 400 {\n  "description": "secret-value"\n}\n',
    "a body line that looks like onboard's": "onboard: FAILED support: HTTP 400 failure\nonboard: secret-value\n",
}


@pytest.mark.parametrize("shape", sorted(ONBOARD_ERRORS))
def test_onboardings_stderr_is_kept_as_data_never_as_text(capsys, monkeypatch, shape):
    """O52: onboard.py prints a failed answer's first 200 characters, which may span lines, and no prefix proves a line
    is its own; the support step keeps its stderr as the statuses it names, its lines, size and hash."""
    err = ONBOARD_ERRORS[shape]
    onboard_failure(monkeypatch, f"hpcop_run_provider_onboard_{len(shape)}", err)
    out = json.loads(capsys.readouterr().out)
    assert "secret-value" not in json.dumps(out)
    assert out["onboard_err"] == {
        "lines": len(err.splitlines()),
        "bytes": len(err.encode()),
        "sha256": __import__("hashlib").sha256(err.encode()).hexdigest()[:16],
        "http_statuses": [400],
    }
    assert out["onboard_out"] == "support HTTP 400; users mapped 0/1"


def test_the_helpers_note_each_http_call_as_a_status_and_a_body_hash_never_a_header_or_body(tmp_path):
    for name in ("fed_zone", "fed_provider"):
        module = load(RUN / f"{name}.py", f"hpcop_note_{name}")
        module.EVIDENCE_FILE = str(tmp_path / f"{name}.jsonl")
        module.note("GET", "/user/tokens/temporary", 200, b'{"token": "secret-value"}')
        text = (tmp_path / f"{name}.jsonl").read_text(encoding="utf-8")
        line = json.loads(text)
        assert set(line) == {"method", "path", "status", "body_bytes", "body_sha256"}
        assert "secret-value" not in text
        assert mode(tmp_path / f"{name}.jsonl") == 0o600


def test_a_nodes_check_prints_the_counts_the_summary_reads(tmp_path):
    env = {**os.environ, "TMPDIR": str(tmp_path)}
    done = subprocess.run(
        [sys.executable, "-I", str(RUN / "nodecheck.py"), base.PROVIDERS[0]],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    doc = json.loads(done.stdout)
    assert doc["role"] == base.PROVIDERS[0]
    assert doc["jobtmp"] == 0
    for key in ("leftover_tmp", "tmp_top", "jobtmp", "listeners", "procs_other", "ps_rc", "ss_rc"):
        assert isinstance(doc[key], int)


@pytest.mark.parametrize("command", ["ps", "ss"])
def test_a_nodes_check_whose_listing_command_fails_records_it_and_fails_the_summary(run_dir, tmp_path, command):
    """O52: a command that failed prints nothing, which would count as no process or no listener."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / command
    fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    done = subprocess.run(
        [sys.executable, "-I", str(RUN / "nodecheck.py"), "S"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    rc = json.loads(done.stdout)[f"{command}_rc"]
    assert rc == 1
    # Only the exit status is taken from this host's check: its other counts depend on what runs here.
    change(run_dir, "teardown/nodecheck-S.json", lambda d: dict(d, **{f"{command}_rc": rc}))
    assert failed(evidence.summary(run_dir)) == {"nodes_clean"}


def test_a_nodes_check_without_a_job_tmpdir_reports_none_which_fails_the_summary(run_dir):
    env = {k: v for k, v in os.environ.items() if k != "TMPDIR"}
    done = subprocess.run(
        [sys.executable, "-I", str(RUN / "nodecheck.py"), "S"],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        check=False,
    )
    assert json.loads(done.stdout)["jobtmp"] is None
    write_files(run_dir, {"teardown/nodecheck-S.json": json.loads(done.stdout)})
    assert failed(evidence.summary(run_dir)) == {"nodes_clean"}


# --- the egress canary and the binds of the cell ---


def test_the_canary_and_the_agent_run_under_one_array_of_binds_and_every_bind_is_in_it():
    assert SANDBOX.count('"${BINDS[@]}"') == 2
    definition = re.search(r"^BINDS=\((.*)\)$", SANDBOX, re.M).group(1)
    for target in ("/cell:ro", "/hpcop-egress:ro", "/hpcop-shim:ro", "/etc/ld.so.preload:ro"):
        assert f":{target}" in definition
    cell_execs = [line for line in SANDBOX.splitlines() if "apptainer exec --containall --cleanenv" in line]
    assert len(cell_execs) == 2
    for line in cell_execs:
        assert "-B " not in line


def test_the_canary_runs_after_the_collector_and_the_gate_and_before_the_agent():
    canary = SANDBOX.index("canary=0")
    assert SANDBOX.index("egress collector ready") < canary
    assert SANDBOX.index("gate ready on") < canary
    assert canary < SANDBOX.index("agent=0")
    assert "python3 -I -c 'import json, socket, sys" in SANDBOX
    assert 'rec canary "$E/sandbox/canary.json" 127.0.0.1 "$port" "$canary"' in SANDBOX


def test_nothing_runs_in_the_cell_with_the_shim_before_the_canary():
    first_bound_exec = SANDBOX.index('"${BINDS[@]}"')
    assert SANDBOX.index("canary=0") < first_bound_exec
    assert "python3 -I -c" in SANDBOX[first_bound_exec : first_bound_exec + 200]
    assert '"$D/preload:/etc/ld.so.preload' not in SANDBOX[: SANDBOX.index("BINDS=(")]


def test_the_collector_gets_a_fresh_socket_directory_made_just_before_it_starts():
    made = SANDBOX.index('EGRESS=$(mktemp -d "$D/egress.XXXXXX"); chmod 700 -- "$EGRESS"')
    started = SANDBOX.index('python3.11 -I "$BUNDLE/egress-collector.py" "$EGRESS" "$D/egress.jsonl"')
    assert made < started
    between = SANDBOX[made:started].splitlines()
    assert len(between) <= 2
    assert '"$D/egress"' not in SANDBOX


def test_record_keeps_the_canary_endpoint_the_exit_status_and_the_cells_result(tmp_path):
    said = tmp_path / "canary.out"
    said.write_text('{"connect": "open"}\n', encoding="utf-8")
    assert record("canary", str(tmp_path / "c.json"), "127.0.0.1", "41234", "0", str(said)).returncode == 0
    assert parsed(tmp_path / "c.json") == {"address": "127.0.0.1", "port": 41234, "rc": 0, "result": "open"}


@pytest.mark.parametrize(
    "output", ["", "not json\n", '["open"]\n', '{"connect": 5}\n', '{"connect": "open"}\n{"x": 1}\n']
)
def test_record_marks_a_canary_output_that_is_not_one_connect_result_as_unparsable(tmp_path, output):
    said = tmp_path / "canary.out"
    said.write_text(output, encoding="utf-8")
    record("canary", str(tmp_path / "c.json"), "127.0.0.1", "41234", "0", str(said))
    assert parsed(tmp_path / "c.json")["result"] == "unparsable"


def test_a_canary_output_that_was_not_a_connect_result_fails_the_egress_item(run_dir, tmp_path):
    said = tmp_path / "canary.out"
    said.write_text("garbled\n", encoding="utf-8")
    record("canary", str(run_dir / "sandbox" / "canary.json"), "127.0.0.1", "41234", "0", str(said))
    assert failed(evidence.summary(run_dir)) == {"egress"}
