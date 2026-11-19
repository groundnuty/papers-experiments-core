"""B20: the scheduler gives a backend that sends a session header its attempt's opaque id, once the attempt has its
number: the campaign, the run, the attempt and the lease decide it, and a backend that sends none is not touched."""

import re

from campaign_support import make_schema
from scheduler_support import Factory, FakeExecutor, ReportingBackend, attempts_of, build, key_of, settings_for

from expcore.campaign.manifest import fingerprint_sha256
from expcore.models.fleetheaders import session_id

SMALL = {"tasks": ("task-a",), "models": ("m1",), "seeds": (1,)}


class SessionBackend(ReportingBackend):
    """A stub that, like the openai_compat adapter, names a session header and takes the id it is bound to."""

    session_header = "X-Session-Id"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bound: str | None = None

    def bind_session(self, identity: str) -> None:
        self.bound = identity


class SessionFactory(Factory):
    def __init__(self, backend_class=SessionBackend):
        super().__init__()
        self.backend_class = backend_class

    def __call__(self, spec, *, seed, limits, base_url, time_left):
        backend = self.backend_class(spec.key, seed, base_url, self.refuse.get(spec.key))
        self.built.append(backend)
        return backend


def test_f01_each_attempt_of_a_run_is_bound_to_its_own_opaque_id(tmp_path):
    """B20: two attempts of one run get two ids, each the function of the stored campaign fingerprint, the run key and
    the attempt number; the run key is not in either."""
    schema = make_schema()
    key = key_of(schema, "mount-read")
    factory = SessionFactory()
    executor = FakeExecutor({key: [("infra", "sandbox")]})
    with build(tmp_path, executor=executor, factory=factory, settings=settings_for(max_attempts=2), plan=SMALL) as run:
        run.run()
        assert attempts_of(run.store, key) == [("infra_fail", 1, 1), ("done", 0, 2)]
        sha = fingerprint_sha256(run.fingerprint)
        bound = [b.bound for b in factory.built if b.bound is not None]
        assert session_id(sha, key, 1) in bound
        assert session_id(sha, key, 2) in bound
        assert all(re.fullmatch(r"[0-9a-f]{32}", b) for b in bound)
        assert not any(key in b for b in bound)
        assert len(set(bound)) == len(bound)


def test_f01_a_backend_that_names_no_session_header_is_never_bound(tmp_path):
    """B20: the stub backends of every other test name no header and have no `bind_session`: nothing is called."""
    factory = Factory()
    with build(tmp_path, factory=factory, plan=SMALL) as run:
        run.run()
    assert all(not hasattr(b, "bind_session") for b in factory.built)


def test_f01_a_backend_whose_header_is_unset_is_left_alone(tmp_path):
    """B20: a backend that can be bound but whose model sets no header (`session_header` None) is not bound."""

    class Unset(SessionBackend):
        session_header = None

    factory = SessionFactory(Unset)
    with build(tmp_path, factory=factory, plan=SMALL) as run:
        run.run()
    assert factory.built
    assert all(b.bound is None for b in factory.built)
