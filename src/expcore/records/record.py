# Provenance:
#   source: www-2027-experiments @ 46a5828ddb3276ba9582e4b4a6c2ae0f217e9da7, harness/agent/record.py, blob 7d033daa1e8228d4a997fc5f66203f66039620bc
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""The run record: one JSON file per attempt.

A record holds everything needed to audit, rescore and replay an attempt
offline: the neutral transcript with each provider's native messages, every
tool call with the exact text shown to the model and the full SPARQL result,
token counts per turn, the outcome and the final rows. It holds no secrets:
route and store descriptions are built without keys, and the writer redacts, in
string values and keys only, the value of any ``*KEY*``/``*TOKEN*``/``*SECRET*``/
``*PASSWORD*`` environment variable that is at least 16 characters long and not
only digits. Numbers, booleans and null are never changed, and writing a loaded
record gives back its exact bytes: a top-level field added in a later schema
version is not written for a record of an earlier version.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
import threading
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# agent-run/2 adds, over agent-run/1: per turn ``served_model`` and ``model_mismatch``;
# in the outcome ``model_mismatch_turns`` and ``served_models``; in the token totals
# ``cache_write_5m_tokens`` and ``cache_write_1h_tokens``.
# agent-run/3 adds: per tool call ``infra_retries`` and ``infra_s``; in the outcome
# ``infra_s`` and ``infra_retries``; the failure type ``infrastructure``.
# agent-run/4 adds the top-level ``runner`` field: what the pilot runner knows about the
# attempt beyond this one try (``infra_tries``: how many earlier tries of this attempt
# failed infrastructure, and their run ids); None for a record not written by the runner.
# agent-run/5 adds per tool call ``engine_flags``: query shapes on which the pinned engine
# departs from SPARQL (harness/agent/engine_flags.py), recorded at run time (issue 12).
# agent-run/6 adds the top-level ``load`` field: the load on the model while the try ran, as
# the pilot runner saw it (experiments issue 3): the attempt's arm and position in the seeded
# interleaved order, the configured and currently allowed workers for its model, the attempts
# in flight (this one included) for its model and in total at the try's start and end, and the
# runner's UTC start and end. None for a record not written by the runner. Replay does not
# check it: it describes the conditions of the try, not the attempt.
# agent-run/7 adds in the outcome ``infra_source`` ("store", "provider" or None): from this version
# a transient provider failure after the last retry (429, 5xx, 529, time-out, connection error) is
# failure type ``infrastructure`` from the provider, where earlier versions recorded provider_error
# (experiments issue 3). The agent config adds ``iri_rule``: from this version submit_report refuses
# an IRI whose scheme is not a known one (a prefixed name such as CHEBI:15377) with a tool error the
# model sees (tools.py, issue 16); a record without it ran under the earlier rule, which took any
# scheme, and replays under it.
# agent-run/8 adds no field: from this version a provider step that the wall budget cuts short is
# ``capped:wall`` only when the attempt's own deadline cut off a call the provider was answering
# normally; a transient provider error or an unclipped time-out with no budget left to retry is
# ``failed:infrastructure`` from the provider (providers.py). Earlier versions recorded both as
# capped:wall (stage "provider").
# agent-run/9 adds the cap "context" and, in the agent config, ``context_overflow`` ("capped"): a context
# overflow (providers.py's table, or a context-window stop reason) ends the attempt ``capped:context``, an
# agent outcome, where earlier versions recorded ``failed:context_overflow`` (issue 16). A record without
# the setting replays under the earlier rule. No outcome field is added: the provider's message is the
# outcome's ``error_message``, and its error class and status are in ``provider_error``.
# agent-run/10 (runner review): the agent config's ``iri_rule`` is "registered_schemes" (a scheme in the IANA
# registry is absolute; tools.py), where /7 to /9 used "known_schemes", and records of those replay under it;
# ``load`` adds ``order_key`` (the attempt's place in an order that a larger plan never reshuffles) and the
# actual start sequence: ``start_index``, ``model_start_index`` and ``runner_started_at``. The store client
# confirms a 504 after a probe with one more try (infra.py); the record holds what came of it, as before.
# agent-run/11 (second runner review): ``iri_rule`` "registered_schemes_v2" (an http(s) IRI needs '//' and a
# host, and no IRI a broken percent escape; tools.py), records of /10 replay under "registered_schemes";
# ``load.start_index`` and ``model_start_index`` are taken at the attempt's first provider request (run_attempt's
# ``on_start``), and ``load.started_at`` and the in-flight counts at start at each try's first request, where /10
# took them when the worker passed the start gate. ``runner_started_at`` still names the runner's start. The
# store client sends the agent's query at most once more after a probe's 504 (infra.py).
# agent-run/12 (third runner review): ``iri_rule`` "registered_schemes_v3" (an http(s) IRI needs a non-empty
# host after any userinfo and before any port, and a datatype IRI gets the same checks as any IRI, whitespace
# included; tools.py), records of /11 replay under "registered_schemes_v2"; ``load.started_at`` and the
# in-flight counts at start of an attempt's first try are taken inside the start gate, with its start indices.
# agent-run/13 (issue 24) adds per tool call ``query_timings``: the store client's own timing of each request
# behind the call (wall time, client and server time-outs, the server's execution time, the outcome, rows and
# possibly_partial), when the client keeps one (harness.sparql VirtuosoClient), else None; replay does not compare
# it, as it does not compare ``elapsed_s``. The outcome adds ``engine_suspect``, the runner's part of the
# engine-suspect stratum (a union, change-log entry 57): {"any_agent_query": whether an executed query has the
# shape the store is known to answer wrongly (harness/engine_suspect.py)}; such a call also gets the engine flag
# "engine_suspect". The checked query's and the footprint's parts are in the checker's evidence. The agent config
# adds ``engine_suspect_classes`` (empty: never flagged). ``load``
# adds ``store_connections_start`` and ``store_connections_end`` when the store client reports its current client
# connections (Virtuoso, readable without admin rights).
# agent-run/14 adds the top-level ``checker`` field (issue 15, the checker loop): the cell's checker
# settings, the initial answer with its checked query, verdict, check reports and the store responses
# the checks read, and the retry's message, turns and nudges; None outside checker cells. The agent
# config adds ``answer_skipped_calls``: from this version the calls of a reply after its submission or
# abstention get a "skipped" result in that reply's tool turn, so a retry branched from the record starts
# from the same bytes; a record without the setting replays under the earlier rule. A branched retry
# arm's checker field adds ``branch``: its label, its place in the output's seeded arm order, and its
# sampling seed (None where the route takes none).
# agent-run/15 (issue 15, change-log entry 66) adds no field: the agent config adds ``max_result_chars`` (80000 in
# the frozen settings), the most characters of one sparql_query result the model is shown (tools.py: leading rows
# that fit, then the line "[result truncated at 80000 characters: K of M rows shown]"; a first row too long to fit
# is cut inside and marked "[row 1 cut here]", a header too long for that is cut inside and marked "[header cut
# here]", each with K = 0; errors and ASK results are never cut), and the sparql_query description states it, so
# ``prompt.tools_sha256`` changes. The stored result (``result_raw``, ``result_meta``) is not cut. A record
# without the setting ran with no character cap and the earlier description, and replays under them.
# agent-run/16 (the runner round before the dev natural phase; science rulings on issues 3 and 22) adds per turn
# ``first_byte_s``: for the request that succeeded, the time to the first line of a streamed reply's body, and for
# a non-streamed reply (which arrives whole) its ``latency_s``. The outcome adds ``stream_guard_firings``
# ({"rejected_streams", "call_identity", "token_gap"}: the replies the route's stream guards rejected, providers.py).
# A rejection is a transient retry whose retry_errors entry carries ``stream_guard`` (each firing guard's evidence),
# and a failed step's ``provider_error`` carries ``stream_guard`` (the list of its rejections); the route settings
# carry ``stream_guards`` when a guard is on. A text channel of a reply without tool calls that holds a JSON
# object naming one of the agent's tools with "parameters" or "arguments" (Llama's call format, keys in any order)
# is now ``leaked_tool_call`` (shape "bare_json"), where earlier versions nudged it; replay serves the recorded
# detection. The agent config adds ``transport`` ("streamed" or "non-streamed") and ``stream_guards`` (the guards
# on), recorded only (the route acts on them; None and [] outside the runner, and by replay.config_of for an older
# record); its ``max_turns``, ``max_wall_s``, ``max_tokens`` and ``max_result_chars`` now come from the model's entry
# in the runner's table (issue 3, entry 90), never from a run option. The runner names each file of an attempt
# "<claim digest>.<run_id>.json" (it was "<run_id>.json"; the record's bytes do not depend on its name).
# agent-run/17 (change-log entry 91; the confirmation review of 57e148c, which had put these fields under /16, so
# that records /16 wrote before them stopped replaying) adds the status ``refused`` (rule refusal-v1, loop.py: the
# provider declined, or the final message after the last nudge declines the task in the first person; scored wrong)
# with the outcome's ``refusal`` ({"rule", "signal"}); the failure type ``harness_defect`` (a crash inside our own
# code on valid input; never scored) with the outcome's ``harness_defect`` ({"exception", "stack_sha256"}, also in
# the crashed call's error); and the failure type ``model_changed`` (a turn served by another model id than the
# model's first in the run, checked as each turn arrives; the attempt ends at that turn, never scored) with the
# outcome's ``model_changed`` ({"first_served_model", "served_model"}; the changed turn's calls are logged
# ``skipped``). The agent config adds ``refusal_rule`` and ``tool_crash_rule``, which say which rules ran
# ("failed": the rules before, failed:other and failed:no_submission, under which replay.config_of replays an older
# record), and ``graph`` (the run's graph, for which ``max_result_chars`` was resolved; None outside the runner).
# A submitted cell whose datatype or language tag is not a string, or an integer too long to write out, is now an
# invalid report returned to the agent (where it crashed the harness); inputs the harness already answered keep
# their exact messages. A tool call whose arguments hold an unpaired surrogate (JSON's \u escapes can carry one; UTF-8
# cannot encode it) is malformed (providers.py), and so is such a query or cell for the tools (tools.py); the writer
# stores one anywhere in a record as its \u escape (``dumps``), where it used to fail.
# agent-run/18 adds, under an ontology prompt condition only (issue 16; feat/prompt-conditions, which named it
# agent-run/7 before the merge, where /7 is infra_source, and the integration first numbered it /16, before
# feat/e0.1-skeleton's /16 and /17 merged, whose /16 records must keep replaying): ``agent_config.
# ontology_block_sha256`` and ``prompt.ontology_block_sha256``, the hash of the ontology block the model was shown.
# Records of /15 to /17 written by the runner's branch (which numbered the cap /15, had no /14 and never the checker
# field: results/model_63-pilot-20261019, tests/fixtures/agent-run-16) have no ``checker`` key, while the grid launcher
# wrote /15 with it: the reader notes a key such a record lacks (``absent_keys``), and it is never added on writing
# the record back.
# agent-run/19 (issue 24, item 10; feat/a15-census-clean, merged into the freeze integration) adds, for an answer that
# reached a row limit the SPARQL client was configured with (harness.sparql.VirtuosoClient ``result_cap``; an endpoint
# may cut an answer there without a flag), ``store_cut`` true in the call's ``result_meta`` and ``"total_rows_exact":
# false`` in the text the model is shown, the whole JSON or the cut one, after ``truncated``. Both appear only when
# set and only such a client sets them, so every other record and text is as under /18; replay rebuilds the answer's
# ``store_cut`` from ``result_meta``.
# agent-run/20 (issue 3, science ruling of 20 Oct; the runner's path for a store shared through a query gate,
# harness/query_gate.py) adds, for a store client that waits for the gate (harness.sparql.VirtuosoClient
# ``gate_wait_s``), per ``query_timings`` entry ``gate_wait_s`` (the wait for the gate before the request) and
# ``server_wait_s`` (the wait, still holding the gate, for the server time-out after a request with no whole or a
# slow answer); the call's ``infra_s``, and so the outcome's, includes both, so they are outside the wall cap
# (infra.py). The agent config adds ``total_cap_factor`` (only when set; the runner sets 3 for such a store): a phase
# whose whole time, waits included, reaches that many times its wall cap while the agent's own time is under its cap
# ends ``failed:infrastructure`` (stage "store"; once the agent's own time reaches the cap, ``capped:wall`` stands),
# decided from the recorded clock like the caps. A gate still closed after the client's bound ends the
# attempt ``failed:infrastructure`` (stage "store") with no probe or re-send. Every other record is as under /19.
# agent-run/21 (change-log entries 110 and 112; science agent and owner, issue 3, 20 Oct; numbered after
# feat/a15-runner's /20, unmerged when this was written) adds the agent config's ``report_key_rule``, only when set:
# how submit_report read a cell object's strings (tools.REPORT_KEY_RULES; model_61's "json_string_cells_v1" from UniProt
# dev prompted on, a per-model setting of the runner's table, completed by the follow-up of 21 Oct 00:20: a type
# "number" is read as a literal, a bare number by its JSON text in the call's ``arguments_raw``, which every turn's
# tool calls keep). The tools the model is shown, and so
# ``prompt.tools_sha256``, do not change with it; a record without it ran under the strict reading and replays under
# it (replay.config_of). Each turn adds ``system_fingerprint`` (science ruling on issue 3, 20 Oct 23:24): the server's
# fingerprint its reply carried (OpenAI wire), null when it sent none (every Anthropic turn); a run's served-model check
# compares it as it does the served id, and a change ends the attempt failed:model_changed with the outcome's
# ``model_changed`` adding "first_system_fingerprint" and "system_fingerprint". Every other record is as under /20.
# agent-run/22 adds, each only when set (one version for two branches of 21 Oct, merged: the Forge mitigation A/B's,
# results/forge-mitigations-20261020, and model-58's rule of the science rulings on issue 3, 21 Oct 00:09 and 01:31):
# * the route setting ``echo_reasoning`` (providers.ECHO_REASONING_FIELDS): each earlier turn's reasoning channel was
#   sent back under that name; each turn of such a route then records ``sent_messages_sha256``, the digest of the
#   messages it sent (providers.messages_sha256), which every replay rebuilds from the record and checks;
# * the agent config's ``reasoning_call_rule`` (whether a call written into the reasoning channel ran,
#   providers.REASONING_CALL_RULES; model-58's);
# * under the latter, on an assistant turn whose call leaked into its reasoning, ``recovered_tool_call``: {"rule",
#   "taken": true, "channel", "offset", "end", "text", "call"} for the one call the rule ran (the turn's ``tool_calls``
#   stay as the server sent them, empty), or {"rule", "taken": false, "reason", "detail"}; and a call it ran has
#   ``source`` "reasoning" in the tool-call log (loop.py). (The A/B branch's route option reasoning_call_recovery, which
#   wrote another form of this field, was replaced by the rule before any record was written with it.)
# Every other record is as under /21.
# All versions are read.
SCHEMA_VERSION = "agent-run/22"
READABLE_SCHEMAS = ("agent-run/1", "agent-run/2", "agent-run/3", "agent-run/4", "agent-run/5", "agent-run/6",
                    "agent-run/7", "agent-run/8", "agent-run/9", "agent-run/10", "agent-run/11", "agent-run/12",
                    "agent-run/13", "agent-run/14", "agent-run/15", "agent-run/16", "agent-run/17", "agent-run/18",
                    "agent-run/19", "agent-run/20", "agent-run/21", "agent-run/22")

_NAME_PREFIX = re.compile(r"[A-Za-z0-9_-]+")
_LONE_SURROGATE = re.compile(r"[\ud800-\udfff]")  # in a str only unpaired (a valid pair decodes to one character)
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD", re.IGNORECASE)
_MIN_SECRET_LEN = 16
REDACTED = "[redacted]"

# Top-level record fields added after agent-run/1, with the version that added them. A record of an
# older version is written without them, so loading and writing it back gives the same bytes.
_TOP_LEVEL_ADDED = {"runner": "agent-run/4", "load": "agent-run/6", "checker": "agent-run/14"}
# The runner's branch numbered the result cap /15 with no /14, and its /16 and /17 followed: its records of these
# versions have no checker key (a record written by the merged writer, /18, always has it).
_CHECKER_OPTIONAL_IN = ("agent-run/15", "agent-run/16", "agent-run/17")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


_IDENTITY_LOCK = threading.Lock()
_IDENTITY: dict[str, Any] = {}  # the last identity read: {"root", "stat", "code"}


def harness_commit() -> dict:
    """The code this process runs, as every record and run manifest names it (dress rehearsal F1):
    ``{git_commit, git_dirty, source, tree_sha256}``, the replay check's own identity (replay_check.code_commit), so
    the two never disagree: a deploy's COMMIT and TREE files when both are at the code root (they take precedence
    over git), clean only while the tree digest recomputed from the files on disk equals TREE; else git (clean when
    the code paths have no uncommitted change). ``tree_sha256`` is always recomputed from disk.

    Re-verified at every call (F1 review, item 8: a record is written, and a start checks its code, under the code as
    it is then): the files' sizes and modification times, COMMIT's and TREE's, and git's HEAD, reflog and index are
    compared with those of the last read (replay_check.code_stat, no file read), and the identity is read again on any
    change. A copy each call. Never raises: a replay builds records too; the runners refuse to start on code this
    cannot name (replay_check.require_identified_code)."""
    # imported here: replay_check imports this module
    from harness.agent import replay_check

    root = replay_check.CODE_ROOT
    with _IDENTITY_LOCK:
        stat = replay_check.code_stat(root)
        if _IDENTITY.get("root") != root or _IDENTITY.get("stat") != stat:
            _IDENTITY.update(root=root, stat=stat, code=replay_check.code_commit(root))
        return dict(_IDENTITY["code"])


harness_commit.cache_clear = _IDENTITY.clear  # type: ignore[attr-defined]


def _is_secret_value(value: str) -> bool:
    """A value worth redacting: at least 16 characters and not only digits (real keys are long
    random strings; short or numeric values would match ordinary data). A value containing a
    bracket, or inside the marker, is skipped so that redacting twice changes nothing."""
    return (len(value) >= _MIN_SECRET_LEN and not value.isdigit() and "[" not in value and "]" not in value
            and value not in REDACTED)


def _secrets(environ: dict[str, str] | None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    return {k: v for k, v in env.items() if _SECRET_NAME.search(k) and v and _is_secret_value(v)}


def secret_names(environ: dict[str, str] | None = None) -> list[str]:
    """The names (never the values) of the environment variables treated as secrets."""
    return sorted(_secrets(environ))


def secret_values(environ: dict[str, str] | None = None) -> list[str]:
    return sorted(set(_secrets(environ).values()), key=len, reverse=True)


def redact_structure(obj: Any, environ: dict[str, str] | None = None) -> tuple[Any, int]:
    """Replace secret values inside a JSON-like structure; returns (structure, replacements).

    Only strings are redacted (values and dict keys): each occurrence of a secret becomes
    ``[redacted]``. Numbers, booleans and null are never touched, so token counts and the
    results a replay re-renders stay intact. Working on parsed values keeps the output valid
    JSON, and it is a fixed point: redacting the result again changes nothing.
    """
    secrets = secret_values(environ)
    count = 0

    def text(value: str) -> str:
        nonlocal count
        for secret in secrets:
            if secret in value:
                count += value.count(secret)
                value = value.replace(secret, REDACTED)
        return value

    def walk(value: Any) -> Any:
        nonlocal count
        if isinstance(value, str):
            return text(value)
        if isinstance(value, dict):
            out: dict = {}
            for k, v in value.items():
                key = text(k) if isinstance(k, str) else k
                if key in out:
                    raise ValueError("two keys are the same once secret values are redacted")
                out[key] = walk(v)
            return out
        if isinstance(value, (list, tuple)):
            return [walk(v) for v in value]
        return value

    return walk(obj), count


def redact_secrets(text: str, environ: dict[str, str] | None = None) -> tuple[str, int]:
    """Replace secret values (raw or JSON-escaped) in plain ``text`` such as a log line;
    returns (text, replacements). For JSON documents use ``redact_structure``."""
    count = 0
    for value in secret_values(environ):
        for form in {value, json.dumps(value)[1:-1]}:
            if form in text:
                count += text.count(form)
                text = text.replace(form, REDACTED)
    return text, count


@dataclass
class RunRecord:
    run_id: str
    question_id: str
    base_question_id: str | None
    template: str | None
    split: str | None
    repetition: int
    condition: str
    model: str
    route: dict
    store: dict
    prompt: dict
    agent_config: dict
    harness: dict
    started_at: str
    ended_at: str
    wall_s: float
    transcript: list[dict]
    tool_calls: list[dict]
    tokens: dict
    outcome: dict
    final_rows: list | None
    score: dict | None = None
    replay_of: str | None = None
    runner: dict | None = None
    load: dict | None = None
    checker: dict | None = None
    schema_version: str = SCHEMA_VERSION
    # ``checker`` absent from a stored agent-run/15 to /17 record (read by from_json): one written by the runner's
    # branch, which never had the checker loop's field; not added when it is written back. Only that key, in only
    # those versions.
    absent_keys: tuple[str, ...] = dataclasses.field(default=(), repr=False, compare=False)

    def to_json(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        absent = data.pop("absent_keys")
        ordered = {"schema_version": data.pop("schema_version")}
        ordered.update(data)
        version = ordered["schema_version"]
        for name, added_in in _TOP_LEVEL_ADDED.items():
            if ordered.get(name) is None and (name in absent or (
                    version in READABLE_SCHEMAS and READABLE_SCHEMAS.index(version) < READABLE_SCHEMAS.index(added_in))):
                del ordered[name]  # the field did not exist in this record's version, or in this stored record
        return ordered

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "RunRecord":
        version = data.get("schema_version")
        if version not in READABLE_SCHEMAS:
            raise ValueError(f"unsupported run record schema {version!r} (readable: {', '.join(READABLE_SCHEMAS)})")
        names = {f.name for f in dataclasses.fields(cls)} - {"absent_keys"}
        unknown = set(data) - names
        if unknown:
            raise ValueError(f"unknown run record fields: {sorted(unknown)}")
        absent = ("checker",) if version in _CHECKER_OPTIONAL_IN and "checker" not in data else ()
        return cls(**data, absent_keys=absent)

    def dumps(self) -> str:
        """The record's text: JSON, indent 1, non-ASCII characters as they are, but an unpaired surrogate (which a
        model's reply can carry through JSON's \\u escapes, and UTF-8 cannot encode) as its \\u escape, so every
        record can be written as UTF-8 and reads back to the same value and the same text; a record without one is
        written exactly as before."""
        data, count = redact_structure(self.to_json())
        text = json.dumps(data, ensure_ascii=False, indent=1)
        text = _LONE_SURROGATE.sub(lambda m: f"\\u{ord(m.group()):04x}", text)
        if count:
            warnings.warn(f"run record {self.run_id}: redacted {count} secret value(s) before writing",
                          stacklevel=2)
        return text

    def write(self, out_dir: str | os.PathLike, *, prefix: str | None = None) -> Path:
        """Write ``<out_dir>/<run_id>.json`` atomically and return its path; with ``prefix``,
        ``<out_dir>/<prefix>.<run_id>.json`` (the pilot runner's claim digest of the attempt, so a
        reader can place the file without opening it; the run id stays in the record). The temporary file
        is ``<prefix>.<run_id>.<random>.tmp``, so one a crash leaves behind is placed by its name too
        (confirmation review of 1b7b7ec); without a prefix it is ``.<run_id>.<random>.tmp`` as before."""
        if prefix is not None and not _NAME_PREFIX.fullmatch(prefix):
            raise ValueError(f"a record file prefix is letters, digits, '-' and '_' only, got {prefix!r}")
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        target = out / (f"{prefix}.{self.run_id}.json" if prefix else f"{self.run_id}.json")
        text = self.dumps()
        fd, tmp = tempfile.mkstemp(dir=out, prefix=f"{prefix}.{self.run_id}." if prefix else f".{self.run_id}.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return target


def load_record(path: str | os.PathLike) -> RunRecord:
    return RunRecord.from_json(json.loads(Path(path).read_text(encoding="utf-8")))
