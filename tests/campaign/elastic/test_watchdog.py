"""C43: the login watchdog drains a site's federations once the orchestrator has not reached the site for 30 min."""

from pathlib import Path

from channel_support import site_of
from elastic_support import FakeClock

from expcore.campaign.elastic import channel as ch
from expcore.campaign.elastic.exchange import FORMAT, handle
from expcore.campaign.elastic.watchdog import STALE_S, Watchdog


def contact(site, clock):
    handle({"format": FORMAT, "site": site.name, "root": site.root, "contact": {}, "federations": []}, clock=clock)


def two_federations(site):
    for label in ("fed-1", "fed-2"):
        ch.make_dirs(ch.federation_dir(site.root, label))


def test_c43_the_watchdog_waits_while_contacts_are_fresh_and_drains_once_after_thirty_minutes(tmp_path):
    """C43: contacts within 30 minutes keep it quiet; at 30 minutes without one it writes a drain in every
    federation's inbox and runs its command once, with the labels, and never again."""
    site, clock = site_of(tmp_path), FakeClock()
    two_federations(site)
    contact(site, clock)
    calls = []
    watchdog = Watchdog(Path(site.root), clock=clock, on_out=calls.append)
    clock.advance(STALE_S - 1)
    assert not watchdog.check()
    contact(site, clock)
    clock.advance(STALE_S - 1)
    assert not watchdog.check()
    clock.advance(1)
    assert watchdog.check()
    assert calls == [["fed-1", "fed-2"]]
    for label in ("fed-1", "fed-2"):
        inbox = ch.federation_dir(site.root, label) / ch.INBOX
        assert ch.read_listed(inbox, ch.listing(inbox), ch.DRAIN)["format"] == ch.DRAIN_FORMAT
    assert watchdog.check()
    assert calls == [["fed-1", "fed-2"]]


def test_c43_without_any_contact_the_watchdog_counts_from_its_own_start(tmp_path):
    """C43: a site never reached since the watchdog started is stale 30 minutes after that start, not at once."""
    site, clock = site_of(tmp_path), FakeClock()
    two_federations(site)
    watchdog = Watchdog(Path(site.root), clock=clock)
    clock.advance(STALE_S - 1)
    assert not watchdog.check()
    clock.advance(1)
    assert watchdog.check()


def test_c43_the_watchdog_is_bounded_and_exits_quiet_at_its_end(tmp_path):
    """C43: with contacts arriving, it exits 0 at `--until` having done nothing; nothing is drained."""
    site, clock = site_of(tmp_path), FakeClock()
    two_federations(site)
    watchdog = Watchdog(Path(site.root), clock=clock)

    def sleep(seconds):
        clock.advance(seconds)
        contact(site, clock)

    assert watchdog.serve(until=clock() + 4 * STALE_S, sleep=sleep, check_s=60.0) == 0
    inbox = ch.federation_dir(site.root, "fed-1") / ch.INBOX
    assert ch.DRAIN not in ch.listing(inbox)
