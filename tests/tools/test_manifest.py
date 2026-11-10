"""Frozen tool manifests: the file format, the comparison with a live server's tools, and the command line (L13)."""

import json
import sys
from pathlib import Path

import pytest

from expcore.tools.manifest import FrozenManifest, ManifestError, diff, digest, main, mismatches

SERVER = str(Path(__file__).resolve().parents[1] / "support" / "fake_mcp_server.py")


def tool(name, description="d", schema=None):
    return {
        "name": name,
        "description": description,
        "input_schema": schema or {"type": "object"},
        "output_schema": None,
        "annotations": None,
    }


def frozen_of(*tools):
    tools = list(tools)
    return FrozenManifest(tools, digest(tools), "server", "1.0", "2025-11-25")


def test_equal_manifests_have_no_differences():
    assert diff([tool("a"), tool("b")], [tool("a"), tool("b")]) == []


def test_differences_name_the_tool_and_the_field():
    a = [tool("a"), tool("b", "old")]
    b = [tool("b", "new"), tool("c")]
    assert diff(a, b) == ["only in A: a", "only in B: c", "b.description differs"]


def test_a_changed_input_schema_is_a_difference():
    one, two = tool("a", schema={"type": "object", "properties": {"x": {"type": "string"}}}), tool("a")
    assert diff([one], [two]) == ["a.input_schema differs"]


def test_l13_mismatches_name_extra_and_missing_tools():
    """L13: an extra tool and a missing tool are each a mismatch that names the tool."""
    frozen = frozen_of(tool("a"), tool("b"))
    assert mismatches([tool("a"), tool("b")], frozen) == []
    problems = mismatches([tool("a"), tool("x")], frozen)
    assert "extra on the server: x" in problems
    assert "missing from the server: b" in problems


def test_l13_the_allow_list_must_equal_what_the_server_shows():
    """L13: an allow-list name the server lacks, and a tool shown outside the allow-list, both fail."""
    frozen = frozen_of(tool("a"), tool("b"))
    live = [tool("a"), tool("b")]
    assert mismatches(live, frozen, ["a", "b"]) == []
    assert any("lacks: ['c']" in p for p in mismatches(live, frozen, ["a", "b", "c"]))
    assert any("outside the allow-list: ['b']" in p for p in mismatches(live, frozen, ["a"]))


def test_a_frozen_manifest_survives_a_round_trip_through_its_file(tmp_path):
    frozen = frozen_of(tool("a"), tool("b"))
    path = tmp_path / "tools.json"
    path.write_text(frozen.dump(), encoding="utf-8")
    assert FrozenManifest.load(path) == frozen


def test_a_frozen_manifest_edited_by_hand_is_refused(tmp_path):
    path = tmp_path / "tools.json"
    path.write_text(frozen_of(tool("a")).dump(), encoding="utf-8")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["tools"][0]["description"] = "quietly changed"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ManifestError, match="edited"):
        FrozenManifest.load(path)


@pytest.mark.parametrize("text", ["not json", "[]", '{"tools": 3}', '{"tools": [{"description": "no name"}]}'])
def test_a_file_that_is_not_a_frozen_manifest_is_refused(tmp_path, text):
    path = tmp_path / "tools.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ManifestError):
        FrozenManifest.load(path)


def test_a_missing_file_is_refused_with_its_name(tmp_path):
    with pytest.raises(ManifestError, match=r"missing\.json"):
        FrozenManifest.load(tmp_path / "missing.json")


def test_the_command_line_freezes_a_server_and_checks_it_later(tmp_path, capsys):
    out = tmp_path / "tools.json"
    server = [sys.executable, SERVER, "--tools", "echo,add"]
    assert main(["freeze", str(out), "--", *server]) == 0
    assert FrozenManifest.load(out).tools[0]["name"] == "add"
    assert main(["check", str(out), "--", *server]) == 0
    assert "equals the frozen one" in capsys.readouterr().out
    assert main(["check", str(out), "--", sys.executable, SERVER, "--tools", "echo"]) == 1
    assert "missing from the server: add" in capsys.readouterr().err


def test_l13_the_command_line_takes_its_options_after_the_file_as_its_usage_shows(tmp_path, capsys):
    """L13: `freeze OUT --pass-env NAME -- COMMAND`, the documented form, freezes the server; the options are not
    taken for the server's command."""
    out = tmp_path / "tools.json"
    server = [sys.executable, SERVER, "--tools", "echo,add"]
    assert main(["freeze", str(out), "--pass-env", "TOYLAB_HOME", "--timeout", "60", "--", *server]) == 0
    assert [t["name"] for t in FrozenManifest.load(out).tools] == ["add", "echo"]
    assert main(["check", str(out), "--timeout", "60", "--pass-env", "TOYLAB_HOME", "--", *server]) == 0
    assert main(["--pass-env", "TOYLAB_HOME", "check", str(out), "--", *server]) == 0  # before the action too
    assert "No such file" not in capsys.readouterr().err


def test_l13_the_variables_and_the_timeout_reach_the_server_and_the_rest_is_its_command(tmp_path, monkeypatch):
    """L13: everything after the first `--` is the server's, a later `--` included; the options before it are ours."""
    seen = {}

    def freeze(path, params, timeout_s):
        seen.update(path=path, params=params, timeout_s=timeout_s)
        return 0

    monkeypatch.setattr("expcore.tools.manifest._freeze", freeze)
    monkeypatch.setenv("TOYLAB_HOME", "/srv/toylab")
    argv = ["freeze", str(tmp_path / "t.json"), "--pass-env", "TOYLAB_HOME", "--timeout", "7", "--"]
    assert main([*argv, "toy-server", "--flag", "--", "rest"]) == 0
    assert (seen["params"].command, seen["params"].args) == ("toy-server", ["--flag", "--", "rest"])
    assert seen["params"].env == {"TOYLAB_HOME": "/srv/toylab"}
    assert seen["timeout_s"] == 7.0


def test_the_command_line_needs_a_server_command(tmp_path, capsys):
    assert main(["check", str(tmp_path / "t.json")]) == 2
    assert "give the server command" in capsys.readouterr().err
