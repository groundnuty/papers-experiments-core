"""O98: an owner outage fails a space's reads over to a mirror only when a mirror's provider id sorts below the owner's,
so the order is computed from the platform's ids, checked, and told by labels alone (`expcore_onedata.failover`).

Every name and id here is invented. A provider is `owner-site`, `mirror-one` or `mirror-two`; its id is whatever the
test gives it, so the order is the test's own."""

import dataclasses
import json

import pytest

from expcore.config.loading import ConfigError
from expcore_onedata import failover
from expcore_onedata.config import load_platform
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.testing.world import with_placement

OWNER, MIRROR_ONE, MIRROR_TWO = "owner-site", "mirror-one", "mirror-two"
LABEL = {OWNER: "providers[0]", MIRROR_ONE: "providers[1]", MIRROR_TWO: "providers[2]"}


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")


def platform(tmp_path, ids, spaces):
    """A platform of the three providers with the ids `ids` gives (by name), and `[[spaces]]` of `(supporters, holder)`
    each, the first supporter local: every space is supported by `mirror-one`, which is the agent's local provider."""

    def edit(data):
        data["providers"] = {name: {"url": f"https://{name}.test", "id": value} for name, value in ids.items()}
        del data["space"], data["support"]
        data["spaces"] = [
            {
                "label": f"entry-{index}",
                "name": f"invented-{index}-space",
                "id": f"sp-invented-{index}",
                "prefixes": [f"invented-{index}-"],
                "providers": list(supporters),
                "local": MIRROR_ONE,
                "holder": holder,
            }
            for index, (supporters, holder) in enumerate(spaces)
        ]

    return load_platform(write_platform(tmp_path, edit))


ALL_THREE = ((OWNER, MIRROR_ONE, MIRROR_TWO), OWNER)  # one space, the owner holding it, both others mirroring it


def ids(owner, one, two):
    return {OWNER: owner, MIRROR_ONE: one, MIRROR_TWO: two}


def test_o98_a_mirror_whose_id_sorts_below_the_owners_is_asked_first_and_the_order_passes(tmp_path):
    """O98: the supporters are ordered by id, and the first being a mirror is what makes a failover possible."""
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200"), [ALL_THREE])
    (found,) = failover.admit(config, [OWNER])
    assert found.mirror_first is True
    assert found.order == (LABEL[MIRROR_ONE], LABEL[MIRROR_TWO], LABEL[OWNER])


def test_o98_an_owner_whose_id_sorts_below_every_mirrors_is_refused_naming_labels_only(tmp_path):
    """O98: the owner first means op-worker asks the stopped owner first, so the member is refused, never passed; the
    message names the space and the providers by position and holds no name and no id."""
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [ALL_THREE])
    with pytest.raises(failover.FailoverRefused) as refused:
        failover.admit(config, [OWNER])
    text = str(refused.value)
    assert text.startswith("failover order: ")
    assert "the owner's provider id sorts before every mirror's, so op-worker would ask the stopped owner first" in text
    assert "spaces[0]" in text
    assert "owner providers[0]" in text
    assert "mirrors providers[1], providers[2]" in text
    for secret in (OWNER, MIRROR_ONE, MIRROR_TWO, "id-0100", "id-0200", "id-0300", "invented-0-space", "entry-0"):
        assert secret not in text


@pytest.mark.parametrize(
    ("owner", "one", "two", "passes"),
    [
        ("id-3", "id-1", "id-2", True),  # both mirrors below the owner
        ("id-2", "id-1", "id-3", True),  # one mirror below it, the other above: the first holder in order serves
        ("id-2", "id-3", "id-1", True),  # the same with the mirrors the other way round
        ("id-1", "id-2", "id-3", False),  # the owner below both
    ],
)
def test_o98_with_several_mirrors_it_passes_when_any_one_sorts_before_the_owner(tmp_path, owner, one, two, passes):
    """O98: the first holder in the order serves, so one mirror below the owner is enough, and none is not."""
    config = platform(tmp_path, ids(owner, one, two), [ALL_THREE])
    found = failover.orders(config, [OWNER])[0]
    assert found.mirror_first is passes
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
        platform(tmp_path, ids("id-same", "id-same", "id-0200"), [ALL_THREE])


def test_o98_a_provider_with_no_id_in_a_space_in_scope_is_refused_not_passed(tmp_path):
    """O98: a provider built in code without an id gives no order, which is no pass."""
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [ALL_THREE])
    providers = {**config.providers, MIRROR_ONE: dataclasses.replace(config.providers[MIRROR_ONE], id=None)}
    bare = dataclasses.replace(config, providers=providers)
    with pytest.raises(failover.FailoverError, match="providers\\[1\\] is not known"):
        failover.admit(bare, [OWNER])


# -- scope: only the spaces an owner outage reaches ------------------------------------------------------------------


def test_o98_no_declared_owner_outage_puts_nothing_in_scope(tmp_path):
    """O98: with no owner declared nothing is read: no spaces in scope, whatever the ids are (the owner sorts first)."""
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [ALL_THREE])
    assert failover.orders(config, []) == ()
    assert failover.admit(config, ()) == ()


def test_o98_a_provider_that_holds_no_space_with_a_mirror_is_no_owner_stop(tmp_path):
    """O98: a mirror taken down, a provider that holds nothing and an owner of a space nothing else supports are not
    owner outages: nothing is in scope for any of them, even with the worst ids."""
    sole = ((MIRROR_ONE,), MIRROR_ONE)
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [ALL_THREE, sole])
    assert failover.orders(config, [MIRROR_ONE]) == ()  # it only mirrors the first, and holds a space with no mirror
    assert failover.orders(config, [MIRROR_TWO]) == ()  # it only mirrors
    assert [found.space for found in failover.orders(config, [OWNER])] == ["spaces[0]"]


def test_o98_only_the_spaces_the_owner_holds_are_in_scope_and_each_is_told_by_its_position(tmp_path):
    """O98: several spaces, two owners: each owner's own spaces, in the file's order, by `spaces[i]`; a space another
    provider holds is judged only when that provider is the one declared."""
    spaces = [ALL_THREE, ((MIRROR_ONE, MIRROR_TWO), MIRROR_TWO), ALL_THREE]
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), spaces)
    assert [found.space for found in failover.orders(config, [OWNER])] == ["spaces[0]", "spaces[2]"]
    assert [found.space for found in failover.orders(config, [MIRROR_TWO])] == ["spaces[1]"]
    both = failover.orders(config, [OWNER, MIRROR_TWO])
    assert [found.space for found in both] == ["spaces[0]", "spaces[1]", "spaces[2]"]
    assert both[1].order == (LABEL[MIRROR_ONE], LABEL[MIRROR_TWO])  # mirror-one's id is below mirror-two's


def test_o98_the_one_space_form_is_in_scope_through_its_placement_holder(tmp_path):
    """O98: the one-space form names its data provider in `[placement]`; it is the owner there, and without that
    table it has no holder and so no scope."""

    def edit(data):
        invented = ids("id-0300", "id-0100", "id-0200")
        data["providers"] = {name: {"url": f"https://{name}.test", "id": value} for name, value in invented.items()}
        data["support"] = {"providers": [OWNER, MIRROR_ONE], "local": MIRROR_ONE}

    config = load_platform(write_platform(tmp_path, edit))
    assert failover.orders(config, [OWNER]) == ()
    (tmp_path / "with").mkdir()

    def with_holder(data):
        edit(data)
        with_placement(data, holder=OWNER)

    placed = load_platform(write_platform(tmp_path / "with", with_holder))
    (found,) = failover.orders(placed, [OWNER])
    assert (found.space, found.owner, found.mirrors) == ("spaces[0]", "providers[0]", ("providers[1]",))
    assert found.order == ("providers[1]", "providers[0]")  # mirror-one's id is below the owner's


def test_o98_an_outage_target_the_platform_does_not_declare_is_an_error_naming_no_provider(tmp_path):
    """O98: a target that is no provider of the platform is no scope to skip silently."""
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [ALL_THREE])
    with pytest.raises(failover.FailoverError) as error:
        failover.orders(config, ["a-provider-nobody-declared"])
    assert "a-provider-nobody-declared" not in str(error.value)


# -- the record ---------------------------------------------------------------------------------------------------


def test_o98_the_record_holds_labels_and_the_order_and_nothing_else(tmp_path):
    """O98: per space in scope, its position, the owner's and mirrors' labels, the order and `mirror_first`; no name."""
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200"), [ALL_THREE])
    text = failover.record_text(failover.admit(config, [OWNER]))
    assert text.endswith("\n")
    assert json.loads(text) == {
        "version": "expcore-failover-order/1",
        "spaces": [
            {
                "space": "spaces[0]",
                "owner": "providers[0]",
                "mirrors": ["providers[1]", "providers[2]"],
                "order": ["providers[1]", "providers[2]", "providers[0]"],
                "mirror_first": True,
            }
        ],
    }
    for secret in (OWNER, MIRROR_ONE, MIRROR_TWO, "id-0", "invented", "entry-"):
        assert secret not in text
    assert failover.read_record(text)["spaces"][0]["order"][0] == "providers[1]"


@pytest.mark.parametrize(
    "changed",
    [
        {"mirror_first": False},
        {"order": ["providers[0]", "providers[1]", "providers[2]"]},  # the owner first
        {"order": ["providers[1]", "providers[0]"]},  # not every supporter
        {"owner": 7},
        {"unexpected": 1},
    ],
)
def test_o98_a_record_that_is_not_what_was_written_is_not_read(tmp_path, changed):
    """O98: a record that says the owner is first, lacks a supporter or holds an extra key is refused when read."""
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200"), [ALL_THREE])
    document = json.loads(failover.record_text(failover.admit(config, [OWNER])))
    document["spaces"][0].update(changed)
    with pytest.raises(failover.FailoverError):
        failover.read_record(json.dumps(document))
    with pytest.raises(failover.FailoverError):
        failover.read_record("not json")
