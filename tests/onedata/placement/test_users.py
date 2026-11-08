"""Leased platform users for the two planes: slots through the core's lease pool, and tokens no free slot outlives."""

import os

import pytest
from placement_support import with_placement
from platform_support import BASELINE, CEILING_S, CREDENTIALS_DIR, MANAGE, NOW, write_platform

from expcore.campaign.lease import LeasePool
from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.tokencheck import TokenRefused
from expcore_onedata.tokens import TokenIssuer, token_window
from expcore_onedata.users import GROUP, SAFETY_MARGIN_S, SlotProbe, SlotTokens, TokenJournal, load_slots

LOGINS = {  # login name -> (password, user id)
    "slot-one-data": ("pw-1d", "u-1d"),
    "slot-one-tool": ("pw-1t", "u-1t"),
    "slot-two-data": ("pw-2d", "u-2d"),
    "slot-two-tool": ("pw-2t", "u-2t"),
}


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
        TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, clock=lambda: NOW
    ).mint(lease, ceiling_s=CEILING_S, condition="read")
    slot = loaded.users_of(lease)
    assert tokens.data_path.subject == slot.data_path.user_id
    assert tokens.tool_server is not None
    assert tokens.tool_server.subject == slot.tool_server.user_id
    assert journal.kinds == ["provisional", "exact"]
    assert journal.minted_at_write == [0, 2]  # the provisional line before any token, the exact one after both
    assert journal.latest_expiry(lease.key) == max(tokens.data_path.expires_at, tokens.tool_server.expires_at)
    assert tokens.data_path.reveal() not in (tmp_path / "leases" / "tokens.jsonl").read_text()


def test_c19_a_crash_after_minting_leaves_the_provisional_worst_case_and_the_slot_stays_held(
    slots, client, tmp_path, fake
):
    """C19: the process dies after the tokens exist and before their exact line is written. The journal holds only the
    provisional line, whose end is the clock before minting plus the longest lifetime plus the time a mint may take;
    the slot is held until that end plus the margin, which is later than the tokens' own end."""
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = SpyJournal(tmp_path / "leases", fake, crash_on_exact=True)
    minter = SlotTokens(TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, clock=lambda: NOW)
    with pytest.raises(SystemExit):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    assert len([r for r in fake.requests if r.path.endswith("/tokens/temporary")]) == 2  # both tokens exist
    window = token_window(CEILING_S, config.tokens)
    provisional = NOW + window.max_ttl_s + config.tokens.mint_headroom_s
    assert journal.kinds == ["provisional"]
    assert TokenJournal(tmp_path / "leases").latest_expiry(lease.key) == provisional
    assert provisional > NOW + window.mint_ttl_s  # later than the end a token asked for can have, with no delay
    probes = [(provisional + SAFETY_MARGIN_S - 1, False), (provisional + SAFETY_MARGIN_S, False)]
    probes.append((provisional + SAFETY_MARGIN_S + 1, True))
    for moment, freed in probes:
        probe = SlotProbe(TokenJournal(tmp_path / "leases"), clock=lambda moment=moment: moment)
        assert probe.resource_gone(lease.key, {}) is freed


def test_c19_the_journalled_end_is_the_tokens_actual_end_even_when_the_privilege_reads_take_time(
    slots, client, tmp_path, monkeypatch
):
    """C19 with O06: the clocks advance during the privilege reads before each mint, so the tokens end later than any
    bound taken before minting. The journal holds the later plane's actual expiry, and the slot is free only past it
    plus the margin."""
    config, loaded = slots
    now = [NOW]
    reads = client.get

    def slow_read(path, *args, **kwargs):
        if path.endswith("/privileges"):
            now[0] += 100.0  # a privilege read that takes 100 s
        return reads(path, *args, **kwargs)

    monkeypatch.setattr(client, "get", slow_read)
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    tokens = SlotTokens(
        TokenIssuer(client, config, clock=lambda: now[0]), config, loaded, journal, clock=lambda: now[0]
    ).mint(lease, ceiling_s=CEILING_S, condition="read")
    assert tokens.tool_server is not None
    bound_before_minting = NOW + token_window(CEILING_S, config.tokens).max_ttl_s
    assert tokens.tool_server.expires_at > bound_before_minting  # the old bound would have ended before the token
    ended = journal.latest_expiry(lease.key)
    assert ended == tokens.tool_server.expires_at > tokens.data_path.expires_at
    probe_at = [ended + SAFETY_MARGIN_S - 1, ended + SAFETY_MARGIN_S, ended + SAFETY_MARGIN_S + 1]
    freed = []
    for moment in probe_at:
        now[0] = moment
        freed.append(SlotProbe(journal, clock=lambda: now[0]).resource_gone(lease.key, {}))
    assert freed == [False, False, True]


def test_c19_a_mint_that_fails_after_a_token_exists_journals_a_bound_that_covers_it(
    slots, client, tmp_path, slot_federation
):
    """C19: the tool-server user holds other rights than the condition declares, so the mint is refused after the
    data path's token was minted (and dropped, not revoked). The slot is not free before that token ends."""
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    slot_federation.grant(loaded.users_of(lease).tool_server.user_id, MANAGE)
    journal = TokenJournal(tmp_path / "leases")
    minter = SlotTokens(TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, clock=lambda: NOW)
    with pytest.raises(TokenRefused):
        minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    window = token_window(CEILING_S, config.tokens)
    assert journal.latest_expiry(lease.key) >= NOW + window.mint_ttl_s  # the data path's token lives until then
    assert SlotProbe(journal, clock=lambda: NOW + window.mint_ttl_s).resource_gone(lease.key, {}) is False


def test_a_ceiling_the_window_refuses_journals_and_mints_nothing(slots, client, tmp_path, fake):
    config, loaded = slots
    pool = LeasePool(tmp_path / "leases", loaded.keys_of, SlotProbe(TokenJournal(tmp_path / "leases")))
    lease = pool.acquire(GROUP, timeout_s=0)
    journal = TokenJournal(tmp_path / "leases")
    minter = SlotTokens(TokenIssuer(client, config, clock=lambda: NOW), config, loaded, journal, clock=lambda: NOW)
    with pytest.raises(TokenRefused, match="finite number"):
        minter.mint(lease, ceiling_s=float("inf"), condition="read")
    assert journal.latest_expiry(lease.key) is None
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
