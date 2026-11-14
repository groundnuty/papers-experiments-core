"""A provider's panel can answer 500 while it is still settling after its start, and the first up of a live slot failed
on one such read of its storages ("GET <provider> panel storages: answered 500", the retry passed). Up's panel reads are
tried again, a bounded number of times, with a backoff that stays within a bound; a panel write is never tried again,
since a support call that was received twice could support a space twice."""

import pytest


class FakeRest:
    """The federation's REST answers, scripted per (method, URL suffix): a list of outcomes, an exception raised or a
    value returned, taken in turn, the last one repeating. Every call is recorded."""

    def __init__(self, fed, script):
        self.fed, self.script, self.calls = fed, {key: list(outcomes) for key, outcomes in script.items()}, []

    def json(self, method, url, **options):
        self.calls.append((method, url))
        for (verb, suffix), outcomes in self.script.items():
            if verb == method and url.endswith(suffix):
                outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"an unscripted call: {method} {url}")

    def count(self, method, suffix):
        return sum(1 for verb, url in self.calls if verb == method and url.endswith(suffix))


@pytest.fixture
def rest_module(fed):
    """The deploy scripts' REST module, importable once `fed` has put them on the path."""
    import fedrest

    return fedrest


def error(status):
    import fedrest

    return fedrest.RestError(f"answered {status}", status)


def bootstrap(fed, script):
    """A bootstrap of the default layout over scripted answers, whose waits are recorded, never slept."""
    spec = fed.spec.load_spec(None)
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.rest, boot.spec = FakeRest(fed, script), spec
    boot.ids = fed.boot.Ids(
        providers={p.name: f"pid-{p.name}" for p in spec.providers},
        spaces={s.name: f"sid-{s.name}" for s in spec.spaces},
    )
    boot._admin_token = "a-test-token"
    boot.waits = []
    boot._sleep = boot.waits.append
    return boot


STORAGES = ("GET", "/provider/storages")
SUPPORT = ("POST", "/provider/spaces")


def test_a_panel_read_answered_500_twice_then_200_returns_the_answer(fed, rest_module):
    """The slot's case: two answers of 500, then the storages. The reads are tried again after 1 s and 2 s."""
    answers = [error(500), error(500), {"ids": ["storage-1"]}]
    boot = bootstrap(fed, {STORAGES: answers})
    service = boot.spec.providers[0]
    assert boot.panel(service, "GET", "/provider/storages", "storages") == {"ids": ["storage-1"]}
    assert boot.rest.count(*STORAGES) == 3
    assert boot.waits == [1.0, 2.0]


@pytest.mark.parametrize("failure", [500, 502, 503, 504, None], ids=["500", "502", "503", "504", "no answer"])
def test_a_panel_read_that_the_panel_could_not_answer_yet_is_tried_again(fed, rest_module, failure):
    """Any answer of a server failure, and a read that got no answer at all, is a panel not ready yet."""
    boot = bootstrap(fed, {STORAGES: [error(failure), {"ids": ["storage-1"]}]})
    assert boot.panel(boot.spec.providers[0], "GET", "/provider/storages", "storages")["ids"] == ["storage-1"]
    assert boot.rest.count(*STORAGES) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_a_panel_read_the_panel_refused_is_not_tried_again(fed, rest_module, status):
    """A client error does not pass by waiting: it is raised at once, as it was before."""
    boot = bootstrap(fed, {STORAGES: [error(status)]})
    with pytest.raises(rest_module.RestError) as refused:
        boot.panel(boot.spec.providers[0], "GET", "/provider/storages", "storages")
    assert refused.value.status == status
    assert boot.rest.count(*STORAGES) == 1
    assert boot.waits == []


def test_a_panel_read_that_keeps_failing_is_given_up_after_a_bounded_number_of_tries(fed, rest_module):
    """Five tries and four waits that grow and stop growing at 10 s; the last error is the one raised."""
    boot = bootstrap(fed, {STORAGES: [error(500)]})
    with pytest.raises(rest_module.RestError) as given_up:
        boot.panel(boot.spec.providers[0], "GET", "/provider/storages", "storages")
    assert given_up.value.status == 500
    assert boot.rest.count(*STORAGES) == fed.boot.PANEL_READ_TRIES == 5
    assert boot.waits == [1.0, 2.0, 4.0, 8.0]
    assert max(boot.waits) <= fed.boot.PANEL_READ_BACKOFF_CAP_S == 10.0
    assert sum(boot.waits) < 60  # well inside up's own timeouts


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_a_panel_write_is_never_tried_again(fed, rest_module, method):
    """A write that failed with a server error may have been received: it is raised, once, with no wait."""
    boot = bootstrap(fed, {(method, "/provider/spaces"): [error(500), {}]})
    with pytest.raises(rest_module.RestError):
        boot.panel(boot.spec.providers[0], method, "/provider/spaces", "support", json={"token": "t"})
    assert boot.rest.count(method, "/provider/spaces") == 1
    assert boot.waits == []


def test_ensure_support_survives_a_transient_storages_read_and_sends_each_support_call_once(fed, rest_module):
    """Up's own step: the first storages read fails twice and then answers, and every support call, the write, is sent
    exactly once."""
    spec = fed.spec.load_spec(None)
    script = {
        ("GET", "/providers"): [{"providers": []}],
        STORAGES: [error(500), error(500), {"ids": ["storage-1"]}],
        ("POST", "/user/tokens/temporary"): [{"token": "invite"}],
        SUPPORT: [{}],
    }
    boot = bootstrap(fed, script)
    boot.provider = lambda service, method, path, label, **options: {"fileId": "root"}  # each space serves its root
    boot.ensure_support(timeout_s=30)
    supports = sum(len(space.supporters) for space in spec.spaces)
    assert boot.rest.count(*SUPPORT) == supports
    assert boot.rest.count(*STORAGES) == supports + 2  # one read each, and the first one's two failed tries
    assert boot.waits == [1.0, 2.0]


def test_ensure_support_does_not_send_a_support_call_again_when_it_failed(fed, rest_module):
    """The write of up's support step fails once: up fails with it, and the call was sent once."""
    script = {
        ("GET", "/providers"): [{"providers": []}],
        STORAGES: [{"ids": ["storage-1"]}],
        ("POST", "/user/tokens/temporary"): [{"token": "invite"}],
        SUPPORT: [error(500)],
    }
    boot = bootstrap(fed, script)
    boot.provider = lambda service, method, path, label, **options: {"fileId": "root"}
    with pytest.raises(rest_module.RestError):
        boot.ensure_support(timeout_s=30)
    assert boot.rest.count(*SUPPORT) == 1
    assert boot.waits == []
