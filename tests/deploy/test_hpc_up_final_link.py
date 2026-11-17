"""C06, O63: `up`'s last write, `linked`, is a compare-and-set on `bootstrapped` and never over a core phase that
failed.

`up` says `linked` once its own core phase has ended, under the root's core-phase lock. That write was unconditional, so
it overwrote a phase another command had written since (a `down`'s `stopping`), and a rerun of the core's phase by hand
that began after `up`'s own ended and failed its link check (Codex third pass, 17 Nov 2026, item 423): the rerun left
the phase `bootstrapped`, which the write then replaced with `linked`, over a check that had just failed. The write now
moves `bootstrapped` to `linked` only, and a core phase that began and did not end (it failed, or died) is recorded in
the state, so `up` stays `bootstrapped` and says why. Slurm is the fake of hpc_fed_support, the bootstrap, the link
check and the space reads are stand-ins, and nothing reaches a cluster."""

import pytest
from hpc_fed_support import hpc_on_path
from hpc_ingested_support import artifacts, driver_over, held, real_core, site_env, study_layout
from hpc_rehearsal_support import link_check_stand_in

from expcore_onedata.linkcheck import LinkReport, PairResult

STOPPING_NOT_OVERWRITTEN = "the federation's phase is not bootstrapped"


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as modules:
        import fedlinks

        modules.fedlinks = fedlinks
        yield modules


@pytest.fixture(scope="module")
def ca(tmp_path_factory) -> bytes:
    tls = pytest.importorskip("tls_support")
    if not tls.HAS_OPENSSL:
        pytest.skip("no openssl on this host")
    return tls.make_federation_ca(tmp_path_factory.mktemp("ca")).ca.read_bytes()


def failing_links(config, client) -> LinkReport:
    """`fedlinks.check_links` finding a pair that does not transfer."""
    pair = PairResult("providers[0]", "providers[1]", False, reason="the fetch failed")
    return LinkReport(False, "providers[0] <- providers[1]: the fetch failed", (pair,), 1.0, 2.0)


def about_to_up(hpc, tmp_path, ca):
    """A driver over the fake cluster and the real core's phase on stand-ins, whose `up` has not run."""
    fed = driver_over(hpc, tmp_path / "fed", study_layout(tmp_path / "layout"), "fed-a", ca)
    fed.site = site_env(tmp_path / "fed", artifacts(tmp_path / "art"))
    real_core(hpc, fed, held({}))
    return fed


def run_up(hpc, fed) -> str:
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", link_check_stand_in([]))
        return fed.driver.up(hpc.settings.site_from_environment(fed.site))


def after_its_core_phase(fed, action) -> None:
    """`up`'s own core phase runs and ends, then `action` happens before `up` writes its last phase."""
    real = fed.driver.run_core

    def run() -> str:
        out = real()
        action()
        return out

    fed.driver.run_core = run


def rerun_by_hand(hpc, fed, links) -> None:
    """`hpcfed.py core` started on the root once `up`'s own core phase has released the lock."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hpc.fedlinks, "check_links", links)
        fed.driver.core_here()


def phase(fed) -> str:
    return fed.driver.store.read()["phase"]


def test_c06_up_ends_linked_when_nothing_else_wrote_the_state(hpc, tmp_path, ca):
    """C06, O63: `up` is unchanged where nothing happened in the window: its core phase ended, and it says `linked`."""
    fed = about_to_up(hpc, tmp_path, ca)
    run_up(hpc, fed)
    assert phase(fed) == "linked"


def test_c06_a_phase_a_down_wrote_after_the_core_phase_is_never_overwritten_by_up(hpc, tmp_path, ca):
    """C06, O63: a `down` that began after `up`'s core phase ended wrote `stopping`: `up`'s write found it and, being a
    compare-and-set on `bootstrapped`, changes nothing; `up` fails, saying the phase is not the one it waited for, and
    the federation stays `stopping` for the `down` to finish."""
    fed = about_to_up(hpc, tmp_path, ca)
    after_its_core_phase(fed, lambda: fed.driver.set(lambda s: s.update(phase="stopping")))
    with pytest.raises(RuntimeError, match=STOPPING_NOT_OVERWRITTEN) as refused:
        run_up(hpc, fed)
    assert "stopping" in str(refused.value)
    assert phase(fed) == "stopping"


def test_c06_up_does_not_link_over_a_rerun_by_hand_whose_check_failed_after_its_own_core_phase(hpc, tmp_path, ca):
    """C06, O63 (Codex, 17 Nov 2026): the rerun began once `up`'s own core phase had released the lock, failed its link
    check and released it too; `up`'s write then took the lock and made the federation `linked`, though the latest
    check had failed. The failed phase is recorded as not ended, so `up` stays `bootstrapped` and fails saying why, and
    `shards.py plan` goes on refusing the federation."""
    fed = about_to_up(hpc, tmp_path, ca)

    def failing_rerun() -> None:
        with pytest.raises(RuntimeError, match="the link check failed"):
            rerun_by_hand(hpc, fed, failing_links)

    after_its_core_phase(fed, failing_rerun)
    with pytest.raises(RuntimeError, match="did not end"):
        run_up(hpc, fed)
    assert phase(fed) == "bootstrapped"


def test_c06_up_does_not_link_over_a_core_phase_that_died_after_its_own(hpc, tmp_path, ca):
    """C06, O63: a rerun that never ended (the login node's session lost, a step killed: it raised nothing `up` could
    see) is as unfinished as a failed one: the mark is made when the phase starts and cleared only when it returns."""
    fed = about_to_up(hpc, tmp_path, ca)

    def dies() -> None:
        def killed(layout, owner, values, environ=None):
            raise KeyboardInterrupt

        real = fed.driver.core
        fed.driver.core = killed
        try:
            with pytest.raises(KeyboardInterrupt):
                fed.driver.core_here()
        finally:
            fed.driver.core = real

    after_its_core_phase(fed, dies)
    with pytest.raises(RuntimeError, match="did not end"):
        run_up(hpc, fed)
    assert phase(fed) == "bootstrapped"


def test_c06_up_links_over_a_rerun_by_hand_that_passed_after_its_own_core_phase(hpc, tmp_path, ca):
    """C06, O63: nothing newer than a passing check is held against `up`: a rerun by hand in the window whose link
    check passed leaves `bootstrapped` as `up`'s own did, and `up` says `linked`."""
    fed = about_to_up(hpc, tmp_path, ca)
    after_its_core_phase(fed, lambda: rerun_by_hand(hpc, fed, link_check_stand_in([])))
    run_up(hpc, fed)
    assert phase(fed) == "linked"


def test_c06_a_rerun_that_passes_after_one_that_failed_clears_the_mark_and_up_links(hpc, tmp_path, ca):
    """C06, O63: the mark is the latest core phase's: a failed rerun and then a passing one leave nothing against
    `up`, which says `linked`."""
    fed = about_to_up(hpc, tmp_path, ca)

    def failed_then_passed() -> None:
        with pytest.raises(RuntimeError, match="the link check failed"):
            rerun_by_hand(hpc, fed, failing_links)
        rerun_by_hand(hpc, fed, link_check_stand_in([]))

    after_its_core_phase(fed, failed_then_passed)
    run_up(hpc, fed)
    assert phase(fed) == "linked"
