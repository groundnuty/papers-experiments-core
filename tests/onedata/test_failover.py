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


def written_record(tmp_path, spaces):
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200"), spaces)
    return failover.record_text(failover.admit(config, [OWNER]))


def test_o98_a_record_of_several_spaces_is_read_and_one_of_none_is_not(tmp_path):
    """O98: what the writer produces for two spaces in scope is read; the same record with its spaces taken out is the
    record of an admission that writes none, which no run produces and no reader takes for a pass."""
    text = written_record(tmp_path, [ALL_THREE, ALL_THREE])
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
        lambda d: d["spaces"][0].update(mirrors=["providers[1]", "providers[1]"]),
        lambda d: d["spaces"][0].update(space="providers[0]"),  # a label of the wrong kind
        lambda d: d["spaces"][0].update(owner="spaces[0]"),
    ],
)
def test_o98_a_record_with_a_repeated_or_misplaced_label_or_a_key_the_writer_never_writes_is_not_read(tmp_path, mutate):
    """O98: the reader takes what the writer produces and nothing else: no extra key, no label twice, no label of one
    kind where the other belongs."""
    document = json.loads(written_record(tmp_path, [ALL_THREE]))
    mutate(document)
    with pytest.raises(failover.FailoverError):
        failover.read_record(json.dumps(document))


# -- only the declared mirrors are in the order (the second code review) ---------------------------------------------


# the owner's space is supported by `mirror-one`, the agent's local provider, which holds none of the space's blocks
# unless it is designated a mirror, and by `mirror-two`: the local provider sorts below the owner, the owner below it
LOCAL_BELOW_OWNER_BELOW_MIRROR = ids("id-0200", "id-0100", "id-0300")
ONLY_MIRROR_TWO = {"spaces[0]": ["providers[2]"]}  # the one provider the study designates a mirror of the space


def test_o98_counting_every_supporter_lets_a_local_provider_that_holds_nothing_make_the_order_pass(tmp_path):
    """O98: with no declaration every supporter counts, as it always did: the local provider sorts below the owner, so
    the order passes. That is the behaviour a federation with no layout keeps (Compose, a test)."""
    config = platform(tmp_path, LOCAL_BELOW_OWNER_BELOW_MIRROR, [ALL_THREE])
    (found,) = failover.admit(config, [OWNER])
    assert found.mirrors == (LABEL[MIRROR_ONE], LABEL[MIRROR_TWO])
    assert found.order == (LABEL[MIRROR_ONE], LABEL[OWNER], LABEL[MIRROR_TWO])


def test_o98_with_the_declared_mirrors_a_supporter_that_is_none_is_ignored_and_the_owner_stop_is_refused(tmp_path):
    """O98, the second code review: the file is replicated to the designated mirror alone and the local provider holds
    zero bytes, so op-worker chooses between the owner and that mirror, and with the owner's id below the mirror's it
    asks the stopped owner first. Ordered over the owner and the declared mirror the same ids are refused, and the
    message names the mirror and not the local provider."""
    config = platform(tmp_path, LOCAL_BELOW_OWNER_BELOW_MIRROR, [ALL_THREE])
    with pytest.raises(failover.FailoverRefused) as refused:
        failover.admit(config, [OWNER], ONLY_MIRROR_TWO)
    (failed,) = refused.value.failed
    assert (failed.owner, failed.mirrors, failed.order) == (
        LABEL[OWNER],
        (LABEL[MIRROR_TWO],),
        (LABEL[OWNER], LABEL[MIRROR_TWO]),
    )
    assert "(owner providers[0]; mirrors providers[2])" in str(refused.value)
    assert "providers[1]" not in str(refused.value)  # the local provider is no part of the order


def test_o98_a_declared_mirror_below_the_owner_passes_whatever_the_ignored_supporter_does(tmp_path):
    """O98: the declared mirror sorts below the owner, and the local provider above both: the order passes over the two
    and the record lists the declared mirror alone."""
    config = platform(tmp_path, ids("id-0200", "id-0900", "id-0100"), [ALL_THREE])
    (found,) = failover.admit(config, [OWNER], ONLY_MIRROR_TWO)
    assert (found.mirrors, found.order) == ((LABEL[MIRROR_TWO],), (LABEL[MIRROR_TWO], LABEL[OWNER]))
    assert json.loads(failover.record_text([found]))["spaces"][0]["mirrors"] == [LABEL[MIRROR_TWO]]


def test_o98_a_space_the_declaration_lists_with_no_mirror_is_out_of_scope_and_one_it_omits_counts_every_supporter(
    tmp_path,
):
    """O98: `{"spaces[0]": []}` designates no mirror, so nothing is in scope there; a space the mapping does not list is
    ordered over all its supporters, as with no mapping."""
    config = platform(tmp_path, LOCAL_BELOW_OWNER_BELOW_MIRROR, [ALL_THREE, ALL_THREE])
    assert [found.space for found in failover.orders(config, [OWNER], {"spaces[0]": []})] == ["spaces[1]"]
    only_second = failover.orders(config, [OWNER], {"spaces[1]": ["providers[2]"]})
    assert [(found.space, found.mirrors) for found in only_second] == [
        ("spaces[0]", (LABEL[MIRROR_ONE], LABEL[MIRROR_TWO])),
        ("spaces[1]", (LABEL[MIRROR_TWO],)),
    ]


@pytest.mark.parametrize(
    "wanted",
    [["providers[0]"], ["providers[2]", "providers[2]"], ["providers[9]"], ["providers[2]", "spaces[0]"]],
    ids=["the owner", "one twice", "no provider of the file", "a label of the other kind"],
)
def test_o98_a_declared_mirror_that_is_no_other_supporter_of_the_space_gives_no_order(tmp_path, wanted):
    """O98: the owner as its own mirror, a mirror twice, and one the file does not have as a supporter are no order and
    no pass; the message names the space and holds no name and no id."""
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200"), [ALL_THREE])
    with pytest.raises(
        failover.FailoverError, match="spaces\\[0\\]: a declared mirror is no other supporter"
    ) as refused:
        failover.admit(config, [OWNER], {"spaces[0]": wanted})
    assert not isinstance(refused.value, failover.FailoverRefused)


# -- the injector's check over a root's record -------------------------------------------------------------------------


def record_of(*spaces):
    """The text of a record that holds `spaces`, each `(space, owner, mirrors, order)` of labels."""
    return failover.record_text([failover.SpaceOrder(*space) for space in spaces])


# the record `up` wrote for the platform's one space: the owner and the declared mirror alone, the mirror first
RECORD_OF_MIRROR_TWO = record_of(("spaces[0]", LABEL[OWNER], (LABEL[MIRROR_TWO],), (LABEL[MIRROR_TWO], LABEL[OWNER])))


def stop_of(tmp_path, record, platform_ids=LOCAL_BELOW_OWNER_BELOW_MIRROR, spaces=(ALL_THREE,)):
    """`admit_stop` of the owner over a root `tmp_path / "root"` holding `record` (None: none) and a platform with the
    ids `platform_ids`."""
    root = tmp_path / "root"
    root.mkdir()
    if record is not None:
        (root / failover.FILE).write_text(record, encoding="utf-8")
    return failover.admit_stop(platform(tmp_path, platform_ids, list(spaces)), OWNER, root)


def test_o98_without_a_record_a_stop_is_checked_over_every_supporter_as_before(tmp_path):
    """O98: no record in the root (a Compose federation, a test): the local provider counts and the order passes."""
    (found,) = stop_of(tmp_path, None)
    assert found.mirrors == (LABEL[MIRROR_ONE], LABEL[MIRROR_TWO])


def test_o98_with_a_record_a_stop_takes_each_listed_spaces_mirrors_from_it_against_the_current_ids(tmp_path):
    """O98, the second code review: the record designates mirror-two alone; against the platform's current ids (the
    owner's below its mirror's) the stop is refused, though counting the local provider that sorts first would pass."""
    with pytest.raises(failover.FailoverRefused, match="owner providers\\[0\\]; mirrors providers\\[2\\]"):
        stop_of(tmp_path, RECORD_OF_MIRROR_TWO)
    other = tmp_path / "other"
    other.mkdir()
    (found,) = stop_of(other, RECORD_OF_MIRROR_TWO, ids("id-0200", "id-0900", "id-0100"))
    assert (found.mirrors, found.order) == ((LABEL[MIRROR_TWO],), (LABEL[MIRROR_TWO], LABEL[OWNER]))


def test_o98_a_space_the_record_does_not_list_is_checked_over_every_supporter_so_a_record_never_loosens_the_check(
    tmp_path,
):
    """O98: the record lists spaces[0] only. Its mirrors decide it (refused: the owner sorts below the one mirror), and
    spaces[1], which the owner holds as well, counts all its supporters (it would pass: the local provider sorts
    below the owner), so a record narrows the check of the spaces it lists and loosens none."""
    with pytest.raises(failover.FailoverRefused) as refused:
        stop_of(tmp_path, RECORD_OF_MIRROR_TWO, spaces=(ALL_THREE, ALL_THREE))
    assert [failed.space for failed in refused.value.failed] == ["spaces[0]"]


@pytest.mark.parametrize(
    "record",
    [
        "not json",
        "[]",
        '{"version": "another/9", "spaces": []}',
        record_of(("spaces[0]", LABEL[OWNER], (LABEL[MIRROR_TWO],), (LABEL[MIRROR_TWO], LABEL[OWNER]))).replace(
            '"mirror_first": true', '"mirror_first": false'
        ),
        record_of(
            ("spaces[5]", LABEL[OWNER], (LABEL[MIRROR_TWO],), (LABEL[MIRROR_TWO], LABEL[OWNER]))
        ),  # no such space
        record_of(("spaces[0]", LABEL[MIRROR_ONE], (LABEL[MIRROR_TWO],), (LABEL[MIRROR_TWO], LABEL[MIRROR_ONE]))),
        record_of(("spaces[0]", LABEL[OWNER], ("providers[7]",), ("providers[7]", LABEL[OWNER]))),  # no such provider
    ],
    ids=[
        "no json",
        "no object",
        "another version",
        "mirror_first false",
        "a space the file lacks",
        "another owner",
        "a provider the file lacks",
    ],
)
def test_o98_a_record_that_is_unreadable_or_does_not_resolve_in_the_platform_file_refuses_the_stop(tmp_path, record):
    """O98: strict and closed: a record that is not what `up` writes, or whose labels are not the platform file's (a
    space or provider that is not there, another data provider than the record's owner), is no ground to take the owner
    down, however the ids sort; the message holds labels only."""
    with pytest.raises(failover.FailoverError) as refused:
        stop_of(tmp_path, record, ids("id-0300", "id-0100", "id-0200"))
    text = str(refused.value)
    assert text.startswith("failover order: ")
    for secret in (OWNER, MIRROR_ONE, MIRROR_TWO, "id-0"):
        assert secret not in text


def test_o98_a_record_with_a_space_of_another_owner_that_the_file_lacks_refuses_the_stop(tmp_path):
    """O98: the stop is of the owner of spaces[0], which the record resolves; the record also lists a space of another
    provider at a position the platform file has no space at: the record is not of this file, so no order is told, even
    though the owner's own space would pass (its mirror sorts below it)."""
    mine = ("spaces[0]", LABEL[OWNER], (LABEL[MIRROR_TWO],), (LABEL[MIRROR_TWO], LABEL[OWNER]))
    foreign = ("spaces[3]", LABEL[MIRROR_ONE], (LABEL[MIRROR_TWO],), (LABEL[MIRROR_TWO], LABEL[MIRROR_ONE]))
    passing = ids("id-0300", "id-0100", "id-0200")
    (found,) = stop_of(tmp_path, record_of(mine), passing)  # the record of the owner's space alone goes ahead
    assert found.order[0] == LABEL[MIRROR_TWO]
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(failover.FailoverError, match="spaces\\[3\\] of the record does not resolve"):
        stop_of(other, record_of(mine, foreign), passing)


def test_o98_a_record_that_cannot_be_read_as_text_refuses_the_stop(tmp_path):
    """O98: bytes that are no UTF-8 text, and a record that is a directory, are unreadable, not absent."""
    root = tmp_path / "root"
    root.mkdir()
    (root / failover.FILE).write_bytes(b"\xff\xfe not text")
    config = platform(tmp_path, ids("id-0300", "id-0100", "id-0200"), [ALL_THREE])
    with pytest.raises(failover.FailoverError, match="no JSON"):
        failover.admit_stop(config, OWNER, root)
    (root / failover.FILE).unlink()
    (root / failover.FILE).mkdir()
    with pytest.raises(failover.FailoverError, match="cannot be read"):
        failover.admit_stop(config, OWNER, root)


def test_o98_a_stop_of_a_provider_that_owns_no_space_with_a_mirror_reads_no_record(tmp_path):
    """O98: the mirror taken down is no owner stop: a record that is garbage is not read, and nothing is refused."""
    root = tmp_path / "root"
    root.mkdir()
    (root / failover.FILE).write_text("garbage", encoding="utf-8")
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [ALL_THREE])
    assert failover.admit_stop(config, MIRROR_TWO, root) == ()


def test_o98_a_stop_of_a_name_that_is_no_provider_of_the_platform_is_an_error_not_a_pass(tmp_path):
    """O98: the owner the injector was built with is a provider of the platform; anything else is no order."""
    config = platform(tmp_path, ids("id-0100", "id-0200", "id-0300"), [ALL_THREE])
    with pytest.raises(failover.FailoverError, match="no provider of the platform"):
        failover.admit_stop(config, "not-a-provider", tmp_path)


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
