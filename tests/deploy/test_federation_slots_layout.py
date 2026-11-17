"""The slots and the tool server a layout declares (O83, deploy/compose-federation): what is accepted, and each way a
declaration is refused at load, naming the file and the key. The same layout file brings both platforms up, so this is
the one place its slots are checked."""

import pytest
from federation_support import MINIMAL
from slots_support import BASE, COMMIT, READ, TOOLSERVER, USERS, WITH_SLOTS, WITH_TOOLSERVER, WRITE


def load(fed, tmp_path, text):
    path = tmp_path / "layout.toml"
    path.write_text(text, encoding="utf-8")
    return fed.spec.load_spec(path)


def test_o83_a_layout_declares_slots_that_pair_two_declared_users_and_a_tool_server_with_its_conditions(fed, tmp_path):
    """O83: a slot names two principals of the layout, and the tool server's conditions declare the rights of the
    slots' tool-server users; the users stay ordinary principals, so every step that provisions principals provisions
    them."""
    spec = load(fed, tmp_path, WITH_TOOLSERVER)
    assert [(s.name, s.data_path, s.toolserver) for s in spec.slots] == [
        ("slot-1", "slot1-data", "slot1-tools"),
        ("slot-2", "slot2-data", "slot2-tools"),
    ]
    assert [p.name for p in spec.principals] == list(USERS)
    tool = spec.toolserver
    assert (tool.command, tool.args, tool.commit, tool.start_timeout_s) == (
        "@study_artifacts@/venv/bin/python",
        ("-m", "onedata_mcp"),
        COMMIT,
        90,
    )
    assert [c.name for c in tool.conditions] == ["knowledge", "writing"]
    assert tool.conditions[0].rights == tuple(sorted(spec.principal("slot1-tools").privileges))
    assert tool.conditions[1].rights == tuple(sorted(spec.principal("slot2-tools").privileges))


def test_o83_a_layout_without_slots_declares_none_and_slots_alone_need_no_tool_server(fed, tmp_path):
    """O83: every layout that existed declares neither table, and a platform whose runs have no tool server still
    leases a slot's users."""
    plain = load(fed, tmp_path, MINIMAL)
    assert (plain.slots, plain.toolserver) == ((), None)
    only = load(fed, tmp_path, WITH_SLOTS)
    assert len(only.slots) == 2
    assert only.toolserver is None


def test_o83_the_committed_layouts_declare_no_slots_unless_they_are_the_slot_layout(fed):
    """O83: the layouts that were there before declare none, so their rendered platform files and facts stay as they
    were."""
    for layout in (
        None,
        fed.spec.HERE / "layouts" / "five-providers.toml",
        fed.spec.HERE / "layouts" / "storage-import.toml",
    ):
        spec = fed.spec.load_spec(layout)
        assert (spec.slots, spec.toolserver) == ((), None)


def _without(text, *lines):
    for line in lines:
        assert line in text
        text = text.replace(line, "", 1)
    return text


SLOT_REFUSALS = {
    "an undeclared data-path user": (('data_path = "slot1-data"', 'data_path = "nobody"'), "slots.slot-1.data_path"),
    "an undeclared tool-server user": (
        ('toolserver = "slot1-tools"', 'toolserver = "nobody"'),
        "slots.slot-1.toolserver",
    ),
    "one user for both planes": (
        ('toolserver = "slot1-tools"', 'toolserver = "slot1-data"'),
        "slots.slot-1.toolserver",
    ),
    "a user of two slots": (('data_path = "slot2-data"', 'data_path = "slot1-data"'), "slots.slot-2.data_path"),
    "a tool-server user of the first slot as another's data path": (
        ('data_path = "slot2-data"', 'data_path = "slot1-tools"'),
        "slots.slot-2.data_path",
    ),
    "a data-path user with more than the baseline": (
        (f"[principals.slot1-data]\nprivileges = [{BASE}]", f"[principals.slot1-data]\nprivileges = [{READ}]"),
        "slots.slot-1.data_path",
    ),
    "a tool-server user without the baseline": (
        (
            f"[principals.slot1-tools]\nprivileges = [{READ}]",
            '[principals.slot1-tools]\nprivileges = ["space_view_qos"]',
        ),
        "slots.slot-1.toolserver",
    ),
    "a tool-server user with a right the core does not know": (
        (
            f"[principals.slot1-tools]\nprivileges = [{READ}]",
            f'[principals.slot1-tools]\nprivileges = [{BASE}, "space_write_data"]',
        ),
        "slots.slot-1.toolserver",
    ),
    "a user the agent may not use": (
        (
            f"[principals.slot1-data]\nprivileges = [{BASE}]",
            f"[principals.slot1-data]\nprivileges = [{BASE}]\nagent = false",
        ),
        "slots.slot-1.data_path",
    ),
    "a slot name the core does not take": (("[slots.slot-1]", "[slots.1-slot]"), "slots.1-slot"),
    "an unknown key": (("[slots.slot-1]", '[slots.slot-1]\nuser = "x"'), "slots.slot-1.user"),
    "a missing plane": (('data_path = "slot1-data"\n', ""), "slots.slot-1.data_path"),
}


@pytest.mark.parametrize("what", SLOT_REFUSALS)
def test_o83_a_slot_that_would_lease_users_the_core_refuses_is_refused_at_load(fed, tmp_path, what):
    """O83: the checks of `expcore_onedata.users.load_slots` and the token checks are made when the layout is read, so
    no federation is brought up whose slots the platform layer would refuse."""
    (old, new), key = SLOT_REFUSALS[what]
    assert old in WITH_TOOLSERVER
    with pytest.raises(fed.spec.LayoutError, match=key.replace(".", r"\.")):
        load(fed, tmp_path, WITH_TOOLSERVER.replace(old, new, 1))


def test_o83_every_user_of_every_slot_is_a_member_of_the_same_spaces(fed, tmp_path):
    """O83: a slot's users must be members of exactly the plane's spaces, and any slot may serve any run, so a slot
    whose users reach other spaces than another's is refused."""
    text = WITH_TOOLSERVER.replace(
        f"[principals.slot2-data]\nprivileges = [{BASE}]",
        f'[principals.slot2-data]\nprivileges = [{BASE}]\nspaces = ["beta-space"]',
    ).replace("[principals.alpha-reader]", '[spaces.beta-space]\nowner = "elm"\n\n[principals.alpha-reader]')
    with pytest.raises(fed.spec.LayoutError, match=r"slots\.slot-2\.data_path"):
        load(fed, tmp_path, text)


def test_o83_a_slot_may_not_reach_a_space_the_agent_may_not(fed, tmp_path):
    """O83: the existing rule that only a user declared `agent = false` may be a member of a hidden space holds for a
    slot's users: it is refused at the principal, as for any."""
    text = WITH_TOOLSERVER.replace(
        "[principals.alpha-reader]",
        '[spaces.hidden-space]\nowner = "elm"\nagent_visible = false\n\n[principals.alpha-reader]',
    )
    text = text.replace(
        f"[principals.slot1-data]\nprivileges = [{BASE}]",
        f'[principals.slot1-data]\nprivileges = [{BASE}]\nspaces = ["hidden-space"]',
    )
    with pytest.raises(fed.spec.LayoutError, match=r"principals\.slot1-data\.spaces"):
        load(fed, tmp_path, text)


TOOL_REFUSALS = {
    "a command that is not a Python": (
        ('command = "@study_artifacts@/venv/bin/python"', 'command = "@study_artifacts@/bin/server"'),
        "toolserver.command",
    ),
    "a command outside the artifacts": (
        ('command = "@study_artifacts@/venv/bin/python"', 'command = "/usr/bin/python3"'),
        "toolserver.command",
    ),
    "a command with a step above": (
        ('command = "@study_artifacts@/venv/bin/python"', 'command = "@study_artifacts@/../bin/python"'),
        "toolserver.command",
    ),
    "arguments that are not -m <module>": (
        ('args = ["-m", "onedata_mcp"]', 'args = ["onedata_mcp"]'),
        "toolserver.args",
    ),
    "a module that is an option": (('args = ["-m", "onedata_mcp"]', 'args = ["-m", "-c"]'), "toolserver.args"),
    "an argument that names the placeholder": (
        ('args = ["-m", "onedata_mcp"]', 'args = ["-m", "onedata_mcp", "@study_artifacts@/x"]'),
        "toolserver.args",
    ),
    "a clone outside the artifacts": (
        ('clone = "@study_artifacts@/onedata-mcp"', 'clone = "/srv/onedata-mcp"'),
        "toolserver.clone",
    ),
    "a commit that is not forty hex digits": ((f'commit = "{COMMIT}"', 'commit = "main"'), "toolserver.commit"),
    "no commit": ((f'commit = "{COMMIT}"\n', ""), "toolserver.commit"),
    "no clone": (('clone = "@study_artifacts@/onedata-mcp"\n', ""), "toolserver.clone"),
    "a condition no slot's tool-server user holds exactly": (
        ("rights = [" + WRITE + "]\nmanifest", "rights = [" + READ + ', "space_manage_qos"]\nmanifest'),
        "toolserver.conditions.writing.rights",
    ),
    "a condition with unknown rights": (
        ("rights = [" + READ + "]\nmanifest", 'rights = ["space_view"]\nmanifest'),
        "toolserver.conditions.knowledge.rights",
    ),
    "an empty allow-list": (
        ('allow = ["get_file_distribution", "list_space_providers", "get_transfer"]', "allow = []"),
        "toolserver.conditions.knowledge.allow",
    ),
    "a tool twice in an allow-list": (
        (
            'allow = ["get_file_distribution", "list_space_providers", "get_transfer"]',
            'allow = ["get_transfer", "get_transfer"]',
        ),
        "toolserver.conditions.knowledge.allow",
    ),
    "a manifest outside the artifacts": (
        ('manifest = "@study_artifacts@/manifests/knowledge.json"', 'manifest = "manifests/knowledge.json"'),
        "toolserver.conditions.knowledge.manifest",
    ),
    "an unknown key": (
        ("start_timeout_s = 90", "start_timeout_s = 90\ninsecure_tls = true"),
        "toolserver.insecure_tls",
    ),
    "a timeout that is not a number": (
        ("start_timeout_s = 90", 'start_timeout_s = "ninety"'),
        "toolserver.start_timeout_s",
    ),
}


@pytest.mark.parametrize("what", TOOL_REFUSALS)
def test_o83_a_tool_server_the_core_could_not_start_or_that_no_slot_serves_is_refused_at_load(fed, tmp_path, what):
    """O83: the launcher's rule (a Python interpreter started by `-m <module>`), the pinned checkout, and each
    condition's rights against the slots' users are checked when the layout is read, so a federation is never brought
    up whose platform file the core would refuse, or whose conditions no slot could serve."""
    (old, new), key = TOOL_REFUSALS[what]
    assert old in WITH_TOOLSERVER
    with pytest.raises(fed.spec.LayoutError, match=key.replace(".", r"\.")):
        load(fed, tmp_path, WITH_TOOLSERVER.replace(old, new, 1))


def test_o83_a_tool_server_needs_slots_and_at_least_one_condition(fed, tmp_path):
    """O83: its users are the slots', so a layout with a tool server and no slots is refused, and a tool server with
    no condition serves nothing."""
    with pytest.raises(fed.spec.LayoutError, match=r"toolserver"):
        load(fed, tmp_path, MINIMAL + TOOLSERVER)
    head = WITH_TOOLSERVER.split("[toolserver.conditions.knowledge]", 1)[0]
    with pytest.raises(fed.spec.LayoutError, match=r"toolserver\.conditions"):
        load(fed, tmp_path, head)


def test_o83_a_condition_may_share_the_rights_of_another_and_any_order_of_rights_is_the_same_set(fed, tmp_path):
    """O83: two conditions with the same rights share a group of slots (O31), and the rights are a set: written in
    another order they are the slots' rights all the same."""
    text = WITH_TOOLSERVER + (
        '\n[toolserver.conditions.also-reading]\nallow = ["get_transfer"]\n'
        'rights = ["space_view_qos", "space_read_data", "space_view", "space_view_transfers"]\n'
        'manifest = "@study_artifacts@/manifests/also-reading.json"\n'
    )
    spec = load(fed, tmp_path, text)
    assert spec.toolserver.conditions[2].rights == spec.toolserver.conditions[0].rights
