"""Leased platform users for the two planes: slots through the core's lease pool, and tokens no free slot outlives."""

import os
import stat

import pytest

from expcore.campaign.lease import LeasePool
from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.testing import BASELINE, CEILING_S, CREDENTIALS_DIR, MANAGE, NOW, with_placement, write_platform
from expcore_onedata.tokencheck import TokenRefused
from expcore_onedata.tokens import DroppedToken, ExpiryMismatch, TokenIssuer, token_window
from expcore_onedata.users import (
    EVENT_EXPIRY_BEYOND_REQUEST,
    GROUP,
    SAFETY_MARGIN_S,
    SlotProbe,
    SlotTokens,
    TokenJournal,
    load_slots,
)

LOGINS = {  # login name -> (password, user id)
    "slot-one-data": ("pw-1d", "u-1d"),
    "slot-one-tool": ("pw-1t", "u-1t"),
    "slot-two-data": ("pw-2d", "u-2d"),
    "slot-two-tool": ("pw-2t", "u-2t"),
}


def no_events(kind, detail):
    """The event sink of a test that is not about events."""


def declare(data, *, second_data="slot-two-data", second_tool="slot-two-tool"):
    """Add two slots to a platform document; the second slot's users can be swapped to provoke a refusal."""
    with_placement(data)
    data["slots"] = {
        "one": {
            "data_path": {"username": "slot-one-data", "user_id": "u-1d"},
            "toolserver": {"username": "slot-one-tool", "user_id": "u-1t"},
        },
        "two": {
            "data_path": {"username": second_data, "user_id": LOGINS.get(second_data, ("", second_data))[1]},
            "toolserver": {"username": second_tool, "user_id": LOGINS.get(second_tool, ("", second_tool))[1]},
        },
    }


@pytest.fixture
def slot_federation(fake):
    """The fake federation with the four slot users registered, able to log in, with the privileges of their plane."""
    for login, (password, user_id) in LOGINS.items():
        fake.add_user(login, password, user_id)
        fake.grant(user_id, BASELINE)
    return fake


def write(tmp_path, **kw):
    directory = tmp_path / CREDENTIALS_DIR
    directory.mkdir(exist_ok=True)
    for login, (password, _) in LOGINS.items():
        file = directory / f"{login}.pass"
        file.write_text(password + "\n", encoding="utf-8")
        os.chmod(file, 0o600)
    return write_platform(tmp_path, lambda d: declare(d, **kw))


@pytest.fixture
def slots(tmp_path, slot_federation):
    path = write(tmp_path)
    config = load_platform(path)
    return config, load_slots(path, config)


def test_slots_are_read_in_order_with_a_user_for_each_plane(slots):
    _, loaded = slots
    assert loaded.keys_of(GROUP) == ["one", "two"]
    assert loaded.get("one").data_path.user_id == "u-1d"
    assert loaded.get("one").tool_server.user_id == "u-1t"


def test_a_group_the_layer_does_not_declare_is_refused_not_read_as_empty(slots):
    with pytest.raises(ValueError, match="platform_users"):
        slots[1].keys_of("typo")


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"second_data": "slot-one-data"}, r"slots\[2\]: key 'data_path' names a user that another"),
        ({"second_tool": "login-tools"}, r"slots\[2\]: key 'toolserver' names a user that another"),
        ({"second_data": "slot-two-data", "second_tool": "slot-two-data"}, r"key 'toolserver' names a user"),
    ],
)
def test_a_user_used_twice_is_refused_naming_the_slot_and_the_plane_and_not_the_user(
    tmp_path, slot_federation, kw, message
):
    """C19: two runs sharing a principal would make the audit log ambiguous, so a user belongs to one slot and plane."""
    path = write(tmp_path, **kw)
    with pytest.raises(ConfigError, match=message) as raised:
        load_slots(path, load_platform(path))
    assert "slot-one-data" not in str(raised.value)
    assert "login-tools" not in str(raised.value)


def test_a_slot_user_without_a_credential_file_is_refused_at_load(tmp_path, slot_federation):
    path = write(tmp_path)
    (tmp_path / CREDENTIALS_DIR / "slot-two-tool.pass").unlink()
    with pytest.raises(ConfigError, match=r"slots\[2\]: key 'toolserver' its credential cannot be used"):
        load_slots(path, load_platform(path, require_secrets=False))


def test_secrets_need_not_exist_when_the_caller_does_not_ask_for_them(tmp_path, slot_federation):
    path = write(tmp_path)
    (tmp_path / CREDENTIALS_DIR / "slot-two-tool.pass").unlink()
    assert len(load_slots(path, load_platform(path, require_secrets=False), require_secrets=False).slots) == 2


def test_a_platform_without_slots_is_refused_naming_the_key(tmp_path, slot_federation):
    path = write_platform(tmp_path)
    with pytest.raises(ConfigError, match=r"platform\.toml: key 'slots' is required"):
        load_slots(path, load_platform(path))


def test_a_slot_name_that_is_not_a_plain_name_is_refused(tmp_path, slot_federation):
    path = write(tmp_path)
    path.write_text(path.read_text().replace("[slots.one", "[slots.1one"), encoding="utf-8")
    with pytest.raises(ConfigError, match="starting with a letter"):
        load_slots(path, load_platform(path))


def test_c19_two_runs_hold_two_different_slots_and_a_third_waits(slots, tmp_path):
    """C19: the core's pool gives each run one slot, with its two users; with none free a run gets no lease."""
    _, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    first, second = pool.acquire(GROUP, timeout_s=0), pool.acquire(GROUP, timeout_s=0)
    assert {first.key, second.key} == {"one", "two"}
    users = {loaded.users_of(first).data_path.user_id, loaded.users_of(second).data_path.user_id}
    assert users == {"u-1d", "u-2d"}
    assert pool.try_acquire(GROUP) is None
    first.release()
    again = pool.try_acquire(GROUP)
    assert again is not None
    assert again.key == first.key


def test_users_of_a_lease_that_was_released_or_belongs_to_another_group_is_refused(slots, tmp_path):
    _, loaded = slots
    pool = LeasePool(tmp_path / "leases", lambda group: ["one"], SlotProbe(TokenJournal(tmp_path / "leases")))
    other = pool.acquire("elsewhere", timeout_s=0)
    with pytest.raises(ValueError, match="not a held lease"):
        loaded.users_of(other)
    other.release()
    held = LeasePool(tmp_path / "l2", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "l2"))).acquire(GROUP)
    held.release()
    with pytest.raises(ValueError, match="not a held lease"):
        loaded.users_of(held)


class SpyJournal(TokenJournal):
    """A journal that notes how many tokens the federation had minted when each line was written, and can be made to
    fail on an exact line, as a process that dies after minting would."""

    def __init__(self, directory, fake, *, crash_on_exact=False):
        super().__init__(directory)
        self.fake, self.crash_on_exact = fake, crash_on_exact
        self.minted_at_write, self.kinds = [], []

    def record(self, slot, plane, expires_at, *, mint=None, provisional=False):
        if self.crash_on_exact and not provisional:
            raise SystemExit("the process died after minting")
        self.minted_at_write.append(len([r for r in self.fake.requests if r.path.endswith("/tokens/temporary")]))
        self.kinds.append("provisional" if provisional else "exact")
        super().record(slot, plane, expires_at, mint=mint, provisional=provisional)


def test_tokens_of_a_leased_slot_are_minted_on_its_two_users_and_their_end_is_journalled(slots, client, tmp_path, fake):
    """C19: each plane's token is minted by that plane's slot user. A provisional line is written before anything is
    minted, and once both are minted the exact line holds the later of their examined expiries and settles it."""
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = SpyJournal(tmp_path / "leases", fake)
    tokens = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, events=no_events, clock=lambda: NOW
    ).mint(lease, ceiling_s=CEILING_S, condition="read")
    slot = loaded.users_of(lease)
    assert tokens.data_path.subject == slot.data_path.user_id
    assert tokens.tool_server is not None
    assert tokens.tool_server.subject == slot.tool_server.user_id
    assert journal.kinds == ["provisional", "exact"]
    assert journal.minted_at_write == [0, 2]  # the provisional line before any token, the exact one after both
    assert journal.latest_expiry(lease.key) == max(tokens.data_path.expires_at, tokens.tool_server.expires_at)
    assert tokens.data_path.reveal() not in (tmp_path / "leases" / "tokens.jsonl").read_text()


def time_caveats(fake):
    """The expiry in the time caveat of every token the federation has issued."""
    return {c["validUntil"] for token in fake.issued.values() for c in token["caveats"] if c["type"] == "time"}


def test_c19_a_crash_after_minting_leaves_the_provisional_end_and_the_slot_stays_held(slots, client, tmp_path, fake):
    """C19: the process dies after the tokens exist and before their exact line is written. The journal holds only the
    provisional line, whose end is the absolute expiry both tokens were minted with; the slot is held until that end
    plus the margin."""
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = SpyJournal(tmp_path / "leases", fake, crash_on_exact=True)
    minter = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, events=no_events, clock=lambda: NOW
    )
    with pytest.raises(SystemExit):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    assert len(fake.issued) == 2  # both tokens exist
    provisional = int(NOW) + token_window(CEILING_S, config.tokens).mint_ttl_s
    assert time_caveats(fake) == {provisional}
    assert journal.kinds == ["provisional"]
    assert TokenJournal(tmp_path / "leases").latest_expiry(lease.key) == provisional
    for moment, freed in (
        (provisional + SAFETY_MARGIN_S - 1, False),
        (provisional + SAFETY_MARGIN_S, False),
        (provisional + SAFETY_MARGIN_S + 1, True),
    ):
        probe = SlotProbe(TokenJournal(tmp_path / "leases"), clock=lambda moment=moment: moment)
        assert probe.resource_gone(lease.key, {}) is freed


def pause_before_each_mint(client, monkeypatch, now, seconds):
    """Move the clock on by `seconds` just before each token request, as slow privilege reads would."""
    mint = client.mint_own_token

    def slow(principal, caveats):
        now[0] += seconds
        return mint(principal, caveats)

    monkeypatch.setattr(client, "mint_own_token", slow)


def test_c19_a_pause_before_the_mint_cannot_push_a_tokens_expiry_past_its_journal_line(
    slots, client, tmp_path, monkeypatch
):
    """C19 with O06: the clock moves 100 s before each mint call. The expiry was computed once, before the journal
    line, and both tokens are minted with exactly it, so each token's expiry equals the journalled one; the slot is
    free only past it plus the margin."""
    config, loaded = slots
    now = [NOW]
    pause_before_each_mint(client, monkeypatch, now, 100.0)
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    tokens = SlotTokens(
        TokenIssuer(client, config, clock=lambda: now[0]),
        config,
        loaded,
        journal,
        events=no_events,
        clock=lambda: now[0],
    ).mint(lease, ceiling_s=CEILING_S, condition="read")
    assert tokens.tool_server is not None
    journalled = int(NOW) + token_window(CEILING_S, config.tokens).mint_ttl_s
    assert now[0] == NOW + 200.0  # the pauses happened
    assert tokens.data_path.expires_at == tokens.tool_server.expires_at == journalled
    assert journal.latest_expiry(lease.key) == journalled
    freed = []
    for moment in (journalled + SAFETY_MARGIN_S - 1, journalled + SAFETY_MARGIN_S, journalled + SAFETY_MARGIN_S + 1):
        now[0] = moment
        freed.append(SlotProbe(journal, clock=lambda: now[0]).resource_gone(lease.key, {}))
    assert freed == [False, False, True]


def test_c19_a_pause_so_long_that_a_token_would_be_too_short_refuses_it_and_the_journal_still_covers_it(
    slots, client, tmp_path, monkeypatch, fake
):
    """C19: 200 s before each mint call leaves the second token less life than the run needs, so it is refused. Both
    tokens exist, both end at the journalled expiry, and the journal holds it."""
    config, loaded = slots
    now = [NOW]
    pause_before_each_mint(client, monkeypatch, now, 200.0)
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    minter = SlotTokens(
        TokenIssuer(client, config, clock=lambda: now[0]),
        config,
        loaded,
        journal,
        events=no_events,
        clock=lambda: now[0],
    )
    with pytest.raises(TokenRefused, match="shorter"):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    journalled = int(NOW) + token_window(CEILING_S, config.tokens).mint_ttl_s
    assert len(fake.issued) == 2
    assert time_caveats(fake) == {journalled}
    assert journal.latest_expiry(lease.key) == journalled


def test_c19_a_token_that_lives_longer_than_the_journalled_expiry_is_refused_and_its_expiry_recorded(
    slots, client, tmp_path, slot_federation
):
    """C19: the platform hands back a token 50 s longer than asked for. It is refused and the expiry it has is
    journalled, so the slot is held until that expiry plus the margin."""
    config, loaded = slots
    slot_federation.mint_hook = lambda user, caveats: [
        {**c, "validUntil": c["validUntil"] + 50} if c["type"] == "time" else c for c in caveats
    ]
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    minter = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, events=no_events, clock=lambda: NOW
    )
    with pytest.raises(ExpiryMismatch):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    journalled = int(NOW) + token_window(CEILING_S, config.tokens).mint_ttl_s
    assert journal.latest_expiry(lease.key) == journalled + 50
    assert SlotProbe(journal, clock=lambda: journalled + 50 + SAFETY_MARGIN_S).resource_gone(lease.key, {}) is False
    assert SlotProbe(journal, clock=lambda: journalled + 51 + SAFETY_MARGIN_S).resource_gone(lease.key, {}) is True


def test_c19_a_mint_that_fails_after_a_token_exists_leaves_a_journal_line_that_covers_it(
    slots, client, tmp_path, slot_federation
):
    """C19: the tool-server user holds other rights than the condition declares, so the mint is refused after the
    data path's token was minted (and dropped, not revoked). The slot is not free before that token ends."""
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    slot_federation.grant(loaded.users_of(lease).tool_server.user_id, MANAGE)
    journal = TokenJournal(tmp_path / "leases")
    minter = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, events=no_events, clock=lambda: NOW
    )
    with pytest.raises(TokenRefused):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    expiry = int(NOW) + token_window(CEILING_S, config.tokens).mint_ttl_s
    assert time_caveats(slot_federation) == {expiry}  # the data path's token
    assert journal.latest_expiry(lease.key) == expiry
    assert SlotProbe(journal, clock=lambda: expiry + SAFETY_MARGIN_S).resource_gone(lease.key, {}) is False


def test_a_ceiling_the_window_refuses_journals_and_mints_nothing(slots, client, tmp_path, fake):
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    minter = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, events=no_events, clock=lambda: NOW
    )
    with pytest.raises(TokenRefused, match="finite number"):
        minter.mint(lease, ceiling_s=float("inf"), condition="read")
    assert journal.latest_expiry(lease.key) is None
    assert not [r for r in fake.requests if r.path.endswith("/tokens/temporary")]


def watch_fsync(monkeypatch, events):
    """Record every os.fsync as a file's or a directory's, and still do it."""
    real = os.fsync

    def fsync(descriptor):
        events.append("fsync directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "fsync file")
        real(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)


def test_c19_the_provisional_line_is_durable_before_anything_is_minted(slots, client, tmp_path, monkeypatch):
    """C19: the line is written, flushed and fsynced before the mint; the directory holding the new file is fsynced,
    and so is the parent of every directory the append created (here two, so three directory syncs); the exact line,
    written to a file that exists, is fsynced too."""
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    events = []
    real_mint = issuer.mint_run_tokens

    def mint(**kwargs):
        events.append("mint")
        return real_mint(**kwargs)

    monkeypatch.setattr(issuer, "mint_run_tokens", mint)
    watch_fsync(monkeypatch, events)
    journal = TokenJournal(tmp_path / "fresh" / "journal")  # neither the directory nor the file exists yet
    SlotTokens(issuer, config, loaded, journal, events=no_events, clock=lambda: NOW).mint(
        lease, ceiling_s=CEILING_S, condition="read"
    )
    assert events == ["fsync file", "fsync directory", "fsync directory", "fsync directory", "mint", "fsync file"]


def test_c19_a_journal_line_that_cannot_be_made_durable_stops_the_mint(slots, client, tmp_path, fake, monkeypatch):
    """C19: if the provisional line cannot be fsynced, nothing is minted: a token must never exist that the journal
    might not hold."""
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)

    def failing(descriptor):
        raise OSError("the disk refused")

    monkeypatch.setattr(os, "fsync", failing)
    minter = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW),
        config,
        loaded,
        TokenJournal(tmp_path / "journal"),
        events=no_events,
        clock=lambda: NOW,
    )
    with pytest.raises(OSError, match="refused"):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    assert not [r for r in fake.requests if r.path.endswith("/tokens/temporary")]


def test_c19_an_exact_line_settles_the_provisional_line_of_its_mint_and_only_that_one(tmp_path):
    """C19: the exact line of a mint replaces its provisional line; another mint's provisional line, a provisional
    line nothing settled and every exact line still count, and the slot's end is the latest of those."""
    journal = TokenJournal(tmp_path)
    journal.record("one", "provisional", NOW + 900, mint="m1", provisional=True)
    assert journal.latest_expiry("one") == NOW + 900
    journal.record("one", "data_path", NOW + 700, mint="m1")
    assert journal.latest_expiry("one") == NOW + 700  # settled: the exact end stands for the mint
    journal.record("one", "provisional", NOW + 650, mint="m2", provisional=True)
    assert journal.latest_expiry("one") == NOW + 700  # an earlier exact line is never lowered
    journal.record("one", "provisional", NOW + 1200, mint="m3", provisional=True)
    assert journal.latest_expiry("one") == NOW + 1200  # a mint that never settled stands at its worst case
    assert journal.latest_expiry("two") is None


def test_c19_a_provisional_line_without_its_mint_id_keeps_the_slot_quarantined(tmp_path):
    """C19: a provisional line that cannot say which mint it belongs to cannot be settled, so it cannot be read."""
    (tmp_path / "tokens.jsonl").write_text(
        '{"slot": "one", "plane": "provisional", "expires_at": 1.0, "provisional": true}\n', encoding="utf-8"
    )
    assert SlotProbe(TokenJournal(tmp_path), clock=lambda: NOW).resource_gone("one", {}) is False


def test_a_slot_is_not_free_until_its_tokens_have_ended(tmp_path):
    """C19 with O06: no token is revoked, so a dead holder's slot stays quarantined until the journalled end and the
    safety margin have passed; exactly at the margin it is still held."""
    journal = TokenJournal(tmp_path)
    journal.record("one", "bound", NOW + 700)
    now = [NOW + 100]
    probe = SlotProbe(journal, clock=lambda: now[0], margin_s=30)
    assert probe.resource_gone("one", {}) is False
    now[0] = NOW + 701  # past the end, inside the margin
    assert probe.resource_gone("one", {}) is False
    now[0] = NOW + 730
    assert probe.resource_gone("one", {}) is False
    now[0] = NOW + 731
    assert probe.resource_gone("one", {}) is True
    assert probe.resource_gone("two", {}) is True  # no token was ever minted for this slot


def test_the_safety_margin_defaults_to_a_positive_number_and_refuses_a_negative_or_unusable_one(tmp_path):
    """C19: a slot is never freed at the very second its tokens end; a margin that is not a finite number of seconds
    is refused."""
    journal = TokenJournal(tmp_path)
    journal.record("one", "bound", NOW)
    assert SAFETY_MARGIN_S > 0
    assert SlotProbe(journal, clock=lambda: NOW + SAFETY_MARGIN_S - 1).resource_gone("one", {}) is False
    assert SlotProbe(journal, clock=lambda: NOW + SAFETY_MARGIN_S + 1).resource_gone("one", {}) is True
    for bad in (-1, float("inf"), float("nan"), True, "60"):
        with pytest.raises(ValueError, match="safety margin"):
            SlotProbe(journal, margin_s=bad)


def test_a_slot_whose_journal_cannot_be_read_stays_quarantined(tmp_path):
    (tmp_path / "tokens.jsonl").write_text("not json\n", encoding="utf-8")
    assert SlotProbe(TokenJournal(tmp_path), clock=lambda: NOW).resource_gone("one", {}) is False


@pytest.mark.parametrize("answer", [False, None, "yes", 1])
def test_a_sandbox_probe_that_does_not_say_exactly_true_keeps_the_slot_quarantined(tmp_path, answer):
    probe = SlotProbe(TokenJournal(tmp_path), sandbox_gone=lambda key, note: answer, clock=lambda: NOW)
    assert probe.resource_gone("one", {}) is False


def test_a_sandbox_probe_that_says_true_frees_a_slot_whose_tokens_ended(tmp_path):
    probe = SlotProbe(TokenJournal(tmp_path), sandbox_gone=lambda key, note: True, clock=lambda: NOW)
    assert probe.resource_gone("one", {}) is True


def test_the_holder_question_is_the_cores(tmp_path):
    from expcore.campaign.lease import process_owner

    assert SlotProbe(TokenJournal(tmp_path)).holder_gone(process_owner()) is False  # this process is alive


def test_c19_a_token_refused_for_its_lifetime_still_has_its_expiry_journalled(slots, client, tmp_path, slot_federation):
    """C19: the platform hands back a token far longer-lived than allowed, so examination refuses it for its lifetime
    before any expiry comparison; it is dropped, not revoked, and the expiry it really has holds the slot."""
    config, loaded = slots
    extra = 400
    slot_federation.mint_hook = lambda user, caveats: [
        {**c, "validUntil": c["validUntil"] + extra} if c["type"] == "time" else c for c in caveats
    ]
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    minter = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, events=no_events, clock=lambda: NOW
    )
    with pytest.raises(TokenRefused, match="longer than"):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    actual = int(NOW) + token_window(CEILING_S, config.tokens).mint_ttl_s + extra
    assert journal.latest_expiry(lease.key) == actual
    assert SlotProbe(journal, clock=lambda: actual + SAFETY_MARGIN_S).resource_gone(lease.key, {}) is False


def test_c19_a_dropped_token_of_unknown_expiry_holds_the_slot_until_an_operator_clears_it(
    slots, client, tmp_path, slot_federation
):
    """C19: the token is minted but its examination answer has no readable time caveat, so its expiry is unknown;
    the journal records it as indeterminate and no clock ever frees the slot."""
    config, loaded = slots
    slot_federation.mint_hook = lambda user, caveats: [c for c in caveats if c["type"] != "time"]
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    minter = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, events=no_events, clock=lambda: NOW
    )
    with pytest.raises(TokenRefused):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    assert journal.latest_expiry(lease.key) == float("inf")
    assert SlotProbe(journal, clock=lambda: NOW + 10**9).resource_gone(lease.key, {}) is False


@pytest.mark.parametrize(
    ("hook", "expect_expiry"),
    [
        # an expiry too large for a float: unknown, so indeterminate
        (lambda user, caveats: [{**c, "validUntil": 10**400} if c["type"] == "time" else c for c in caveats], None),
        # a path caveat the checks cannot decode: the token's readable expiry still reaches the journal
        (
            lambda user, caveats: [
                {**c, "whitelist": ["\u00e9"]}
                if c["type"] == "data.path"
                else {**c, "validUntil": c["validUntil"] + 400}
                if c["type"] == "time"
                else c
                for c in caveats
            ],
            400,
        ),
    ],
)
def test_c19_an_examination_answer_the_checks_cannot_read_still_journals_the_dropped_token(
    slots, client, tmp_path, slot_federation, hook, expect_expiry
):
    """C19: answers that once escaped as crashes (an expiry too large for a float, a path caveat that is not
    base64) are refusals; the token is dropped and its readable expiry, or an indeterminate line, holds the slot."""
    config, loaded = slots
    slot_federation.mint_hook = hook
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    minter = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, events=no_events, clock=lambda: NOW
    )
    with pytest.raises(TokenRefused):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    if expect_expiry is None:
        assert journal.latest_expiry(lease.key) == float("inf")
    else:
        expected = int(NOW) + token_window(CEILING_S, config.tokens).mint_ttl_s + expect_expiry
        assert journal.latest_expiry(lease.key) == expected


def test_c19_a_token_request_with_no_usable_answer_holds_the_slot_as_indeterminate(
    slots, client, tmp_path, slot_federation
):
    """C19: the server creates a token but answers without it; the token may live as long as the server chose, so
    the journal records it as indeterminate and the slot is never freed by the clock."""
    config, loaded = slots
    slot_federation.blank_mint_answer = True
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    minter = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, events=no_events, clock=lambda: NOW
    )
    with pytest.raises(TokenRefused, match="no usable answer"):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    assert slot_federation.issued  # a token does exist on the server
    assert journal.latest_expiry(lease.key) == float("inf")


def test_c19_a_run_without_a_data_path_mints_only_its_tool_servers_token_and_one_that_needs_neither_mints_nothing(
    slots, client, tmp_path, fake
):
    """C19: a slot serves a run with a tool server and no data path by minting only the tool server's token, on the
    slot's tool-server user, journalled as any other; a run that needs neither plane takes no slot, so a mint for it is
    refused before anything is journalled or minted."""
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    tokens = SlotTokens(
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, events=no_events, clock=lambda: NOW
    )
    with pytest.raises(ValueError, match="neither"):
        tokens.mint(lease, ceiling_s=CEILING_S, condition=None, interface=None)
    assert journal.latest_expiry(lease.key) is None
    assert fake.issued == {}
    minted = tokens.mint(lease, ceiling_s=CEILING_S, condition="read", interface=None)
    assert minted.data_path is None
    assert minted.tool_server is not None
    assert minted.tool_server.subject == loaded.users_of(lease).tool_server.user_id
    assert journal.latest_expiry(lease.key) == minted.tool_server.expires_at
    assert [token["subject"]["id"] for token in fake.issued.values()] == [loaded.users_of(lease).tool_server.user_id]


def lengthen_every_token(federation, seconds):
    """The platform disregards the requested time caveat: every token it mints lives `seconds` longer than asked."""
    federation.mint_hook = lambda user, caveats: [
        {**c, "validUntil": c["validUntil"] + seconds} if c["type"] == "time" else c for c in caveats
    ]


def mint_recording(slots, client, tmp_path, events):
    """Lease a slot and mint its tokens for the `read` condition, events going to `events`; return the lease, the
    journal and the expiry the tokens were asked for, and the refusal if the mint was refused."""
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    issuer = TokenIssuer(client, config, clock=lambda: NOW)
    minter = SlotTokens(issuer, config, loaded, journal, events=events, clock=lambda: NOW)
    requested = int(NOW) + token_window(CEILING_S, config.tokens).mint_ttl_s
    try:
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    except DroppedToken as refused:
        return lease, journal, requested, refused
    return lease, journal, requested, None


def test_c19_a_token_that_outlives_its_requested_expiry_records_the_event_with_the_slot_and_both_expiries(
    slots, client, tmp_path, slot_federation
):
    """C19 with O12: the platform hands back a token 50 s longer than asked (ExpiryMismatch, a later expiry). Besides
    the journal line, `token_expiry_beyond_request` is recorded once, naming the slot by its position (`slots[i]`),
    never by its configured name, with both expiries and never the token."""
    lengthen_every_token(slot_federation, 50)
    events = []
    lease, journal, requested, refused = mint_recording(slots, client, tmp_path, lambda *e: events.append(e))
    assert isinstance(refused, ExpiryMismatch)
    assert journal.latest_expiry(lease.key) == requested + 50
    assert [kind for kind, _ in events] == [EVENT_EXPIRY_BEYOND_REQUEST] == ["token_expiry_beyond_request"]
    detail = events[0][1]
    assert f"slots[{slots[1].keys_of(GROUP).index(lease.key)}]" in detail
    assert lease.key not in detail
    assert str(requested) in detail
    assert str(requested + 50) in detail
    assert not any(value in detail for value in slot_federation.issued)


def test_c19_a_dropped_token_whose_readable_expiry_is_later_records_the_event(slots, client, tmp_path, slot_federation):
    """C19: a token so much longer-lived than asked that examination refuses it for its lifetime is a DroppedToken
    that is not an ExpiryMismatch; its readable expiry is later than requested, so the event is recorded."""
    lengthen_every_token(slot_federation, 400)
    events = []
    _, _, requested, refused = mint_recording(slots, client, tmp_path, lambda *e: events.append(e))
    assert isinstance(refused, DroppedToken)
    assert not isinstance(refused, ExpiryMismatch)
    assert [kind for kind, _ in events] == [EVENT_EXPIRY_BEYOND_REQUEST]
    assert str(requested + 400) in events[0][1]


@pytest.mark.parametrize("seconds", [0, -5])
def test_c19_a_token_that_ends_no_later_than_requested_records_no_event(
    slots, client, tmp_path, slot_federation, seconds
):
    """C19: a token minted with exactly the expiry asked for, or refused with an earlier one, is no sign that the
    platform disregarded the caveat; no event is recorded."""
    lengthen_every_token(slot_federation, seconds)
    events = []
    mint_recording(slots, client, tmp_path, lambda *e: events.append(e))
    assert events == []


def test_c19_an_event_sink_that_fails_does_not_hide_the_refusal_or_the_journal_line(
    slots, client, tmp_path, slot_federation
):
    """C19: the event cannot be written; the token is still refused, as the refusal it is, and the expiry it has is
    still journalled, so the slot is held."""
    lengthen_every_token(slot_federation, 50)

    def broken(kind, detail):
        raise OSError("the event log is gone")

    lease, journal, requested, refused = mint_recording(slots, client, tmp_path, broken)
    assert isinstance(refused, ExpiryMismatch)
    assert isinstance(refused.__cause__, OSError)
    assert journal.latest_expiry(lease.key) == requested + 50
