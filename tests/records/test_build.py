"""Building the record of an attempt through the loop: sections, per-turn settings, partial records, stable text."""

import asyncio
import json

import pytest
from loop_support import ClockSleep, FakeClock, FakeProvider, ScriptedBackend, malformed, never, turn
from record_support import CODE, IDENTITY, hand_made, make_config, record_run, sha
from replay_scenarios import BadRequest, Hangs

from expcore.errors import ContextOverflow, InfraError, RequestAbandoned
from expcore.models.adapters import StubAdapter
from expcore.plugin import ToolOutput
from expcore.prompt.templates import PromptTemplate
from expcore.records.build import PromptText, build_record, record_attempt
from expcore.records.digest import digest
from expcore.records.record import RunRecord, decode
from expcore.records.recording import RecordingBackend
from expcore.records.schema import RecordError

ECHO = {"echo": lambda args: f"echoed {args.get('x', '')}"}


def reread(record: RunRecord) -> RunRecord:
    """Write the record to text and read it back, which validates every section."""
    return RunRecord.from_json(decode(record.dumps()))


def test_a_record_holds_each_section_of_the_attempt():
    record = reread(record_run([[("echo", {"x": "a"})], "done"], ECHO))
    assert (record.run_key, record.factors, record.attempt) == (IDENTITY.run_key, dict(IDENTITY.factors), 1)
    assert record.code == CODE
    assert record.core_version == "0.1.0"
    assert record.prompt["system"]["sha256"] == sha("be brief")
    assert record.prompt["user"]["text"] == "do the task"
    assert len(record.prompt["tools_sha256"]) == 64
    assert [t["name"] for t in record.tools] == ["echo"]
    assert record.agent_config["limits"]["output_cap_chars"] == 200
    assert record.agent_config_sha256 == digest(record.agent_config)
    assert [t["index"] for t in record.transcript] == [0, 1]
    first = record.transcript[0]
    assert first["neutral"]["tool_calls"][0]["name"] == "echo"
    assert first["native"]["role"] == "assistant"
    assert [m["role"] for m in first["tool_messages"]] == ["tool"]
    assert first["usage"]["input_tokens"] is not None
    assert first["usage"]["cache_read_input_tokens"] is None
    assert record.tool_calls[0]["text"] == "echoed a"
    assert record.tool_calls[0]["status"] == "ok"
    assert [label for label, _ in record.clock][:2] == ["start", "before_model"]
    assert record.outcome["name"] == "finished"
    assert record.totals["final_text"] == "done"
    assert record.scores == {}
    assert record.instruments == {}


def test_a_prompt_rendered_from_a_template_keeps_the_template_hash_and_the_slots():
    template = PromptTemplate("Task: {TASK}")
    rendered = template.render({"TASK": "count"})
    record = asyncio.run(
        record_attempt(
            StubAdapter(["done"]),
            [FakeProvider(ECHO)],
            "sys",
            rendered,
            make_config(),
            IDENTITY,
            code=CODE,
            clock=FakeClock(),
            sleep=never,
        )
    )
    assert record.prompt["user"] == {
        "text": "Task: count",
        "sha256": rendered.rendered_sha256,
        "template_sha256": template.sha256,
        "slots": {"TASK": "count"},
    }
    assert record.prompt["system"]["template_sha256"] is None


def test_a_malformed_call_keeps_the_raw_arguments_in_the_turn_and_in_the_log():
    backend = ScriptedBackend([turn(malformed("m1", "echo", raw='{"x":')), "done"])
    record = reread(record_run([], ECHO, backend=backend))
    neutral = record.transcript[0]["neutral"]["tool_calls"][0]
    assert neutral["raw_arguments"] == '{"x":'
    assert neutral["parse_error"]
    logged = record.tool_calls[0]
    assert (logged["status"], logged["raw_arguments"]) == ("malformed", '{"x":')
    assert logged["arguments"] == {"_raw": '{"x":'}
    assert logged["text"].startswith("MalformedArguments:")
    assert record.totals["malformed_calls"] == 1
    assert record.totals["malformed_events"][0]["raw"] == '{"x":'


def test_a_cut_result_keeps_the_raw_text_and_the_flag():
    record = reread(record_run([[("echo", {"x": "z" * 600})], "done"], ECHO))
    logged = record.tool_calls[0]
    assert logged["truncated"] is True
    assert len(logged["text"]) <= 200 < len(logged["raw_text"])


class ChangingSettings(ScriptedBackend):
    """A backend whose request settings change after its second reply, as a mid-run override would."""

    def info(self):
        sent = {"model": "m", "temperature": 0.0 if self._i < 2 else 0.7}
        return {**super().info(), "settings_sent": sent}


def test_each_turn_carries_the_digest_of_the_settings_it_sent_and_each_distinct_set_is_kept_once():
    """A setting that changes mid-run stays visible: two sets, three turns naming them by digest."""
    backend = ChangingSettings([[("echo", {"x": "1"})], [("echo", {"x": "2"})], "done"])
    record = reread(record_run([], ECHO, backend=backend))
    digests = [t["settings_sha256"] for t in record.transcript]
    assert len(digests) == 3
    assert len(set(digests)) == 2
    sets = record.route["settings"]
    assert set(sets) == set(digests)
    assert {sets[d]["temperature"] for d in sets} == {0.0, 0.7}
    assert all(digest(sets[d]) == d for d in sets)
    assert record.route["settings_sent_sha256"] == digests[-1]
    assert "settings_sent" not in record.route


def test_R06_the_request_digest_covers_the_conversation_the_request_carried():
    """R06: turn k pins the conversation before it, so the first turn pins the prompts and the second adds turn one."""
    record = record_run([[("echo", {"x": "a"})], "done"], ECHO)
    opening = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "do the task"}]
    assert record.transcript[0]["request_sha256"] == digest(opening)
    second = [*opening, record.transcript[0]["native"], *record.transcript[0]["tool_messages"]]
    assert record.transcript[1]["request_sha256"] == digest(second)


def test_R09_an_endpoint_failure_at_turn_three_leaves_turns_one_and_two_with_their_usage(tmp_path):
    """R09: the partial record keeps the turns so far, the usage seen and the failed request, and carries no score."""
    script = [[("echo", {"x": "1"})], [("echo", {"x": "2"})], InfraError("endpoint", "HTTP 503 after 8 retries")]
    record = record_run(script, ECHO)
    assert record.set_aside is True
    assert (record.outcome["name"], record.outcome["infra_kind"]) == ("infrastructure", "endpoint")
    assert record.outcome["detail"] == "HTTP 503 after 8 retries"
    assert [t["index"] for t in record.transcript] == [0, 1]
    assert record.totals["tokens_used"] == sum(
        t["usage"]["input_tokens"] + t["usage"]["output_tokens"] for t in record.transcript
    )
    assert record.failed_request is not None
    assert (record.failed_request["kind"], record.failed_request["infra_kind"]) == ("infra", "endpoint")
    assert record.scores == {}
    path = record.write(tmp_path)
    assert json.loads(path.read_text(encoding="utf-8"))["outcome"]["name"] == "infrastructure"


@pytest.mark.parametrize(
    ("raised", "kind"),
    [
        (InfraError("endpoint", "down"), "infra"),
        (ContextOverflow(400, "context_length"), "context_overflow"),
        (RequestAbandoned("read timeout"), "abandoned"),
        (ValueError("text that differs between interpreters"), "error"),
    ],
)
def test_a_failed_request_is_recorded_by_kind_with_stable_fields(raised, kind):
    record = reread(record_run([raised], ECHO))
    assert record.failed_request is not None
    assert record.failed_request["kind"] == kind
    assert "interpreters" not in record.dumps()


def test_R07_exception_text_is_a_class_name_plus_a_stable_message():
    """R07: a library's own message is not stored for an error, a tool crash or a failed call; the class name is."""
    message = "Expecting value: line 1 column 1 (char 0) -- text of one interpreter"
    record = record_run([BadRequest(message)], ECHO)
    assert record.outcome["name"] == "error"
    assert (record.outcome["error_type"], record.outcome["detail"]) == ("BadRequest", "BadRequest")
    assert "interpreter" not in record.dumps()
    backend_crash = record_run([ValueError(message)], ECHO)
    assert backend_crash.outcome["name"] == "harness_defect"
    assert (backend_crash.outcome["detail"], backend_crash.failed_request["error_type"]) == ("ValueError", "ValueError")
    assert "interpreter" not in backend_crash.dumps()

    def crash(_args):
        raise KeyError("vary-by-version")

    crashed = record_run([[("echo", {})], "done"], {"echo": crash})
    assert crashed.outcome["name"] == "harness_defect"
    assert (crashed.outcome["detail"], crashed.tool_calls[0]["detail"]) == ("KeyError", "KeyError")
    assert crashed.tool_calls[0]["status"] == "failed"
    assert "vary-by-version" not in crashed.dumps()


def test_R07_the_cores_own_fixed_fields_are_kept():
    """R07: a context overflow keeps its status and pattern, and an infrastructure failure from a tool its kind."""
    overflow = record_run([ContextOverflow(413, "too_long")], ECHO)
    assert overflow.outcome["detail"] == "context overflow (HTTP 413, too_long)"
    assert (overflow.failed_request["status"], overflow.failed_request["pattern"]) == (413, "too_long")

    def down(_args):
        raise InfraError("knowledge", "tool server exited")

    failed = record_run([[("echo", {})], "done"], {"echo": down})
    assert failed.tool_calls[0]["detail"] == "infrastructure: knowledge"
    assert (failed.outcome["name"], failed.outcome["infra_kind"]) == ("infrastructure", "knowledge")


def test_an_attempt_cut_at_its_time_cap_records_the_request_in_flight_as_cut():
    clock = FakeClock()
    record = record_run([], ECHO, backend=Hangs([], clock, 400), clock=clock, sleep=ClockSleep(clock))
    assert record.outcome["name"] == "time_cap"
    assert record.failed_request is not None
    assert record.failed_request["kind"] == "cut"
    assert record.totals["usage_incomplete"] is True
    assert "watch" in [label for label, _ in record.clock]


def test_the_record_refuses_a_backend_whose_history_is_not_the_recorded_turns():
    class Stray(StubAdapter):
        async def step(self):
            reply = await super().step()
            self._messages.append({"role": "user", "content": "unexpected"})
            return reply

    with pytest.raises(RecordError, match="the backend's conversation is not"):
        record_run([], ECHO, backend=Stray(["done"]))


def test_a_recording_that_missed_a_reply_is_refused():
    from expcore.loop.agent_loop import run_agent
    from expcore.loop.toolbox import ToolBox

    config = make_config()
    recording = RecordingBackend(StubAdapter(["done"]))
    box = ToolBox([FakeProvider(ECHO)], cap_chars=config.limits.output_cap_chars)
    result = asyncio.run(run_agent(recording, box, "s", "u", config, clock=FakeClock(), sleep=never))
    recording.captured.clear()
    with pytest.raises(RecordError, match="counted 1 replies"):
        build_record(result, recording, identity=IDENTITY, system="s", user="u", specs=[], code=CODE)


def test_prompt_text_accepts_a_string_a_rendered_template_or_itself():
    assert PromptText.of("x") == PromptText("x")
    same = PromptText("y", "a" * 64, {"A": "1"})
    assert PromptText.of(same) is same
    assert hand_made().prompt["user"]["slots"] is None


def test_a_tool_that_returns_an_error_result_is_kept_as_the_class_and_the_raw_message():
    provider = FakeProvider({"echo": lambda a: ToolOutput("no such file", is_error=True, error_class="NotFound")})
    record = reread(record_run([[("echo", {})], "done"], {}, providers=[provider]))
    logged = record.tool_calls[0]
    assert (logged["status"], logged["error_class"], logged["raw_text"]) == (
        "error",
        "NotFound",
        "NotFound: no such file",
    )


def test_the_core_version_defaults_to_the_installed_one_and_the_stamp_is_kept_as_given():
    """The record names the version of the core it ran on; an explicit one wins."""
    from expcore.models.route import sdk_version

    def run(**kwargs):
        return asyncio.run(
            record_attempt(
                StubAdapter(["done"]),
                [FakeProvider(ECHO)],
                "s",
                "u",
                make_config(),
                IDENTITY,
                code=CODE,
                clock=FakeClock(),
                sleep=never,
                **kwargs,
            )
        )

    assert run().core_version == sdk_version("papers-experiments-core")
    assert run(core_version="9.9").core_version == "9.9"
    assert run().code == CODE
