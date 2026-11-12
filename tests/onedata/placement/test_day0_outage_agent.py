"""O28: the outage check's mount cell, run for real on this host's files, and how a tool's call is filled and read.

The mount cell is the code the check runs inside the agent's container; here it runs in a child of this interpreter on
a temporary directory, so its outcomes are real: a whole read with its hash, an error with its errno by name and
number, a read that blocks (a FIFO with no writer) killed at its deadline, and a listing."""

import hashlib
import json
import os
import subprocess
import sys

from expcore_onedata.day0 import outage_mount
from expcore_onedata.day0.outage_tools import Scrubber, _json, arguments, kind_of, transfer_id
from expcore_onedata.sandbox.api import ExecResult


def run_cell(ops, deadline_s=1.0):
    code = outage_mount.cell(ops, deadline_s)
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False)
    return outage_mount.results(ExecResult(done.stdout, done.stderr, done.returncode, 0.0))


def test_o28_the_mount_cell_reads_whole_files_names_an_errno_and_kills_a_read_that_blocks(tmp_path):
    """O28: the cell reads a file whole (its bytes and hash), names a failed read's errno by name and number with its
    exception class, kills a read still blocked at its deadline (recording that it blocked), and lists a directory."""
    data = b"x" * 5000
    (tmp_path / "whole.bin").write_bytes(data)
    os.mkfifo(tmp_path / "stuck")
    rows = run_cell(
        [
            {"key": "whole", "op": "read", "path": str(tmp_path / "whole.bin")},
            {"key": "gone", "op": "read", "path": str(tmp_path / "missing.bin")},
            {"key": "stuck", "op": "read", "path": str(tmp_path / "stuck")},
            {"key": "listing", "op": "list", "path": str(tmp_path)},
        ]
    )
    whole = outage_mount.read_facts(rows["whole"], len(data), hashlib.sha256(data).hexdigest(), 1.0)
    assert (whole["outcome"], whole["bytes"], whole["blocked"]) == ("served", 5000, False)
    gone = outage_mount.read_facts(rows["gone"], 1, "0" * 64, 1.0)
    assert (gone["outcome"], gone["errno"], gone["errno_number"], gone["error_kind"]) == (
        "refused",
        "ENOENT",
        2,
        "FileNotFoundError",
    )
    stuck = outage_mount.read_facts(rows["stuck"], 1, "0" * 64, 1.0)
    assert (stuck["outcome"], stuck["blocked"], stuck["killed"]) == ("blocked", True, True)
    assert stuck["seconds"] >= 1.0
    listing = outage_mount.listing_facts(rows["listing"], ["whole.bin", "stuck", "other.bin"], 1.0)
    assert (listing["outcome"], listing["missing"]) == ("partial", 1)


def test_o28_a_mount_timeout_is_no_answer_or_blocked_by_its_seconds_never_a_refusal():
    """O28: a mount's ETIMEDOUT, which Python reports as TimeoutError, is a call that got no answer: `no answer` when
    it came early, `blocked` when it took the bound's blocked fraction, `partial` when bytes came first and it did not
    block, as a REST timeout is (`outage_cells.failure_facts`); its errno is kept. Any other errno is a refusal."""
    timeout = {"key": "a", "error": "ETIMEDOUT", "errno": 110, "kind": "TimeoutError", "blocked": False}
    early = outage_mount.read_facts({**timeout, "seconds": 1.0, "bytes": 0}, 10, "0" * 64, 20.0)
    assert (early["outcome"], early["errno"], early["errno_number"], early["error_kind"]) == (
        "no answer",
        "ETIMEDOUT",
        110,
        "TimeoutError",
    )
    late = outage_mount.read_facts({**timeout, "seconds": 19.0, "bytes": 0}, 10, "0" * 64, 20.0)
    assert (late["outcome"], late["blocked"]) == ("blocked", False)  # it waited its bound; the child was not killed
    cut = outage_mount.read_facts({**timeout, "seconds": 2.0, "bytes": 4}, 10, "0" * 64, 20.0)
    assert cut["outcome"] == "partial"
    listed = outage_mount.listing_facts({**timeout, "seconds": 19.0}, ["x"], 20.0)
    assert (listed["outcome"], listed["errno"]) == ("blocked", "ETIMEDOUT")
    assert outage_mount.listing_facts({**timeout, "seconds": 1.0}, ["x"], 20.0)["outcome"] == "no answer"
    refused = {"key": "a", "error": "EIO", "errno": 5, "kind": "OSError", "seconds": 19.0, "bytes": 0}
    assert outage_mount.read_facts(refused, 10, "0" * 64, 20.0)["outcome"] == "refused"
    assert outage_mount.listing_facts(refused, ["x"], 20.0)["outcome"] == "refused"


def test_o28_a_cell_that_prints_no_result_measured_nothing():
    """O28: a cell whose output is not its result (it was killed, or the sandbox failed it) is a reason, not facts."""
    said = outage_mount.results(ExecResult("Traceback ...\n", "", 1, 0.0, timed_out=True))
    assert isinstance(said, str)
    assert "timed out: True" in said
    assert outage_mount.read_facts(None, 1, "0" * 64, 1.0) is None
    assert outage_mount.read_facts({"key": "a", "unreadable": True}, 1, "0" * 64, 1.0) is None


def test_o28_a_tools_arguments_come_from_its_schema_and_an_unfillable_one_is_not_called():
    """O28: each argument is filled by what its name says it is; a required argument the check cannot fill, or a
    kind the tool needs that no argument takes, leaves the call unmade with the reason."""
    values = {"file": "f-1", "provider": "p-2", "space": "s-3", "transfer": "t-4"}
    schema = {"properties": {"file_id": {}, "target_provider_id": {}}, "required": ["file_id", "target_provider_id"]}
    assert arguments("schedule_file_replication", schema, values) == {"file_id": "f-1", "target_provider_id": "p-2"}
    transfer = {"properties": {"transferId": {}}, "required": []}
    assert arguments("get_transfer", transfer, values) == {"transferId": "t-4"}
    refused = arguments("get_file_distribution", {"properties": {"path": {}}, "required": ["path"]}, values)
    assert "cannot fill" in refused
    assert "no argument for the space" in arguments("list_space_providers", {"properties": {}}, values)
    assert (kind_of("fileId"), kind_of("space_id"), kind_of("limit")) == ("file", "space", None)


def test_o28_a_tools_answer_is_unwrapped_read_and_scrubbed():
    """O28: an answer wrapped in an MCP server's structured content is unwrapped; the transfer id is found under the
    key that names it; every known name in a text is replaced by its label, the longest first, and the text is cut."""
    wrapped = json.dumps({"result": json.dumps({"transfer": {"transferId": "t-9"}})})
    assert transfer_id(_json(wrapped)) == "t-9"
    assert _json("not json") is None
    scrub = Scrubber({"https://alpha.test": "providers[0]", "alpha.test": "providers[0]", "pr-1": "providers[0]"})
    scrub.add("study-x", "spaces[0]")
    assert scrub("https://alpha.test/pr-1 in study-x") == "providers[0]/providers[0] in spaces[0]"
    assert len(scrub("y" * 1000)) == 400
