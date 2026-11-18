"""C43: the channel's files: written whole and once, read only after a listing shows them, and never half-written."""

import os

from channel_support import site_of

from expcore.campaign.elastic import channel as ch
from expcore.campaign.elastic.exchange import FORMAT, handle

LABEL = "fed-1"


def request(site, **entry):
    return {
        "format": FORMAT,
        "site": site.name,
        "root": site.root,
        "contact": {"seq": 1},
        "federations": [{"label": LABEL, **entry}],
    }


def completion(lease_id: int) -> dict:
    return {
        "format": ch.COMPLETION_FORMAT,
        "federation": LABEL,
        "lease_id": lease_id,
        "run_key": "a run",
        "archive_sha256": "0" * 64,
        "state": "done",
        "charged": 1,
        "spend_usd": 0.1,
    }


def test_c43_a_name_is_written_once_so_a_late_delivery_and_a_tombstone_never_both_land(tmp_path):
    """C43: a lease file and its tombstone share one name, created only where no file has it: the second of the two
    is refused and the first stays as it was written."""
    path = tmp_path / ch.lease_name(4)
    assert ch.create_json(path, ch.tombstone(LABEL, "site-a", 4))
    assert not ch.create_json(path, {"format": ch.LEASE_FORMAT, "federation": LABEL, "lease": {"lease_id": 4}})
    assert ch.is_tombstone(ch.read_listed(tmp_path, ch.listing(tmp_path), path.name))
    assert ch.listing(tmp_path) == {path.name}  # no draft is left beside it


def test_c43_a_draft_or_a_file_that_does_not_parse_is_never_read_from_an_outbox(tmp_path):
    """C43: a writer's draft (a dotted name) is not listed, and a file that is not one JSON object is passed over:
    the reading holds the whole completion only."""
    site = site_of(tmp_path)
    outbox = ch.federation_dir(site.root, LABEL) / ch.OUTBOX
    ch.make_dirs(outbox.parent)
    ch.write_json(outbox / ch.completion_name(1), completion(1))
    (outbox / f".{ch.completion_name(2)}.0123").write_text('{"format": "expcore-elastic-comp', encoding="utf-8")
    (outbox / ch.completion_name(3)).write_text('{"format": "expcore-elastic-completion/1", "lease', encoding="utf-8")
    (outbox / ch.HEARTBEAT).write_text("[1, 2]", encoding="utf-8")
    box = handle(request(site))["federations"][LABEL]["outbox"]
    assert [item["lease_id"] for item in box["completions"]] == [1]
    assert box["completions"][0]["record"] == completion(1)
    assert box["heartbeat"] is None


def test_c43_a_file_is_read_only_once_a_listing_shows_its_name(tmp_path, monkeypatch):
    """C43: an NFS client keeps a failed lookup for up to 30 s, so a reader opens a file only after a fresh listing
    shows its exact name. A listing that does not show a completion (stale) leaves it unread, and the next listing
    that shows it reads it."""
    site = site_of(tmp_path)
    outbox = ch.federation_dir(site.root, LABEL) / ch.OUTBOX
    ch.make_dirs(outbox.parent)
    ch.write_json(outbox / ch.completion_name(1), completion(1))
    real, stale, listed = os.scandir, [True], []

    class Hidden:
        def __init__(self, entries):
            self.entries = [entry for entry in entries if not entry.name.startswith("completion-")]

        def __enter__(self):
            return iter(self.entries)

        def __exit__(self, *exc):
            return False

    def scandir(path=None):
        if path is not None and os.fspath(path) == str(outbox):
            listed.append(stale[0])
            if stale[0]:
                with real(path) as entries:
                    return Hidden(list(entries))
        return real() if path is None else real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert handle(request(site))["federations"][LABEL]["outbox"]["completions"] == []
    stale[0] = False
    assert [item["lease_id"] for item in handle(request(site))["federations"][LABEL]["outbox"]["completions"]] == [1]
    assert listed == [True, False]


def test_c43_a_delivery_of_a_lease_already_in_the_inbox_changes_nothing_and_says_so(tmp_path):
    """C43: the same lease delivered twice (a call retried after a lost reply) is written once; a lease whose name a
    tombstone holds is never delivered over it, and the reply says the name holds a tombstone."""
    site = site_of(tmp_path)
    lease = {
        "format": ch.LEASE_FORMAT,
        "federation": LABEL,
        "site": site.name,
        "lease": {"lease_id": 9, "run_key": "a run"},
    }
    first = handle(request(site, deliver=[lease]))["federations"][LABEL]
    again = handle(request(site, deliver=[lease]))["federations"][LABEL]
    assert first["inbox"] == again["inbox"] == {"9": "lease"}
    settled = handle(request(site, settle=[10]))["federations"][LABEL]
    assert settled["inbox"] == {"10": "tombstone"}
    late = handle(request(site, deliver=[{**lease, "lease": {"lease_id": 10, "run_key": "a run"}}]))
    assert late["federations"][LABEL]["inbox"] == {"10": "tombstone"}
    inbox = ch.federation_dir(site.root, LABEL) / ch.INBOX
    assert ch.listing(inbox) == {ch.lease_name(9), ch.lease_name(10)}


def test_c43_the_exchange_stamps_the_contact_with_the_login_nodes_clock(tmp_path):
    """C43: `contact.json` carries the time of the host that wrote it, which the watchdog on that host reads."""
    site = site_of(tmp_path)
    handle(request(site), clock=lambda: 1234.5)
    contact = ch.read_listed(
        tmp_path / site.name / "channels", ch.listing(tmp_path / site.name / "channels"), ch.CONTACT
    )
    assert (contact["format"], contact["at"], contact["seq"], contact["site"]) == (
        ch.CONTACT_FORMAT,
        1234.5,
        1,
        site.name,
    )
