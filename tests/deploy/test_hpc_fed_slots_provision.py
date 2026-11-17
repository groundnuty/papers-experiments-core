"""O83: a slot user that exists is verified and never changed, and one that does not is made as any principal is.

The pilot's Compose federation already has slot users made by hand. Provisioning must be idempotent over them: a slot
user that exists exactly as the layout declares it (its zone user, its password file, its memberships and their
privileges, direct and effective, no group, no cluster, no other space) is recorded `matched` and nothing is sent for
it; any difference is refused naming the user and the attribute, and nothing is changed. The Compose bootstrap and the
HPC one are run over the same pre-existing users, each on a fake zone of its own that records every write.
"""

import pytest
from hpc_fed_support import hpc_on_path
from slots_boot import boots, made_by_hand, modules, provision
from slots_support import (
    BASE_SET,
    LAYOUT,
    READ_SET,
    SLOT_USERS,
    TEMPLATE,
    WITH_SLOTS,
    WRITE_SET,
    layout_with_template,
    secret_of,
)

TWO_SPACES = LAYOUT.replace(
    "[principals.alpha-reader]", '[spaces.beta-space]\nowner = "elm"\n\n[principals.alpha-reader]'
)


@pytest.fixture(scope="module")
def hpc():
    with hpc_on_path() as found:
        yield modules(found)


@pytest.fixture(params=["compose", "hpc"])
def platform(request, hpc, tmp_path):
    """One platform's bootstrap over the layout, with its zone and root: `(zone, boot, layout)`."""
    (_, zone, boot, root) = next(b for b in boots(hpc, tmp_path) if b[0] == request.param)
    return zone, boot, root


def test_o83_a_slot_user_that_does_not_exist_is_made_with_exactly_the_declared_privileges_and_recorded_created(
    platform,
):
    """O83: as any principal is made: the zone user, its password in a private file, its membership of the declared
    space with its declared privileges; and the facts say each slot user was `created` (the fixed user is no slot's)."""
    zone, boot, layout = platform
    spec = boot.spec
    assert provision(boot, spec) == dict.fromkeys(SLOT_USERS, "created")
    held = {login: zone.members["sid-alpha-space"][uid] for login, (uid, _) in zone.users.items()}
    assert held["slot1-data"] == BASE_SET
    assert held["slot1-tools"] == READ_SET
    assert held["slot2-data"] == BASE_SET
    assert held["slot2-tools"] == WRITE_SET
    assert layout.password("slot1-tools").read_text(encoding="utf-8") == zone.users["slot1-tools"][1]


def test_o83_a_slot_user_that_exists_exactly_as_declared_is_matched_and_nothing_is_sent_for_it(platform):
    """O83: made by hand, with the password file the root holds: provisioning reads it, records it `matched`, and sends
    no create, no invite, no join and no privilege change; the password file is the one that was there."""
    zone, boot, layout = platform
    spec = boot.spec
    made_by_hand(zone, layout, spec)
    before = zone.snapshot()
    assert provision(boot, spec) == dict.fromkeys(SLOT_USERS, "matched")
    assert zone.writes == []
    assert zone.snapshot() == before
    assert all(layout.password(u).read_text(encoding="utf-8") == secret_of(u) for u in SLOT_USERS)


def test_o83_a_rerun_matches_what_the_first_up_made_and_sends_nothing(hpc, tmp_path):
    """O83: up, then up again over the same zone, as the resets do: the first makes the slot users, the second finds
    each exactly as declared and sends nothing."""
    for _, zone, boot, layout in boots(hpc, tmp_path):
        provision(boot, boot.spec)
        sent = len(zone.writes)
        second = type(boot)(zone, layout, *((5001, 5001),) if type(boot).__name__ == "HpcBootstrap" else ())
        second.zone = zone.zone
        second.ids.spaces = dict(boot.ids.spaces)
        assert provision(second, second.spec) == dict.fromkeys(SLOT_USERS, "matched")
        assert len(zone.writes) == sent


def deviate_direct(zone, spec):
    zone.members["sid-alpha-space"]["uid-slot1-tools"].add("space_manage_qos")


def deviate_missing(zone, spec):
    zone.members["sid-alpha-space"]["uid-slot2-tools"].discard("space_schedule_replication")


def deviate_effective(zone, spec):
    zone.effective_extra[("sid-alpha-space", "uid-slot1-tools")] = {"space_manage_qos"}


def deviate_membership(zone, spec):
    del zone.members["sid-alpha-space"]["uid-slot1-data"]


def deviate_spaces(zone, spec):
    zone.join("beta-space", "slot1-data", {"space_view"})


def deviate_group(zone, spec):
    zone.groups["uid-slot2-data"] = ["a-group"]


def deviate_cluster(zone, spec):
    zone.clusters["uid-slot1-tools"] = ["a-cluster"]


def deviate_password(zone, spec):
    zone.users["slot2-tools"] = ("uid-slot2-tools", "another password than the root's file")


DEVIATIONS = {
    "direct privileges": (deviate_direct, "slot1-tools", "direct privileges in alpha-space"),
    "a privilege short": (deviate_missing, "slot2-tools", "direct privileges in alpha-space"),
    "effective privileges": (deviate_effective, "slot1-tools", "effective privileges in alpha-space"),
    "membership": (deviate_membership, "slot1-data", "membership of alpha-space"),
    "another space": (deviate_spaces, "slot1-data", "spaces"),
    "a group": (deviate_group, "slot2-data", "group"),
    "a cluster": (deviate_cluster, "slot1-tools", "cluster"),
    "the password": (deviate_password, "slot2-tools", "password"),
}


@pytest.mark.parametrize("what", DEVIATIONS)
def test_o83_a_slot_user_that_exists_and_differs_is_refused_naming_the_user_and_the_attribute_and_is_never_changed(
    hpc, tmp_path, what
):
    """O83: each difference an existing slot user may have (a direct or an effective privilege, a membership, another
    space, a group, a cluster, a password the root's file does not match) is refused on both platforms, naming the
    principal and the attribute and saying it was never changed; the zone is as it was, no join, privilege change or
    user made (a refused creation is the zone's, which answered that the user exists)."""
    deviate, user, attribute = DEVIATIONS[what]
    for _, zone, boot, layout in boots(hpc, tmp_path / "p", WITH_SLOTS, TWO_SPACES):
        spec = boot.spec
        made_by_hand(zone, layout, spec)
        deviate(zone, spec)
        before = zone.snapshot()
        with pytest.raises(RuntimeError, match=f"slot user {user}: {attribute}: .*never changed") as refused:
            provision(boot, spec)
        assert zone.snapshot() == before
        assert not [w for w in zone.writes if w[0] != "create user"]
        assert "pw-" not in str(refused.value)  # no secret in a message


def test_o83_a_password_this_call_made_for_a_user_the_zone_refused_is_not_left_behind(hpc, tmp_path):
    """O83: a slot user the zone has, whose password file the root lacks, is refused as `password`, and the file this
    call made for it is removed, so the operator's own file can be put there."""
    for _, zone, boot, layout in boots(hpc, tmp_path):
        zone.add_user("slot1-tools", "a password only the operator holds")
        with pytest.raises(RuntimeError, match="slot user slot1-tools: password"):
            provision(boot, boot.spec)
        assert not layout.password("slot1-tools").exists()


def test_o83_a_fixed_principal_is_converged_as_before_and_is_no_slot_user(hpc, tmp_path):
    """O83: only the slot users are read-only. A fixed principal that exists with another privilege is set as declared,
    as it always was, and is in no slot's record."""
    for _, zone, boot, layout in boots(hpc, tmp_path):
        spec = boot.spec
        made_by_hand(zone, layout, spec)
        zone.members["sid-alpha-space"]["uid-alpha-reader"].add("space_manage_qos")
        assert provision(boot, spec) == dict.fromkeys(SLOT_USERS, "matched")
        assert zone.members["sid-alpha-space"]["uid-alpha-reader"] == BASE_SET
        assert [w[0] for w in zone.writes] == ["privileges"]  # one privilege change, no join


def test_o83_a_template_with_no_slots_has_no_slot_users_and_every_user_is_made_as_before(hpc, tmp_path):
    """O83: nothing is read-only without `[slots]`: the audit records nothing, the principals converge as ever."""
    for _, zone, boot, layout in boots(hpc, tmp_path, TEMPLATE):
        spec = boot.spec
        made_by_hand(zone, layout, spec)
        zone.members["sid-alpha-space"]["uid-slot1-tools"].add("space_manage_qos")
        assert provision(boot, spec) == {}
        assert zone.members["sid-alpha-space"]["uid-slot1-tools"] == READ_SET


def test_o83_a_template_that_declares_slots_and_cannot_be_read_before_it_is_rendered_is_refused(hpc, tmp_path):
    """O83: provisioning must know which users it may never change before the file is rendered, so `[slots]` is read
    from the template as it stands; a placeholder in a table's name leaves nothing to read, and is refused."""
    template = WITH_SLOTS.replace("[slots.slot-1.data_path]", "[slots.@user_id.slot1-data@.data_path]")
    spec = hpc.fedspec.load_spec(layout_with_template(template, tmp_path))
    with pytest.raises(RuntimeError, match="no TOML before it is rendered"):
        hpc.fedtools.slot_users(spec)


def test_o83_the_facts_record_each_slot_user_as_created_or_matched(hpc, tmp_path):
    """O83: `facts.json` says, per slot user, what provisioning found, and holds no key for a layout with no slot."""
    import json

    for _, zone, boot, layout in boots(hpc, tmp_path):
        made_by_hand(zone, layout, boot.spec, only={"slot1-data", "slot1-tools", "alpha-reader"})
        provision(boot, boot.spec)
        facts = hpc.fedfacts.facts_of(boot)
        assert facts["slot_users"] == {
            "slot1-data": "matched",
            "slot1-tools": "matched",
            "slot2-data": "created",
            "slot2-tools": "created",
        }
        assert json.dumps(facts).count("pw-") == 0
    for _, _, boot, _ in boots(hpc, tmp_path / "plain", TEMPLATE):
        provision(boot, boot.spec)
        assert "slot_users" not in hpc.fedfacts.facts_of(boot)
