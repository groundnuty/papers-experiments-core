"""A platform template's slots and tool server (O83, deploy/compose-federation): which users are the slots', what the
study's artifacts must be, and what the rendered file is checked against. The slot users are ordinary principals of the
layout, so the file's `[slots]` is the template's own text, filled with the facts' ids; what the code adds is the
refusal of a template that disagrees with the layout it was provisioned from, before a federation is called ready."""

import json
import os
import tomllib
from types import SimpleNamespace

import pytest
from federation_support import MINIMAL
from slots_support import (
    BASE,
    COMMIT,
    LAYOUT,
    READ,
    READ_TOOLS,
    SLOT_USERS,
    TEMPLATE,
    WITH_SLOTS,
    WITH_TOOLSERVER,
    WRITE,
    artifacts,
    frozen,
    ids_of,
    layout_with_template,
    make_root,
)

from expcore_onedata.config import load_platform
from expcore_onedata.users import load_slots


def staged(fed, tmp_path, template=WITH_TOOLSERVER, layout=LAYOUT, *, credentials=True):
    """A layout of `layout` with `template` beside it, a root holding the users' credential files, and a bootstrap that
    holds the ids Onezone would have given."""
    spec = fed.spec.load_spec(layout_with_template(template, tmp_path, layout))
    root = fed.layout.Layout(tmp_path / "root", spec=spec)
    secrets = make_root(root, passwords=credentials)
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    boot.layout, boot.ids = root, fed.boot.Ids(admin="uid-admin", **ids_of(spec))
    return root, boot, secrets


def test_o83_the_platform_file_holds_the_slots_and_tool_server_the_template_declares_and_the_core_loads_it(
    fed, tmp_path
):
    """O83: `[slots]` is the template's, each user's id the facts' (`@user_id.<name>@`); `[toolserver]` has its paths
    under the study's artifacts; and the platform layer loads the file and its slots."""
    layout, boot, _ = staged(fed, tmp_path)
    study = artifacts(tmp_path / "artifacts")
    assert fed.facts.write_facts(layout, boot, str(study)) == "facts.json and platform.toml written"
    document = tomllib.loads(layout.platform.read_text(encoding="utf-8"))
    assert document["slots"]["slot-1"]["data_path"] == {"username": "slot1-data", "user_id": "uid-slot1-data"}
    assert document["slots"]["slot-2"]["toolserver"] == {
        "username": "slot2-tools",
        "user_id": "uid-slot2-tools",
        "rights": json.loads(f"[{WRITE}]"),
    }
    tool = document["toolserver"]
    assert (tool["command"], tool["args"], tool["clone"], tool["commit"]) == (
        f"{study}/venv/bin/python",
        ["-m", "onedata_mcp"],
        f"{study}/onedata-mcp",
        COMMIT,
    )
    assert tool["conditions"]["writing"]["manifest"] == f"{study}/manifests/writing.json"
    config = load_platform(layout.platform)
    slots = load_slots(layout.platform, config)
    assert [slot.name for slot in slots.slots] == ["slot-1", "slot-2"]
    assert [c.rights for c in config.toolserver.conditions.values()] == [slot.rights for slot in slots.slots]
    assert config.toolserver.conditions["knowledge"].manifest == (study / "manifests" / "knowledge.json").resolve()
    assert slots.group_for(config.condition("writing").rights) == slots.group_for(slots.slots[1].rights)


def test_o83_the_slot_users_are_read_from_the_template_before_it_is_rendered(fed, tmp_path):
    """O83: provisioning needs them first, to know which users it may never change; the template as it stands gives each
    with its plane, and a template with no `[slots]` gives none."""
    spec = fed.spec.load_spec(layout_with_template(WITH_TOOLSERVER, tmp_path / "a"))
    found = fed.tools.slot_users(spec)
    assert found == {
        "slot1-data": "data_path",
        "slot1-tools": "toolserver",
        "slot2-data": "data_path",
        "slot2-tools": "toolserver",
    }
    assert fed.tools.slot_users(fed.spec.load_spec(layout_with_template(TEMPLATE, tmp_path / "b"))) == {}
    assert fed.tools.slot_users(fed.spec.load_spec(layout_with_template(WITH_SLOTS, tmp_path / "c"))) == found


def test_o83_a_layout_without_slots_renders_the_platform_file_it_always_did(fed, tmp_path):
    """O83: a template with no `[slots]` is rendered as ever, with no placeholder added and no check made."""
    layout, boot, _ = staged(fed, tmp_path, TEMPLATE, MINIMAL)
    facts = fed.facts.facts_of(boot)
    assert "slot_users" not in facts
    fed.facts.write_facts(layout, boot)
    template = layout.spec.platform_template.read_text(encoding="utf-8")
    assert layout.platform.read_text(encoding="utf-8") == fed.facts.render(template, fed.facts.placeholders(facts))


def test_o83_slots_without_a_tool_server_are_checked_and_need_no_study_directory(fed, tmp_path):
    """O83: a platform whose runs have no tool server still leases a slot's users: its `[slots]` is checked as the other
    is, and no directory is asked for."""
    layout, boot, _ = staged(fed, tmp_path, WITH_SLOTS)
    fed.facts.write_facts(layout, boot)
    assert set(tomllib.loads(layout.platform.read_text(encoding="utf-8"))["slots"]) == {"slot-1", "slot-2"}


def test_o83_the_facts_record_what_provisioning_found_of_each_slot_user_and_hold_no_secret(fed, tmp_path):
    """O83: facts.json names, per slot user, `created` or `matched`; none of the root's secrets is in it."""
    layout, boot, secrets = staged(fed, tmp_path)
    boot.slot_status.update(dict.fromkeys(SLOT_USERS, "matched"))
    fed.facts.write_facts(layout, boot, str(artifacts(tmp_path / "artifacts")))
    text = layout.facts.read_text(encoding="utf-8")
    assert json.loads(text)["slot_users"] == dict.fromkeys(SLOT_USERS, "matched")
    assert not [value for value in secrets.values() if value in text]


def test_o83_a_template_naming_the_study_artifacts_with_none_given_is_refused_naming_both_settings(fed, tmp_path):
    """O83: `@study_artifacts@` with no directory is refused when the file is rendered, and before it, by the
    preflight, saying where each platform takes it from, and no path."""
    layout, boot, _ = staged(fed, tmp_path)
    with pytest.raises(RuntimeError, match="@study_artifacts@"):
        fed.facts.write_facts(layout, boot, None)
    assert not layout.platform.exists()
    with pytest.raises(RuntimeError, match=f"{fed.tools.ENVIRONMENT}.*HPC_STUDY_ARTIFACTS") as refused:
        fed.tools.check_artifacts(layout.spec, None)
    assert str(tmp_path) not in str(refused.value)


def swapped(old: str, new: str) -> str:
    assert old in WITH_TOOLSERVER, old
    return WITH_TOOLSERVER.replace(old, new, 1)


def more_rights(principal: str, rights: str, extra: str) -> str:
    return LAYOUT.replace(
        f"[principals.{principal}]\nprivileges = [{rights}]",
        f"[principals.{principal}]\nprivileges = [{rights}{extra}]",
    )


RENDER_REFUSALS = {
    "a slot user that is no principal of the layout": (
        swapped('username = "slot1-data"', 'username = "nobody"'),
        LAYOUT,
        r"slots\[0\]: its data_path user is no principal",
    ),
    "a typed id that is not the facts'": (
        swapped('user_id = "@user_id.slot1-tools@"', 'user_id = "u-typed-by-hand"'),
        LAYOUT,
        r"slots\[0\]: the id of its toolserver user",
    ),
    "rights typed in the template that are not the principal's privileges": (
        swapped(f"rights = [{READ}]\n\n[slots.slot-2", f'rights = [{READ}, "space_manage_qos"]\n\n[slots.slot-2'),
        LAYOUT,
        r"slots\[0\]: its tool-server rights are not exactly the privileges",
    ),
    "a principal with more rights than its template declares": (
        WITH_TOOLSERVER,
        more_rights("slot2-tools", WRITE, ', "space_manage_qos"'),
        r"slots\[1\]: its tool-server rights are not exactly the privileges",
    ),
    "a data-path user with more than the baseline": (
        WITH_TOOLSERVER,
        LAYOUT.replace(
            f"[principals.slot1-data]\nprivileges = [{BASE}]", f"[principals.slot1-data]\nprivileges = [{READ}]"
        ),
        r"slots\[0\]: its data-path user holds more or less than the baseline",
    ),
    "a slot user the agent may not use": (
        WITH_TOOLSERVER,
        LAYOUT.replace(
            f"[principals.slot2-data]\nprivileges = [{BASE}]",
            f"[principals.slot2-data]\nprivileges = [{BASE}]\nagent = false",
        ),
        r"slots\[1\]: its data_path user is declared agent = false",
    ),
    "a condition whose rights no slot's tool-server user holds": (
        swapped(f"rights = [{WRITE}]\nmanifest", f'rights = [{WRITE}, "space_view_statistics"]\nmanifest'),
        LAYOUT,
        r"conditions\[1\]: no slot's tool-server user holds its rights",
    ),
}


@pytest.mark.parametrize("what", RENDER_REFUSALS)
def test_o83_a_template_that_disagrees_with_the_layout_it_was_provisioned_from_is_refused_when_rendered(
    fed, tmp_path, what
):
    """O83: the rights in the file are the privileges the bootstrap gave, from the one declaration: a slot user that
    is no principal, an id typed by hand, rights typed in the template that are not the principal's privileges (or the
    reverse), a data-path user with more than the baseline, a user the agent may not use, and a condition no slot's user
    could serve are each refused, naming the slot or the condition by its position, never a name."""
    template, layout_text, message = RENDER_REFUSALS[what]
    layout, boot, _ = staged(fed, tmp_path, template, layout_text)
    if "nobody" in template:
        (layout.users / "nobody.pass").write_text("pw-nobody", encoding="utf-8")
        (layout.users / "nobody.pass").chmod(0o600)
    with pytest.raises(RuntimeError, match=message) as refused:
        fed.facts.write_facts(layout, boot, str(artifacts(tmp_path / "artifacts")))
    assert not [u for u in (*SLOT_USERS, "nobody") if f"{u}:" in str(refused.value)]


def test_o83_a_slot_user_who_is_a_member_of_other_spaces_than_the_platform_file_reaches_is_refused(fed, tmp_path):
    """O83: the token of a slot's tool-server user is checked against exactly the plane's spaces, which the platform
    file declares; a layout whose slot users are members of a space the file does not declare for the plane is refused
    when the file is rendered, not when the first run's token is."""
    text = LAYOUT.replace(
        "[principals.alpha-reader]", '[spaces.beta-space]\nowner = "elm"\n\n[principals.alpha-reader]'
    )
    for user in SLOT_USERS:
        head = f"[principals.{user}]\nprivileges = ["
        end = text.index("]\n", text.index(head)) + 2
        text = text[:end] + 'spaces = ["alpha-space", "beta-space"]\n' + text[end:]
    layout, boot, _ = staged(fed, tmp_path, WITH_TOOLSERVER, text)
    with pytest.raises(RuntimeError, match=r"slots\[0\]: its data_path user is not a member of exactly"):
        fed.facts.write_facts(layout, boot, str(artifacts(tmp_path / "artifacts")))


def test_o83_a_credential_a_slot_user_lacks_refuses_the_rendered_file_and_names_no_user(fed, tmp_path):
    """O83: the lease pool needs each user's credential file as the fixed principals' (a private file); a root that
    lacks one, or whose file others can read, is refused when the file is rendered."""
    layout, boot, secrets = staged(fed, tmp_path)
    study = str(artifacts(tmp_path / "artifacts"))
    layout.password("slot2-tools").unlink()
    with pytest.raises(RuntimeError, match="refused") as refused:
        fed.facts.write_facts(layout, boot, study)
    layout.password("slot2-tools").write_text("pw", encoding="utf-8")
    os.chmod(layout.password("slot2-tools"), 0o644)
    with pytest.raises(RuntimeError, match="refused") as open_one:
        fed.facts.write_facts(layout, boot, study)
    shown = str(refused.value) + str(open_one.value)
    assert not [u for u in SLOT_USERS if u in shown]
    assert not [v for v in secrets.values() if v in shown]


ARTIFACT_REFUSALS = {
    "a command that is not an executable file": (lambda s: (s / "venv" / "bin" / "python").chmod(0o644), "command"),
    "a command that is missing": (lambda s: (s / "venv" / "bin" / "python").unlink(), "command"),
    "a clone that is not a directory": (lambda s: (s / "onedata-mcp").rmdir(), "clone"),
    "a manifest that is missing": (lambda s: (s / "manifests" / "writing.json").unlink(), r"conditions\[1\]"),
    "a manifest that was edited": (
        lambda s: (s / "manifests" / "writing.json").write_text(
            (s / "manifests" / "writing.json").read_text(encoding="utf-8").replace("the get_transfer tool", "edited"),
            encoding="utf-8",
        ),
        r"conditions\[1\]",
    ),
    "a manifest that is not a manifest": (
        lambda s: (s / "manifests" / "knowledge.json").write_text("[]", encoding="utf-8"),
        r"conditions\[0\]",
    ),
    "a manifest showing a tool the allow-list lacks": (
        lambda s: (s / "manifests" / "knowledge.json").write_text(
            frozen([*READ_TOOLS, "schedule_file_replication"]), encoding="utf-8"
        ),
        r"conditions\[0\].*shows 1 tool\(s\) its allow-list lacks",
    ),
    "a manifest lacking a tool the allow-list names": (
        lambda s: (s / "manifests" / "writing.json").write_text(frozen(READ_TOOLS), encoding="utf-8"),
        r"conditions\[1\].*lacks 1",
    ),
}


@pytest.mark.parametrize("what", ARTIFACT_REFUSALS)
def test_o83_artifacts_the_tool_server_cannot_run_from_are_refused_before_anything_is_brought_up(fed, tmp_path, what):
    """O83: the command must be an executable file, the clone a directory, and each frozen manifest one, unedited,
    showing exactly its condition's allow-list (the client fails a server's start on any other); each refusal names what
    is wrong, a condition by its position, and no path."""
    spec = fed.spec.load_spec(layout_with_template(WITH_TOOLSERVER, tmp_path))
    study = artifacts(tmp_path / "artifacts")

    def pinned(clone, commit):
        return commit

    fed.tools.check_artifacts(spec, str(study), verify=pinned)
    damage, message = ARTIFACT_REFUSALS[what]
    damage(study)
    with pytest.raises(RuntimeError, match=message) as refused:
        fed.tools.check_artifacts(spec, str(study), verify=pinned)
    assert str(tmp_path) not in str(refused.value)


def test_o83_the_clone_must_be_at_the_pinned_commit_the_template_names(fed, tmp_path):
    """O83: the launcher's own check (`expcore_onedata.pin`), made before a job is held: the clone and the commit the
    template names are handed to it, and its refusal is the refusal."""
    from expcore_onedata.pin import PinError

    spec = fed.spec.load_spec(layout_with_template(WITH_TOOLSERVER, tmp_path))
    study = artifacts(tmp_path / "artifacts")
    seen = []
    fed.tools.check_artifacts(spec, str(study), verify=lambda clone, commit: seen.append((clone, commit)) or commit)
    assert seen == [(study / "onedata-mcp", COMMIT)]

    def not_pinned(clone, commit):
        raise PinError("the tool-server checkout is not at the pinned commit")

    with pytest.raises(RuntimeError, match="pinned commit"):
        fed.tools.check_artifacts(spec, str(study), verify=not_pinned)


def test_o83_a_template_without_a_tool_server_checks_no_artifacts(fed, tmp_path):
    """O83: nothing to check, and no directory needed."""
    fed.tools.check_artifacts(fed.spec.load_spec(layout_with_template(WITH_SLOTS, tmp_path)), None)


def test_o83_a_compose_deployment_is_told_where_the_artifacts_are_by_one_variable(fed):
    """O83: `EXPCORE_FED_STUDY_ARTIFACTS`, the Compose counterpart of site.env's `HPC_STUDY_ARTIFACTS`: absolute, plain
    characters, no `..`; unset or empty is no directory."""
    tools = fed.tools
    assert tools.study_artifacts_from({}) is None
    assert tools.study_artifacts_from({tools.ENVIRONMENT: ""}) is None
    assert tools.study_artifacts_from({tools.ENVIRONMENT: "/srv/study/artifacts"}) == "/srv/study/artifacts"
    for bad in ("artifacts", "/srv/../etc", '/srv/"x', "/srv/\\x"):
        with pytest.raises(ValueError, match=tools.ENVIRONMENT):
            tools.study_artifacts_from({tools.ENVIRONMENT: bad})


def up_stubbed(fed, monkeypatch, tmp_path, template, layout=LAYOUT):
    """Compose's `up` over a stand-in for everything it touches; returns the layout and what was called, with the
    arguments `write_facts` was given."""
    spec = fed.spec.load_spec(layout_with_template(template, tmp_path, layout))
    root = fed.layout.Layout(tmp_path / "fed", spec=spec)
    calls: list = []
    monkeypatch.setattr(fed.cli, "require_images", lambda: None)
    monkeypatch.setattr(fed.cli, "project_containers", lambda project: [])
    monkeypatch.setattr(fed.cli, "project_networks", lambda project: [])
    for name in ("require_own", "require_ports_free", "require_memory_fits", "prepare", "bring_up"):
        monkeypatch.setattr(fed.cli, name, lambda *arguments, name=name: calls.append(name))
    monkeypatch.setattr(fed.cli, "Rest", lambda ca: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(fed.cli, "Bootstrap", lambda rest, layout: SimpleNamespace(ids=SimpleNamespace(spaces={})))
    monkeypatch.setattr(fed.cli, "Content", lambda boot: SimpleNamespace(reset=lambda timeout: None))
    monkeypatch.setattr(fed.cli, "write_facts", lambda layout, boot, *given: calls.append(("facts", given)) or "ok")
    monkeypatch.setattr(fed.cli, "check_after_up", lambda layout, ids: "links")
    monkeypatch.setattr("expcore_onedata.pin.verify_pin", lambda clone, commit: commit)
    return root, calls


def test_o83_compose_up_checks_the_study_artifacts_before_it_touches_anything(fed, monkeypatch, tmp_path):
    """O83: a template naming `@study_artifacts@` with no `EXPCORE_FED_STUDY_ARTIFACTS` is refused before the root is
    prepared or a container started; and one given a directory that cannot run the server is refused the same way,
    naming no path."""
    layout, calls = up_stubbed(fed, monkeypatch, tmp_path, WITH_TOOLSERVER)
    monkeypatch.delenv(fed.tools.ENVIRONMENT, raising=False)
    with pytest.raises(RuntimeError, match=fed.tools.ENVIRONMENT):
        fed.cli.up(layout)
    study = artifacts(tmp_path / "artifacts")
    (study / "venv" / "bin" / "python").chmod(0o644)
    monkeypatch.setenv(fed.tools.ENVIRONMENT, str(study))
    with pytest.raises(RuntimeError, match="command") as refused:
        fed.cli.up(layout)
    assert str(tmp_path) not in str(refused.value)
    assert not {"require_ports_free", "require_memory_fits", "prepare", "bring_up"} & set(calls)


def test_o83_compose_up_gives_the_facts_the_study_artifacts_only_when_it_was_given_one(fed, monkeypatch, tmp_path):
    """O83: the directory reaches `write_facts` when the environment names one, and a layout with no tool server and no
    directory writes its facts by the call it always made."""
    layout, calls = up_stubbed(fed, monkeypatch, tmp_path, WITH_TOOLSERVER)
    study = artifacts(tmp_path / "artifacts")
    monkeypatch.setenv(fed.tools.ENVIRONMENT, str(study))
    fed.cli.up(layout)
    assert ("facts", (str(study),)) in calls
    (tmp_path / "plain").mkdir()
    plain, calls = up_stubbed(fed, monkeypatch, tmp_path / "plain", TEMPLATE, MINIMAL)
    monkeypatch.delenv(fed.tools.ENVIRONMENT, raising=False)
    fed.cli.up(plain)
    assert ("facts", ()) in calls


RELATIVE = (
    TEMPLATE
    + f"""
[toolserver]
command = "/bin/sh"
args = ["-m", "onedata_mcp"]
clone = "clone"
commit = "{COMMIT}"

[toolserver.conditions.knowledge]
allow = {json.dumps(READ_TOOLS)}
rights = [{READ}]
manifest = "manifests/knowledge.json"
"""
)


def test_o83_a_relative_clone_and_manifest_are_read_from_the_root_where_the_platform_file_lies(
    fed, tmp_path, monkeypatch
):
    """O83: the platform layer reads a relative clone and manifest from the platform file's directory, which is the
    root, so the preflight reads them there, whatever directory `up` is run from; a root without them is refused."""
    spec = fed.spec.load_spec(layout_with_template(RELATIVE, tmp_path))
    root = tmp_path / "root"
    (root / "clone").mkdir(parents=True)
    (root / "manifests").mkdir()
    (root / "manifests" / "knowledge.json").write_text(frozen(READ_TOOLS), encoding="utf-8")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")

    def pinned(clone, commit):
        assert clone == root / "clone"
        return commit

    fed.tools.check_artifacts(spec, None, root, verify=pinned)
    (root / "manifests" / "knowledge.json").unlink()
    with pytest.raises(RuntimeError, match=r"conditions\[0\]"):
        fed.tools.check_artifacts(spec, None, root, verify=pinned)
    (root / "clone").rmdir()
    with pytest.raises(RuntimeError, match="clone"):
        fed.tools.check_artifacts(spec, None, root, verify=pinned)
