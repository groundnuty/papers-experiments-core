# Provenance: new
"""A sound bound on the prompt of the request in flight, for the record of an abandoned request (B19).

When the loop gives up a request already in flight, or its read timeout fires, nothing came back: the record says
`abandoned_request = {"reason": ..., "usage": "unknown"}`. The loose bound on that request is the model's whole
context window W (`ModelSpec.context_tokens`) for its prompt, so a run that had sent 100,000 tokens is charged
1,000,000 more for a request of a few thousand. This module bounds that prompt tighter, from what the adapter knows
when it builds the request, and the entry says which bound it used. The bound is **recorded only**: no budget gate
reads it. A study's scorer prices it with `expcore.campaign.abandoned_bound`.

**Which request.** Exactly one send is in flight when a request is abandoned, and it is the request built last:
- Both SDK clients are built with `max_retries=0`, so the SDK never resends. `Retrier.call` resends only after a
  transient error, the same body, so a resend has the bound of its step. A send that failed before it may have been
  billed and was charged as a top-up when it failed (C26): it is outside this bound.
- A read timeout is never resent (B03), and the client's read timeout is at least the run's time cap plus 60 s
  (`_read_timeout_s`), so it fires only when the run's time is gone. A cancel while the retrier waits to resend has no
  request in flight and abandons nothing (`Retrier.call`); only a cancel inside the send does.
- An entry made with no request in flight (`abandon` after a reply, an admission probe) is bounded by W.

**The bound on its prompt, P.** Let request k be the last whose reply reported both counts: `in_k`, the whole prompt
with its cached part (`prompt_tokens` for an OpenAI-compatible endpoint, the sum of the three input counts for the
Messages API, `adapters._anthropic_counts`), and `out_k`, its completion. Let n be the request in flight. Its prompt is
request k's prompt, plus what the model generated and the harness appended after k was built, plus the framing the
template writes around each message:

    P(n) <= in_k + out_k + bytes(messages appended after k) + bytes(system prompt, tools, extras, if they changed)
            + FRAMING_MARGIN_TOKENS x (1 + messages appended)

`bytes` is the length of each message as the adapter sends it, as JSON written the way a chat template writes it
(`message_bytes`, below): the reply as it goes back (with its reasoning when the entry echoes it, its thinking blocks
and signatures, a malformed call wrapped as `{"_raw": ...}`) and every tool result. Both `out_k` and the bytes of the
reply are added although they describe the
same message, because each sees what the other cannot: the completion count includes tokens the returned message does
not show (reasoning that comes back only as a signature), and the bytes cover the reply as it is written again, which
need not tokenize as it was generated. Only one reply is added twice, because the anchor moves with every reply.

P(n) is replaced by W (`BY_WINDOW`) when no reply has reported both counts (the first request has nothing to anchor
on, and a provider adds text of its own to it, a tool-use preamble or a template's rendering of the tool list, that its
bytes do not show); when the reply of the request just before reported a count short (`unreported`: the chain
re-anchors at the next reply that reports both, which holds the hidden part); when the bound is W or more, since the
endpoint refuses a prompt above its window (the assumption of C25); when the history is no extension of the anchor's
(its first messages are not the anchor's, digest for digest: a replaced, reordered or shortened history) or a message
cannot be written as JSON; and for the rest of the conversation once the byte assumption has been shown false (below)
or a request was built while another was in flight (`overlapped`: the anchor rests on one request at a time, as the
retrier sends them, and nothing would say whose reply came first). With no W and nothing to anchor on there is no
bound (`BY_NONE`), and the entry says so. A change to the system prompt, the tools or the extras counts all of their
bytes as appended: the anchor's prompt held the old text, and the new text costs at most its bytes.

**The reply, R.** The reply of the request is at most the `max_tokens` it carried, kept as `reply_max_tokens`. Whether
the endpoint applies it is the campaign's decision (C27), made after the adapter built the request, so the entry holds
no dollars and no total: `abandoned_bound.abandoned_request_bound` prices it from the record, with R when the attempt
was held to it, and with the window alone (the prompt and the reply together within W) when it was not.

**The assumptions, and where each is tested.** The bound is sound while these hold. They are the same for the two
adapters (`OpenAICompatAdapter`, `AnthropicAdapter`); a backend of a study's own has no `in_flight` account, and its
abandoned request keeps the entry the retrier makes, which a scorer prices by the window:
1. A token stands for at least one byte of the text it was read from, so tokens <= bytes. True of byte-level BPE, and of
   tokenizers whose tokens cover whole characters, of one byte or more each. A token that stands for no text is
   framing (a chat template's role and separator tokens, a tool call's wrapper), which `FRAMING_MARGIN_TOKENS` covers
   beyond the JSON structure the bytes already pay for (a message's keys, quotes and ids come to 30 bytes or more, a
   tool call's to 70). The text is the text the template writes, which for a tool call's arguments is not the text
   the adapter sent: an OpenAI-compatible server parses the arguments and a template writes them again with Jinja's
   `tojson`, which escapes every non-ASCII character as `\\uXXXX` (6 bytes, 12 for one past the Basic Multilingual
   Plane) and `<`, `>`, `&` and `'` as `\\u003c` and its kin (6 bytes for 1). `message_bytes` counts a message as that
   escaped ASCII JSON, which is never fewer bytes than the UTF-8 the wire carries, and adds 5 for each of the four
   characters, wherever they stand in the message. A template that writes arguments in more bytes than that (indented
   JSON of a large nested object, say) breaks the assumption, and the tripwire below is what shows it.
2. A reply's reported prompt count is the tokens of the whole prompt it was billed, the assumption the token cap makes
   (C25), and its completion count is every token it generated.
3. The history only grows: every message is appended and none is changed, which the tracker checks by digest and
   answers with the window when it does not hold. A template that drops earlier reasoning only makes the real prompt
   smaller.
Assumption 1 and the template's framing are tested by every completed request bounded by bytes: a reply that reports a
prompt above the bound it was sent with (`exceeded`) shows the assumption false for this endpoint, and every later
request of the conversation is bounded by W; the entry counts what was checked and what failed (`bound_checked`,
`bound_exceeded`), the replies that left a count out (`bound_unreported`) and the requests built while another was in
flight (`bound_overlapped`). That is a tripwire, not a proof: an
endpoint that is wrong only on the request that hangs is not seen, and the guarantee is the assumptions above.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

BY_BYTES = "bytes"  # the prompt bound is the last reported prompt and completion plus the bytes appended since
BY_WINDOW = "window"  # the prompt bound is the model's context window
BY_NONE = "none"  # no window and nothing to anchor on: the prompt has no bound
BOUND_BASES = (BY_BYTES, BY_WINDOW, BY_NONE)

# Tokens a chat template can write around a message that its JSON form does not already pay for, and once per request
# for the generation header. The JSON form of a message (role, keys, quotes, an id) is 30 bytes or more, and a token
# is at least a byte, so this is a margin on top of a margin.
FRAMING_MARGIN_TOKENS = 32

# The characters Jinja's `tojson` (the filter chat templates write tool-call arguments with) escapes so its output is
# safe in HTML: each is one byte in the arguments the adapter sends and six, `<` and its kin, in the template's text.
_TEMPLATE_ESCAPED = "<>&'"
_TEMPLATE_ESCAPE_EXTRA = 5


def _measure(value: Any) -> tuple[int, bytes]:
    """Return the bytes of a value as a chat template can write it, and a digest of its text (see `message_bytes`)."""
    text = json.dumps(value, ensure_ascii=True, default=str)
    size = len(text) + _TEMPLATE_ESCAPE_EXTRA * sum(text.count(c) for c in _TEMPLATE_ESCAPED)
    return size, hashlib.blake2b(text.encode("ascii"), digest_size=16).digest()


def message_bytes(value: Any) -> int:
    """Return the bytes of a message, or of any part of a request, as a chat template can write it: its JSON text with
    every non-ASCII character as the `\\uXXXX` escapes (6 bytes, 12 past the Basic Multilingual Plane, never fewer than
    its UTF-8), and 5 more for each `<`, `>`, `&` and `'`, which `tojson` writes as 6 (see the module docstring).

    Bytes and not characters, because a character of several bytes can cost several tokens. A lone surrogate, which a
    tool result decoded with errors kept can hold, is escaped like any other. Raises ValueError or TypeError for a
    value that cannot be written as JSON at all (a circular reference).
    """
    return _measure(value)[0]


@dataclass(frozen=True)
class RequestBound:
    """What is known of the prompt and the reply of one request, when it is built.

    `prompt_tokens` bounds the prompt and `by` says how: None only when `by` is `BY_NONE`. `reply_max_tokens` is the
    `max_tokens` the request carried. `checked` and `exceeded` count the completed requests of the conversation whose
    byte bound was tested against the prompt they reported, and failed; `unreported` the replies that left a count out;
    `overlapped` the requests built while another was still in flight.
    """

    prompt_tokens: int | None
    by: str
    reply_max_tokens: int | None
    window_tokens: int | None
    checked: int = 0
    exceeded: int = 0
    unreported: int = 0
    overlapped: int = 0

    def entry(self) -> dict[str, Any]:
        """Return the fields an abandoned request adds to the record, in plain values."""
        return {
            "prompt_bound_tokens": self.prompt_tokens,
            "prompt_bound_by": self.by,
            "reply_max_tokens": self.reply_max_tokens,
            "window_tokens": self.window_tokens,
            "bound_checked": self.checked,
            "bound_exceeded": self.exceeded,
            "bound_unreported": self.unreported,
            "bound_overlapped": self.overlapped,
        }


@dataclass(frozen=True)
class _Anchor:
    """A request whose reply reported both counts: the two, a digest of each message the request carried, and the
    digest of its body outside the messages. A later request is an extension of it only when its first messages have
    these digests, in this order."""

    prompt_tokens: int
    completion_tokens: int
    messages: tuple[bytes, ...]
    fixed: bytes


@dataclass(frozen=True)
class _InFlight:
    """The request sent and not yet answered: its bound, and what it would anchor, None when it cannot be measured."""

    bound: RequestBound
    messages: tuple[bytes, ...] | None
    fixed: bytes | None


class PromptBound:
    """Bounds the prompt of each request of one conversation, as the module docstring derives, and tests the bound.

    One per backend, for requests sent one at a time. `begin` starts a conversation, `sent` is called with the body of
    each request as it is built, `replied` with the counts its reply reported (None for one it left out), `gave_up`
    when the request ended with no reply. A request built while another is in flight breaks the one-at-a-time rule the
    anchor rests on, since a reply could then be taken for the other request's: it is counted in `overlapped`, and the
    conversation is bounded by the window from then on. The tracker holds conversation state that changes with every
    request, by design: it is the point of it.
    """

    def __init__(self, window: int | None, reply_max: int | None):
        self.window, self.reply_max = window, reply_max
        self.begin()

    def begin(self) -> None:
        """Start a conversation: no anchor, no check, no lost trust."""
        self._anchor: _Anchor | None = None
        self._current: _InFlight | None = None
        self._checked = 0
        self._exceeded = 0
        self._unreported = 0
        self._overlapped = 0

    def sent(self, request: Mapping[str, Any]) -> RequestBound:
        """Bound the request just built and keep it as the one in flight.

        `request` is the body as it goes out: its `messages`, and everything else (system prompt, tools, extras) as the
        fixed part. The reply maximum is the body's own `max_tokens`.
        """
        if self._current is not None:
            self._overlapped += 1  # the earlier request has not ended: nothing says whose reply will come first
        reply_max = request.get("max_tokens")
        reply_max = reply_max if isinstance(reply_max, int) and not isinstance(reply_max, bool) else self.reply_max
        try:
            fixed_size, fixed = _measure({k: v for k, v in request.items() if k != "messages"})
            measured = [_measure(message) for message in request["messages"]]
        except (TypeError, ValueError):  # not written as JSON: nothing to measure, so the window's bound, said so
            prompt, by = self._window_bound()
            held = _InFlight(self._bound(prompt, by, reply_max), None, None)
        else:
            digests = tuple(digest for _, digest in measured)
            prompt, by = self._prompt_bound([size for size, _ in measured], digests, fixed_size, fixed)
            held = _InFlight(self._bound(prompt, by, reply_max), digests, fixed)
        self._current = held
        return held.bound

    def replied(self, prompt_tokens: int | None, completion_tokens: int | None) -> None:
        """The request in flight was answered, reporting these counts (None: left out).

        Both counts make it the anchor of the next request. A bound by bytes that the reported prompt passes shows the
        byte assumption false, for good. A reply with either count missing leaves nothing to anchor on, and the next
        request is bounded by the window until a reply reports both.
        """
        if self._current is None:
            return
        held, self._current = self._current, None
        if prompt_tokens is not None and held.bound.by == BY_BYTES and held.bound.prompt_tokens is not None:
            self._checked += 1
            self._exceeded += prompt_tokens > held.bound.prompt_tokens
        if prompt_tokens is None or completion_tokens is None:
            self._anchor = None
            self._unreported += 1
        elif held.messages is None or held.fixed is None:
            self._anchor = None  # a body that cannot be measured anchors nothing
        else:
            self._anchor = _Anchor(prompt_tokens, completion_tokens, held.messages, held.fixed)

    def gave_up(self) -> None:
        """The request in flight ended with no reply (an error, a cancel): nothing is in flight any more. The anchor is
        the last reply's, and the history it measures from has not changed."""
        self._current = None

    def in_flight(self) -> dict[str, Any]:
        """Return the entry for a request given up now: the one sent and not answered; with none in flight, the
        window's bound, which holds for any request."""
        if self._current is not None:
            return self._current.bound.entry()
        prompt, by = self._window_bound()
        return self._bound(prompt, by, self.reply_max).entry()

    def _bound(self, prompt: int | None, by: str, reply_max: int | None) -> RequestBound:
        return RequestBound(
            prompt, by, reply_max, self.window, self._checked, self._exceeded, self._unreported, self._overlapped
        )

    def _window_bound(self) -> tuple[int | None, str]:
        """The bound that needs no assumption about bytes: the window, or none without one."""
        return (None, BY_NONE) if self.window is None else (self.window, BY_WINDOW)

    def _prompt_bound(
        self, sizes: list[int], digests: tuple[bytes, ...], fixed_size: int, fixed: bytes
    ) -> tuple[int | None, str]:
        """The bytes bound of a request, or the window's when the bytes cannot be trusted or do not tighten it.

        The request has to be an extension of the anchor's: its first messages are the anchor's, digest for digest. A
        replaced, reordered or shortened history is not, and the window bounds it. The part of the body outside the
        messages is free to differ; if it does, all of it is counted as appended, since the anchor's prompt held the
        old text and the new text costs at most its bytes.
        """
        anchor = self._anchor
        held = len(anchor.messages) if anchor is not None else 0
        if anchor is None or self._exceeded or self._overlapped or digests[:held] != anchor.messages:
            return self._window_bound()
        grown = 0 if fixed == anchor.fixed else fixed_size
        framing = FRAMING_MARGIN_TOKENS * (1 + len(sizes) - held)
        bytes_bound = anchor.prompt_tokens + anchor.completion_tokens + sum(sizes[held:]) + grown + framing
        if self.window is not None and bytes_bound >= self.window:
            return self.window, BY_WINDOW
        return bytes_bound, BY_BYTES
