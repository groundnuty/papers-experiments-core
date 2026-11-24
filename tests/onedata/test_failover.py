"""O98: an owner outage fails a space's reads over to a failover holder only when a holder's provider id sorts below the
owner's, so the order is computed from the platform's ids over the owner and the space's declared failover holders,
checked, and told by labels alone (`expcore_onedata.failover`).

A provider that merely supports a space is no holder: the agent's local provider supports every space and holds none of
its data. Only the providers a space's `failover` names count, and a space that declares none is out of scope.

Every name and id here is invented. A provider is `owner-site`, `mirror-one` (the agent's local provider), `mirror-two`
or `mirror-three`; its id is whatever the test gives it, so the order is the test's own."""

import dataclasses
import json

import pytest

from expcore.config.loading import ConfigError
from expcore_onedata import failover
from expcore_onedata.config import load_platform
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.testing.world import with_placement

OWNER, MIRROR_ONE, MIRROR_TWO, MIRROR_THREE = "owner-site", "mirror-one", "mirror-two", "mirror-three"
LABEL = {OWNER: "providers[0]", MIRROR_ONE: "providers[1]", MIRROR_TWO: "providers[2]", MIRROR_THREE: "providers[3]"}


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")


def platform(tmp_path, ids, spaces):
    """A platform of the four providers with the ids `ids` gives (by name), and `[[spaces]]` of `(supporters, holder)`
    or `(supporters, holder, failover)` each, `failover` declared only when it is given. `mirror-one` is the agent's
    local provider in every entry, so every space it appears in lists it, as the platform's `local` key requires."""

    def edit(data):
        data["providers"] = {name: {"url": f"https://{name}.test", "id": value} for name, value in ids.items()}
        del data["space"], data["support"]
        data["spaces"] = [
            {
                "label": f"entry-{index}",
                "name": f"invented-{index}-space",
                "id": f"sp-invented-{index}",
                "prefixes": [f"invented-{index}-"],
                "providers": list(space[0]),
                "local": MIRROR_ONE,
                "holder": space[1],
                **({"failover": list(space[2])} if len(space) > 2 else {}),
            }
            for index, space in enumerate(spaces)
        ]

    tmp_path.mkdir(parents=True, exist_ok=True)
    return load_platform(write_platform(tmp_path, edit))


# one space, the owner holding it, supported by the local provider and by `mirror-two`, which holds its whole replica
SPACE = ((OWNER, MIRROR_ONE, MIRROR_TWO), OWNER, (MIRROR_TWO,))
UNDECLARED = ((OWNER, MIRROR_ONE, MIRROR_TWO), OWNER)  # the same space with no failover holder declared
WIDE = ((OWNER, MIRROR_ONE, MIRROR_TWO, MIRROR_THREE), OWNER, (MIRROR_TWO, MIRROR_THREE))  # two holders and the local


def ids(owner, one, two, three="id-0900"):
    return {OWNER: owner, MIRROR_ONE: one, MIRROR_TWO: two, MIRROR_THREE: three}


def test_o98_a_failover_holder_whose_id_sorts_below_the_owners_is_asked_first_and_the_order_passes(tmp_path):
    """O98: the owner and the declared holder are ordered by id, and the first being the holder is what makes a failover
    possible. The local provider is no part of the order."""
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200"), [SPACE])
    (found,) = failover.admit(config, [OWNER])
    assert found.mirror_first is True
    assert found.mirrors == (LABEL[MIRROR_TWO],)
    assert found.order == (LABEL[MIRROR_TWO], LABEL[OWNER])


def test_o98_an_owner_whose_id_sorts_below_every_failover_holders_is_refused_naming_labels_only(tmp_path):
    """O98: the owner first means op-worker asks the stopped owner first, so the member is refused, never passed; the
    message names the space and the providers by position and holds no name and no id, and not the local provider,
    which sorts below both and is no holder."""
    config = platform(tmp_path, ids("id-0200", "id-0100", "id-0300"), [SPACE])
    with pytest.raises(failover.FailoverRefused) as refused:
        failover.admit(config, [OWNER])
    text = str(refused.value)
    assert text.startswith("failover order: ")
    assert "the owner's provider id sorts before every mirror's, so op-worker would ask the stopped owner first" in text
    assert "spaces[0]" in text
    assert "(owner providers[0]; mirrors providers[2])" in text
    assert "providers[1]" not in text
    for secret in (OWNER, MIRROR_ONE, MIRROR_TWO, "id-0100", "id-0200", "id-0300", "invented-0-space", "entry-0"):
        assert secret not in text


@pytest.mark.parametrize(
    ("owner", "two", "three", "passes"),
    [
        ("id-3", "id-1", "id-2", True),  # both holders below the owner
        ("id-2", "id-1", "id-3", True),  # one holder below it, the other above: the first holder in order serves
        ("id-2", "id-3", "id-1", True),  # the same with the holders the other way round
        ("id-1", "id-2", "id-3", False),  # the owner below both, though the local provider (id-0) is below it
    ],
)
def test_o98_with_several_holders_it_passes_when_any_one_sorts_before_the_owner(tmp_path, owner, two, three, passes):
    """O98: the first holder in the order serves, so one holder below the owner is enough, and none is not."""
    config = platform(tmp_path, ids(owner, "id-0", two, three), [WIDE])
    found = failover.orders(config, [OWNER])[0]
    assert found.mirror_first is passes
    assert found.mirrors == (LABEL[MIRROR_TWO], LABEL[MIRROR_THREE])
    if passes:
        assert failover.admit(config, [OWNER]) == (found,)
    else:
        with pytest.raises(failover.FailoverRefused):
            failover.admit(config, [OWNER])


def test_o98_the_ids_are_compared_as_bytes_with_no_case_folding(tmp_path):
    """O98: Erlang compares binaries bytewise: an upper-case letter sorts before a lower-case one, and a digit before
    both, whatever a locale would say."""
    assert failover.ordered({"a": "b-id", "b": "A-id", "c": "1-id"}) == ("c", "b", "a")
    assert failover.ordered({"x": "é", "y": "z"}) == ("y", "x")  # U+00E9 is 0xC3 0xA9 in UTF-8, above `z`


def test_o98_two_ids_that_are_one_or_an_id_that_is_missing_give_no_order_and_ties_are_never_broken():
    """O98: a tie has no order and a missing id has none either; neither is guessed into a pass."""
    with pytest.raises(failover.FailoverError, match="one provider id"):
        failover.ordered({"a": "same", "b": "same"})
    for missing in (None, ""):
        with pytest.raises(failover.FailoverError, match="providers\\[1\\] is not known"):
            failover.ordered({"providers[0]": "id-1", "providers[1]": missing})


def test_o98_the_loader_never_accepts_two_providers_with_one_id_so_a_platform_has_no_ties(tmp_path):
    """O98: ties are impossible on a platform that loaded: `[providers]` refuses a repeated id."""
    with pytest.raises(ConfigError, match="own id"):
        platform(tmp_path, ids("id-same", "id-same", "id-0200"), [SPACE])


def test_o98_a_provider_with_no_id_in_a_space_in_scope_is_refused_not_passed(tmp_path):
    """O98: a failover holder built in code without an id gives no order, which is no pass."""
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [SPACE])
    providers = {**config.providers, MIRROR_TWO: dataclasses.replace(config.providers[MIRROR_TWO], id=None)}
    bare = dataclasses.replace(config, providers=providers)
    with pytest.raises(failover.FailoverError, match="providers\\[2\\] is not known"):
        failover.admit(bare, [OWNER])


# -- scope: only the spaces an owner outage reaches that declare their failover holders --------------------------------


def test_o98_no_declared_owner_outage_puts_nothing_in_scope(tmp_path):
    """O98: with no owner declared nothing is read: no spaces in scope, whatever the ids are (the owner sorts first)."""
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [SPACE])
    assert failover.orders(config, []) == ()
    assert failover.admit(config, ()) == ()


def test_o98_a_provider_that_holds_no_space_another_provider_supports_is_no_owner_stop(tmp_path):
    """O98: a holder taken down, a provider that holds nothing and an owner of a space nothing else supports are not
    owner outages: nothing is in scope for any of them, even with the worst ids."""
    sole = ((MIRROR_ONE,), MIRROR_ONE)
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [SPACE, sole])
    assert failover.orders(config, [MIRROR_ONE]) == ()  # it only supports the first; the space it holds has no one else
    assert failover.orders(config, [MIRROR_TWO]) == ()  # it only supports
    assert [found.space for found in failover.orders(config, [OWNER])] == ["spaces[0]"]


def test_o98_a_space_that_declares_no_failover_holder_is_out_of_scope(tmp_path):
    """O98: nothing says where an undeclared space's reads go, and its supporters are never assumed to hold blocks: with
    the ids that would pass if every supporter counted (the local provider sorts below the owner) and with the ones that
    would not, `orders` and `admit` give nothing for it."""
    for number, given in enumerate((ids("id-0200", "id-0100", "id-0300"), ids("id-0100", "id-0200", "id-0300"))):
        config = platform(tmp_path / str(number), given, [UNDECLARED])
        assert failover.orders(config, [OWNER]) == ()
        assert failover.admit(config, [OWNER]) == ()


def test_o98_only_the_declaring_spaces_the_owner_holds_are_in_scope_and_each_is_told_by_its_position(tmp_path):
    """O98: several spaces, two owners: each owner's own declaring spaces, in the file's order, by `spaces[i]`; a space
    another provider holds is judged only when that provider is the one declared, and one declaring nothing never."""
    held_by_two = ((MIRROR_ONE, MIRROR_TWO, MIRROR_THREE), MIRROR_TWO, (MIRROR_THREE,))
    spaces = [SPACE, held_by_two, UNDECLARED, SPACE]
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300", "id-0250"), spaces)
    assert [found.space for found in failover.orders(config, [OWNER])] == ["spaces[0]", "spaces[3]"]
    assert [found.space for found in failover.orders(config, [MIRROR_TWO])] == ["spaces[1]"]
    both = failover.orders(config, [OWNER, MIRROR_TWO])
    assert [found.space for found in both] == ["spaces[0]", "spaces[1]", "spaces[3]"]
    assert both[1].order == (LABEL[MIRROR_THREE], LABEL[MIRROR_TWO])  # mirror-three's id is below mirror-two's


def test_o98_the_one_space_form_cannot_declare_failover_holders_so_it_is_out_of_scope(tmp_path):
    """O98: the one-space form names its data provider in `[placement]` and has no `failover` key: `orders` gives
    nothing for it, though another provider supports it, and `require_declared` refuses it for an owner that holds
    it."""

    def edit(data):
        invented = ids("id-0300", "id-0100", "id-0200")
        data["providers"] = {name: {"url": f"https://{name}.test", "id": value} for name, value in invented.items()}
        data["support"] = {"providers": [OWNER, MIRROR_ONE], "local": MIRROR_ONE}
        with_placement(data, holder=OWNER)

    placed = load_platform(write_platform(tmp_path, edit))
    assert failover.orders(placed, [OWNER]) == ()
    with pytest.raises(failover.FailoverError, match="spaces\\[0\\]: declares no failover holder"):
        failover.require_declared(placed, [OWNER])
    assert failover.admit_stop(placed, OWNER) == ()  # nothing in the platform declares one: no check, as before


def test_o98_an_outage_target_the_platform_does_not_declare_is_an_error_naming_no_provider(tmp_path):
    """O98: a target that is no provider of the platform is no scope to skip silently."""
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [SPACE])
    with pytest.raises(failover.FailoverError) as error:
        failover.orders(config, ["a-provider-nobody-declared"])
    assert "a-provider-nobody-declared" not in str(error.value)


# -- the supporters are not the holders: the local provider (the counterexample of the review of #548) ----------------

# the local provider `mirror-one` sorts below the owner, which sorts below the holder `mirror-two`
LOCAL_BELOW_OWNER_BELOW_HOLDER = ids("id-0200", "id-0100", "id-0300")


def test_o98_a_local_provider_that_holds_nothing_does_not_make_the_order_pass(tmp_path):
    """O98, the review of #548: supporters owner, local and a real holder with the ids local < owner < holder. The local
    provider supports the space and holds none of its data, so op-worker chooses between the owner and the holder and
    asks the stopped owner first: refused, the message naming the holder and not the local provider. Counting every
    supporter (as the branch did before) made it pass."""
    config = platform(tmp_path, LOCAL_BELOW_OWNER_BELOW_HOLDER, [SPACE])
    with pytest.raises(failover.FailoverRefused) as refused:
        failover.admit(config, [OWNER])
    (failed,) = refused.value.failed
    assert (failed.owner, failed.mirrors, failed.order) == (
        LABEL[OWNER],
        (LABEL[MIRROR_TWO],),
        (LABEL[OWNER], LABEL[MIRROR_TWO]),
    )
    assert "(owner providers[0]; mirrors providers[2])" in str(refused.value)
    assert "providers[1]" not in str(refused.value)  # the local provider is no part of the order


def test_o98_a_declared_holder_below_the_owner_passes_whatever_the_local_provider_does(tmp_path):
    """O98: the declared holder sorts below the owner and the local provider above both: the order passes over the two,
    and the record lists the holder alone."""
    config = platform(tmp_path, ids("id-0200", "id-0800", "id-0100"), [SPACE])
    (found,) = failover.admit(config, [OWNER])
    assert (found.mirrors, found.order) == ((LABEL[MIRROR_TWO],), (LABEL[MIRROR_TWO], LABEL[OWNER]))
    assert json.loads(failover.record_text([found]))["spaces"][0]["mirrors"] == [LABEL[MIRROR_TWO]]


def test_o98_the_holders_are_in_the_order_of_the_spaces_providers_whatever_order_failover_lists_them(tmp_path):
    """O98: `failover = [three, two]` and `[two, three]` give one set of holders, listed as the space lists its
    providers, so the record of a member does not depend on how the declaration was written."""
    spaces = [((OWNER, MIRROR_ONE, MIRROR_TWO, MIRROR_THREE), OWNER, (MIRROR_THREE, MIRROR_TWO))]
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200", "id-0150"), spaces)
    (found,) = failover.admit(config, [OWNER])
    assert found.mirrors == (LABEL[MIRROR_TWO], LABEL[MIRROR_THREE])


@pytest.mark.parametrize(
    "wanted",
    [(OWNER,), (MIRROR_TWO, MIRROR_TWO), ("no-such-provider",), (MIRROR_TWO, "no-such-provider")],
    ids=["the owner", "one twice", "no provider of the file", "one of two that is none"],
)
def test_o98_a_declared_holder_that_is_no_other_supporter_of_the_space_gives_no_order(tmp_path, wanted):
    """O98: the loader refuses these, and a platform built in code may still hold one: the owner as its own holder, one
    twice and one the file does not have are no order and no pass; the message names the space and holds no name."""
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200"), [SPACE])
    broken = dataclasses.replace(config, spaces=(dataclasses.replace(config.spaces[0], failover=wanted),))
    with pytest.raises(failover.FailoverError, match="spaces\\[0\\]: a declared failover holder is no other") as bad:
        failover.admit(broken, [OWNER])
    assert not isinstance(bad.value, failover.FailoverRefused)
    for name in (OWNER, MIRROR_TWO, "no-such-provider"):
        assert name not in str(bad.value)


# -- the check before an owner stop, over the platform file alone ------------------------------------------------------


def test_o98_a_platform_that_declares_no_failover_holder_anywhere_is_not_checked_as_before(tmp_path):
    """O98: nothing in the platform says where an owner's reads go, so a stop of the owner is not checked, whatever the
    ids (the owner sorts first and the local provider below it): the behaviour before this check existed."""
    for number, given in enumerate((ids("id-0100", "id-0200", "id-0300"), LOCAL_BELOW_OWNER_BELOW_HOLDER)):
        config = platform(tmp_path / str(number), given, [UNDECLARED])
        assert failover.admit_stop(config, OWNER) == ()


def test_o98_an_owner_stop_is_checked_over_the_declared_holders_and_the_local_provider_does_not_count(tmp_path):
    """O98, the review of #548: the same platform and ids as the counterexample, through `admit_stop`: refused, over
    the owner and its declared holder alone; with the holder below the owner it passes."""
    config = platform(tmp_path, LOCAL_BELOW_OWNER_BELOW_HOLDER, [SPACE])
    with pytest.raises(failover.FailoverRefused, match="owner providers\\[0\\]; mirrors providers\\[2\\]"):
        failover.admit_stop(config, OWNER)
    other = tmp_path / "other"
    (found,) = failover.admit_stop(platform(other, ids("id-0200", "id-0800", "id-0100"), [SPACE]), OWNER)
    assert (found.mirrors, found.order) == ((LABEL[MIRROR_TWO],), (LABEL[MIRROR_TWO], LABEL[OWNER]))


def test_o98_where_a_space_declares_holders_a_supported_space_of_the_owner_with_none_refuses_the_stop(tmp_path):
    """O98: spaces[0] declares its holder, which sorts below the owner, so it passes. spaces[1], which the owner holds
    as well and the others support, declares none: nothing says where its reads go, and counting its supporters would
    count the local provider, so the stop is refused as no order, not as a failed one."""
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200"), [SPACE, UNDECLARED])
    with pytest.raises(failover.FailoverError, match="spaces\\[1\\]: declares no failover holder") as refused:
        failover.admit_stop(config, OWNER)
    assert not isinstance(refused.value, failover.FailoverRefused)
    text = str(refused.value)
    for secret in (OWNER, MIRROR_ONE, MIRROR_TWO, "id-0", "spaces[0]"):
        assert secret not in text


def test_o98_a_space_another_provider_holds_needs_no_declaration_for_the_stop_of_this_owner(tmp_path):
    """O98: spaces[1] is held by mirror-two, not by the owner the stop takes down, so it need not declare a holder."""
    other_holds = ((OWNER, MIRROR_ONE, MIRROR_TWO), MIRROR_TWO)
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200"), [SPACE, other_holds])
    (found,) = failover.admit_stop(config, OWNER)
    assert (found.space, found.mirrors) == ("spaces[0]", (LABEL[MIRROR_TWO],))


def test_o98_a_stop_of_a_provider_that_holds_no_supported_space_is_no_owner_stop_and_is_not_checked(tmp_path):
    """O98: the holder taken down is no owner stop: nothing is in scope or refused for it, even where the platform
    declares holders and another space of its is undeclared."""
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [SPACE, UNDECLARED])
    assert failover.admit_stop(config, MIRROR_TWO) == ()


def test_o98_a_stop_of_a_name_that_is_no_provider_of_the_platform_is_an_error_not_a_pass(tmp_path):
    """O98: the owner the injector was built with is a provider of the platform; anything else is no order."""
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [SPACE])
    with pytest.raises(failover.FailoverError, match="no provider of the platform"):
        failover.admit_stop(config, "not-a-provider")


# -- the record ---------------------------------------------------------------------------------------------------


def test_o98_the_record_holds_labels_and_the_order_and_nothing_else(tmp_path):
    """O98: per space in scope, its position, the owner's and holders' labels, the order and `mirror_first`; no name,
    and no label of the local provider."""
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200", "id-0150"), [WIDE])
    text = failover.record_text(failover.admit(config, [OWNER]))
    assert text.endswith("\n")
    assert json.loads(text) == {
        "version": "expcore-failover-order/1",
        "spaces": [
            {
                "space": "spaces[0]",
                "owner": "providers[0]",
                "mirrors": ["providers[2]", "providers[3]"],
                "order": ["providers[3]", "providers[2]", "providers[0]"],
                "mirror_first": True,
            }
        ],
    }
    for secret in (OWNER, MIRROR_ONE, MIRROR_TWO, "id-0", "invented", "entry-", "providers[1]"):
        assert secret not in text
    assert failover.read_record(text)["spaces"][0]["order"][0] == "providers[3]"


@pytest.mark.parametrize(
    "changed",
    [
        {"mirror_first": False},
        {"order": ["providers[0]", "providers[2]", "providers[3]"]},  # the owner first
        {"order": ["providers[3]", "providers[0]"]},  # not every supporter
        {"owner": 7},
        {"unexpected": 1},
    ],
)
def test_o98_a_record_that_is_not_what_was_written_is_not_read(tmp_path, changed):
    """O98: a record that says the owner is first, lacks a supporter or holds an extra key is refused when read."""
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200", "id-0150"), [WIDE])
    document = json.loads(failover.record_text(failover.admit(config, [OWNER])))
    document["spaces"][0].update(changed)
    with pytest.raises(failover.FailoverError):
        failover.read_record(json.dumps(document))
    with pytest.raises(failover.FailoverError):
        failover.read_record("not json")


def written_record(tmp_path, spaces):
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200", "id-0150"), spaces)
    return failover.record_text(failover.admit(config, [OWNER]))


def test_o98_a_record_of_several_spaces_is_read_and_one_of_none_is_not(tmp_path):
    """O98: what the writer produces for two spaces in scope is read; the same record with its spaces taken out is the
    record of an admission that writes none, which no run produces and no reader takes for a pass."""
    text = written_record(tmp_path, [WIDE, WIDE])
    assert [space["space"] for space in failover.read_record(text)["spaces"]] == ["spaces[0]", "spaces[1]"]
    document = json.loads(text)
    document["spaces"] = []
    with pytest.raises(failover.FailoverError):
        failover.read_record(json.dumps(document))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(extra=1),
        lambda d: d["spaces"].append(dict(d["spaces"][0])),  # one space label twice
        lambda d: d["spaces"][0].update(mirrors=["providers[2]", "providers[2]"]),
        lambda d: d["spaces"][0].update(space="providers[0]"),  # a label of the wrong kind
        lambda d: d["spaces"][0].update(owner="spaces[0]"),
    ],
)
def test_o98_a_record_with_a_repeated_or_misplaced_label_or_a_key_the_writer_never_writes_is_not_read(tmp_path, mutate):
    """O98: the reader takes what the writer produces and nothing else: no extra key, no label twice, no label of one
    kind where the other belongs."""
    document = json.loads(written_record(tmp_path, [WIDE]))
    mutate(document)
    with pytest.raises(failover.FailoverError):
        failover.read_record(json.dumps(document))


# -- the record's orders are one order of the providers, and its labels have no bound ---------------------------------


def spaces_record(*spaces):
    """The text of a record of `spaces`, each `(space, owner, mirrors, order)` as written, with no check of its own."""
    return json.dumps(
        {
            "version": failover.VERSION,
            "spaces": [
                {"space": space, "owner": owner, "mirrors": list(mirrors), "order": list(order), "mirror_first": True}
                for space, owner, mirrors, order in spaces
            ],
        }
    )


# each space is well formed alone: its first provider is a mirror
ONE_BEFORE_ZERO = ("spaces[0]", "providers[0]", ["providers[1]"], ["providers[1]", "providers[0]"])
ZERO_BEFORE_ONE = ("spaces[1]", "providers[1]", ["providers[0]"], ["providers[0]", "providers[1]"])
TWO_BEFORE_ONE = ("spaces[1]", "providers[2]", ["providers[1]"], ["providers[1]", "providers[2]"])


def test_o98_a_record_whose_spaces_order_two_providers_oppositely_is_not_read(tmp_path):
    """O98, the second code review: one set of ids has one order, so a record where spaces[0] has providers[1] below
    providers[0] and spaces[1] the reverse is no record `up` wrote, though each space is well formed alone."""
    with pytest.raises(failover.FailoverError, match="contradict each other"):
        failover.read_record(spaces_record(ONE_BEFORE_ZERO, ZERO_BEFORE_ONE))


def test_o98_a_record_whose_orders_make_a_longer_cycle_is_not_read(tmp_path):
    """O98: no pair is reversed outright, but 0 below 1, 1 below 2 and 2 below 0 have no order."""
    around = (
        ("spaces[0]", "providers[1]", ["providers[0]"], ["providers[0]", "providers[1]"]),
        ("spaces[1]", "providers[2]", ["providers[1]"], ["providers[1]", "providers[2]"]),
        ("spaces[2]", "providers[0]", ["providers[2]"], ["providers[2]", "providers[0]"]),
    )
    with pytest.raises(failover.FailoverError, match="contradict each other"):
        failover.read_record(spaces_record(*around))


def test_o98_a_record_of_several_spaces_that_one_order_of_the_providers_gives_is_read(tmp_path):
    """O98: the orders share providers and agree (1 below 0, 1 below 2): a consistent record is read."""
    found = failover.read_record(spaces_record(ONE_BEFORE_ZERO, TWO_BEFORE_ONE))
    assert [space["space"] for space in found["spaces"]] == ["spaces[0]", "spaces[1]"]
    assert failover.consistent([["a", "b", "c"], ["a", "c"], ["b", "c"], ["d"]]) is True
    assert failover.consistent([["a", "b"], ["b", "c"], ["c", "a"]]) is False


def test_o98_a_label_with_any_number_of_digits_and_no_leading_zero_is_read(tmp_path):
    """O98: the writer writes the position of any space and provider, so a space at 10000 and a provider at 10000 read
    as the record they are (a four-digit cap read them as unreadable); a leading zero still does not."""
    big = ("spaces[10000]", "providers[10000]", ["providers[10001]"], ["providers[10001]", "providers[10000]"])
    assert failover.read_record(spaces_record(big))["spaces"][0]["space"] == "spaces[10000]"
    text = failover.record_text([failover.SpaceOrder(big[0], big[1], tuple(big[2]), tuple(big[3]))])
    assert failover.read_record(text)["spaces"][0]["owner"] == "providers[10000]"
    for refused in ("spaces[010000]", "spaces[00]"):
        with pytest.raises(failover.FailoverError):
            failover.read_record(spaces_record((refused, *big[1:])))


def test_o98_positions_order_by_their_number_across_a_change_of_digits_and_a_very_long_one_is_no_crash(tmp_path):
    """O98: spaces[9] comes before spaces[10] and not after it, and a label of five thousand digits (past the limit of
    `int` on a digit string) is an ordinary label to the reader, which makes no integer of it."""
    nine = ("spaces[9]", "providers[0]", ["providers[1]"], ["providers[1]", "providers[0]"])
    ten = ("spaces[10]", "providers[2]", ["providers[1]"], ["providers[1]", "providers[2]"])
    assert len(failover.read_record(spaces_record(nine, ten))["spaces"]) == 2
    with pytest.raises(failover.FailoverError, match="not in the platform's order"):
        failover.read_record(spaces_record(ten, nine))
    huge = (f"spaces[{'9' * 5000}]", *nine[1:])
    assert len(failover.read_record(spaces_record(nine, huge))["spaces"]) == 2
