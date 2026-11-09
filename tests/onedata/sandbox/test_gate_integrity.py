"""O05, O16, C01: the data gate's own refusals end a run as infrastructure of a kind no condition can declare.

A compressed answer, the token in a header and the token in a body (`credential_in_response`, a cut) are neither the
agent's doing nor a condition's manipulation: the run is set aside and counted, never scored.
"""

import asyncio
import dataclasses
import json

import pytest
from campaign_support import STUDY_TOML, write_study
from sandbox_support import make_spec, start_options

from expcore.campaign.attempts import classify
from expcore.campaign.executor import failure_result
from expcore.campaign.factors import load_factor_schema
from expcore.config.loading import ConfigError
from expcore.errors import InfraError
from expcore_onedata.sandbox import gatelog
from expcore_onedata.sandbox.codetool import CodeExecutionProvider
from expcore_onedata.sandbox.hostops import CmdResult
from expcore_onedata.sandbox.sandbox import DockerSandbox

KIND = "data_gate_integrity"
ENDED = "2026-01-01T00:00:00Z"
CUT = {  # the line the gate writes when it cuts a body that held its token
    "host": "oneprovider",
    "method": "GET",
    "path": "/api/v3/oneprovider/data/f1/content",
    "decision": "allow",
    "tag": "content",
    "status": 502,
    "upstream_status": 200,
    "bytes": 4000,
    "cut": True,
    "unread_bytes": 200,
    "cut_reason": "credential_in_response",
    "gate_refusal": "credential_in_response",
    "error_origin": "upstream",
    "client_saw": "reset",
}


class NoWaits:
    def infrastructure_wait(self, kind):
        raise AssertionError(f"running the agent's code opened a wait of kind {kind!r}")


def cell_under_a_gate_that_cuts(world) -> tuple[DockerSandbox, BaseException | None]:
    """A REST sandbox whose gate cuts the cell's read for its credential, and what the code tool raised, if anything."""
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "via-gate"), **start_options(world))
    log = sandbox.dir / "gatelog" / "requests.jsonl"

    def reads_and_the_gate_cuts(container, argv, stdin):
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(CUT) + "\n")
        return CmdResult(1, "", "ConnectionResetError: the read was reset")

    world.host.exec_handler = reads_and_the_gate_cuts
    provider = CodeExecutionProvider(sandbox)
    try:
        asyncio.run(provider.execute("python", {"code": "read()"}, timeout_s=30, ctx=NoWaits()))
    except Exception as raised:  # what the run's attempt ends in
        return sandbox, raised
    return sandbox, None


def test_o05_c01_a_credential_cut_under_a_condition_that_treats_sandbox_failures_is_set_aside(world):
    """O05, C01: under a condition that declares `sandbox` as its treatment, a cell whose read the gate cut for its
    credential ends the run as infrastructure of kind data_gate_integrity: set aside, never scored as treatment and
    never as the agent's failure. The same condition's sandbox failure is scored, as it declares."""
    sandbox, error = cell_under_a_gate_that_cuts(world)
    assert isinstance(error, InfraError)
    assert error.kind == KIND
    assert "credential_in_response" in error.detail
    declared = ("sandbox",)
    cut = dataclasses.replace(
        failure_result(error, ended_utc=ENDED, wall_s=1.0, treatment_kinds=declared), model_turns=3
    )
    assert classify(cut, declared) == ("infra_fail", True, False)
    died = InfraError("sandbox", "the agent's container died")
    scored = dataclasses.replace(
        failure_result(died, ended_utc=ENDED, wall_s=1.0, treatment_kinds=declared), model_turns=3
    )
    assert classify(scored, declared).treatment is True  # the control: what the condition declares is scored
    sandbox.stop()


def test_o16_the_record_says_the_gate_cut_the_conditions_read_and_why(world):
    """O16: the run's record counts the cut with its reason, apart from what the agent read, so a cut is never read as
    the agent reading less."""
    sandbox, _ = cell_under_a_gate_that_cuts(world)
    gate = sandbox.metadata()["gate"]
    assert gate["cut_transfers"] == 1
    assert gate.get("cut_reasons") == {"credential_in_response": 1}
    assert gate.get("gate_refusals") == {"credential_in_response": 1}
    sandbox.stop()
    assert gatelog.summarize([json.dumps(CUT)]).get("gate_refusals") == {"credential_in_response": 1}


def test_o05_declaring_data_gate_integrity_as_treatment_is_refused_at_load(tmp_path):
    """O05, C01: a run the gate refused says nothing about the manipulation, so no condition can declare the kind."""
    import expcore_onedata.sandbox.kinds  # noqa: F401  the platform registers its kinds when it is imported

    declared = STUDY_TOML.replace(
        '[conditions.api-none]\ndata_path = "api"\ntools = "none"',
        f'[conditions.api-none]\ndata_path = "api"\ntools = "none"\ninfrastructure_is_treatment = ["{KIND}"]',
    )
    assert declared != STUDY_TOML
    with pytest.raises(ConfigError) as caught:
        load_factor_schema(write_study(tmp_path, declared))
    assert f"names infrastructure kind '{KIND}', which can never be a treatment" in str(caught.value)


def test_o16_a_gate_log_that_cannot_be_read_ends_the_run_as_data_gate_integrity(world):
    """O16: an unreadable gate log says nothing of the gate's refusals, which is unknown, never none: the cell ends
    the run as infrastructure of kind data_gate_integrity, with the reason gate_log_unreadable."""
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "via-gate"), **start_options(world))
    log = sandbox.dir / "gatelog" / "requests.jsonl"

    def the_log_goes_unreadable(container, argv, stdin):
        log.unlink(missing_ok=True)
        log.mkdir()  # reading it now raises IsADirectoryError, an OSError
        return CmdResult(0, "read", "")

    world.host.exec_handler = the_log_goes_unreadable
    provider = CodeExecutionProvider(sandbox)
    with pytest.raises(InfraError) as caught:
        asyncio.run(provider.execute("python", {"code": "read()"}, timeout_s=30, ctx=NoWaits()))
    assert caught.value.kind == KIND
    assert "gate_log_unreadable" in caught.value.detail
    log.rmdir()
    sandbox.stop()


def test_o16_a_gate_log_that_is_not_utf8_is_unreadable_and_ends_the_run_as_data_gate_integrity(world):
    """O16: a gate log holding bytes that are not UTF-8 cannot be read for its refusals, which is unknown, never none:
    gate_log_unreadable, and the cell ends the run as data_gate_integrity."""
    sandbox = DockerSandbox.start(world.host, world.s, make_spec(world, "via-gate"), **start_options(world))
    log = sandbox.dir / "gatelog" / "requests.jsonl"

    def the_log_goes_bad(container, argv, stdin):
        with open(log, "ab") as f:
            f.write(b'{"status": 200}\n\xff\xfe not utf-8\n')
        return CmdResult(0, "read", "")

    world.host.exec_handler = the_log_goes_bad
    provider = CodeExecutionProvider(sandbox)
    with pytest.raises(InfraError) as caught:
        asyncio.run(provider.execute("python", {"code": "read()"}, timeout_s=30, ctx=NoWaits()))
    assert caught.value.kind == KIND
    assert "gate_log_unreadable" in caught.value.detail
    sandbox.stop()
