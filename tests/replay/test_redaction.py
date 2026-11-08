"""A record built from a run that held a secret pins the redacted data, and replays from it (R12, R03)."""

import asyncio
import copy
import warnings

import pytest
from loop_support import FakeClock, FakeProvider, ScriptedBackend, never
from record_support import CODE, IDENTITY, make_config, record_run

from expcore.config.loading import ConfigError
from expcore.loop.agent_loop import run_agent
from expcore.loop.capping import cap_text
from expcore.loop.toolbox import ToolBox
from expcore.prompt.templates import PromptTemplate, sha256_of
from expcore.records.build import build_record, record_attempt
from expcore.records.digest import digest
from expcore.records.record import RunRecord, decode
from expcore.records.recording import RecordingBackend
from expcore.records.redact import REDACTED, register_secret
from expcore.replay.check import check_record
from expcore.replay.divergence import ReplayDivergence
from expcore.replay.rendering import shown_follows_raw
from expcore.replay.replay import replay_record

KEY = "sk-live-0123456789abcdefghij"


@pytest.fixture
def key_in_environment(monkeypatch):
    monkeypatch.setenv("RUN_API_KEY", KEY)


def test_R12_a_secret_in_the_prompt_is_redacted_when_the_record_is_built_and_listed_by_path(key_in_environment):
    """R12: the stored prompt holds the placeholder, the record lists the field, and nothing is left to redact later."""
    with pytest.warns(RuntimeWarning, match="redacted secrets at 1 place"):
        record = record_run(["done"], {}, user=f"use the key {KEY} to log in")
    assert record.prompt["user"]["text"] == f"use the key {REDACTED} to log in"
    assert record.redacted_paths == ["prompt.user.text"]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        text = record.dumps()
    assert KEY not in text
    assert RunRecord.from_json(decode(text)).dumps() == text


def test_R12_the_digests_are_those_of_the_redacted_data_so_a_single_turn_run_with_a_secret_replays_cleanly(
    key_in_environment,
):
    """R12: prompt, conversation and configuration digests are taken after redaction; replay compares those."""
    with pytest.warns(RuntimeWarning):
        record = record_run(["done"], {}, user=f"the key is {KEY}")
    user = record.prompt["user"]
    assert user["sha256"] == sha256_of(user["text"]) != sha256_of(f"the key is {KEY}")
    opening = [{"role": "system", "content": "be brief"}, {"role": "user", "content": user["text"]}]
    assert record.transcript[0]["request_sha256"] == digest(opening)
    replayed = replay_record(record)
    assert replayed.outcome["name"] == "finished"
    assert replayed.prompt == record.prompt


def test_R12_a_slot_value_that_is_a_secret_is_redacted_and_the_rendered_text_follows_it(key_in_environment):
    """R12: a prompt rendered from a template keeps the template hash, and its slots and text hold the placeholder."""
    template = PromptTemplate("Log in with {CREDENTIAL} and report.")
    rendered = template.render({"CREDENTIAL": KEY})
    with pytest.warns(RuntimeWarning):
        record = asyncio.run(
            record_attempt(
                ScriptedBackend(["done"]),
                [FakeProvider({"echo": lambda a: "x"})],
                "sys",
                rendered,
                make_config(),
                IDENTITY,
                code=CODE,
                clock=FakeClock(),
                sleep=never,
            )
        )
    user = record.prompt["user"]
    assert user["slots"] == {"CREDENTIAL": REDACTED}
    assert user["text"] == f"Log in with {REDACTED} and report."
    assert user["template_sha256"] == template.sha256
    assert record.redacted_paths == ["prompt.user.slots.CREDENTIAL", "prompt.user.text"]
    assert replay_record(record).prompt["user"] == user


def test_R12_a_tool_result_that_held_a_secret_is_redacted_in_the_log_and_the_messages_and_replays(key_in_environment):
    """R12: the call log and the native messages agree on the redacted text, and the next request pins it."""
    with pytest.warns(RuntimeWarning):
        record = record_run([[("echo", {"x": "a"})], "done"], {"echo": lambda a: f"the key is {KEY}"})
    assert record.tool_calls[0]["text"] == f"the key is {REDACTED}"
    assert KEY not in record.dumps()
    assert replay_record(record).tool_calls == record.tool_calls
    second = [{"role": "system", "content": "be brief"}, {"role": "user", "content": "do the task"}]
    second += [record.transcript[0]["native"], *record.transcript[0]["tool_messages"]]
    assert record.transcript[1]["request_sha256"] == digest(second)


def test_R12_a_secret_registered_after_it_was_minted_is_redacted_from_a_record_built_later():
    """R12: a minted token is in no variable; once registered it is gone from the records that would hold it."""
    token = "minted-5a4b3c2d1e0f"
    register_secret(token)
    with pytest.warns(RuntimeWarning):
        record = record_run(["done"], {}, user=f"token {token}")
    assert token not in record.dumps()
    assert record.redacted_paths == ["prompt.user.text"]
    assert replay_record(record).outcome["name"] == "finished"


def test_R12_secrets_given_for_one_record_are_redacted_from_it_and_remembered_by_no_other():
    """R12: the builder takes secrets for one record, so a platform can pass what it minted for this run."""
    token = "run-only-token-8f7e6d5c"
    config = make_config()
    recording = RecordingBackend(ScriptedBackend(["done"]))
    box = ToolBox([], cap_chars=config.limits.output_cap_chars)
    result = asyncio.run(run_agent(recording, box, "s", f"use {token}", config, clock=FakeClock(), sleep=never))
    with pytest.warns(RuntimeWarning):
        record = build_record(
            result, recording, identity=IDENTITY, system="s", user=f"use {token}", specs=[], code=CODE, secrets=[token]
        )
    assert record.prompt["user"]["text"] == f"use {REDACTED}"
    plain = build_record(result, recording, identity=IDENTITY, system="s", user=f"use {token}", specs=[], code=CODE)
    assert plain.prompt["user"]["text"] == f"use {token}"


def run_with(options, *, user="do the task", environ=None, providers=None, script=None, backend=None, **config):
    """Record a run with the given record options and a clean environment for the builder."""
    return record_run(
        script if script is not None else ["done"],
        {},
        user=user,
        record_options={"environ": {} if environ is None else environ, **options},
        providers=providers,
        backend=backend,
        **config,
    )


def test_R12_a_variable_the_caller_declares_is_redacted_whatever_its_name():
    """R12: the builder takes secret_env, the names of variables that hold secrets the name pattern would not find."""
    secret = "declared-value-9a8b7c"
    environ = {"ODD_NAME": secret}
    plain = run_with({}, user=f"use {secret}", environ=environ)
    assert secret in plain.dumps(environ={})  # nothing names it a secret yet
    with pytest.warns(RuntimeWarning):
        declared = run_with({"secret_env": ["ODD_NAME"]}, user=f"use {secret}", environ=environ)
    assert declared.prompt["user"]["text"] == f"use {REDACTED}"
    assert replay_record(declared).outcome["name"] == "finished"


def test_R12_the_key_variable_of_the_model_in_use_is_added_automatically():
    """R12: the model table's key_env names the variable that holds its key, so it needs no declaring."""
    from expcore.config.models import ModelSpec, ModelTable

    secret = "model-key-5f4e3d2c1b"
    environ = {"PRIVATE_MODEL_VAR": secret}
    spec = ModelSpec(
        key="model-a",
        backend="openai_compat",
        api_id="model-a",
        status="draft",
        base_url="https://llm.example.invalid/v1",
        key_env="PRIVATE_MODEL_VAR",
    )
    backend = ScriptedBackend(["done"])
    backend.spec = spec  # the adapters hold their table entry as `spec`
    with pytest.warns(RuntimeWarning):
        by_backend = run_with({}, user=f"key {secret}", environ=environ, backend=backend)
    assert by_backend.prompt["user"]["text"] == f"key {REDACTED}"
    with pytest.warns(RuntimeWarning):
        by_table = run_with({"models": ModelTable({"model-a": spec})}, user=f"key {secret}", environ=environ)
    assert by_table.prompt["user"]["text"] == f"key {REDACTED}"


def test_R12_a_short_secret_in_the_environment_refuses_the_builder_before_the_run_starts():
    """R12: a ConfigError naming the variable, never its value, and the model is never asked."""

    class Started(ScriptedBackend):
        began = False

        def begin(self, system, user, tools):
            Started.began = True
            super().begin(system, user, tools)

    with pytest.raises(ConfigError, match="environment variable DB_PASSWORD") as caught:
        run_with({}, environ={"DB_PASSWORD": "zq9"}, backend=Started(["done"]))
    assert "zq9" not in str(caught.value)
    assert Started.began is False


def test_R12_a_tool_with_a_secret_named_property_keeps_its_schema_and_its_manifest_hash_replays():
    """R12: a JSON-schema definition names things; a property called token is not a credential."""
    schema = {"type": "object", "properties": {"token": {"type": "string"}, "user": {"type": "string"}}}

    def login(arguments):
        return "logged in"

    provider = FakeProvider({"login": login}, schemas={"login": schema})
    with pytest.warns(RuntimeWarning):
        record = run_with(
            {}, providers=[provider], script=[[("login", {"token": "live-secret-value-77", "user": "ann"})], "done"]
        )
    assert record.tools[0]["input_schema"] == schema
    assert record.tool_calls[0]["arguments"] == {"token": REDACTED, "user": "ann"}
    assert "live-secret-value-77" not in record.dumps(environ={})
    assert replay_record(record).prompt["tools_sha256"] == record.prompt["tools_sha256"]


def test_R12_a_secret_found_only_when_the_record_is_written_leaves_no_stale_digest(tmp_path):
    """R12 and R06: the writer seals again whatever it redacts, so the file it writes replays and checks."""
    secret = "late-secret-value-3c2b1a09"
    record = run_with({}, user=f"use {secret}", script=[[("echo", {"x": secret})], "done"], providers=None)
    assert secret in record.prompt["user"]["text"]  # nobody knew it was a secret when the record was built
    with pytest.warns(RuntimeWarning):
        path = record.write(tmp_path, environ={"LATE_API_KEY": secret})
    written = path.read_text(encoding="utf-8")
    assert secret not in written
    stored = RunRecord.from_json(decode(written))
    assert stored.prompt["user"]["sha256"] == sha256_of(stored.prompt["user"]["text"])
    assert stored.prompt["user"]["sha256"] != record.prompt["user"]["sha256"]
    assert stored.transcript[1]["request_sha256"] != record.transcript[1]["request_sha256"]
    assert stored.redacted_paths
    assert replay_record(stored).outcome == record.outcome
    assert check_record(written.encode("utf-8")) is None


def test_R12_the_digests_of_a_record_are_those_of_its_data_whether_or_not_anything_was_redacted():
    """R12: writing a record whose digests are already right changes nothing."""
    record = run_with({}, script=[[("echo", {"x": "a"})], "done"])
    assert RunRecord.from_json(decode(record.dumps(environ={}))) == record


def test_R12_a_result_whose_rendering_was_redacted_is_served_as_recorded_and_marked(key_in_environment):
    """R12: the cap counts characters, and redaction changed them; the shown text is served, the rest still checked."""
    big = "a" * 50 + KEY + "b" * 300  # cut by the cap of 200 characters, with the whole key inside the kept part
    with pytest.warns(RuntimeWarning):
        record = record_run(
            [[("echo", {"x": "1"}), ("echo", {"x": "2"})], "done"],
            {"echo": lambda args: big if args["x"] == "1" else "plain result"},
        )
    secret_call, plain_call = record.tool_calls
    assert (secret_call["rendering_verified"], plain_call["rendering_verified"]) == (False, True)
    assert secret_call["truncated"] is True
    assert KEY not in record.dumps()
    replayed = replay_record(record)
    assert replayed.tool_calls == record.tool_calls


def test_R12_a_redaction_that_lengthens_a_result_at_the_cap_still_replays(monkeypatch):
    """R12: a four-character secret becomes ten characters, so a result of exactly the cap no longer fits it."""
    monkeypatch.setenv("SHORT_API_KEY", "abcd")
    text = "x" * 190 + "abcd" + "y" * 6  # exactly 200 characters, the cap
    with pytest.warns(RuntimeWarning):
        record = record_run([[("echo", {"x": "1"})], "done"], {"echo": lambda args: text})
    call = record.tool_calls[0]
    assert (call["rendering_verified"], call["truncated"], len(call["raw_text"])) == (False, False, 206)
    assert replay_record(record).tool_calls == record.tool_calls


def test_R12_a_call_served_as_recorded_still_has_its_identity_arguments_and_pairing_checked(key_in_environment):
    """R12: only the rendering is taken from the record; everything else about the call is still compared."""
    with pytest.warns(RuntimeWarning):
        record = record_run([[("echo", {"x": "1"})], "done"], {"echo": lambda args: f"the key is {KEY}"})
    assert record.tool_calls[0]["rendering_verified"] is False
    for path, change in (
        (("tool_calls", 0, "arguments"), {"x": "other"}),
        (("tool_calls", 0, "name"), "fail"),
    ):
        data = copy.deepcopy(record.to_json())
        node = data
        for step in path[:-1]:
            node = node[step]
        node[path[-1]] = change
        with pytest.raises(ReplayDivergence):
            replay_record(RunRecord.from_json(data))
    data = copy.deepcopy(record.to_json())
    message = data["transcript"][0]["tool_messages"][0]
    message["content"] = "something else"
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data))
    assert caught.value.path == "transcript[0].tool_messages"
    data = copy.deepcopy(record.to_json())
    data["transcript"][1]["request_sha256"] = "0" * 64
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data))
    assert caught.value.path == "transcript[1].request_sha256"


def test_R12_a_call_that_was_not_affected_keeps_the_cap_recomputation(key_in_environment):
    """R12: rendering_verified is true for it, and a changed shown text is still a divergence."""
    with pytest.warns(RuntimeWarning):
        record = record_run(
            [[("echo", {"x": "1"}), ("echo", {"x": "2"})], "done"],
            {"echo": lambda args: f"key {KEY}" if args["x"] == "1" else "z" * 500},
        )
    assert [c["rendering_verified"] for c in record.tool_calls] == [False, True]
    data = copy.deepcopy(record.to_json())
    data["tool_calls"][1]["text"] = "z" * 100
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data))
    assert caught.value.path == "tool_calls[1].text"


def cut_after_six(secret, cap=200):
    """The filler length with which the output cap cuts the secret after six of its characters."""
    for n in range(60, cap - 40):
        shown = cap_text("a" * n + secret + "b" * 400, cap)[0]
        head = shown.split("\n[output truncated")[0]
        if head == "a" * n + secret[:6]:
            return n
    raise AssertionError("no filler length cuts the secret after six characters")


def test_R12_a_cap_that_cuts_a_registered_secret_after_six_characters_leaves_none_of_it_in_the_record():
    """R12: the shown text ends with a fragment of the secret before the cap's notice; no substring rule finds that."""
    secret = "zqx7-live-key-4455667788"
    register_secret(secret)
    n = cut_after_six(secret)
    result = "a" * n + secret + "b" * 400
    with pytest.warns(RuntimeWarning):
        record = record_run([[("echo", {"x": "1"})], "done"], {"echo": lambda args: result})
    dumped = record.dumps()
    for k in range(4, len(secret) + 1):
        assert secret[:k] not in dumped
    call = record.tool_calls[0]
    assert call["truncated"] is True
    assert call["text"].split("\n[output truncated")[0] == "a" * n + REDACTED
    assert call["rendering_verified"] is False
    assert replay_record(record).tool_calls == record.tool_calls


def test_R12_a_secret_registered_during_the_run_is_redacted_from_the_record_built_after_it():
    """R12: a provider that mints a token registers it and returns it; the record never holds it."""
    minted = "minted-in-run-7c6b5a4938"

    def mint(arguments):
        register_secret(minted)
        return f"issued {minted}"

    with pytest.warns(RuntimeWarning):
        record = record_run([[("echo", {"x": "1"})], "done"], {"echo": mint})
    assert minted not in record.dumps(environ={})
    assert record.tool_calls[0]["text"] == f"issued {REDACTED}"
    assert replay_record(record).tool_calls == record.tool_calls


def test_R03_a_call_marked_as_not_verifiable_that_redaction_never_touched_diverges_naming_the_call():
    """R03: the flag opens a hole in replay, so it is honoured only where redacted_paths shows a redaction."""
    record = record_run([[("echo", {"x": "1"})], "done"], {"echo": lambda args: "a plain result"})
    assert record.redacted_paths == []
    data = copy.deepcopy(record.to_json())
    data["tool_calls"][0]["rendering_verified"] = False
    data["tool_calls"][0]["raw_text"] = "a result nobody produced"
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data))
    assert caught.value.path == "tool_calls[0].rendering_verified"
    assert "call_1_0" in str(caught.value)


def test_R03_a_flagged_call_with_a_changed_raw_text_diverges_even_when_the_flag_is_honoured(key_in_environment):
    """R03: Codex's fabrication; the flag is honoured, and the shown text must still equal the raw text."""
    with pytest.warns(RuntimeWarning):
        record = record_run([[("echo", {"x": "1"})], "done"], {"echo": lambda args: f"the key is {KEY}"})
    assert record.tool_calls[0]["rendering_verified"] is False
    assert replay_record(record).outcome["name"] == "finished"
    data = copy.deepcopy(record.to_json())
    data["tool_calls"][0]["raw_text"] = "a different raw result"
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data))
    assert caught.value.path == "tool_calls[0].text"


def test_R03_a_truncated_flagged_call_must_show_a_cut_of_its_raw_text(key_in_environment):
    """R03: what precedes the cap's notice must be a prefix of the redacted raw text."""
    big = "a" * 50 + KEY + "b" * 300
    with pytest.warns(RuntimeWarning):
        record = record_run([[("echo", {"x": "1"})], "done"], {"echo": lambda args: big})
    assert record.tool_calls[0]["truncated"] is True
    assert replay_record(record).tool_calls == record.tool_calls  # the faithful redacted capped call passes
    data = copy.deepcopy(record.to_json())
    data["tool_calls"][0]["raw_text"] = "X" + data["tool_calls"][0]["raw_text"][1:]
    with pytest.raises(ReplayDivergence) as caught:
        replay_record(RunRecord.from_json(data))
    assert caught.value.path == "tool_calls[0].text"


@pytest.mark.parametrize(
    ("text", "raw", "truncated", "follows"),
    [
        ("same", "same", False, True),
        ("same", "other", False, False),
        ("abc\n[output truncated: 5 of 9 characters dropped by the harness cap]", "abcdefghi", True, True),
        ("abd\n[output truncated: 5 of 9 characters dropped by the harness cap]", "abcdefghi", True, False),
        ("ab[reda\n[output truncated: 5 of 9 characters dropped by the harness cap]", "ab[redacted]", True, True),
        (
            "ab" + "xyz" + "\n[output truncated: 5 of 9 characters dropped by the harness cap]",
            "ab[redacted]cd",
            True,
            True,
        ),
        (
            "ab" + "xyzw" + "\n[output truncated: 5 of 9 characters dropped by the harness cap]",
            "ab[redacted]cd",
            True,
            False,
        ),
        ("plain cut with no notice", "plain cut with no notice and more", True, True),
        (
            "ab[redacted]\n[output truncated: 5 of 9 characters dropped by the harness cap]",
            "abXYZ and more",
            True,
            True,
        ),
        ("ab[redacted]\n[output truncated: 5 of 9 characters dropped by the harness cap]", "zzzzzz", True, False),
        ("[redacted]\n[output truncated: 5 of 9 characters dropped by the harness cap]", "anything", True, True),
        ("longer than its raw text", "longer", True, False),
    ],
)
def test_R03_what_can_be_checked_about_a_shown_text(text, raw, truncated, follows):
    """R03: equal when not truncated; a cut of the raw text when truncated, a cut inside a secret or marker allowed."""
    assert shown_follows_raw(text, raw, truncated) is follows


def test_R03_a_cut_that_ends_on_ordinary_text_beginning_a_secret_is_marked_and_replays_faithfully():
    """R03: Codex's case; x repeated, then abcd, then Y repeated, with a secret that begins abcd registered."""
    register_secret("abcd-secret-value-0001")
    n = next(
        n
        for n in range(60, 160)
        if cap_text("x" * n + "abcd" + "Y" * 400, 200)[0].split("\n[output truncated")[0] == "x" * n + "abcd"
    )
    result = "x" * n + "abcd" + "Y" * 400
    with pytest.warns(RuntimeWarning):
        record = record_run(
            [[("echo", {"x": "1"})], "done"], {"echo": lambda args: result}, user="say abcd now, abcd again"
        )
    call = record.tool_calls[0]
    assert call["text"].split("\n[output truncated")[0] == "x" * n + REDACTED  # one marker at the cut
    assert call["text"].count(REDACTED) == 1
    assert call["raw_text"] == result  # the raw result held no secret: it is as it was
    assert record.prompt["user"]["text"] == "say abcd now, abcd again"  # ordinary abcd elsewhere is untouched
    assert "tool_calls[0].text" in record.redacted_paths
    assert call["rendering_verified"] is False
    replayed = replay_record(record)
    assert replayed.tool_calls == record.tool_calls
    assert replayed.outcome["name"] == "finished"
