"""Revoking a slot's tokens when its run ends, and the release rule that follows from it (C19).

A slot is free once its sandbox is shown gone and its tokens are either past their journalled expiry plus the margin,
or revoked, confirmed and settled: the generation of both slot users rose, every token the run held answers
`tokenRevoked`, and the settle time written in the journal's revocation line has passed (with no margin, since it is an
observation and not a prediction of a clock).
"""

import itertools
import json
from types import SimpleNamespace

import pytest

from expcore.campaign.lease import LeasePool
from expcore_onedata.client import PlatformError
from expcore_onedata.testing import CEILING_S, NOW
from expcore_onedata.tokens import RevocationUnconfirmed, data_path_spec
from expcore_onedata.users import (
    GROUP,
    JOURNAL,
    REVOKED,
    REVOKING,
    SAFETY_MARGIN_S,
    SlotProbe,
    SlotTokens,
    TokenJournal,
)

KEY = "slot-a"  # the first slot of the platform document
OTHER = "slot-b"
SETTLE_S = 330  # the default revocation_settle_s of [tokens]


def no_events(kind, detail):
    """The event sink of a test that is not about events."""


NAMES = (f"r-test-{n}" for n in itertools.count())


def revocation(journal: TokenJournal, slot: str, settle_until: float, *, between=()) -> None:
    """Append a revocation's start and revoked lines as `SlotTokens.revoke` writes them, without asking the federation;
    `between` are lines journalled after its start and before its revoked line, each a `TokenJournal.record` call."""
    name = next(NAMES)
    journal.record_revoking(slot, revocation=name, started_at=settle_until - SETTLE_S)
    for args, kwargs in between:
        journal.record(*args, **kwargs)
    journal.record_revoked(
        slot,
        revocation=name,
        revoked_at=settle_until - SETTLE_S,
        settle_until=settle_until,
        generations={"data_path": (1, 2), "tool_server": (1, 2)},
        verified=0,
    )


def lines(directory) -> list[dict]:
    path = directory / JOURNAL
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


# ---------------------------------------------------------------------------------------------------- the journal's cap
def test_c19_a_revocation_line_caps_every_earlier_line_of_its_slot_at_its_settle_end(tmp_path):
    """C19: exact, provisional (unsettled) and refused lines before a revocation end no later than its settle end."""
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 5000)
    journal.record(KEY, "provisional", NOW + 6000, mint="m1", provisional=True)
    journal.record(KEY, "refused", NOW + 7000)
    assert journal.latest_expiry(KEY) == NOW + 7000
    revocation(journal, KEY, NOW + SETTLE_S)
    assert journal.latest_expiry(KEY) == NOW + SETTLE_S


def test_c19_a_revocation_clears_an_indeterminate_line_before_it(tmp_path):
    """C19: a dropped token of unknown expiry held the slot until an operator cleared it; a confirmed bump of its
    user's generation revoked it whatever its expiry, so the revocation caps it like any other line."""
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "provisional", NOW + 6000, mint="m1", provisional=True)
    journal.record_indeterminate(KEY)
    assert journal.latest_expiry(KEY) == float("inf")
    revocation(journal, KEY, NOW + SETTLE_S)
    assert journal.latest_expiry(KEY) == NOW + SETTLE_S


def test_c19_an_indeterminate_line_after_the_last_revocation_still_holds_the_slot(tmp_path):
    journal = TokenJournal(tmp_path)
    revocation(journal, KEY, NOW + SETTLE_S)
    journal.record_indeterminate(KEY)
    assert journal.latest_expiry(KEY) == float("inf")


def test_c19_a_later_runs_lines_stand_after_a_revocation(tmp_path):
    """C19: a revocation caps the lines before it; a mint journalled after it is a later run's, which it did not
    revoke, and stands."""
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 5000)
    revocation(journal, KEY, NOW + SETTLE_S)
    journal.record(KEY, "provisional", NOW + 9000, mint="m2", provisional=True)
    assert journal.latest_expiry(KEY) == NOW + 9000
    revocation(journal, KEY, NOW + 2 * SETTLE_S)
    assert journal.latest_expiry(KEY) == NOW + 2 * SETTLE_S


def test_c19_tokens_that_expire_before_the_settle_end_end_at_their_expiry(tmp_path):
    """C19: the cap is a bound, never an extension: a token that expires before the settle end ends then."""
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 100)
    revocation(journal, KEY, NOW + SETTLE_S)
    assert journal.latest_expiry(KEY) == NOW + 100


def test_c19_a_revocation_caps_only_its_own_slot(tmp_path):
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 5000)
    journal.record(OTHER, "data_path", NOW + 5000)
    revocation(journal, KEY, NOW + SETTLE_S)
    assert journal.latest_expiry(KEY) == NOW + SETTLE_S
    assert journal.latest_expiry(OTHER) == NOW + 5000


def test_c19_a_mint_journalled_after_a_revocations_start_keeps_its_own_end(tmp_path):
    """C19: a line after the start line may be a mint that ended after the bump, with a token of the new generation:
    the revocation never caps it, its provisional line and its exact line alike."""
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 5000)
    revocation(
        journal,
        KEY,
        NOW + SETTLE_S,
        between=[
            ((KEY, "provisional", NOW + 9000), {"mint": "m2", "provisional": True}),
            ((KEY, "data_path", NOW + 8000), {"mint": "m2"}),
        ],
    )
    assert journal.latest_expiry(KEY) == NOW + 8000  # its exact line settles its provisional one, uncapped
    journal.record(KEY, "provisional", NOW + 9500, mint="m3", provisional=True)
    assert journal.latest_expiry(KEY) == NOW + 9500


def test_c19_a_start_line_whose_revocation_never_finished_caps_nothing(tmp_path):
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 5000)
    journal.record_revoking(KEY, revocation="r-unfinished", started_at=NOW)
    assert journal.latest_expiry(KEY) == NOW + 5000


def test_c19_a_revocation_line_with_no_start_line_before_it_keeps_the_slot_held(tmp_path):
    """C19: without its start line what the revocation caps is unknown, so nothing is capped and the slot is held."""
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 100)
    journal.record_revoked(
        KEY, revocation="r-alone", revoked_at=NOW, settle_until=NOW + SETTLE_S, generations={}, verified=0
    )
    assert probe_at(journal, NOW + 10**9).resource_gone(KEY, {}) is False
    journal.record_revoking(KEY, revocation="r-alone", started_at=NOW)  # a start after it is not before it
    assert probe_at(journal, NOW + 10**9).resource_gone(KEY, {}) is False


def test_c19_removing_an_earlier_line_never_brings_a_later_mint_under_the_cap(tmp_path):
    """C19: the cap is bounded by the start line, not by a position in the file, so an operator who removes an
    indeterminate line, of this slot or another, never moves a later mint's line under it."""
    journal = TokenJournal(tmp_path)
    journal.record_indeterminate(OTHER)
    journal.record_indeterminate(KEY)
    mint = ((KEY, "provisional", NOW + 9000), {"mint": "m2", "provisional": True})
    revocation(journal, KEY, NOW + SETTLE_S, between=[mint])
    path = tmp_path / JOURNAL
    kept = [line for line in path.read_text(encoding="utf-8").splitlines() if '"indeterminate"' not in line]
    path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    assert journal.latest_expiry(KEY) == NOW + 9000
    assert journal.latest_expiry(OTHER) is None


# ------------------------------------------------------------------------------------------------------- the probe
def probe_at(journal, moment, sandbox_gone=None):
    return SlotProbe(journal, sandbox_gone=sandbox_gone, clock=lambda: moment)


def test_c19_the_probe_frees_a_revoked_slot_once_the_settle_end_has_passed_with_no_margin(tmp_path):
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 89_000)  # a whole day of tokens
    revocation(journal, KEY, NOW + SETTLE_S)
    assert probe_at(journal, NOW + SETTLE_S).resource_gone(KEY, {}) is False  # the settle not yet passed
    assert probe_at(journal, NOW + SETTLE_S + 0.5).resource_gone(KEY, {}) is True  # no 60 s margin on top of it


def test_c19_a_revoked_slot_whose_sandbox_is_not_shown_gone_stays_held(tmp_path):
    """C19: revocation replaces the time rule, never the sandbox's proof."""
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 89_000)
    revocation(journal, KEY, NOW + SETTLE_S)
    asked = []
    held = probe_at(journal, NOW + 10 * SETTLE_S, sandbox_gone=lambda key, note: asked.append(key) or False)
    assert held.resource_gone(KEY, {}) is False
    assert asked == [KEY]
    assert probe_at(journal, NOW + 10 * SETTLE_S, sandbox_gone=lambda key, note: True).resource_gone(KEY, {}) is True


def test_c19_an_unrevoked_slot_keeps_its_expiry_and_the_margin(tmp_path):
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 1000)
    assert probe_at(journal, NOW + 1000 + SAFETY_MARGIN_S).resource_gone(KEY, {}) is False
    assert probe_at(journal, NOW + 1000 + SAFETY_MARGIN_S + 1).resource_gone(KEY, {}) is True


def test_c19_tokens_that_expire_before_the_settle_end_keep_their_margin(tmp_path):
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 100)
    revocation(journal, KEY, NOW + SETTLE_S)
    assert probe_at(journal, NOW + 100 + SAFETY_MARGIN_S).resource_gone(KEY, {}) is False
    assert probe_at(journal, NOW + 100 + SAFETY_MARGIN_S + 1).resource_gone(KEY, {}) is True


@pytest.mark.parametrize("settle_until", [None, "soon", float("nan"), float("inf")])
def test_c19_a_revocation_line_without_a_finite_settle_end_keeps_the_slot_held(tmp_path, settle_until):
    journal = TokenJournal(tmp_path)
    journal.record(KEY, "data_path", NOW + 100)
    journal.record_revoking(KEY, revocation="r-raw", started_at=NOW)
    with open(tmp_path / JOURNAL, "a", encoding="utf-8") as handle:
        line = {"slot": KEY, "plane": REVOKED, "revocation": "r-raw", "settle_until": settle_until}
        handle.write(json.dumps(line) + "\n")
    assert probe_at(journal, NOW + 10**9).resource_gone(KEY, {}) is False


# ------------------------------------------------------------------------------------------ revoke, confirm, settle
@pytest.fixture
def minted(config, slots, issuer, fake, tmp_path):
    """A slot leased from the pool, its run's tokens minted on its two users, and the minter that revokes them."""
    journal = TokenJournal(tmp_path / "leases")
    pool = LeasePool(tmp_path / "leases", slots.keys_of, SlotProbe(journal), clock=lambda: 0.0, sleep=lambda s: None)
    lease = pool.acquire(GROUP, run_key="run-1")
    now = [NOW]
    minter = SlotTokens(issuer, config, slots, journal, events=no_events, clock=lambda: now[0])
    tokens = minter.mint(lease, ceiling_s=CEILING_S, condition="read")
    return SimpleNamespace(
        minter=minter, tokens=tokens, journal=journal, lease=lease, slot=slots.get(lease.key), now=now, fake=fake
    )


def requests_of(fake, path):
    return [r for r in fake.requests if r.path.endswith(path)]


def test_c19_revoking_bumps_both_slot_users_confirms_each_token_and_journals_the_settle_end(minted, tmp_path):
    """C19: for each of the slot's two users, the generation is read, the user's own DELETE is sent and the generation
    read again and required to have risen; then every token the run held must answer `tokenRevoked`. Only then is
    the revocation line written, with the settle end and the generations by plane, never a token or a user."""
    expiry = minted.journal.latest_expiry(KEY)
    minted.now[0] = NOW + 42
    done = minted.minter.revoke(KEY, minted.tokens)
    assert (done.revoked_at, done.settle_until) == (NOW + 42, NOW + 42 + SETTLE_S)
    assert done.generations == {"data_path": (1, 2), "tool_server": (1, 2)}
    assert done.verified == 2
    revoking = [r for r in requests_of(minted.fake, "/onezone/user/tokens/temporary") if r.method != "POST"]
    assert [r.method for r in revoking] == ["GET", "DELETE", "GET", "GET", "DELETE", "GET"]
    assert all(r.token is None for r in revoking)  # the user's own Basic login, never the admin's token
    assert len(requests_of(minted.fake, "/tokens/verify_access_token")) == 2
    assert not [r for r in minted.fake.requests if "/users/" in r.path and r.path.endswith("/tokens/temporary")]
    last = lines(tmp_path / "leases")[-1]
    assert last["plane"] == REVOKED
    assert last["settle_until"] == NOW + 42 + SETTLE_S
    assert last["generations"] == {"data_path": [1, 2], "tool_server": [1, 2]}
    text = (tmp_path / "leases" / JOURNAL).read_text(encoding="utf-8")
    assert minted.tokens.data_path.reveal() not in text
    assert minted.slot.data_path.user_id not in text
    assert minted.slot.tool_server.user_id not in text
    assert last["expires_at"] == last["settle_until"]  # a reader of the earlier journal takes it as an exact line
    assert minted.journal.latest_expiry(KEY) == NOW + 42 + SETTLE_S < expiry


def test_c19_revoke_confirm_settle_then_the_lease_is_free(minted, tmp_path):
    """C19: the run's lease, finished at once, is quarantined until the settle end; one look after it frees it, far
    sooner than the tokens' expiry."""
    expiry = minted.journal.latest_expiry(KEY)
    minted.minter.revoke(KEY, minted.tokens)
    probe = SlotProbe(minted.journal, sandbox_gone=lambda key, note: True, clock=lambda: minted.now[0])
    pool = LeasePool(
        tmp_path / "leases", lambda group: [KEY, OTHER], probe, clock=lambda: 0.0, sleep=lambda s: None,
        quarantine_retry_s=0.0,
    )  # fmt: skip
    assert pool.finish(minted.lease) is False  # the settle has not passed
    minted.now[0] = NOW + SETTLE_S
    assert pool.settle_quarantines() == []
    minted.now[0] = NOW + SETTLE_S + 1
    assert pool.settle_quarantines() == [KEY]
    assert minted.now[0] < expiry


def test_c19_revoking_one_slot_leaves_the_other_slots_tokens_valid(minted, slots, issuer, config, tmp_path, fake):
    other = slots.get(OTHER)
    token = issuer.mint(data_path_spec(config.space.id), other.data_path, ceiling_s=CEILING_S)
    minted.minter.revoke(KEY, minted.tokens)
    assert fake.generations.get(other.data_path.user_id, 1) == 1
    with pytest.raises(RevocationUnconfirmed, match="tokenCaveatUnverified"):  # refused for its caveats, not revoked
        issuer.require_revoked(token.reveal())


def assert_nothing_journalled(minted, tmp_path, before):
    """No revoked line: only the revocation's start line, which caps nothing, follows the lines from before."""
    after = lines(tmp_path / "leases")
    assert after[: len(before)] == before
    assert [line["plane"] for line in after[len(before) :]] == [REVOKING]
    assert minted.journal.latest_expiry(KEY) == before[-1]["expires_at"]
    held = SlotProbe(minted.journal, clock=lambda: NOW + SETTLE_S + 1)
    assert held.resource_gone(KEY, {}) is False  # the expiry rule stands


def test_c19_a_revocation_whose_delete_fails_journals_nothing(minted, tmp_path):
    before = lines(tmp_path / "leases")
    minted.fake.revoke_status = 503
    with pytest.raises(PlatformError) as caught:
        minted.minter.revoke(KEY, minted.tokens)
    assert caught.value.status == 503
    assert_nothing_journalled(minted, tmp_path, before)


def test_c19_a_generation_that_does_not_rise_journals_nothing(minted, tmp_path):
    before = lines(tmp_path / "leases")
    minted.fake.revoke_bumps = False
    with pytest.raises(RevocationUnconfirmed, match="did not rise"):
        minted.minter.revoke(KEY, minted.tokens)
    assert_nothing_journalled(minted, tmp_path, before)


@pytest.mark.parametrize(
    "answer",
    [(200, None), (400, "tokenCaveatUnverified"), (401, "unauthorized"), (500, "internalServerError")],
    ids=["still-valid", "caveat", "unauthorized", "server-error"],
)
def test_c19_a_verification_that_answers_anything_but_token_revoked_journals_nothing(minted, tmp_path, answer):
    """C19: before a revocation the caveated tokens are refused for their caveats anyway, so only `tokenRevoked`
    shows the revocation; every other answer, a success included, leaves the expiry rule."""
    before = lines(tmp_path / "leases")
    minted.fake.verify_override = answer
    with pytest.raises(RevocationUnconfirmed):
        minted.minter.revoke(KEY, minted.tokens)
    assert_nothing_journalled(minted, tmp_path, before)


class Died(BaseException):
    """The process dying at a given point: nothing after it runs."""


def test_c19_a_mint_after_the_bump_keeps_its_full_expiry_even_if_the_process_dies_right_after_the_cap(
    minted, tmp_path, monkeypatch
):
    """C19: a mint that ends after the bump holds a token of the new generation, which stays valid. Its lines follow
    the revocation's start line, so the cap never covers them, with nothing written after the cap: a process that dies
    right after the revoked line leaves the slot held until that token's own expiry, never freed at the settle end."""
    issuer = minted.minter._issuer
    confirm, record_revoked = issuer.require_revoked, minted.journal.record_revoked
    later = []

    def concurrent(value):  # after both bumps: a mint of the slot that runs all the same
        if not later:
            minted.now[0] = NOW + 100
            later.append(minted.minter.mint(minted.lease, ceiling_s=CEILING_S, condition="read"))
        return confirm(value)

    def dying(*args, **kwargs):
        record_revoked(*args, **kwargs)
        raise Died

    monkeypatch.setattr(issuer, "require_revoked", concurrent)
    monkeypatch.setattr(minted.journal, "record_revoked", dying)
    with pytest.raises(Died):
        minted.minter.revoke(KEY, minted.tokens)
    written = lines(tmp_path / "leases")
    assert written[-1]["plane"] == REVOKED  # the cap is the last line: nothing corrected it
    [fresh] = later
    expiry = max(t.expires_at for t in (fresh.data_path, fresh.tool_server) if t is not None)
    assert minted.journal.latest_expiry(KEY) == expiry > written[-1]["settle_until"]
    with pytest.raises(RevocationUnconfirmed, match="tokenCaveatUnverified"):  # still valid: refused for its caveats
        issuer.require_revoked(fresh.data_path.reveal())
    settled, after = written[-1]["settle_until"] + 1, expiry + SAFETY_MARGIN_S + 1
    assert (
        SlotProbe(minted.journal, sandbox_gone=lambda k, n: True, clock=lambda: settled).resource_gone(KEY, {}) is False
    )
    assert SlotProbe(minted.journal, sandbox_gone=lambda k, n: True, clock=lambda: after).resource_gone(KEY, {}) is True


@pytest.mark.parametrize("settle_s", [0, 314, -1, float("inf"), True])
def test_c19_a_direct_settle_below_the_propagation_bound_is_refused_before_anything_is_sent(minted, tmp_path, settle_s):
    """C19: the settle passed directly is held to the same bound as the configured one (315 s: a provider's 300 s
    cache purge after a lost connection to Onezone, and a session's 15 s recheck), and refused before any request."""
    before, sent = lines(tmp_path / "leases"), len(minted.fake.requests)
    with pytest.raises(ValueError, match="at least 315 s"):
        minted.minter.revoke(KEY, minted.tokens, settle_s=settle_s)
    assert len(minted.fake.requests) == sent
    assert lines(tmp_path / "leases") == before


@pytest.mark.parametrize("settle_s", [315, 330])
def test_c19_a_direct_settle_at_or_above_the_propagation_bound_is_accepted(minted, settle_s):
    done = minted.minter.revoke(KEY, minted.tokens, settle_s=settle_s)
    assert done.settle_until == done.revoked_at + settle_s


def test_c19_a_slot_whose_run_holds_no_token_is_revoked_on_the_generations_alone(minted, tmp_path):
    """C19: a holder that crashed has no token values; the generation check alone confirms the revocation."""
    done = minted.minter.revoke(KEY)
    assert done.verified == 0
    assert lines(tmp_path / "leases")[-1]["plane"] == REVOKED
    assert minted.journal.latest_expiry(KEY) == NOW + SETTLE_S
