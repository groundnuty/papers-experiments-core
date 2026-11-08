# Provenance:
#   source: www-2027-experiments @ 46a5828ddb3276ba9582e4b4a6c2ae0f217e9da7, harness/agent/replay.py, blob a0f49e7173040376dd0ee101d3eabfbb4924a4f7
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Offline replay of a recorded attempt: no model, no endpoint.

``RecordedRoute`` hands back the recorded assistant turns in order, after
checking that the transcript the loop built so far equals the recorded one (so
a change in tool rendering, prompt or tools shows up as a divergence), and, for a
route that echoed reasoning, that the messages rebuilt from the record have the
digest of the ones it sent (agent-run/22).
``RecordedSparql`` serves the recorded results, keyed by call order and query
text, and re-renders each one to check it reproduces the text the model was
shown. ``ReplayClock`` replays the loop's budget checks. ``replay_attempt``
runs the real loop on these and, by default, checks the final rows and outcome
match the original.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from harness.agent.errors import HarnessAbort, InternalToolError
from harness.agent.infra import StoreUnavailable
from harness.agent.loop import (
    UNSCORED_FAILURES,
    AgentConfig,
    run_attempt,
    score_outcome,
    tools_sha256,
)
from harness.agent.providers import (MIXED_FINGERPRINTS, StepFailure, StepResult, messages_sha256,
                                     to_openai_messages)
from harness.agent.record import RunRecord
from harness.agent.tools import TOOLS, ToolSpec, render_query_result
from harness.sparql import QueryResult, SparqlError
from harness.terms import rows_from_sparql_json


class ReplayDivergence(HarnessAbort):
    """The replay asked for something the record does not hold."""


def _as_record(record: RunRecord | dict) -> RunRecord:
    return record if isinstance(record, RunRecord) else RunRecord.from_json(record)


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class RecordedRoute:
    def __init__(self, record: RunRecord | dict):
        self.record = _as_record(record)
        self.model = self.record.model
        self._transcript = self.record.transcript
        self._assistant_at = [i for i, t in enumerate(self._transcript) if t["role"] == "assistant"]
        self._per_turn = {t["turn"]: t for t in self.record.tokens.get("per_turn", [])}
        self.steps_served = 0

    def route_info(self) -> dict:
        return self.record.route

    @property
    def exhausted(self) -> bool:
        expected = len(self._assistant_at) + (1 if self.record.outcome.get("stage") == "provider" else 0)
        return self.steps_served == expected

    def step(self, system: str, transcript: list[dict], tools: tuple[ToolSpec, ...] = TOOLS, *,
             max_tokens: int | None = None, time_left_s: float | None = None) -> StepResult:
        if hashlib.sha256(system.encode("utf-8")).hexdigest() != self.record.prompt.get("sha256"):
            raise ReplayDivergence("the system prompt differs from the recorded one")
        if tools_sha256(tools) != self.record.prompt.get("tools_sha256"):
            raise ReplayDivergence("the tool specs differ from the recorded ones")
        k = self.steps_served
        if k < len(self._assistant_at):
            at = self._assistant_at[k]
            if _canonical(transcript) != _canonical(self._transcript[:at]):
                raise ReplayDivergence(f"before assistant turn {k + 1}, the transcript differs from the recorded one")
            turn = self._transcript[at]
            tokens = self._per_turn.get(turn["turn"], {})
            sent = self._check_sent_messages(system, transcript, tokens, k)
            self.steps_served += 1
            return StepResult(
                text=turn["text"],
                tool_calls=[dict(c) for c in turn["tool_calls"]],
                usage={k2: tokens.get(k2) for k2 in ("input_tokens", "output_tokens", "cache_read_tokens",
                                                     "cache_write_tokens")},
                usage_raw=tokens.get("usage_raw") or {},
                latency_s=tokens.get("latency_s") or 0.0,
                wall_s=tokens.get("wall_s") or 0.0,
                stop_reason=turn.get("stop_reason"),
                response_id=turn.get("response_id"),
                model=turn.get("model"),
                native=turn.get("native") or {},
                native_format=turn.get("native_format"),
                retries=tokens.get("retries") or 0,
                retry_errors=list(tokens.get("retry_errors") or []),
                leaked_tool_call=turn.get("leaked_tool_call"),
                refusal=turn.get("refusal"),
                max_tokens=tokens.get("max_tokens"),
                first_byte_s=tokens.get("first_byte_s"),  # None before agent-run/16
                system_fingerprint=tokens.get("system_fingerprint"),  # None before agent-run/21
                sent_messages_sha256=sent,  # agent-run/22: only an echoing route's turns have one
            )
        outcome = self.record.outcome
        if outcome.get("stage") == "provider" and _canonical(transcript) == _canonical(self._transcript):
            # its last request: an echoing route's every failure, an unreadable reply's included, carries its digest
            self._check_sent_messages(system, transcript, outcome.get("provider_error") or {}, k)
            self.steps_served += 1
            failure_type = outcome["failure_type"] or {"wall": "budget_wall",
                                                       "context": "context_overflow"}.get(outcome.get("cap"))
            raise StepFailure(failure_type, outcome.get("error_message") or "", outcome.get("provider_error") or None)
        raise ReplayDivergence(f"the replay asked for model step {k + 1}; the record holds {len(self._assistant_at)}")


    def _check_sent_messages(self, system: str, transcript: list[dict], tokens: dict, k: int) -> str | None:
        """For a record whose route echoed reasoning (route setting ``echo_reasoning``), or whose turn (``tokens``: its
        per-turn entry, or a provider failure's details for the attempt's last request) holds the digest of the
        messages it sent: the messages the recorded route sends for this transcript, rebuilt with the recorded echo
        setting (providers.to_openai_messages), must have that digest. The transcript checks cannot see the echo:
        a reasoning edited in the record, or an echo setting dropped or added, changes only what was sent. None for
        every other record, which records no digest."""
        settings = (self.record.route or {}).get("settings") or {}
        echo = settings.get("echo_reasoning")
        recorded = tokens.get("sent_messages_sha256")
        if echo is None and recorded is None:
            return None
        rebuilt = messages_sha256(to_openai_messages(system, transcript, echo))
        if rebuilt != recorded:
            raise ReplayDivergence(f"before assistant turn {k + 1}, the messages sent (echo_reasoning={echo!r}) rebuild "
                                   f"to sha256 {rebuilt[:12]}, not the recorded {str(recorded)[:12]}")
        return recorded


class RecordedSparql:
    replays_infra_retries = True  # the loop must not wrap it in RetryingSparql: retries are replayed as recorded

    def __init__(self, record: RunRecord | dict):
        self.record = _as_record(record)
        self._calls = [c for c in self.record.tool_calls if c.get("client_called")]
        self.calls_served = 0
        self.last_retries: list[dict] = []
        self.last_infra_s = 0.0
        self.last_raw_truncated = False
        self.last_elapsed_s: float | None = None  # the call's recorded wall time, which the tool layer reports
        # the call's recorded query timings (agent-run/13 and /20; None where the store client kept none), which the tool
        # layer reports as the call's own, so a replayed record holds them as the original does
        self.last_query_timings: list | None = None
        store = self.record.store
        self._settings = {"endpoint": store.get("endpoint_label"), "timeout_s": store.get("sparql_timeout_s"),
                          "row_cap": store.get("row_cap")}
        # The character cap the model was shown results under (None before agent-run/15): the re-render applies it.
        self._max_result_chars = self.record.agent_config.get("max_result_chars")

    def settings(self) -> dict[str, Any]:
        return dict(self._settings)

    @property
    def exhausted(self) -> bool:
        return self.calls_served == len(self._calls)

    def query(self, query: str) -> QueryResult:
        i = self.calls_served
        if i >= len(self._calls):
            raise ReplayDivergence(f"SPARQL call {i + 1} was not recorded")
        call = self._calls[i]
        expected = (call.get("arguments") or {}).get("query")
        if query != expected:
            raise ReplayDivergence(f"SPARQL call {i + 1}: the query differs from the recorded one")
        self.calls_served += 1
        self.last_raw_truncated = bool(call.get("result_raw_truncated"))  # the stored raw is capped already
        self.last_retries = list(call.get("infra_retries") or [])
        self.last_infra_s = float(call.get("infra_s") or 0.0)
        self.last_elapsed_s = call.get("elapsed_s")
        self.last_query_timings = call.get("query_timings")
        if call.get("error"):
            if call["error"]["kind"] == "infrastructure":
                raise StoreUnavailable(call["error"]["message"], self.last_retries, self.last_infra_s,
                                       suspected_query=bool(call["error"].get("suspected_query")))
            if call["error"]["kind"] == "internal":
                error = InternalToolError(call["error"]["message"], client_called=True)
                if "exception" in call["error"] and "stack_sha256" in call["error"]:  # agent-run/17: a harness defect
                    error.recorded_defect = {"exception": call["error"]["exception"],
                                             "stack_sha256": call["error"]["stack_sha256"]}
                raise error
            raise SparqlError(call["error"]["message"], call["error"]["kind"])
        result = self._rebuild(call)
        if render_query_result(result, self._max_result_chars) != call.get("result_shown"):
            raise ReplayDivergence(f"SPARQL call {i + 1}: the re-rendered result differs from what the model saw")
        return result

    def _rebuild(self, call: dict) -> QueryResult:
        meta = call["result_meta"]
        raw = call["result_raw"] or {}
        if meta["form"] == "ASK":
            return QueryResult("ASK", [], [], meta["boolean"], 0, False, meta["elapsed_s"], raw)
        _, rows = rows_from_sparql_json(raw)
        if meta["truncated"]:
            rows = rows[: self._settings["row_cap"]]
        return QueryResult(meta["form"], list(meta["columns"]), rows, None, meta["total_rows"], meta["truncated"],
                           meta["elapsed_s"], raw, store_cut=bool(meta.get("store_cut", False)))


class ReplayClock:
    """Returns the recorded elapsed times at each of the loop's clock reads."""

    def __init__(self, record: RunRecord | dict):
        rec = _as_record(record)
        starts = [(t["turn"], float(t["started_s"])) for t in rec.tokens.get("per_turn", [])]
        retry = (rec.checker or {}).get("retry")
        if retry:  # a checker cell's retry phase starts with one read of its own
            k = rec.checker["initial"]["outcome"]["turns"]
            values = ([0.0] + [s for t, s in starts if t <= k] + [float(retry["started_s"])]
                      + [s for t, s in starts if t > k])
        else:
            values = [0.0] + [s for _, s in starts]
        if rec.outcome.get("final_check_s") is not None:
            values.append(float(rec.outcome["final_check_s"]))
        self._values = values
        self._final = float(rec.wall_s)
        self._i = 0

    def __call__(self) -> float:
        if self._i < len(self._values):
            value = self._values[self._i]
        else:
            value = self._final
        self._i += 1
        return value


def _json(obj: Any) -> Any:
    return json.loads(json.dumps(obj, ensure_ascii=False))


# The fields each schema version added (see record.py). Replaying an older record
# excuses exactly the fields added by the versions after it, nothing else.
_ADDED = {
    "agent-run/2": {"outcome": ("model_mismatch_turns", "served_models"),
                    "totals": ("cache_write_5m_tokens", "cache_write_1h_tokens"),
                    "per_turn": ("served_model", "model_mismatch"),
                    "tool_calls": ()},
    "agent-run/3": {"outcome": ("infra_s", "infra_retries"),
                    "totals": (),
                    "per_turn": (),
                    "tool_calls": ("infra_retries", "infra_s")},
    "agent-run/4": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},  # adds ``runner`` only
    "agent-run/5": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ("engine_flags",)},
    "agent-run/6": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},  # adds ``load`` only
    "agent-run/7": {"outcome": ("infra_source",), "totals": (), "per_turn": (), "tool_calls": ()},
    "agent-run/8": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},  # a rule, no field
    "agent-run/9": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},  # agent_config only
    "agent-run/10": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},  # agent_config and load
    "agent-run/11": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},  # agent_config and load
    "agent-run/12": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},  # agent_config and load
    "agent-run/13": {"outcome": ("engine_suspect",), "totals": (), "per_turn": (), "tool_calls": ("query_timings",)},
    "agent-run/14": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},  # adds ``checker`` only
    "agent-run/15": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},  # agent_config only
    "agent-run/16": {"outcome": ("stream_guard_firings",), "totals": (), "per_turn": ("first_byte_s",),
                     "tool_calls": ()},
    "agent-run/17": {"outcome": ("refusal", "harness_defect", "model_changed"), "totals": (), "per_turn": (),
                     "tool_calls": ()},
    # adds agent_config.ontology_block_sha256 and prompt.ontology_block_sha256, neither compared here
    "agent-run/18": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},
    # result_meta.store_cut and the shown total_rows_exact, both only when set: nothing an older record lacks is
    # rebuilt by its replay (its answers rebuild with store_cut false)
    "agent-run/19": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},
    # query_timings' gate_wait_s and server_wait_s (query_timings is never compared) and agent_config.total_cap_factor
    # (only when set; config_of reads it, None before): no field an older record lacks is rebuilt by its replay
    "agent-run/20": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},
    # the agent config's report_key_rule (only when set) and each turn's system_fingerprint
    "agent-run/21": {"outcome": (), "totals": (), "per_turn": ("system_fingerprint",), "tool_calls": ()},
    # the route setting echo_reasoning with each turn's sent_messages_sha256, the agent config's reasoning_call_rule, a
    # turn's recovered_tool_call and a call's source, each only when set: nothing an older record lacks is rebuilt by
    # its replay
    "agent-run/22": {"outcome": (), "totals": (), "per_turn": (), "tool_calls": ()},
}
_ORDER = ("agent-run/1", "agent-run/2", "agent-run/3", "agent-run/4", "agent-run/5", "agent-run/6", "agent-run/7",
          "agent-run/8", "agent-run/9", "agent-run/10", "agent-run/11", "agent-run/12", "agent-run/13", "agent-run/14",
          "agent-run/15", "agent-run/16", "agent-run/17", "agent-run/18", "agent-run/19", "agent-run/20",
          "agent-run/21", "agent-run/22")


def _without_later_additions(version: str, name: str, value: Any) -> Any:
    later = _ORDER[_ORDER.index(version) + 1:]
    drop = {part: {k for v in later for k in _ADDED[v][part]} for part in ("outcome", "totals", "per_turn",
                                                                             "tool_calls")}
    if name == "outcome":
        return {k: v for k, v in value.items() if k not in drop["outcome"]}
    if name == "tokens":
        return {**value, "totals": {k: v for k, v in value["totals"].items() if k not in drop["totals"]},
                "per_turn": [{k: v for k, v in t.items() if k not in drop["per_turn"]} for t in value["per_turn"]]}
    if name == "tool_calls":
        return [{k: v for k, v in c.items() if k not in drop["tool_calls"]} for c in value]
    if name == "checker" and value is not None:
        # a checker cell's initial phase carries an outcome the loop writes with the top-level outcome's fields: the
        # same later fields are excused there (the retry block holds no outcome; the scores are compared by rescoring)
        initial = value.get("initial")
        if isinstance(initial, dict) and isinstance(initial.get("outcome"), dict):
            return {**value, "initial": {**initial, "outcome": {k: v for k, v in initial["outcome"].items()
                                                                if k not in drop["outcome"]}}}
    return value


def _unscored(checker: dict | None) -> dict | None:
    if checker is None:
        return None
    out = json.loads(json.dumps(checker, ensure_ascii=False))
    out["initial"]["score"] = None
    return out


# Wall times of the try, not of the attempt: never compared (query_timings from agent-run/13).
_TIMING_FIELDS = ("elapsed_s", "query_timings")


def _without_elapsed(calls: list[dict]) -> list[dict]:
    return [{k: v for k, v in c.items() if k not in _TIMING_FIELDS} for c in calls]


def first_difference(a: Any, b: Any, path: str = "") -> str | None:
    """The path of the first place two JSON values differ, or None when equal."""
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                return f"{path}.{k}"
            found = first_difference(a[k], b[k], f"{path}.{k}")
            if found is not None:
                return found
        return None
    if isinstance(a, list) and isinstance(b, list):
        for i, (x, y) in enumerate(zip(a, b, strict=False)):  # lengths are compared below
            found = first_difference(x, y, f"{path}[{i}]")
            if found is not None:
                return found
        return None if len(a) == len(b) else f"{path} (length {len(a)} vs {len(b)})"
    return None if a == b else path


def _question(rec: RunRecord) -> dict:
    first = rec.transcript[0]
    if first.get("role") != "user":
        raise ReplayDivergence("the record's transcript does not start with the question")
    return {"question_id": rec.question_id, "base_question_id": rec.base_question_id, "template": rec.template,
            "split": rec.split, "text": first["text"]}


# What identifies the prompt a model was given; a record of any version holds the first six (none has no block).
PROMPT_KEYS = ("condition", "id", "sha256", "template_sha256", "nudge_sha256", "tools_sha256", "ontology_block_sha256")


def config_of(rec: RunRecord) -> AgentConfig:
    """The agent config a record ran under. A setting the record lacks was added after it was written: it ran
    under the rule before it (iri_rule: any scheme, before agent-run/7; context_overflow: failed, before
    agent-run/9; answer_skipped_calls: no, before agent-run/14; max_result_chars: no character cap and the
    earlier tool description, before agent-run/15). A setting it holds is used as recorded (iri_rule
    "known_schemes" for agent-run/7 to /9). agent-run/16 adds the recorded-only ``transport`` and
    ``stream_guards`` (None and () before it: the route's settings said it); its per-model turn and wall caps,
    max_tokens and max_result_chars are the config's own fields, recorded as before. agent-run/17 adds
    ``refusal_rule`` and ``tool_crash_rule`` ("failed" before it: a refusal and a tool crash were failed:other, a
    textual decline failed:no_submission) and the recorded-only ``graph`` (None before it). agent-run/20 adds
    ``total_cap_factor`` where the runner set one (None otherwise, and before it). agent-run/21 adds
    ``report_key_rule``, only when set (None before it, and in every record without it: the strict reading), and
    agent-run/22 ``reasoning_call_rule`` likewise (None: a call in the reasoning channel is the leaked_tool_call
    failure)."""
    recorded = dict(rec.agent_config)
    if "stream_guards" in recorded:
        recorded["stream_guards"] = tuple(recorded["stream_guards"])
    return AgentConfig(**{"iri_rule": "any_scheme", "context_overflow": "failed", "answer_skipped_calls": False,
                          "max_result_chars": None, "transport": None, "stream_guards": (), "refusal_rule": "failed",
                          "tool_crash_rule": "failed", **recorded})


def _served_check(rec: RunRecord):
    """The served-model check a record ran under, rebuilt from it (agent-run/17): for an attempt the runner ended at
    a turn served by another model id, the check against the model's first served id it recorded, and for one ended at
    a turn with another system fingerprint (agent-run/21), the check against the model's first fingerprint it
    recorded; each first fails at that turn (every earlier turn passed it live). None for any other record, whose turns
    all passed. A fingerprint change that records ``admitted_system_fingerprints`` (change-log entry 167: endpoints
    joined to a running OUT, scripts/run_pilot.py ServedModels) passes a turn whose fingerprint and the first one are
    both in it, as the runner did; a reply carrying several fingerprints still fails. The runner's admitted set only
    grows, so the set it recorded at the change holds every fingerprint an earlier turn passed on. A change without the
    key is checked exactly as before."""
    changed = rec.outcome.get("model_changed") if rec.outcome.get("failure_type") == "model_changed" else None
    if not changed:
        return None
    if "first_system_fingerprint" in changed:
        known = changed["first_system_fingerprint"]  # None: the change was a reply's several fingerprints joined
        admitted = frozenset(changed.get("admitted_system_fingerprints") or ())

        def check(served, fingerprint):
            fired = bool(fingerprint) and (MIXED_FINGERPRINTS in fingerprint
                                           or (known is not None and fingerprint != known
                                               and not (known in admitted and fingerprint in admitted)))
            return dict(changed) if fired else None

        return check
    first = changed["first_served_model"]
    return lambda served, fingerprint: ({"first_served_model": first, "served_model": served}
                                        if served and served != first else None)


def replay_attempt(record: RunRecord | dict, *, check: bool = True, checker: Any = None) -> RunRecord:
    """Re-run the recorded attempt through the real loop, offline.

    The new record has a new ``run_id`` and ``replay_of`` set to the original.
    With ``check``, raises ``ReplayDivergence`` unless the rebuilt record equals
    the stored one in its prompt (condition, ids and hashes, the ontology block's
    included), final rows, outcome, complete transcript (terminal tool
    turn included), complete tool-call log (statuses, results, skipped calls;
    only the per-call ``elapsed_s`` wall time is excluded) and token counts, and
    every recorded step and query was used.

    A checker cell's record (agent-run/14) replays its checker decision too:
    served from the record by default (``RecordedChecker``), or recomputed by
    ``checker`` (an ``OnlineChecker`` over the recorded store log, see
    harness/checkloop/online.py); the rebuilt ``checker`` field must equal the
    stored one, scores aside.
    """
    rec = _as_record(record)
    route = RecordedRoute(rec)
    sparql = RecordedSparql(rec)
    store = {k: v for k, v in rec.store.items() if k not in ("sparql_timeout_s", "row_cap")}
    if rec.checker is not None and checker is None:
        from harness.checkloop.online import RecordedChecker

        checker = RecordedChecker(rec)
    new = run_attempt(_question(rec), route, sparql, config_of(rec),
                      repetition=rec.repetition, store=store, clock=ReplayClock(rec), replay_of=rec.run_id,
                      checker=checker if rec.checker is not None else None,
                      branched_from=(rec.checker or {}).get("branched_from"), branch=(rec.checker or {}).get("branch"),
                      served_check=_served_check(rec))
    if check:
        # The prompt is checked here too, not only at a model step: an attempt that ended before its first step
        # (a turn cap of 0) would otherwise replay with any block (Codex review of b552d62, item 4).
        for key in PROMPT_KEYS:
            if new.prompt.get(key) != rec.prompt.get(key):
                raise ReplayDivergence(f"the rebuilt prompt differs from the recorded one at prompt.{key}")
        if new.condition != rec.condition:
            raise ReplayDivergence("the rebuilt prompt's condition differs from the record's")
        names = ("final_rows", "outcome", "transcript", "tool_calls", "tokens")
        if rec.checker is not None:
            names += ("checker",)
        for name in names:
            replayed, recorded = getattr(new, name), getattr(rec, name)
            if name == "checker":  # a replay does not score; the stored scores are compared by rescoring
                replayed, recorded = _unscored(replayed), _unscored(recorded)
            if name == "tool_calls":  # elapsed_s is the replay's own wall time
                replayed, recorded = _without_elapsed(replayed), _without_elapsed(recorded)
            if rec.schema_version != _ORDER[-1]:  # fields an older record cannot hold
                replayed = _without_later_additions(rec.schema_version, name, _json(replayed))
            where = first_difference(_json(recorded), _json(replayed), name)
            if where is not None:
                raise ReplayDivergence(f"the replayed record differs from the stored one at {where}")
        if not sparql.exhausted:
            raise ReplayDivergence(f"the replay used {sparql.calls_served} of the recorded SPARQL calls")
        if not route.exhausted:
            raise ReplayDivergence(f"the replay used {route.steps_served} of the recorded model steps")
    return new


def unscored(rec: RunRecord) -> bool:
    """Whether the record's attempt is never scored: it failed infrastructure, a harness defect or a served-model
    change (``UNSCORED_FAILURES``), or the runner aborted it for a served-model change (the runner's layout of
    57e148c, ``runner.aborted.reason`` "model_changed" over a scored outcome)."""
    aborted = (rec.runner or {}).get("aborted") or {}
    return rec.outcome.get("failure_type") in UNSCORED_FAILURES or aborted.get("reason") == "model_changed"


def rescore(record: RunRecord | dict, gold_rows: Any, rules: Any) -> RunRecord:
    """A copy of the record with its score recomputed against ``gold_rows`` and ``rules``; an attempt that is never
    scored (``unscored``) stays unscored. A checker cell's initial answer is rescored too (unless its phase failed in
    a way that is never scored, ``UNSCORED_FAILURES``), so its retry transition follows the same gold. Scoring checks
    the frozen scorer digest first (loop.score_outcome) and raises ScorerNotFrozen on any other scorer."""
    rec = _as_record(record)
    checker = rec.checker
    if checker is not None:
        checker = json.loads(json.dumps(checker, ensure_ascii=False))
        initial = checker["initial"]
        initial["score"] = (None if initial["outcome"].get("failure_type") in UNSCORED_FAILURES
                            else score_outcome(initial["outcome"], initial["final_rows"], gold_rows, rules))
    if unscored(rec):
        return replace(rec, score=None, checker=checker)
    return replace(rec, score=score_outcome(rec.outcome, rec.final_rows, gold_rows, rules), checker=checker)
