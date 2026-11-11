# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/adapters.py, blob dd36da0e1152c8fb450d51cc44f8600f145989b7
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
#   changes: see git history of this file from its import commit
"""Model adapters: one tool-call loop for every model, over each vendor's API directly.

OpenAI-compatible chat completions and the Anthropic Messages API are driven without an agent SDK, so the full
transcript and the per-turn usage are recorded the same way for every model. Replies are not streamed. Requests go
through `Retrier`, which turns the SDKs' own retries off so every retry is counted and recorded.

Per-backend tool-calling adaptation:
  * all backends: tool-call arguments that are not a JSON object are never resent; the history carries
    {"_raw": <original text>} and the model gets an error tool result saying so (a normal tool round).
  * openai_compat: native `tools`; seed, temperature and top_p honoured; other sampling keys and the vendor extras
    go in `extra_body`; reasoning text is recorded and, if the model entry sets `echo_reasoning_field`, sent back
    on the assistant message (B04).
  * anthropic: `tools` with `input_schema`; no seed, and only the sampling keys the entry sets; `thinking` and
    `output_config` come from `extra_body`; assistant content blocks (thinking included) are returned verbatim
    with tool results (B05).

Every request is built once, and the record's settings come from that request object (B15). Usage is counted the
moment a reply arrives and unreported counts stay None (B10). The provider's other numeric usage fields ride on the
turn as `usage_extra`, recorded only.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from expcore.config.models import ClientSettings, Limits, ModelSpec
from expcore.errors import InfraError
from expcore.models.retry import Retrier, RetryPolicy, SupportsUniform
from expcore.models.route import public_url, sdk_version, settings_sent
from expcore.models.served import Served, requested_from_sent
from expcore.models.usage import ANTHROPIC_COUNTED, OPENAI_COUNTED, UsageTally, extra_usage, token_count

PROBE_PROMPT = "ping"
# The probe of the reply maximum (C27): a request that asks for a reply far longer than the max_tokens it carries.
REPLY_CAP_PROBE_TOKENS = 16
REPLY_CAP_PROMPT = "Count from 1 to 500, one number per line, and write nothing else."
# Stop reasons that mean the reply was cut at the output limit: OpenAI-compatible "length", Anthropic "max_tokens".
OUTPUT_LIMIT_STOPS = frozenset({"length", "max_tokens"})


@dataclasses.dataclass
class ToolCall:
    """One tool call the model made."""

    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str | None = None  # set when the model sent arguments that are not a JSON object
    raw_arguments: str | None = None  # the model's original argument text, kept when it did not parse


@dataclasses.dataclass
class ToolResult:
    """What a tool call returned to the model."""

    call: ToolCall
    text: str  # exactly as delivered to the model, after capping
    is_error: bool = False


@dataclasses.dataclass(frozen=True)
class ProbeReply:
    """What the reply to an admission probe reported: the max_tokens the request carried, the reply's usage, a count
    the reply left out being None (B07), and the provider's stop reason, when read (C27)."""

    max_tokens: int
    input_tokens: int | None
    output_tokens: int | None
    stop_reason: str | None = None


@dataclasses.dataclass
class ModelTurn:
    """One reply of the model with its usage; a count the reply did not carry is None, never 0 (B10)."""

    text: str
    tool_calls: list[ToolCall]
    input_tokens: int | None
    output_tokens: int | None
    stop_reason: str | None  # the provider's own finish or stop reason (B12)
    latency_s: float
    reasoning: str | None = None
    # Claude prompt caching: input_tokens above is the whole prompt (the sum of these three parts);
    # None for backends that report no cache split
    uncached_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    served_model: str | None = None  # the id the server said it ran (B06)
    system_fingerprint: str | None = None
    retries: tuple[dict[str, Any], ...] = ()  # the retries this reply took, each with its error (B01)
    # The provider's other numeric usage fields (a credit count, cache or reasoning tokens) under its own names, recorded
    # only: no budget arithmetic reads them. A field the reply left out is absent, never 0; `usage_extra_cut` counts the
    # fields the cap left out (`expcore.models.usage.extra_usage`). None means not recorded (a stub, or a backend that
    # reads no such fields), and the record then has no field; an adapter that read a usage object gives {} and 0.
    usage_extra: dict[str, int | float] | None = None
    usage_extra_cut: int | None = None

    @property
    def hit_output_limit(self) -> bool:
        """Tell whether the reply was cut at the output limit, so it is not an answer (B12)."""
        return self.stop_reason in OUTPUT_LIMIT_STOPS


class ModelBackend(Protocol):
    """What the agent loop needs from a model: a conversation, one reply per step, a record of how it went."""

    @property
    def output_limit(self) -> int | None:
        """The max_tokens a request carries: the study's value or the model's own lower maximum (B11)."""
        ...

    @property
    def context_tokens(self) -> int | None:
        """The endpoint's context window in tokens, when known."""
        ...

    def info(self) -> dict[str, Any]:
        """Describe the route and what the replies reported, for the record."""
        ...

    def begin(self, system: str, user: str, tools: list[dict]) -> None:
        """Start a conversation."""
        ...

    async def step(self) -> ModelTurn:
        """Send the conversation and return the model's next reply."""
        ...

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Append the results of the last reply's tool calls to the conversation."""
        ...

    def transcript(self) -> list[dict]:
        """Return a copy of the conversation as sent, system prompt first."""
        ...

    async def probe(self) -> ProbeReply | None:
        """Send a one-token completion and return what its reply reported; raise when the model cannot serve one (B07).

        None says nothing of the usage, so admission refuses it as usage it cannot show is reported."""
        ...

    async def aclose(self) -> None:
        """Release the client."""
        ...


def _parse_args(raw: str | None) -> tuple[dict, str | None]:
    """Parse a tool call's argument text; return ({}, reason) when it is not a JSON object."""
    try:
        v = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return {}, f"arguments are not valid JSON ({e.msg})"
    if not isinstance(v, dict):
        return {}, "arguments are not a JSON object"
    return v, None


class _SdkBackend:
    """What the two SDK-driven adapters share: the retrier, the usage tally, what the server reported."""

    backend = ""

    def __init__(
        self,
        spec: ModelSpec,
        seed: int,
        limits: Limits,
        client_cfg: ClientSettings | None,
        base_url: str | None,
        sleep: Callable[[float], Awaitable[None]] | None,
        rng: SupportsUniform | None,
        time_left: Callable[[], float | None] | None,
    ):
        self.spec, self.seed, self.limits = spec, seed, limits
        self.cfg = client_cfg or ClientSettings()
        self.base_url = base_url or spec.base_url
        injected = {"sleep": sleep, "rng": rng, "time_left": time_left}
        self.retrier = Retrier(
            RetryPolicy.from_settings(self.cfg), **{k: v for k, v in injected.items() if v is not None}
        )
        self.usage = UsageTally()
        self.served = Served()
        self._sent: dict[str, Any] = {}  # the settings of the latest request, taken from the request itself

    def _remember_sent(self, request: dict[str, Any]) -> None:
        """Keep the settings of the request about to go out, and tell the served-model check which ones they are."""
        self._sent = settings_sent(request)
        self.served.expect(requested_from_sent(self._sent))

    @property
    def output_limit(self) -> int:
        """The max_tokens a request carries (B11)."""
        return self.spec.output_tokens(self.limits.max_output_tokens)

    @property
    def context_tokens(self) -> int | None:
        """The endpoint's context window, as the model table gives it."""
        return self.spec.context_tokens

    def abandon(self, reason: str) -> None:
        """The run's deadline cancelled the request in flight; its usage is unknown."""
        self.retrier.abandon(reason)

    def _read_timeout_s(self) -> float:
        """The read timeout: never shorter than the run's time cap, so it only fires when the run's time is gone."""
        return max(self.cfg.request_timeout_s, self.limits.time_cap_s + 60)

    def _info(self, sdk: str, **extra: Any) -> dict[str, Any]:
        """The fields of the record both adapters share."""
        stats = self.retrier.stats
        return {
            "backend": self.backend,
            "model": self.spec.api_id,
            "base_url": public_url(self.base_url),
            "sdk_versions": {sdk: sdk_version(sdk)},
            "seed": self.seed,
            "seed_honoured": self.spec.seed_supported,
            "settings_sent": dict(self._sent),
            "retries": stats.retries,
            "retry_events": list(self.retrier.events),
            "read_timeouts": stats.read_timeouts,
            "abandoned_request": self.retrier.abandoned,
            "rate_limited_429": stats.rate_limited,
            "usage_seen": self.usage.as_dict(),
            **extra,
            **self.served.info(),
        }

    def _count(self, input_tokens: int | None, output_tokens: int | None) -> None:
        """Count a reply's usage the moment it arrives, and refuse a reply that carried none: its tokens are unknown."""
        self.usage.note(input_tokens, output_tokens)
        if input_tokens is None and output_tokens is None:
            raise InfraError("endpoint", "reply carried no usage: its tokens are unknown", billed=(None, None))


class OpenAICompatAdapter(_SdkBackend):
    """An OpenAI-compatible endpoint (vLLM, a gateway, the vendor itself) named by a base URL."""

    backend = "openai_compat"

    def __init__(
        self,
        spec: ModelSpec,
        seed: int,
        limits: Limits,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client_cfg: ClientSettings | None = None,
        client: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: SupportsUniform | None = None,
        time_left: Callable[[], float | None] | None = None,
    ):
        super().__init__(spec, seed, limits, client_cfg, base_url, sleep, rng, time_left)
        if client is None:
            from openai import AsyncOpenAI, Timeout

            client = AsyncOpenAI(
                base_url=self.base_url,
                # an explicit key keeps the SDK from falling back to a key in the environment
                api_key=api_key or "none",
                timeout=Timeout(self._read_timeout_s(), connect=self.cfg.connect_timeout_s),
                max_retries=0,
            )
        self._client = client
        self._messages: list[dict] = []
        self._tools: list[dict] = []

    def info(self) -> dict[str, Any]:
        """Describe the route and what the replies reported."""
        return self._info("openai")

    def begin(self, system: str, user: str, tools: list[dict]) -> None:
        """Start a conversation with the tools as chat-completion functions."""
        self._messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": user}
        ]
        self._tools = [
            {
                "type": "function",
                "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]},
            }
            for t in tools
        ]

    def _extra_body(self) -> dict[str, Any]:
        """The vendor extras plus the sampling keys the API takes only through `extra_body` (B14)."""
        direct = {"temperature", "top_p"}
        return {**{k: v for k, v in self.spec.sampling.items() if k not in direct}, **self.spec.extra_body}

    def _request(self) -> dict:
        """Build the request, and remember its settings for the record (B15)."""
        kw: dict[str, Any] = {
            "model": self.spec.api_id,
            "messages": self._wire_messages(),
            "seed": self.seed,
            "max_tokens": self.output_limit,
        }
        if self._tools:
            kw.update(tools=self._tools, tool_choice="auto")
        kw.update({k: v for k, v in self.spec.sampling.items() if k in ("temperature", "top_p")})
        if extra := self._extra_body():
            kw["extra_body"] = extra
        self._remember_sent(kw)
        return kw

    async def step(self) -> ModelTurn:
        """Send the conversation and return the reply; usage and the served model are noted on arrival."""
        t0 = time.monotonic()
        kw = self._request()
        resp, retries = await self.retrier.call(lambda: self._client.chat.completions.create(**kw))
        usage = getattr(
            resp, "usage", None
        )  # counted the moment the reply arrives: a reply is billed even if reading it fails
        in_tok, out_tok = token_count(usage, "prompt_tokens"), token_count(usage, "completion_tokens")
        self.served.note(resp)
        self._count(in_tok, out_tok)
        usage_extra, usage_cut = extra_usage(usage, skip=OPENAI_COUNTED)
        choices = getattr(resp, "choices", None)
        if not isinstance(choices, (list, tuple)) or not choices:
            raise InfraError(
                "endpoint",
                f"reply carried no choices ({type(choices).__name__}); usage already counted",
                billed=(in_tok, out_tok),
            )
        choice = choices[0]
        msg = choice.message
        extra = getattr(msg, "model_extra", None) or {}
        reasoning = extra.get("reasoning_content") or extra.get("reasoning")
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = token_count(details, "reasoning_tokens")
        self.served.note_thinking(bool(reasoning), reasoning_tokens)
        entry: dict[str, Any] = {"role": "assistant", "content": msg.content or None}
        if reasoning:
            entry["_reasoning"] = reasoning  # recorded; stripped before sending unless echoed
            if self.spec.echo_reasoning_field:
                entry[self.spec.echo_reasoning_field] = reasoning
        calls: list[ToolCall] = []
        if msg.tool_calls:
            entry["tool_calls"] = []
            for tc in msg.tool_calls:
                raw = tc.function.arguments
                args, err = _parse_args(raw)
                calls.append(ToolCall(tc.id, tc.function.name, args, err, raw if err else None))
                # Malformed arguments are never sent back (servers reject the history with HTTP 400): the
                # history carries a valid JSON form that preserves the raw text. Same for every backend.
                sent = json.dumps({"_raw": raw}) if err else (raw or "{}")
                entry["tool_calls"].append(
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": sent}}
                )
        self._messages.append(entry)
        fingerprint = getattr(resp, "system_fingerprint", None)
        return ModelTurn(
            text=msg.content or "",
            tool_calls=calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            stop_reason=choice.finish_reason,
            latency_s=time.monotonic() - t0,
            reasoning=reasoning,
            served_model=str(resp.model) if getattr(resp, "model", None) else None,
            system_fingerprint=str(fingerprint) if fingerprint else None,
            retries=retries,
            usage_extra=usage_extra,
            usage_extra_cut=usage_cut,
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Append one tool message per result."""
        for r in results:
            self._messages.append({"role": "tool", "tool_call_id": r.call.id, "content": r.text})

    async def probe(self) -> ProbeReply:
        """Send a one-token completion with the model's id and its vendor extras (a provider pin applies to it too)."""
        kw: dict[str, Any] = {
            "model": self.spec.api_id,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
            "max_tokens": 1,
        }
        if extra := self._extra_body():
            kw["extra_body"] = extra
        resp, _ = await self.retrier.call(lambda: self._client.chat.completions.create(**kw))
        if not getattr(resp, "choices", None):
            raise InfraError("endpoint", "the one-token completion carried no choices")
        usage = getattr(resp, "usage", None)  # reported, never tallied: the probe is no part of a run
        return ProbeReply(1, token_count(usage, "prompt_tokens"), token_count(usage, "completion_tokens"))

    async def probe_reply_cap(self) -> ProbeReply:
        """Ask for a long reply under a small max_tokens, with the vendor extras, and return what came back (C27)."""
        kw: dict[str, Any] = {
            "model": self.spec.api_id,
            "messages": [{"role": "user", "content": REPLY_CAP_PROMPT}],
            "max_tokens": REPLY_CAP_PROBE_TOKENS,
        }
        if extra := self._extra_body():
            kw["extra_body"] = extra
        resp, _ = await self.retrier.call(lambda: self._client.chat.completions.create(**kw))
        choices = getattr(resp, "choices", None)
        if not choices:
            raise InfraError("endpoint", "the probe of the reply maximum carried no choices")
        usage = getattr(resp, "usage", None)
        counts = token_count(usage, "prompt_tokens"), token_count(usage, "completion_tokens")
        return ProbeReply(REPLY_CAP_PROBE_TOKENS, *counts, getattr(choices[0], "finish_reason", None))

    def _wire_messages(self) -> list[dict]:
        """The conversation as sent: the internal copy of the reasoning is stripped."""
        return [{k: v for k, v in m.items() if k != "_reasoning"} for m in self._messages]

    def transcript(self) -> list[dict]:
        """Return the conversation including the recorded reasoning."""
        return json.loads(json.dumps(self._messages, default=str))

    async def aclose(self) -> None:
        """Close the client."""
        await self._client.close()


class AnthropicAdapter(_SdkBackend):
    """The Anthropic Messages API. No seed exists; recorded as not honoured."""

    backend = "anthropic"

    def __init__(
        self,
        spec: ModelSpec,
        seed: int,
        limits: Limits,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        client_cfg: ClientSettings | None = None,
        client: Any = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        rng: SupportsUniform | None = None,
        time_left: Callable[[], float | None] | None = None,
    ):
        super().__init__(spec, seed, limits, client_cfg, base_url, sleep, rng, time_left)
        if client is None:
            from anthropic import AsyncAnthropic, Timeout

            client = AsyncAnthropic(
                api_key=api_key,
                base_url=self.base_url,
                timeout=Timeout(self._read_timeout_s(), connect=self.cfg.connect_timeout_s),
                max_retries=0,  # retries are ours
            )
        self._client = client
        self._system = ""
        self._messages: list[dict] = []
        self._tools: list[dict] = []

    @property
    def _cache_control(self) -> dict[str, str] | None:
        """The cache marker, or None when the model entry sets no prompt cache."""
        ttl = self.spec.prompt_cache_ttl
        return {"type": "ephemeral", "ttl": ttl} if ttl else None

    def info(self) -> dict[str, Any]:
        """Describe the route and what the replies reported."""
        return self._info("anthropic", prompt_cache=self._cache_control)

    def begin(self, system: str, user: str, tools: list[dict]) -> None:
        """Start a conversation with the tools as Messages API tools."""
        self._system = system
        self._messages = [{"role": "user", "content": user}]
        self._tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]} for t in tools
        ]

    def _request(self) -> dict:
        """Build the request, and remember its settings for the record (B15)."""
        kw: dict[str, Any] = {
            "model": self.spec.api_id,
            "messages": list(self._messages),
            "max_tokens": self.output_limit,
        }
        if self._tools:
            kw["tools"] = self._tools
        cache = self._cache_control
        if self._system:
            # the system prompt is always one text block (the same model input as a string)
            block: dict[str, Any] = {"type": "text", "text": self._system}
            if cache:
                block["cache_control"] = dict(cache)
            kw["system"] = [block]
        kw.update(self.spec.sampling)  # empty where the vendor says to omit them
        if self.spec.extra_body:
            kw["extra_body"] = dict(self.spec.extra_body)
        if cache:
            kw["cache_control"] = dict(cache)  # history is never touched: markers live only in the request
        self._remember_sent(kw)
        return kw

    async def step(self) -> ModelTurn:
        """Send the conversation and return the reply; usage and the served model are noted on arrival."""
        t0 = time.monotonic()
        kw = self._request()
        resp, retries = await self.retrier.call(lambda: self._client.messages.create(**kw))
        ua = getattr(resp, "usage", None)  # counted the moment the reply arrives (see the OpenAI adapter)
        fresh = token_count(ua, "input_tokens")  # the API's input_tokens is only the uncached remainder
        created = token_count(ua, "cache_creation_input_tokens")
        read = token_count(ua, "cache_read_input_tokens")
        in_tok, out_tok = _anthropic_counts(ua)  # the prompt size the budget counts, cached parts included
        self.served.note(resp)
        self._count(in_tok, out_tok)
        usage_extra, usage_cut = extra_usage(ua, skip=ANTHROPIC_COUNTED)
        if not isinstance(getattr(resp, "content", None), (list, tuple)):
            raise InfraError(
                "endpoint", "reply carried no content blocks; usage already counted", billed=(in_tok, out_tok)
            )
        text, calls, reasoning = [], [], []
        thinking_types = ("thinking", "redacted_thinking")
        self.served.note_thinking(any(getattr(b, "type", None) in thinking_types for b in resp.content))
        for b in resp.content:
            if b.type == "text":
                text.append(b.text)
            elif b.type == "tool_use":
                ok = isinstance(b.input, dict)
                raw = None if ok else json.dumps(b.input, default=str)
                calls.append(
                    ToolCall(
                        b.id, b.name, b.input if ok else {}, None if ok else "arguments are not a JSON object", raw
                    )
                )
            elif b.type in thinking_types:
                reasoning.append(getattr(b, "thinking", "") or "")
        # the assistant turn goes back verbatim (thinking blocks included, signatures with them)
        blocks = [b.model_dump(exclude_none=True) for b in resp.content]
        for blk in blocks:  # same rule as every backend: never resend a non-object input
            if blk.get("type") == "tool_use" and not isinstance(blk.get("input"), dict):
                blk["input"] = {"_raw": json.dumps(blk.get("input"), default=str)}
        self._messages.append({"role": "assistant", "content": blocks})
        return ModelTurn(
            text="".join(text),
            tool_calls=calls,
            input_tokens=in_tok,
            output_tokens=out_tok,
            stop_reason=resp.stop_reason,
            latency_s=time.monotonic() - t0,
            reasoning="\n".join(r for r in reasoning if r) or None,
            uncached_input_tokens=fresh,
            cache_creation_input_tokens=created,
            cache_read_input_tokens=read,
            served_model=str(resp.model) if getattr(resp, "model", None) else None,
            retries=retries,
            usage_extra=usage_extra,
            usage_extra_cut=usage_cut,
        )

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Append one user message holding every result, as the Messages API wants."""
        self._messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r.call.id,
                        "content": r.text,
                        **({"is_error": True} if r.is_error else {}),
                    }
                    for r in results
                ],
            }
        )

    async def probe(self) -> ProbeReply:
        """Send a one-token completion. The vendor extras are left out: extended thinking needs a larger budget."""
        kw = {"model": self.spec.api_id, "messages": [{"role": "user", "content": PROBE_PROMPT}], "max_tokens": 1}
        resp, _ = await self.retrier.call(lambda: self._client.messages.create(**kw))
        if not isinstance(getattr(resp, "content", None), (list, tuple)):
            raise InfraError("endpoint", "the one-token completion carried no content blocks")
        return ProbeReply(1, *_anthropic_counts(getattr(resp, "usage", None)))

    async def probe_reply_cap(self) -> ProbeReply:
        """Ask for a long reply under a small max_tokens, without the vendor extras as the probe above, and return what
        came back (C27)."""
        kw = {
            "model": self.spec.api_id,
            "messages": [{"role": "user", "content": REPLY_CAP_PROMPT}],
            "max_tokens": REPLY_CAP_PROBE_TOKENS,
        }
        resp, _ = await self.retrier.call(lambda: self._client.messages.create(**kw))
        if not isinstance(getattr(resp, "content", None), (list, tuple)):
            raise InfraError("endpoint", "the probe of the reply maximum carried no content blocks")
        counts = _anthropic_counts(getattr(resp, "usage", None))
        return ProbeReply(REPLY_CAP_PROBE_TOKENS, *counts, getattr(resp, "stop_reason", None))

    def transcript(self) -> list[dict]:
        """Return the conversation with the system prompt first; on the wire it travels as a separate parameter."""
        full = ([{"role": "system", "content": self._system}] if self._system else []) + self._messages
        return json.loads(json.dumps(full, default=str))

    async def aclose(self) -> None:
        """Close the client."""
        await self._client.close()


def _anthropic_counts(usage: Any) -> tuple[int | None, int | None]:
    """The whole prompt (the uncached remainder the API calls input_tokens, plus both cache parts) and the output of a
    Messages API usage object; a count it left out is None."""
    fresh = token_count(usage, "input_tokens")
    created, read = token_count(usage, "cache_creation_input_tokens"), token_count(usage, "cache_read_input_tokens")
    whole = None if fresh is None else fresh + (created or 0) + (read or 0)
    return whole, token_count(usage, "output_tokens")


class StubAdapter:
    """Scripted backend for tests and the oracle: replays a list of turns.

    Each item is a final text (str), a list of (tool_name, arguments) calls, a dict {"text": ..., "stop": ...} for a
    reply with a given stop reason, a ready-made `ModelTurn` (for a call with malformed arguments, or usage the reply
    did not report), or an exception, which `step` raises. Token counts of the other items are synthetic (the length
    of the text or arguments), and recorded as such in info().

    The backend of a campaign run is built by the scheduler from the model table, so its script is set afterwards by
    the study's executor, with `use`.
    """

    def __init__(
        self,
        script: list,
        name: str = "stub",
        seed: int = 0,
        *,
        output_limit: int | None = None,
        context_tokens: int | None = None,
        probe_error: BaseException | None = None,
    ):
        self._script, self._i, self.name, self.seed = list(script), 0, name, seed
        self.output_limit, self.context_tokens, self._probe_error = output_limit, context_tokens, probe_error
        self._messages: list[dict] = []
        self._ctx_tokens = 0

    def use(self, script: list) -> None:
        """Replace the script and start it from its first item; the conversation so far is not touched."""
        self._script, self._i = list(script), 0

    def info(self) -> dict[str, Any]:
        """Describe the stub as a route."""
        return {
            "backend": "stub",
            "model": self.name,
            "base_url": None,
            "seed": self.seed,
            "seed_honoured": False,
            "settings_sent": {},
            "synthetic_tokens": True,
        }

    def begin(self, system: str, user: str, tools: list[dict]) -> None:
        """Start a conversation."""
        self._messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": user}
        ]
        self._ctx_tokens = (len(system) + len(user)) // 4

    async def step(self) -> ModelTurn:
        """Return the next scripted reply; "done" once the script is used up."""
        item = self._script[self._i] if self._i < len(self._script) else "done"
        self._i += 1
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, ModelTurn):
            self._messages.append(self._assistant_message(item))
            return item
        if isinstance(item, dict):  # {"text": ..., "stop": "length"}: a reply with a given stop reason
            self._messages.append({"role": "assistant", "content": item["text"]})
            return ModelTurn(item["text"], [], self._ctx_tokens, len(item["text"]) // 4 + 1, item["stop"], 0.0)
        if isinstance(item, str):
            self._messages.append({"role": "assistant", "content": item})
            return ModelTurn(item, [], self._ctx_tokens, len(item) // 4 + 1, "stop", 0.0)
        calls = [ToolCall(f"call_{self._i}_{k}", n, a) for k, (n, a) in enumerate(item)]
        self._messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                    for c in calls
                ],
            }
        )
        out = sum(len(json.dumps(c.arguments)) for c in calls) // 4 + 1
        turn = ModelTurn("", calls, self._ctx_tokens, out, "tool_calls", 0.0)
        self._ctx_tokens += out
        return turn

    @staticmethod
    def _assistant_message(turn: ModelTurn) -> dict[str, Any]:
        """The assistant message a ready-made turn adds to the history.

        Adapters never resend malformed arguments, so the history of a malformed call holds valid JSON with the raw
        text under `_raw`.
        """
        entry: dict[str, Any] = {"role": "assistant", "content": turn.text or None}
        if turn.reasoning:
            entry["_reasoning"] = turn.reasoning
        if turn.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {
                        "name": c.name,
                        "arguments": json.dumps({"_raw": c.raw_arguments} if c.parse_error else c.arguments),
                    },
                }
                for c in turn.tool_calls
            ]
        return entry

    def add_tool_results(self, results: list[ToolResult]) -> None:
        """Append one tool message per result."""
        for r in results:
            self._messages.append({"role": "tool", "tool_call_id": r.call.id, "content": r.text})
            self._ctx_tokens += len(r.text) // 4

    def transcript(self) -> list[dict]:
        """Return a copy of the conversation."""
        return json.loads(json.dumps(self._messages, default=str))

    async def probe(self) -> ProbeReply:
        """Succeed with a synthetic one-token reply, as the stub's counts are synthetic, or raise the error it was given."""
        if self._probe_error is not None:
            raise self._probe_error
        return ProbeReply(1, 1, 1)

    async def aclose(self) -> None:
        """Nothing to release."""
        return None
