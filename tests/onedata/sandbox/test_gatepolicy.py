"""O05, O16: only read calls pass the data gate; everything else is refused as a decision, not an error."""

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
