"""O05, O16: only read calls pass the data gate; everything else is refused as a decision, not an error."""

import json

import pytest

from expcore_onedata.sandbox import gatepolicy as policy

BASE = "/api/v3/"
ALLOWED = [
    ("onezone", "POST", BASE + "onezone/tokens/infer_access_token_scope", "scope"),
    ("oneprovider", "POST", BASE + "oneprovider/lookup-file-id/some/path", "lookup"),
    ("oneprovider", "GET", BASE + "oneprovider/data/f1", "attrs"),
    ("oneprovider", "GET", BASE + "oneprovider/data/f1/children", "children"),
    ("oneprovider", "GET", BASE + "oneprovider/data/f1/content", "content"),
]


@pytest.mark.parametrize(("role", "method", "path", "tag"), ALLOWED)
def test_o05_the_five_read_calls_pass(role, method, path, tag):
    """O05: the gate passes exactly the calls the REST client needs to list, look up and read."""
    decision = policy.decide(role, method, path)
    assert decision.allow
    assert decision.tag == tag


@pytest.mark.parametrize("method", ["PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE", "CONNECT"])
def test_o05_every_other_method_on_a_data_path_is_refused(method):
    """O05: a write, or anything that is not one of the five reads, is refused whatever the path."""
    for path in (
        BASE + "oneprovider/data/f1/content",
        BASE + "oneprovider/data/f1",
        BASE + "oneprovider/data/f1/children",
    ):
        decision = policy.decide("oneprovider", method, path)
        assert not decision.allow
        assert decision.status in (400, 403)


@pytest.mark.parametrize(
    "path",
    [
        BASE + "oneprovider/data/f1/metadata/xattrs",
        BASE + "oneprovider/data/f1/download_url",
        BASE + "oneprovider/shares",
        BASE + "oneprovider/data/f1/children/extra",
        BASE + "onezone/user/tokens/temporary",
        "/",
    ],
)
def test_o05_metadata_shares_and_other_endpoints_are_refused(path):
    """O05: Metadata shares and other endpoints are refused."""
    assert not policy.decide("oneprovider", "GET", path).allow
    assert not policy.decide("onezone", "GET", path).allow


@pytest.mark.parametrize(
    "path",
    [
        BASE + "oneprovider/data/../content",
        BASE + "oneprovider/data/%2e%2e/content",
        BASE + "oneprovider/data/a%2fb/content",
        BASE + "oneprovider/data/a%5cb/content",
        BASE + "oneprovider/data/a\\b/content",
    ],
)
def test_o05_dot_segments_and_encoded_separators_are_refused(path):
    """O05: Dot segments and encoded separators are refused."""
    decision = policy.decide("oneprovider", "GET", path)
    assert not decision.allow
    assert "dot segments" in decision.reason


def test_o05_a_call_for_the_wrong_host_role_is_refused():
    """O05: the zone answers only the scope lookup, the provider only the data calls."""
    assert not policy.decide("onezone", "GET", BASE + "oneprovider/data/f1/content").allow
    assert not policy.decide("oneprovider", "POST", BASE + "onezone/tokens/infer_access_token_scope").allow
    assert not policy.decide("somewhere", "GET", BASE + "oneprovider/data/f1/content").allow


def test_o16_a_refusal_is_a_decision_with_a_client_status():
    """O16: a refusal is answered 4xx, so it is never counted as an error the gate made."""
    decision = policy.decide("oneprovider", "DELETE", BASE + "oneprovider/data/f1")
    assert decision.status == 403
    assert not decision.allow


CHILDREN = BASE + "oneprovider/data/f1/children"


@pytest.mark.parametrize(
    ("query", "body"),
    [
        ("", None),  # no limit: the server's default page of 1000 (the official client always sends one)
        ("limit=1000", None),
        ("limit=1", None),
        ("limit=1000&token=abc", None),
        ("", b'{"attributes": ["name", "type", "size", "mtime", "posixPermissions"]}'),  # the official client's body
        ("limit=1000", b'{"attributes": ["name"], "limit": 1000}'),
        ("", b'{"limit": "1000"}'),
        ("limit=0", None),  # under the server's own minimum: the server answers it, small
    ],
    ids=["no-limit", "maximum", "one", "with-token", "client-body", "both-at-maximum", "string-in-body", "zero"],
)
def test_o05_a_listing_with_no_limit_or_one_within_the_maximum_passes(query, body):
    """O05: a listing without a limit passes unchanged (the server then answers its default page, which fits the
    gate's buffer), and so does one whose limit is at most MAX_LIST_LIMIT, in the query or in a JSON body."""
    decision = policy.decide("oneprovider", "GET", CHILDREN, query, body)
    assert decision.allow
    assert decision.tag == "children"


@pytest.mark.parametrize(
    ("query", "body"),
    [
        ("limit=1001", None),
        ("limit=10000", None),
        ("limit=%31%30%30%31", None),  # 1001, percent-encoded
        ("limit=%2B1001", None),  # +1001
        ("limit=5&limit=1001", None),  # a repeated parameter: either could be the one the server reads
        ("", b'{"limit": 1001}'),  # the server takes its options from a JSON body too
        ("", b'{"limit": "5000"}'),
        ("limit=5", b'{"limit": 1001}'),
    ],
    ids=["above", "server-maximum", "encoded", "signed", "repeated", "body", "body-string", "split"],
)
def test_o05_a_listing_whose_limit_is_above_the_maximum_is_refused_as_the_agents_request(query, body):
    """O05, O16: an explicit limit above MAX_LIST_LIMIT is refused before anything is forwarded, as a 400 with a
    reason the agent reads (a policy refusal, the agent's own outcome), never as an error or a refusal of the gate's
    integrity."""
    decision = policy.decide("oneprovider", "GET", CHILDREN, query, body)
    assert not decision.allow
    assert decision.status == 400
    assert decision.tag == "children"
    assert str(policy.MAX_LIST_LIMIT) in decision.reason
    assert "nextPageToken" in decision.reason  # how to read a larger directory


@pytest.mark.parametrize(
    ("query", "body"),
    [
        ("limit=", None),
        ("limit=ten", None),
        ("limit=1e3", None),
        ("limit=1_000", None),
        ("limit=%201000", None),
        ("", b'{"limit": 1000.0}'),
        ("", b'{"limit": true}'),
        ("", b'{"limit": null}'),
        ("", b"[1000]"),
        ("", b"limit=5000"),
        ("", b'{"limit": 5, "limit": 1001}'),  # a key twice in one object: which one the server reads is not known
    ],
)
def test_o05_a_listing_limit_or_body_the_gate_cannot_read_as_a_whole_number_is_refused(query, body):
    """O05: a limit that is not a plain whole number, or a listing body that is not a JSON object, could be read by the
    server as a size the gate did not check, so it is refused (fail closed), as the agent's request."""
    decision = policy.decide("oneprovider", "GET", CHILDREN, query, body)
    assert not decision.allow
    assert decision.status == 400


def test_o05_the_listing_maximum_is_the_servers_default_page_and_fits_the_gates_buffer():
    """O05, O16: MAX_LIST_LIMIT is the page the server answers without a limit and the official client asks for (1000),
    and its worst case, as the docstring computes it, stays under the gate's buffer cap."""
    from expcore_onedata.sandbox import gateboundary

    assert policy.MAX_LIST_LIMIT == 1000
    per_entry = policy.NAME_JSON_BYTES + policy.FILE_ID_BYTES + policy.ENTRY_REST_BYTES
    assert policy.NAME_JSON_BYTES == 255 * 6
    assert policy.MAX_LIST_LIMIT * per_entry + policy.PAGE_ENVELOPE_BYTES < gateboundary.BUFFER_CAP


@pytest.mark.parametrize("tag_path", [BASE + "oneprovider/data/f1", BASE + "oneprovider/data/f1/content"])
def test_o05_a_limit_on_a_call_that_is_no_listing_is_left_to_the_server(tag_path):
    """O05: only a listing has a page whose size the request sets; other reads pass whatever their query says."""
    assert policy.decide("oneprovider", "GET", tag_path, "limit=50000", None).allow


DEEP = b"[" * 100_000 + b"]" * 100_000  # a body the standard parser cannot read without running out of stack


@pytest.mark.parametrize(
    "body",
    [
        DEEP,
        b'{"limit": 5, "a": ' + b"[" * 100_000 + b"]" * 100_000 + b"}",
        b'{"a": ' + b'{"b": ' * 40 + b"1" + b"}" * 40 + b"}",  # deeper than the bound, though a parser could read it
    ],
    ids=["list", "inside-an-object", "over-the-bound"],
)
def test_o05_a_listing_body_nested_too_deep_is_the_agents_400_and_never_an_error(body):
    """O05, O16: a body nested deeper than the gate reads is refused before it is parsed, as a 400 the agent reads,
    never as an exception that would reset the connection as an error of the gate's own."""
    decision = policy.decide("oneprovider", "GET", CHILDREN, "", body)
    assert not decision.allow
    assert decision.status == 400
    assert decision.tag == "children"


def test_o05_the_nesting_bound_counts_brackets_outside_strings_only():
    """O05: brackets inside a JSON string are text, so a name full of them does not count towards the bound."""
    name = "[" * 200 + "{" * 200
    body = ('{"attributes": ["name"], "index": "' + name + '"}').encode()
    assert policy.decide("oneprovider", "GET", CHILDREN, "", body).allow
    escaped = ('{"index": "\\"' + "[" * 200 + '"}').encode()  # an escaped quote does not end the string
    assert policy.decide("oneprovider", "GET", CHILDREN, "", escaped).allow


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-32-le"])
def test_o05_the_nesting_bound_reads_the_body_as_the_parser_decodes_it(encoding):
    """O05: a body in UTF-16 or UTF-32 is decoded as the parser decodes it before its depth is counted, so an escaped
    quote and the brackets inside a string are read as the parser reads them, and a depth-one object passes."""
    body = json.dumps({"index": '"' + "[" * 40}).encode(encoding)
    assert policy.decide("oneprovider", "GET", CHILDREN, "", body).allow
    deep = ('{"a": ' + "[" * 40 + "]" * 40 + "}").encode(encoding)
    assert policy.decide("oneprovider", "GET", CHILDREN, "", deep).status == 400


def test_o19_a_listing_in_the_complete_mode_is_merged_and_one_in_the_paged_mode_is_not():
    """O19: the data path's listing mode decides whether the gate follows a listing's pages; only a listing is merged,
    and a mode the policy does not know raises rather than being taken for either."""
    complete = policy.decide("oneprovider", "GET", CHILDREN, "limit=1000", listing=policy.COMPLETE)
    assert (complete.allow, complete.merge_pages, complete.tag) == (True, True, "children")
    assert not policy.decide("oneprovider", "GET", CHILDREN, "limit=1000").merge_pages
    assert not policy.decide("oneprovider", "GET", BASE + "oneprovider/data/f1", listing=policy.COMPLETE).merge_pages
    with pytest.raises(ValueError, match="listing mode"):
        policy.decide("oneprovider", "GET", CHILDREN, listing="everything")


@pytest.mark.parametrize(
    ("query", "body"),
    [("token=t", None), ("index=a", None), ("offset=2", None), ("", b'{"token": "t"}'), ("", b'{"index": "a"}')],
)
def test_o19_a_complete_listing_naming_where_it_starts_is_refused_and_its_limit_is_not_read(query, body):
    """O19: where a complete listing starts is not the agent's to say, in the query or the body; its limit is the
    gate's, so a limit the paged mode refuses passes, while a malformed body is still refused."""
    refused = policy.decide("oneprovider", "GET", CHILDREN, query, body, listing=policy.COMPLETE)
    assert (refused.allow, refused.status, refused.reason) == (False, 400, policy.START_REFUSED)
    assert policy.decide("oneprovider", "GET", CHILDREN, "limit=5000", listing=policy.COMPLETE).allow
    assert policy.decide("oneprovider", "GET", CHILDREN, "", b'{"limit": "x"}', listing=policy.COMPLETE).allow
    assert policy.decide("oneprovider", "GET", CHILDREN, "", b"[1]", listing=policy.COMPLETE).status == 400


def test_o19_every_page_goes_with_the_agents_options_but_the_ones_the_gate_sets():
    """O19: the gate drops the agent's limit and the large-listing hint from the query and the body, keeps the rest,
    leaves a body without them as it came, and adds its page size and the token of the page before."""
    pairs, body = policy.complete_listing_request(
        "limit=7&attribute=name&tuneForLargeContinuousListing=true", b'{"limit": 9, "attributes": ["size"]}'
    )
    assert pairs == [("attribute", "name")]
    assert json.loads(body) == {"attributes": ["size"]}
    untouched = b'{ "attributes" : ["size"] }'
    assert policy.complete_listing_request("", untouched) == ([], untouched)
    assert policy.page_target("/p", pairs, None) == f"/p?attribute=name&limit={policy.MAX_LIST_LIMIT}"
    assert policy.page_target("/p", [], "a b") == f"/p?limit={policy.MAX_LIST_LIMIT}&token=a+b"


def test_o19_a_page_gives_its_entries_and_the_next_token_or_ends_the_listing():
    """O19: a page that is not the last gives the token of the next, whatever its length; the last gives none."""
    seen = set()
    page = b'{"children": [], "isLast": false, "nextPageToken": "next-a"}'
    assert policy.listing_page(page, seen) == ([], "next-a")
    entries, after = policy.listing_page(b'{"children": [{"name": "a"}], "isLast": true, "nextPageToken": null}', seen)
    assert (entries, after, seen) == ([{"name": "a"}], None, {"next-a"})


@pytest.mark.parametrize(
    "data",
    [
        b"not json",
        b"[]",
        b'{"isLast": true}',
        b'{"children": [], "isLast": 1}',
        b'{"children": [], "isLast": false}',
        b'{"children": [], "isLast": false, "nextPageToken": ""}',
        b'{"children": [], "isLast": false, "nextPageToken": "next-z"}',
        b'{"children": [NaN], "isLast": true}',
    ],
)
def test_o19_a_page_the_listing_cannot_go_on_from_is_refused(data):
    """O19: no end and no continuation, a token the listing gave before, a page that is not one, or a value JSON
    does not have: the listing cannot be completed from it, so it is never taken for its end."""
    with pytest.raises(ValueError, match="listing page"):
        policy.listing_page(data, {"next-z"})
