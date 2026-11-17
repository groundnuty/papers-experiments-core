"""What a platform file gets for a layout's slots and tool server (O83, deploy/compose-federation): the `[slots]` and
`[toolserver]` tables written from the layout's declarations, the frozen manifests copied into the root, and every
refusal before a federation is brought up or a platform file is called ready. The platform layer loads what is written
here, as a campaign does."""

import json
import os
import stat
import tomllib

import pytest
from federation_support import MINIMAL
from slots_support import (
    COMMIT,
    READ_TOOLS,
    WITH_SLOTS,
    WITH_TOOLSERVER,
    WRITE_TOOLS,
    artifacts,
    frozen,
    ids_of,
    layout_with_template,
    make_root,
)

from expcore_onedata.config import load_platform
from expcore_onedata.users import load_slots


def staged(fed, tmp_path, text=WITH_TOOLSERVER, *, credentials=True):
    """A layout of `text` with its platform template, a root holding the users' credential files, and a bootstrap that
    holds the ids Onezone would have given."""
    spec = fed.spec.load_spec(layout_with_template(text, tmp_path))
    layout = fed.layout.Layout(tmp_path / "root", spec=spec)
    secrets = make_root(layout, passwords=credentials)
    boot = fed.boot.Bootstrap.__new__(fed.boot.Bootstrap)
    ids = ids_of(spec)
    boot.layout, boot.ids = layout, fed.boot.Ids(admin="uid-admin", **ids)
    return layout, boot, secrets


def test_o83_the_platform_file_declares_the_slots_and_the_tool_server_the_layout_declares_and_the_core_loads_it(
    fed, tmp_path
):
    """O83: `[slots]` holds each slot's two users with their ids from the facts, the tool-server user's rights its
    declared privileges; `[toolserver]` the command under the study's artifacts, the pinned clone and each condition
    with its rights and the root's copy of its manifest; and the platform layer loads the file and its slots."""
    layout, boot, _ = staged(fed, tmp_path)
    study = artifacts(tmp_path / "artifacts")
    assert fed.facts.write_facts(layout, boot, str(study)) == "facts.json and platform.toml written"
    text = layout.platform.read_text(encoding="utf-8")
    document = tomllib.loads(text)
    assert document["slots"] == {
        "slot-1": {
            "data_path": {"username": "slot1-data", "user_id": "uid-slot1-data"},
            "toolserver": {
                "username": "slot1-tools",
                "user_id": "uid-slot1-tools",
                "rights": sorted(layout.spec.principal("slot1-tools").privileges),
            },
        },
        "slot-2": {
            "data_path": {"username": "slot2-data", "user_id": "uid-slot2-data"},
            "toolserver": {
                "username": "slot2-tools",
                "user_id": "uid-slot2-tools",
                "rights": sorted(layout.spec.principal("slot2-tools").privileges),
            },
        },
    }
    tool = document["toolserver"]
    assert (tool["command"], tool["args"], tool["clone"], tool["commit"]) == (
        f"{study}/venv/bin/python",
        ["-m", "onedata_mcp"],
        f"{study}/onedata-mcp",
        COMMIT,
    )
    assert tool["start_timeout_s"] == 90
    assert tool["conditions"]["knowledge"]["manifest"] == "toolserver/manifests/knowledge.json"
    assert tool["conditions"]["writing"]["allow"] == WRITE_TOOLS
    config = load_platform(layout.platform)
    slots = load_slots(layout.platform, config)
    assert [slot.name for slot in slots.slots] == ["slot-1", "slot-2"]
    assert [c.rights for c in config.toolserver.conditions.values()] == [slot.rights for slot in slots.slots]
    assert config.toolserver.commit == COMMIT
    assert config.toolserver.clone == (study / "onedata-mcp").resolve()
    assert slots.group_for(config.condition("writing").rights) == slots.group_for(slots.slots[1].rights)


def test_o83_the_frozen_manifests_are_copied_byte_for_byte_into_the_root_beside_the_platform_file(fed, tmp_path):
    """O83: each condition's manifest is the root's own copy (0600, in a 0700 directory), identical to the study's, so a
    platform file copied beside a study reads files of the root and a later edit of the study's file changes nothing
    until the next up."""
    layout, boot, _ = staged(fed, tmp_path)
    study = artifacts(tmp_path / "artifacts")
    fed.facts.write_facts(layout, boot, str(study))
    directory = layout.root / "toolserver" / "manifests"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("knowledge", "writing"):
        copy = directory / f"{name}.json"
        assert copy.read_bytes() == (study / "manifests" / f"{name}.json").read_bytes()
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    (study / "manifests" / "knowledge.json").write_text(frozen(["other_tool"]), encoding="utf-8")
    assert json.loads((directory / "knowledge.json").read_text(encoding="utf-8"))["tools"][0]["name"] == READ_TOOLS[0]
    assert not [p for p in directory.iterdir() if p.name.startswith(".")]  # no part file is left


def test_o83_a_layout_without_slots_renders_the_platform_file_it_always_did(fed, tmp_path):
    """O83: nothing is added for a layout that declares no slot, so every committed layout's platform file and facts
    are what they were, and no root gets a tool-server directory."""
    layout, boot, _ = staged(fed, tmp_path, MINIMAL)
    facts = fed.facts.facts_of(boot)
    assert "slots" not in facts
    fed.facts.write_facts(layout, boot)
    expected = fed.facts.render(
        layout.spec.platform_template.read_text(encoding="utf-8"), fed.facts.placeholders(facts)
    )
    assert layout.platform.read_text(encoding="utf-8") == expected
    assert not layout.toolserver.exists()


def test_o83_slots_without_a_tool_server_render_only_the_slots(fed, tmp_path):
    """O83: a platform whose runs have no tool server still leases a slot's users, so `[slots]` alone is written, and
    the study's artifacts are not needed."""
    layout, boot, _ = staged(fed, tmp_path, WITH_SLOTS)
    fed.facts.write_facts(layout, boot)
    document = tomllib.loads(layout.platform.read_text(encoding="utf-8"))
    assert set(document["slots"]) == {"slot-1", "slot-2"}
    assert "toolserver" not in document
    assert not layout.toolserver.exists()


def test_o83_the_facts_name_each_slots_users_and_hold_no_secret(fed, tmp_path):
    """O83: facts.json pairs the slots with their principals, whose entries hold the ids and privileges; none of the
    root's secrets is in it."""
    layout, boot, secrets = staged(fed, tmp_path)
    fed.facts.write_facts(layout, boot, str(artifacts(tmp_path / "artifacts")))
    facts = json.loads(layout.facts.read_text(encoding="utf-8"))
    assert facts["slots"] == {
        "slot-1": {"data_path": "slot1-data", "toolserver": "slot1-tools"},
        "slot-2": {"data_path": "slot2-data", "toolserver": "slot2-tools"},
    }
    assert set(facts["principals"]) >= {"slot1-data", "slot1-tools", "slot2-data", "slot2-tools"}
    text = layout.facts.read_text(encoding="utf-8")
    assert not [value for value in secrets.values() if value in text]


def test_o83_a_tool_server_with_no_study_artifacts_is_refused_naming_the_placeholder_and_no_path(fed, tmp_path):
    """O83: the layout names `@study_artifacts@`; with no directory given for it nothing is rendered, and the refusal
    says what to give."""
    layout, boot, _ = staged(fed, tmp_path)
    with pytest.raises(RuntimeError, match="@study_artifacts@") as refused:
        fed.facts.write_facts(layout, boot, None)
    assert str(tmp_path) not in str(refused.value)
    assert not layout.platform.exists()


def test_o83_a_template_that_declares_slots_or_a_tool_server_itself_is_refused(fed, tmp_path):
    """O83: they are declared in the layout alone, so they are the same on every platform and never twice."""
    for table in ("[slots.x]", "[toolserver]"):
        directory = tmp_path / table.strip("[]").replace(".", "-")
        directory.mkdir()
        layout, boot, _ = staged(fed, directory)
        template = layout.spec.platform_template
        template.write_text(template.read_text(encoding="utf-8") + f"\n{table}\n", encoding="utf-8")
        with pytest.raises(RuntimeError, match="declares"):
            fed.facts.write_facts(layout, boot, str(artifacts(directory / "artifacts")))


MANIFEST_REFUSALS = {
    "a manifest that is missing": lambda study: (study / "manifests" / "writing.json").unlink(),
    "a manifest that was edited": lambda study: (study / "manifests" / "writing.json").write_text(
        (study / "manifests" / "writing.json").read_text(encoding="utf-8").replace("the get_transfer tool", "edited"),
        encoding="utf-8",
    ),
    "a manifest that is not a manifest": lambda study: (study / "manifests" / "writing.json").write_text(
        "[]", encoding="utf-8"
    ),
    "a manifest showing a tool the allow-list lacks": lambda study: (study / "manifests" / "knowledge.json").write_text(
        frozen([*READ_TOOLS, "schedule_file_replication"]), encoding="utf-8"
    ),
    "a manifest lacking a tool the allow-list names": lambda study: (study / "manifests" / "writing.json").write_text(
        frozen(READ_TOOLS), encoding="utf-8"
    ),
}


@pytest.mark.parametrize("what", MANIFEST_REFUSALS)
def test_o83_a_frozen_manifest_that_is_not_the_conditions_is_refused_before_anything_is_written(fed, tmp_path, what):
    """O83: the client fails a tool server's start on any tool the manifest lacks or any it has beyond it, so a manifest
    that does not show exactly the condition's allow-list, or was edited, is refused when the platform file is made,
    naming the condition by its position and no path."""
    layout, boot, _ = staged(fed, tmp_path)
    study = artifacts(tmp_path / "artifacts")
    MANIFEST_REFUSALS[what](study)
    with pytest.raises(RuntimeError, match=r"conditions\[\d\]") as refused:
        fed.facts.write_facts(layout, boot, str(study))
    assert str(tmp_path) not in str(refused.value)
    assert not layout.platform.exists()


def test_o83_the_artifacts_are_checked_before_a_federation_is_brought_up(fed, tmp_path):
    """O83: the interpreter must be a file one can run, the clone a directory at the pinned commit, and the manifests as
    above; each refusal names what is wrong and no path."""
    spec = fed.spec.load_spec(layout_with_template(WITH_TOOLSERVER, tmp_path))
    tools = fed.tools
    study = artifacts(tmp_path / "artifacts")
    seen = []

    def clone_is_pinned(path, commit):
        seen.append((path, commit))
        return commit

    tools.check_artifacts(spec, str(study), verify=clone_is_pinned)
    assert seen == [(study / "onedata-mcp", COMMIT)]
    (study / "venv" / "bin" / "python").chmod(0o644)
    with pytest.raises(RuntimeError, match="interpreter") as refused:
        tools.check_artifacts(spec, str(study), verify=clone_is_pinned)
    assert str(tmp_path) not in str(refused.value)
    (study / "venv" / "bin" / "python").chmod(0o755)
    (study / "onedata-mcp").rmdir()
    with pytest.raises(RuntimeError, match="clone"):
        tools.check_artifacts(spec, str(study), verify=clone_is_pinned)
    (study / "onedata-mcp").mkdir()

    def clone_is_not_pinned(path, commit):
        from expcore_onedata.pin import PinError

        raise PinError("the tool-server checkout is not at the pinned commit")

    with pytest.raises(RuntimeError, match="pinned commit"):
        tools.check_artifacts(spec, str(study), verify=clone_is_not_pinned)
    with pytest.raises(RuntimeError, match="@study_artifacts@"):
        tools.check_artifacts(spec, None, verify=clone_is_pinned)


def test_o83_a_layout_with_no_tool_server_checks_no_artifacts(fed, tmp_path):
    """O83: nothing to check, and no directory needed."""
    spec = fed.spec.load_spec(layout_with_template(WITH_SLOTS, tmp_path))
    fed.tools.check_artifacts(spec, None)


def test_o83_a_slot_user_who_is_a_member_of_other_spaces_than_the_platform_file_reaches_is_refused(fed, tmp_path):
    """O83: the token of a slot's tool-server user is checked against exactly the plane's spaces, which the platform
    file declares; a layout whose slot users are members of a space the file does not declare for the plane is refused
    when the file is rendered, not when the first run's token is."""
    text = WITH_TOOLSERVER.replace(
        "[principals.alpha-reader]", '[spaces.beta-space]\nowner = "elm"\n\n[principals.alpha-reader]'
    )
    for user in ("slot1-data", "slot1-tools", "slot2-data", "slot2-tools"):
        head = f"[principals.{user}]\nprivileges = ["
        start = text.index(head)
        end = text.index("]\n", start) + 2
        text = text[:end] + 'spaces = ["alpha-space", "beta-space"]\n' + text[end:]
    layout, boot, _ = staged(fed, tmp_path, text)
    with pytest.raises(RuntimeError, match=r"slots\[0\]"):
        fed.facts.write_facts(layout, boot, str(artifacts(tmp_path / "artifacts")))


def test_o83_a_credential_the_slots_user_lacks_refuses_the_rendered_file(fed, tmp_path):
    """O83: the lease pool needs each user's credential file as the fixed principals' (a private file); a root that
    lacks one is refused when the file is rendered."""
    layout, boot, _ = staged(fed, tmp_path)
    layout.password("slot2-tools").unlink()
    with pytest.raises(RuntimeError, match="refused"):
        fed.facts.write_facts(layout, boot, str(artifacts(tmp_path / "artifacts")))
    layout.password("slot2-tools").write_text("pw", encoding="utf-8")
    os.chmod(layout.password("slot2-tools"), 0o644)  # readable by others: refused as a fixed user's would be
    with pytest.raises(RuntimeError, match="refused"):
        fed.facts.write_facts(layout, boot, str(artifacts(tmp_path / "artifacts-2")))


def test_o83_a_compose_deployment_is_told_where_the_artifacts_are_by_one_variable(fed):
    """O83: `EXPCORE_FED_STUDY_ARTIFACTS`, the Compose counterpart of site.env's `HPC_STUDY_ARTIFACTS`: absolute, plain
    characters, no `..`; unset or empty is no directory."""
    fedtools = fed.tools
    assert fedtools.study_artifacts_from({}) is None
    assert fedtools.study_artifacts_from({fedtools.ENVIRONMENT: ""}) is None
    assert fedtools.study_artifacts_from({fedtools.ENVIRONMENT: "/srv/study/artifacts"}) == "/srv/study/artifacts"
    for bad in ("artifacts", "/srv/../etc", '/srv/"x', "/srv/\\x"):
        with pytest.raises(ValueError, match=fedtools.ENVIRONMENT):
            fedtools.study_artifacts_from({fedtools.ENVIRONMENT: bad})


def up_stubbed(fed, monkeypatch, tmp_path, text):
    """Compose's `up` over a stand-in for everything it touches, for a layout of `text`; returns the layout and what was
    called, with the arguments `write_facts` was given."""
    from types import SimpleNamespace

    layout = fed.layout.Layout(tmp_path / "fed", spec=fed.spec.load_spec(layout_with_template(text, tmp_path)))
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
    return layout, calls


def test_o83_compose_up_checks_the_study_artifacts_before_it_touches_anything(fed, monkeypatch, tmp_path):
    """O83: a layout's tool server with no `EXPCORE_FED_STUDY_ARTIFACTS` is refused before the root is prepared or a
    container started; and one given a directory that cannot run it is refused the same way, naming no path."""
    layout, calls = up_stubbed(fed, monkeypatch, tmp_path, WITH_TOOLSERVER)
    monkeypatch.delenv(fed.tools.ENVIRONMENT, raising=False)
    with pytest.raises(RuntimeError, match=fed.tools.ENVIRONMENT):
        fed.cli.up(layout)
    study = artifacts(tmp_path / "artifacts")
    (study / "venv" / "bin" / "python").chmod(0o644)
    monkeypatch.setenv(fed.tools.ENVIRONMENT, str(study))
    with pytest.raises(RuntimeError, match="interpreter") as refused:
        fed.cli.up(layout)
    assert str(tmp_path) not in str(refused.value)
    assert calls == []


def test_o83_compose_up_gives_the_facts_the_study_artifacts_only_when_it_was_given_one(fed, monkeypatch, tmp_path):
    """O83: the directory reaches `write_facts` when the environment names one, and a layout with no tool server and no
    directory writes its facts by the call it always made."""
    layout, calls = up_stubbed(fed, monkeypatch, tmp_path, WITH_TOOLSERVER)
    study = artifacts(tmp_path / "artifacts")
    monkeypatch.setenv(fed.tools.ENVIRONMENT, str(study))
    fed.cli.up(layout)
    assert ("facts", (str(study),)) in calls
    (tmp_path / "plain").mkdir()
    plain, calls = up_stubbed(fed, monkeypatch, tmp_path / "plain", MINIMAL)
    monkeypatch.delenv(fed.tools.ENVIRONMENT, raising=False)
    fed.cli.up(plain)
    assert ("facts", ()) in calls
