"""B19: the bound on an abandoned request's prompt tokens, from the last reported prompt and the bytes appended since.

The tracker measures every request when it is built, because nothing says which one will be abandoned. These tests
hold its arithmetic and every way it must fall back to the window; the adapters' use of it is in
`test_adapters_bound.py`.
"""

import json
import random

import pytest

from expcore.models.request_bound import (
    BY_BYTES,
    BY_NONE,
    BY_WINDOW,
    FRAMING_MARGIN_TOKENS,
    PromptBound,
    message_bytes,
)

WINDOW = 100_000
REPLY = 4096


def rendered(message) -> int:
    """The bytes of a message as a chat template can write it, counted here independently of the module: ASCII JSON
    (every non-ASCII character an escape), and six bytes for each `<`, `>`, `&` and `'`, which `tojson` writes as
    `\\u003c` and its kin."""
    text = json.dumps(message, ensure_ascii=True)
    return len(text) + 5 * sum(text.count(c) for c in "<>&'")


def request(messages, **fixed):
    return {"model": "m", "max_tokens": REPLY, "messages": list(messages), **fixed}


def user(text="go"):
    return {"role": "user", "content": text}


def assistant(text="ok"):
    return {"role": "assistant", "content": text}


def tool(text="result", call="call_1"):
    return {"role": "tool", "tool_call_id": call, "content": text}


def tracker(window=WINDOW):
    bound = PromptBound(window, REPLY)
    bound.begin()
    return bound


def expected(prompt, completion, appended) -> int:
    """The bound the derivation gives: both reported counts, the bytes appended after them and a margin each."""
    return prompt + completion + sum(rendered(m) for m in appended) + FRAMING_MARGIN_TOKENS * (1 + len(appended))


def test_b19_the_first_request_has_no_previous_reply_so_the_window_bounds_it():
    """B19: nothing anchors the first request, and a provider or template may add text of its own to it, so its prompt
    is bounded by the window, whatever its bytes say."""
    sent = tracker().sent(request([user("tiny")]))
    assert (sent.prompt_tokens, sent.by) == (WINDOW, BY_WINDOW)
    assert sent.reply_max_tokens == REPLY


def test_b19_without_a_window_the_first_request_has_no_bound_at_all():
    """B19: no window and no reply to anchor on: there is no bound, and the entry says so instead of inventing one."""
    sent = tracker(window=None).sent(request([user("tiny")]))
    assert (sent.prompt_tokens, sent.by) == (None, BY_NONE)
    assert sent.reply_max_tokens == REPLY


def test_b19_the_next_request_is_the_reported_counts_plus_the_bytes_appended_since_and_a_margin_each():
    """B19: the prompt the reply reported already holds everything before it, the provider's own text included, so
    only what was appended after that request was built is measured: the reply as it goes back, and the tool results.
    The completion count is added too, for tokens of the reply that its message does not show."""
    bound = tracker()
    first = [user("go")]
    bound.sent(request(first))
    bound.replied(1_000, 40)
    appended = [assistant("calling"), tool("x" * 500)]
    sent = bound.sent(request(first + appended))
    assert (sent.prompt_tokens, sent.by) == (expected(1_000, 40, appended), BY_BYTES)


def test_b19_wide_characters_are_counted_as_the_escapes_a_template_writes_and_not_as_characters():
    """B19: a token may be as short as one byte but not shorter, and a template writes a tool call's non-ASCII
    characters as `\\uXXXX` escapes: 6 bytes for one of the Basic Multilingual Plane, 12 for one past it. The same
    number of characters in Japanese and emoji weighs far more than in ASCII, and never less than its UTF-8."""
    results = {}
    wide = "日本語🙂日本語🙂"
    for name, text in (("ascii", "a" * 8), ("wide", wide)):
        bound = tracker()
        bound.sent(request([user()]))
        bound.replied(100, 5)
        results[name] = bound.sent(request([user(), assistant(), tool(text)])).prompt_tokens
    assert len("a" * 8) == len(wide)
    assert results["wide"] - results["ascii"] == 6 * 6 + 2 * 12 - 8
    assert message_bytes(tool(wide)) >= len(json.dumps(tool(wide), ensure_ascii=False).encode("utf-8"))


@pytest.mark.parametrize("character", ["<", ">", "&", "'"])
def test_b19_the_four_characters_a_template_escapes_weigh_six_bytes_each(character):
    """B19: Jinja's `tojson` writes `<`, `>`, `&` and `'` as `\\u003c`, `\\u003e`, `\\u0026` and `\\u0027`, so each
    costs six bytes of the template's text where the adapter sent one."""
    plain, escaped = message_bytes(tool("x" * 10)), message_bytes(tool("x" * 5 + character * 5))
    assert escaped - plain == 5 * 5
    assert rendered(tool(character)) - rendered(tool("x")) == 5


def test_b19_a_code_argument_with_many_apostrophes_is_covered_by_the_bound():
    """B19: the case a margin alone misses. A code tool's `arguments` hold single-quoted Python: 60 apostrophes and a
    few non-ASCII characters, which the adapter sends as the model wrote them. A template parses the arguments and
    writes them with `tojson`: apostrophes as `\\u0027` and the non-ASCII as `\\uXXXX`. The prompt the endpoint reads
    is at least the bytes of that text, one token per byte at the worst, and the bound covers it."""
    code = "print('a')\n" * 30 + "# 日本語 🙂"
    arguments = json.dumps({"code": code}, ensure_ascii=False)  # as the model sent it, and as the adapter sends it
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "call_9", "type": "function", "function": {"name": "run", "arguments": arguments}}],
    }
    template_text = json.dumps(json.loads(arguments), ensure_ascii=True)  # what `tojson` writes, before its escapes
    for character, escape in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("'", "\\u0027")):
        template_text = template_text.replace(character, escape)
    assert len(template_text.encode()) > len(arguments.encode()) + 5 * 60  # the apostrophes alone add 300
    bound = tracker()
    bound.sent(request([user()]))
    bound.replied(100, 50)
    sent = bound.sent(request([user(), message, tool("ok", "call_9")]))
    reply_tokens = len(template_text.encode()) + 27 + 20  # the arguments, a header and a call's wrapper
    tool_tokens = len("ok") + len("call_9") + 27
    assert sent.by == BY_BYTES
    assert sent.prompt_tokens >= 100 + reply_tokens + tool_tokens


def test_b19_a_lone_surrogate_is_counted_and_never_raises():
    """B19: tool output decoded with errors kept as surrogates must not crash the run on the way to its bound."""
    bound = tracker()
    bound.sent(request([user()]))
    bound.replied(100, 5)
    sent = bound.sent(request([user(), assistant(), tool("bad \ud800 bytes")]))
    assert sent.by == BY_BYTES
    assert message_bytes(tool("\ud800")) > message_bytes(tool(""))


def test_b19_a_reply_that_left_a_count_out_gives_the_next_request_the_window_and_the_one_after_a_new_anchor():
    """B19: a reply with no prompt count, or no completion count, may have hidden tokens its message does not show, so
    the request after it is bounded by the window; the reply to that request reports both and anchors the next, whose
    prompt holds the hidden part."""
    for missing in ((None, 5), (500, None), (None, None)):
        bound = tracker()
        history = [user()]
        bound.sent(request(history))
        bound.replied(500, 5)
        history += [assistant("first"), tool("r1")]
        assert bound.sent(request(history)).by == BY_BYTES
        bound.replied(*missing)
        history += [assistant("second"), tool("r2")]
        after_missing = bound.sent(request(history))
        assert (after_missing.prompt_tokens, after_missing.by) == (WINDOW, BY_WINDOW)
        assert after_missing.unreported == 1
        bound.replied(900, 7)
        history += [assistant("third"), tool("r3")]
        again = bound.sent(request(history))
        assert (again.prompt_tokens, again.by) == (expected(900, 7, history[-2:]), BY_BYTES)
        assert again.unreported == 1


def test_b19_a_conversation_whose_replies_never_report_both_counts_stays_on_the_window():
    """B19: with no reply that ever reported both counts there is nothing to anchor on."""
    bound = tracker()
    history = [user()]
    for _ in range(3):
        bound.sent(request(history))
        bound.replied(None, None)
        history += [assistant(), tool()]
    sent = bound.sent(request(history))
    assert (sent.prompt_tokens, sent.by, sent.unreported) == (WINDOW, BY_WINDOW, 3)


def test_b19_a_bound_at_or_above_the_window_is_the_window_and_says_so():
    """B19: the prompt cannot pass the window the endpoint enforces, so a bytes bound at or past it is the window."""
    bound = tracker(window=2_000)
    bound.sent(request([user()]))
    bound.replied(1_900, 50)
    sent = bound.sent(request([user(), assistant(), tool("x" * 400)]))
    assert (sent.prompt_tokens, sent.by) == (2_000, BY_WINDOW)


def test_b19_a_bound_just_under_the_window_is_the_bytes_bound():
    """B19: the boundary: one token under the window is by bytes, exactly the window is by window."""
    appended = [assistant(), tool("x" * 30)]
    base = expected(0, 0, appended)
    for window, by in ((base + 1, BY_BYTES), (base, BY_WINDOW)):
        bound = tracker(window=window)
        bound.sent(request([user()]))
        bound.replied(0, 0)
        sent = bound.sent(request([user(), *appended]))
        assert (sent.prompt_tokens, sent.by) == (base if by == BY_BYTES else window, by)


def test_b19_a_request_after_a_reply_that_beat_its_bound_is_bounded_by_the_window_for_good():
    """B19: a completed request whose reported prompt passes the bound it was sent with shows the byte assumption false
    for this endpoint, so no later request is bounded by bytes: the entry counts the checks and the failure."""
    bound = tracker()
    history = [user()]
    bound.sent(request(history))
    bound.replied(100, 5)
    history += [assistant(), tool()]
    checked = bound.sent(request(history))
    assert checked.by == BY_BYTES
    bound.replied(checked.prompt_tokens + 1, 5)  # the endpoint says the prompt was longer than its bytes allow
    history += [assistant(), tool()]
    after = bound.sent(request(history))
    assert (after.prompt_tokens, after.by) == (WINDOW, BY_WINDOW)
    assert (after.checked, after.exceeded) == (1, 1)
    bound.replied(50, 5)  # a later, smaller count does not bring the trust back
    assert bound.sent(request([*history, assistant(), tool()])).by == BY_WINDOW


def test_b19_a_reply_at_exactly_its_bound_is_within_it_and_a_window_bounded_one_tested_nothing():
    """B19: each completed request bounded by bytes is a test of the assumption, and a count equal to the bound passes;
    a request the window bounded tested nothing, so it is not counted."""
    bound = tracker()
    history = [user()]
    first = bound.sent(request(history))
    assert first.by == BY_WINDOW
    bound.replied(100, 5)
    history += [assistant(), tool()]
    second = bound.sent(request(history))
    bound.replied(second.prompt_tokens, 5)
    history += [assistant(), tool()]
    third = bound.sent(request(history))
    assert (third.checked, third.exceeded) == (1, 0)
    assert third.by == BY_BYTES


def test_b19_a_new_conversation_starts_over():
    """B19: `begin` forgets the anchor, the checks, the unreported replies and a lost trust."""
    bound = tracker()
    bound.sent(request([user()]))
    bound.replied(100, 5)
    bound.sent(request([user(), assistant(), tool()]))
    bound.replied(1_000_000, 5)  # exceeds its bound
    bound.begin()
    sent = bound.sent(request([user()]))
    assert (sent.by, sent.checked, sent.exceeded, sent.unreported) == (BY_WINDOW, 0, 0, 0)
    bound.replied(100, 5)
    assert bound.sent(request([user(), assistant(), tool()])).by == BY_BYTES


def test_b19_growth_of_the_part_outside_the_messages_is_paid_for_and_shrinkage_is_not_credited():
    """B19: the system prompt and the tool list are in the reported prompt; if either grew since, the growth is appended
    text like any other. If one shrank, the real prompt only got smaller, and nothing is taken off."""
    bound = tracker()
    history = [user()]
    bound.sent(request(history, system="short" * 20))
    bound.replied(100, 5)
    history += [assistant(), tool()]
    same = bound.sent(request(history, system="short" * 20))
    grown = bound.sent(request(history, system="short" * 20 + "s" * 300))
    shrunk = bound.sent(request(history, system=""))
    assert grown.prompt_tokens - same.prompt_tokens == 300
    assert shrunk.prompt_tokens == same.prompt_tokens


def test_b19_a_history_shorter_than_the_anchor_is_no_extension_of_it_and_the_window_bounds_it():
    """B19: the bytes are those of what was appended after the anchor's request, so a conversation that is not an
    extension of it has nothing to measure from."""
    bound = tracker()
    bound.sent(request([user(), assistant(), tool(), assistant(), tool()]))
    bound.replied(100, 5)
    sent = bound.sent(request([user()]))
    assert (sent.prompt_tokens, sent.by) == (WINDOW, BY_WINDOW)


def test_b19_a_body_that_cannot_be_measured_is_bounded_by_the_window_and_anchors_nothing():
    """B19: a message that cannot be serialised is no reason to end a run, and no reason to guess: the window, for it
    and for the request after it, whose anchor would rest on bytes nobody counted."""
    bound = tracker()
    bound.sent(request([user()]))
    bound.replied(100, 5)
    loop: dict = {"role": "tool", "content": "x"}
    loop["self"] = loop
    unmeasurable = bound.sent(request([user(), assistant(), loop]))
    assert (unmeasurable.prompt_tokens, unmeasurable.by) == (WINDOW, BY_WINDOW)
    bound.replied(150, 5)
    after = bound.sent(request([user(), assistant(), loop, assistant(), tool()]))
    assert after.by == BY_WINDOW


def test_b19_what_is_in_flight_is_the_request_just_built_and_after_a_reply_nothing_is():
    """B19: the entry for an abandoned request is the one sent and not yet answered; with none in flight (an external
    abandon after a reply, an admission probe) it is the window's, which is sound for any request."""
    bound = tracker()
    bound.sent(request([user()]))
    bound.replied(100, 5)
    sent = bound.sent(request([user(), assistant(), tool()]))
    assert bound.in_flight() == sent.entry()
    bound.replied(150, 5)
    idle = bound.in_flight()
    assert (idle["prompt_bound_tokens"], idle["prompt_bound_by"]) == (WINDOW, BY_WINDOW)
    assert idle["reply_max_tokens"] == REPLY
    bound.sent(request([user(), assistant(), tool(), assistant(), tool()]))
    bound.gave_up()
    assert bound.in_flight()["prompt_bound_by"] == BY_WINDOW


def test_b19_a_request_that_ended_with_no_reply_leaves_the_anchor_where_it_was():
    """B19: an error or a cancel appends no reply, so the next request is measured from the same anchor."""
    bound = tracker()
    history = [user()]
    bound.sent(request(history))
    bound.replied(100, 5)
    history += [assistant(), tool()]
    bound.sent(request(history))
    bound.gave_up()
    again = bound.sent(request(history))
    assert (again.prompt_tokens, again.by) == (expected(100, 5, history[1:]), BY_BYTES)


def test_b19_the_entry_names_its_basis_the_reply_maximum_the_window_and_what_it_checked():
    """B19: everything a reader needs to price the request from the record alone, in plain numbers."""
    bound = tracker()
    bound.sent(request([user()]))
    bound.replied(100, 5)
    sent = bound.sent(request([user(), assistant(), tool()]))
    assert sent.entry() == {
        "prompt_bound_tokens": sent.prompt_tokens,
        "prompt_bound_by": "bytes",
        "reply_max_tokens": REPLY,
        "window_tokens": WINDOW,
        "bound_checked": 0,
        "bound_exceeded": 0,
        "bound_unreported": 0,
    }
    json.dumps(sent.entry())  # plain values, so the record keeps it as it is


def random_text(rng: random.Random, size: int) -> str:
    """Text that costs a tokenizer the most: quotes, backslashes, newlines, control characters and wide characters."""
    alphabet = ['"', "\\", "\n", "\t", "\x01", "{", "}", "日", "🙂", "é", "a", " ", chr(0x2028), "'", "<", ">", "&"]
    return "".join(rng.choice(alphabet) for _ in range(size))


def tojson(text: str) -> str:
    """What Jinja's `tojson` writes for a JSON text that a template renders again: the parsed value dumped as ASCII,
    with `<`, `>`, `&` and `'` as `\\u003c` and its kin, as markupsafe's `htmlsafe_json_dumps` does."""
    written = json.dumps(json.loads(text), ensure_ascii=True)
    for character, escape in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("'", "\\u0027")):
        written = written.replace(character, escape)
    return written


def rendered_tokens(messages) -> int:
    """The prompt as the worst tokenizer reads it: every byte of the text a chat template writes, and of the
    template's own words around each message, a token. No real tokenizer does worse than one token per byte. A tool
    call's arguments are written by `tojson` from the parsed arguments, as common open-model templates do."""
    total = 0
    for message in messages:
        total += 27  # `<|start_header_id|>assistant<|end_header_id|>\n\n` and its end, as a template might write them
        total += len(str(message.get("content") or "").encode("utf-8", "surrogatepass"))
        total += len(str(message.get("tool_call_id") or "").encode())
        for call in message.get("tool_calls", ()):
            total += len(call["function"]["name"]) + len(tojson(call["function"]["arguments"])) + 20
    return total + 27  # the header of the reply asked for


@pytest.mark.parametrize("seed", range(8))
def test_b19_the_bound_is_never_below_what_a_one_token_per_byte_tokenizer_reads(seed):
    """B19: the soundness of the byte bound as a property. A conversation of random rounds, with tool calls whose
    arguments are well-formed and carry apostrophes, angle brackets and wide characters as the model wrote them, or went
    back wrapped in `_raw`, escapes and wide characters in every result, and an endpoint that reads the prompt one token
    per byte of what its template writes (`tojson` for the arguments, the most any tokenizer can) with words of its own
    around each message: every request's bound is at least the prompt it was read as, and nothing was counted as
    exceeded."""
    rng = random.Random(seed)
    bound = tracker(window=10_000_000)
    history = [{"role": "system", "content": random_text(rng, 40)}, user(random_text(rng, 60))]
    previous_prompt = 0
    for _ in range(12):
        sent = bound.sent(request(history))
        actual = rendered_tokens(history)
        assert sent.prompt_tokens >= actual
        bound.replied(actual, rng.randint(1, 200))
        previous_prompt = actual
        text = random_text(rng, rng.randint(0, 120))
        if rng.random() < 0.5:  # code that is mostly single-quoted strings, the case a margin alone cannot cover
            text += "".join(rng.choice("'''<>&") for _ in range(rng.randint(100, 400)))
        # well-formed arguments go back as the model wrote them, raw characters and all; malformed ones wrapped
        arguments = json.dumps({"code": text}, ensure_ascii=False) if rng.random() < 0.6 else json.dumps({"_raw": text})
        history.append(
            {
                "role": "assistant",
                "content": random_text(rng, rng.randint(0, 80)) or None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "run", "arguments": arguments}}
                ],
            }
        )
        history.append(tool(random_text(rng, rng.randint(0, 400))))
    assert previous_prompt > 0
    assert (sent.checked, sent.exceeded) == (10, 0)


def test_b19_the_bound_is_tight_for_a_long_conversation_and_far_below_the_window():
    """B19: the point of it: a conversation of 200,000 reported tokens in a 1,000,000-token window, one more round of
    a few kilobytes, is bounded within a few thousand tokens of what was reported, not at the window."""
    bound = PromptBound(1_000_000, REPLY)
    bound.sent(request([user()]))
    bound.replied(200_000, 800)
    appended = [assistant("a" * 2_000), tool("r" * 3_000)]
    sent = bound.sent(request([user(), *appended]))
    assert sent.by == BY_BYTES
    assert 200_000 < sent.prompt_tokens < 210_000
