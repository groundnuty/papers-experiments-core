# Provenance:
#   source: icsa-2027-experiments @ 6fc798cf6ef196700854d6ff3ec0d353da030e85, icsa/harness/adapters.py, blob dd36da0e1152c8fb450d51cc44f8600f145989b7
#   import: sanitised with study-token map 60f68a1c2af4b0fc87651bf88bc2e52f8aed546687dd3099f912a69a027ddf59
"""Model adapters: one harness-owned tool-call loop for every model.

Both vendors' APIs are driven directly (OpenAI-compatible chat completions for the vLLM models,
the Anthropic Messages API for Claude), so the full transcript and per-turn usage are recorded
the same way for every model. The dataset_77 base used claude-agent-sdk on a subscription for the
Claude leg, which loses the raw trace; that leg is deliberately not carried over.

Per-model tool-calling adaptation (documented in docs/harness.md):
  * all models: tool-call arguments that are not a JSON object are never resent; the history carries
    {"_raw": <original text>} and the model gets an error tool result saying so (a normal tool round).
  * openai_compat: native `tools`; seed, temperature, top_p honoured; other sampling keys and the
    chat-template kwargs go in `extra_body`; reasoning text is recorded and, if the model entry
    sets `echo_reasoning_field`, sent back on the assistant message.
  * anthropic: `tools` with `input_schema`; no seed, temperature, top_p or top_k are sent (the
    vendor requires them omitted); `thinking` and `output_config` come from `request_extra`;
    assistant content blocks (thinking included) are returned verbatim with tool results.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from typing import Any, Protocol

from .config import ClientSettings, Limits, ModelSpec
from dataset_76.servedsettings import Served

from .errors import InfraError, RequestAbandoned


def key_rejected(e: BaseException) -> InfraError:
    """A 401 or 403 from a model API. 401 is a refused key (wrong, revoked, expired). 403 is a key that is known but not allowed this resource (Anthropic's permission_error;
    an OpenAI-compatible gateway's forbidden): for the study either way no run of this model can succeed until someone fixes the key, so both hold the model. The detail is
    the status and the exception class only: the SDK's message can echo part of a key (OpenAI's does), and a key is never recorded."""
    return InfraError("key_rejected", f"HTTP {getattr(e, 'status_code', '?')} {type(e).__name__}")


CACHE_CONTROL = {"type": "ephemeral", "ttl": "1h"}   # Claude only, identical in every option and layout


@dataclasses.dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str | None = None   # set when the model sent arguments that are not a JSON object
    raw_arguments: str | None = None  # the model's original argument text, kept when it did not parse


@dataclasses.dataclass
class ToolResult:
    call: ToolCall
    text: str          # exactly as delivered to the model, after capping
    is_error: bool = False


@dataclasses.dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    latency_s: float
    reasoning: str | None = None
    # Claude prompt caching: input_tokens above is the whole prompt (the sum of these three parts);
    # None / 0 for models that report no cache split
    uncached_input_tokens: int | None = None
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class ModelAdapter(Protocol):
    def info(self) -> dict[str, Any]: ...
    def begin(self, system: str, user: str, tools: list[dict]) -> None: ...
    async def step(self) -> ModelTurn: ...
    def add_tool_results(self, results: list[ToolResult]) -> None: ...
    def transcript(self) -> list[dict]: ...
    async def aclose(self) -> None: ...


def _is_read_timeout(e: BaseException) -> bool:
    """A timeout after the request was sent: the endpoint accepted it and the response is slow. Slowness
    is never infra; the run keeps waiting within its remaining wall time and ends as time_cap. A connect
    timeout (no connection was made) is a connection failure and stays infra. Matched by class name so it
    holds for whichever httpx package the SDKs use."""
    cause = e.__cause__ or e.__context__
    names = {c.__name__ for c in type(cause).__mro__} if cause is not None else set()
    return "ReadTimeout" in names          # pool, write and connect timeouts are failures to reach the endpoint


def _timeout(total_s: float):
    """Long read timeout (the run's wall time), short connect timeout so a dead host fails fast as infra."""
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    return httpx.Timeout(float(total_s), connect=10.0)


def _parse_args(raw: str | None) -> tuple[dict, str | None]:
    try:
        v = json.loads(raw or "{}")
    except json.JSONDecodeError as e:
        return {}, f"arguments are not valid JSON ({e.msg})"
    if not isinstance(v, dict):
        return {}, "arguments are not a JSON object"
    return v, None


class OpenAICompatAdapter:
    """vLLM (or any OpenAI-compatible) endpoint, pinned to one slot for the whole session."""

    def __init__(self, spec: ModelSpec, base_url: str, slot: str, seed: int, limits: Limits,
                 client_cfg: ClientSettings, client: Any = None, api_key: str = "none", backend: str = "hpc"):
        from openai import AsyncOpenAI
        self.spec, self.base_url, self.slot, self.seed = spec, base_url, slot, seed
        self.backend = backend
        self._api_key = api_key
        self.fingerprints: list[str] = []
        self.rate_limited = 0                       # 429 answers met (retried as registered); reported per run
        self.usage_seen = {"responses": 0, "input": 0, "output": 0}   # from every reply as it arrives, before any parsing can raise
        self.usage_missing = 0                      # replies that carried no usage: their tokens are unknown
        self.served = Served()                      # what the replies reported (model id and settings; not the requested values)
        self.limits, self.cfg = limits, client_cfg
        self._client = client or AsyncOpenAI(
            base_url=base_url, api_key=api_key,
            # a read timeout must mean "the run's wall time is gone", so it is never shorter than the time cap
            timeout=_timeout(max(client_cfg.request_timeout_s, limits.time_cap_s + 60)), max_retries=0)
        self._messages: list[dict] = []
        self._tools: list[dict] = []
        self.retries = 0
        self.read_timeouts = 0
        self.abandoned: dict | None = None

    def info(self) -> dict[str, Any]:
        return {"model_api_id": self.spec.api_id, "checkpoint": self.spec.checkpoint,
                "endpoint": self.base_url, "slot": self.slot, "seed": self.seed,
                "seed_honoured": True, "sampling": dict(self.spec.sampling),
                "request_extra": self.spec.request_extra, "retries": self.retries, "read_timeouts": self.read_timeouts, "abandoned_request": self.abandoned,
                "backend": self.backend, "system_fingerprints": sorted(set(self.fingerprints)), **self.served.info(),
                "rate_limited_429": self.rate_limited, "usage_seen": dict(self.usage_seen), "usage_missing": self.usage_missing}

    def abandon(self, reason: str) -> None:
        """The run's deadline cancelled the request in flight; its usage is unknown."""
        self.abandoned = {"reason": reason, "usage": "unknown"}

    def begin(self, system: str, user: str, tools: list[dict]) -> None:
        self._messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
        self._tools = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
            for t in tools]

    def _request(self) -> dict:
        s = dict(self.spec.sampling)
        kw: dict[str, Any] = {"model": self.spec.api_id, "messages": self._wire_messages(),
                              "tools": self._tools, "tool_choice": "auto", "seed": self.seed,
                              "max_tokens": self.spec.output_tokens(self.limits.max_output_tokens)}
        for k in ("temperature", "top_p"):
            if k in s:
                kw[k] = s.pop(k)
        extra = {**s, **self.spec.request_extra}
        if extra:
            kw["extra_body"] = extra
        return kw

    async def _create(self, kw: dict):
        import openai
        transient = (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError,
                     openai.RateLimitError)
        attempt = 0
        while True:
            try:
                return await self._client.chat.completions.create(**kw)
            except asyncio.CancelledError:             # the run's deadline cancelled a request in flight
                self.abandon("deadline")
                raise
            except (openai.AuthenticationError, openai.PermissionDeniedError) as e:
                raise key_rejected(e) from None        # not retried, not the agent's: the key is refused (never the SDK's message, which can echo part of it)
            except openai.APITimeoutError as e:
                if _is_read_timeout(e):
                    self.read_timeouts += 1            # accepted, then slow: abandon it, never resend
                    self.abandoned = {"reason": "read_timeout", "usage": "unknown"}
                    raise RequestAbandoned("read timeout") from e
                if attempt >= self.cfg.connect_retries:
                    raise InfraError("endpoint", f"{type(e).__name__} (connect) after {attempt} retries: {e}"[:500]) from e
                self.retries += 1
                await asyncio.sleep(min(self.cfg.retry_base_s * 2 ** attempt, self.cfg.retry_max_s))
                attempt += 1
            except transient as e:
                if isinstance(e, openai.RateLimitError):
                    self.rate_limited += 1
                if attempt >= self.cfg.connect_retries:
                    raise InfraError("endpoint", f"{type(e).__name__} after {attempt} retries: {e}"[:500]) from e
                self.retries += 1
                await asyncio.sleep(min(self.cfg.retry_base_s * 2 ** attempt, self.cfg.retry_max_s))
                attempt += 1

    async def step(self) -> ModelTurn:
        t0 = time.monotonic()
        resp = await self._create(self._request())
        u0 = getattr(resp, "usage", None)               # counted the moment the reply arrives: a reply is billed even if reading it raises
        self.usage_seen["responses"] += 1
        if u0 is None:
            self.usage_missing += 1
        else:
            self.usage_seen["input"] += getattr(u0, "prompt_tokens", 0) or 0
            self.usage_seen["output"] += getattr(u0, "completion_tokens", 0) or 0
        if u0 is None:                                  # a reply with no usage cannot be held to the token budget
            raise InfraError("endpoint", "reply carried no usage: its tokens are unknown")
        choices = getattr(resp, "choices", None)
        if not isinstance(choices, (list, tuple)) or not choices:
            raise InfraError("endpoint", f"reply carried no choices ({type(choices).__name__}); usage already counted")
        msg = choices[0].message
        extra = getattr(msg, "model_extra", None) or {}
        reasoning = extra.get("reasoning_content") or extra.get("reasoning")
        calls: list[ToolCall] = []
        entry: dict[str, Any] = {"role": "assistant", "content": msg.content or None}
        details = getattr(getattr(resp, "usage", None), "completion_tokens_details", None)
        rtok = getattr(details, "reasoning_tokens", None)
        self.served.note_thinking(bool(reasoning), rtok if isinstance(rtok, int) else None)
        if reasoning:
            entry["_reasoning"] = reasoning          # recorded; stripped before sending unless echoed
            if self.spec.echo_reasoning_field:
                entry[self.spec.echo_reasoning_field] = reasoning
        if msg.tool_calls:
            entry["tool_calls"] = []
            for tc in msg.tool_calls:
                raw = tc.function.arguments
                args, err = _parse_args(raw)
                calls.append(ToolCall(tc.id, tc.function.name, args, err, raw if err else None))
                # Malformed arguments are never sent back (servers reject the history with HTTP 400): the
                # history carries a valid JSON form that preserves the raw text. Same for every model.
                sent = json.dumps({"_raw": raw}) if err else (raw or "{}")
                entry["tool_calls"].append({"id": tc.id, "type": "function", "function": {
                    "name": tc.function.name, "arguments": sent}})
        self._messages.append(entry)
        self.served.note(resp)
        if getattr(resp, "system_fingerprint", None):
            self.fingerprints.append(str(resp.system_fingerprint))
        u = resp.usage
        return ModelTurn(msg.content or "", calls, getattr(u, "prompt_tokens", 0) or 0,
                         getattr(u, "completion_tokens", 0) or 0, resp.choices[0].finish_reason,
                         time.monotonic() - t0, reasoning)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            self._messages.append({"role": "tool", "tool_call_id": r.call.id, "content": r.text})

    async def count_tokens(self, text: str) -> int:
        """vLLM's /tokenize (served beside /v1): the model's own token count of a text."""
        try:
            import httpx2 as httpx
        except ImportError:
            import httpx
        root = self.base_url.rsplit("/v1", 1)[0]
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{root}/tokenize", json={"model": self.spec.api_id, "prompt": text},
                             headers={"Authorization": f"Bearer {self._api_key}"})
            r.raise_for_status()
            return int(r.json()["count"])

    def _wire_messages(self) -> list[dict]:
        return [{k: v for k, v in m.items() if k != "_reasoning"} for m in self._messages]

    def transcript(self) -> list[dict]:
        return json.loads(json.dumps(self._messages, default=str))

    async def aclose(self) -> None:
        await self._client.close()


class AnthropicAdapter:
    """Anthropic Messages API, harness-owned loop. No seed exists; recorded as not honoured."""

    def __init__(self, spec: ModelSpec, seed: int, limits: Limits, api_key: str, client: Any = None,
                 client_cfg: ClientSettings | None = None, prompt_cache: bool = True):
        import anthropic
        self.spec, self.seed, self.limits = spec, seed, limits
        self.prompt_cache = prompt_cache
        # the read timeout is at least the run's wall-time cap, so it only fires when the time is gone
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, max_retries=0, timeout=_timeout(limits.time_cap_s + 60))   # retries are ours, below
        self.read_timeouts = 0
        self.served = Served()
        self.rate_limited = 0
        self.usage_seen = {"responses": 0, "input": 0, "output": 0}
        self.usage_missing = 0
        self.abandoned: dict | None = None
        self.retries = 0
        self.cfg = client_cfg or ClientSettings("", 0, 8, 5.0, 120.0)
        self._system = ""
        self._messages: list[dict] = []
        self._tools: list[dict] = []

    def info(self) -> dict[str, Any]:
        return {"model_api_id": self.spec.api_id, "checkpoint": self.spec.checkpoint,
                "endpoint": "https://api.anthropic.com", "slot": None, "seed": self.seed,
                "seed_honoured": False, "sampling": dict(self.spec.sampling),
                "request_extra": self.spec.request_extra, "read_timeouts": self.read_timeouts,
                "abandoned_request": self.abandoned, "retries": self.retries,
                "prompt_cache": dict(CACHE_CONTROL) if self.prompt_cache else None, **self.served.info(),
                "rate_limited_429": self.rate_limited, "usage_seen": dict(self.usage_seen), "usage_missing": self.usage_missing}

    def abandon(self, reason: str) -> None:
        """The run's deadline cancelled the request in flight; its usage is unknown."""
        self.abandoned = {"reason": reason, "usage": "unknown"}

    def begin(self, system: str, user: str, tools: list[dict]) -> None:
        self._system = system
        self._messages = [{"role": "user", "content": user}]
        self._tools = [{"name": t["name"], "description": t["description"],
                        "input_schema": t["input_schema"]} for t in tools]

    async def step(self) -> ModelTurn:
        t0 = time.monotonic()
        # the system prompt is always one text block (same model input as a string); the caching arm adds
        # the marker to it and a top-level marker that follows the end of the conversation
        sys_block: dict[str, Any] = {"type": "text", "text": self._system}
        if self.prompt_cache:
            sys_block["cache_control"] = dict(CACHE_CONTROL)
        kw: dict[str, Any] = {"model": self.spec.api_id, "system": [sys_block],
                              "messages": self._messages, "tools": self._tools,
                              "max_tokens": self.spec.output_tokens(self.limits.max_output_tokens)}
        kw.update(self.spec.sampling)              # empty for the 5.5 models: vendor says omit
        if self.spec.request_extra:
            kw["extra_body"] = self.spec.request_extra
        if not self._system:
            kw.pop("system")                      # the protocol prompt is a single user message
        if self.prompt_cache:
            kw["cache_control"] = dict(CACHE_CONTROL)   # history is never touched: markers live only in the request
        import anthropic
        attempt = 0
        while True:
            try:
                resp = await self._client.messages.create(**kw)   # the SDK does not retry (max_retries=0)
                break
            except asyncio.CancelledError:                        # the run's deadline cancelled a request in flight
                self.abandon("deadline")
                raise
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
                raise key_rejected(e) from None                   # not retried, not the agent's (see the OpenAI adapter)
            except anthropic.APITimeoutError as e:
                if _is_read_timeout(e):
                    self.read_timeouts += 1                       # accepted, then slow: abandon, never resend
                    self.abandoned = {"reason": "read_timeout", "usage": "unknown"}
                    raise RequestAbandoned("read timeout") from e
                err = e
            except (anthropic.APIConnectionError, anthropic.InternalServerError, anthropic.RateLimitError) as e:
                if isinstance(e, anthropic.RateLimitError):
                    self.rate_limited += 1
                err = e
            if attempt >= self.cfg.connect_retries:           # connection failures and 5xx/429 only
                raise InfraError("endpoint", f"{type(err).__name__} after {attempt} retries: {err}"[:500]) from err
            self.retries += 1
            await asyncio.sleep(min(self.cfg.retry_base_s * 2 ** attempt, self.cfg.retry_max_s))
            attempt += 1
        self.served.note(resp)
        ua = getattr(resp, "usage", None)                # counted the moment the reply arrives (see the OpenAI adapter)
        self.usage_seen["responses"] += 1
        if ua is None:
            self.usage_missing += 1
        else:
            self.usage_seen["input"] += (getattr(ua, "input_tokens", 0) or 0) + (getattr(ua, "cache_creation_input_tokens", 0) or 0) \
                + (getattr(ua, "cache_read_input_tokens", 0) or 0)
            self.usage_seen["output"] += getattr(ua, "output_tokens", 0) or 0
        if ua is None:
            raise InfraError("endpoint", "reply carried no usage: its tokens are unknown")
        if not isinstance(getattr(resp, "content", None), (list, tuple)):
            raise InfraError("endpoint", "reply carried no content blocks; usage already counted")
        text, calls, reasoning = [], [], []
        self.served.note_thinking(any(getattr(b, "type", None) in ("thinking", "redacted_thinking") for b in resp.content))   # no separate count in this API
        for b in resp.content:
            if b.type == "text":
                text.append(b.text)
            elif b.type == "tool_use":
                ok = isinstance(b.input, dict)
                calls.append(ToolCall(b.id, b.name, b.input if ok else {},
                                      None if ok else "arguments are not a JSON object",
                                      None if ok else json.dumps(b.input, default=str)))
            elif b.type in ("thinking", "redacted_thinking"):
                reasoning.append(getattr(b, "thinking", "") or "")
        # the assistant turn goes back verbatim (thinking blocks included)
        blocks = [b.model_dump(exclude_none=True) for b in resp.content]
        for blk in blocks:                         # same rule as every model: never resend a non-object input
            if blk.get("type") == "tool_use" and not isinstance(blk.get("input"), dict):
                blk["input"] = {"_raw": json.dumps(blk.get("input"), default=str)}
        self._messages.append({"role": "assistant", "content": blocks})
        u = resp.usage
        fresh = u.input_tokens or 0                # the API's input_tokens is only the uncached remainder
        created = getattr(u, "cache_creation_input_tokens", 0) or 0
        read = getattr(u, "cache_read_input_tokens", 0) or 0
        inp_tok = fresh + created + read           # the prompt size: what P3 and {TOKEN_BUDGET} count
        return ModelTurn("".join(text), calls, inp_tok, u.output_tokens or 0, resp.stop_reason,
                         time.monotonic() - t0, "\n".join(r for r in reasoning if r) or None,
                         fresh, created, read)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self._messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.call.id, "content": r.text,
             **({"is_error": True} if r.is_error else {})} for r in results]})

    def transcript(self) -> list[dict]:
        # the system prompt travels as a separate API parameter; the record keeps it first
        full = ([{"role": "system", "content": self._system}] if self._system else []) + self._messages
        return json.loads(json.dumps(full, default=str))

    async def aclose(self) -> None:
        await self._client.close()


class StubAdapter:
    """Scripted adapter for tests and the oracle: replays a list of turns.

    Each item is either a final text (str) or a list of (tool_name, arguments) calls. Token counts
    are synthetic (len of the text/args), and recorded as such in info().
    """

    def __init__(self, script: list, name: str = "stub", seed: int = 0):
        self._script, self._i, self.name, self.seed = list(script), 0, name, seed
        self._messages: list[dict] = []
        self._ctx_tokens = 0

    def info(self) -> dict[str, Any]:
        return {"model_api_id": self.name, "checkpoint": self.name, "endpoint": "stub://", "slot": None,
                "seed": self.seed, "seed_honoured": False, "sampling": {}, "request_extra": {},
                "synthetic_tokens": True}

    def begin(self, system: str, user: str, tools: list[dict]) -> None:
        self._messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
        self._ctx_tokens = (len(system) + len(user)) // 4

    async def step(self) -> ModelTurn:
        item = self._script[self._i] if self._i < len(self._script) else "done"
        self._i += 1
        if isinstance(item, dict):                 # {"text": ..., "stop": "length"}: a reply with a given stop reason
            self._messages.append({"role": "assistant", "content": item["text"]})
            return ModelTurn(item["text"], [], self._ctx_tokens, len(item["text"]) // 4 + 1, item["stop"], 0.0)
        if isinstance(item, str):
            self._messages.append({"role": "assistant", "content": item})
            return ModelTurn(item, [], self._ctx_tokens, len(item) // 4 + 1, "stop", 0.0)
        calls = [ToolCall(f"call_{self._i}_{k}", n, a) for k, (n, a) in enumerate(item)]
        self._messages.append({"role": "assistant", "content": None, "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
            for c in calls]})
        out = sum(len(json.dumps(c.arguments)) for c in calls) // 4 + 1
        turn = ModelTurn("", calls, self._ctx_tokens, out, "tool_calls", 0.0)
        self._ctx_tokens += out
        return turn

    def add_tool_results(self, results: list[ToolResult]) -> None:
        for r in results:
            self._messages.append({"role": "tool", "tool_call_id": r.call.id, "content": r.text})
            self._ctx_tokens += len(r.text) // 4

    def transcript(self) -> list[dict]:
        return json.loads(json.dumps(self._messages, default=str))

    async def aclose(self) -> None:
        return None
