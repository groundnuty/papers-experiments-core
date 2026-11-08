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
    frozen = frozen_of(tool("a"), tool("b"))
    assert mismatches([tool("a"), tool("b")], frozen) == []
    problems = mismatches([tool("a"), tool("x")], frozen)
    assert "extra on the server: x" in problems
    assert "missing from the server: b" in problems


def test_l13_the_allow_list_must_equal_what_the_server_shows():
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


def test_the_command_line_needs_a_server_command(tmp_path, capsys):
    assert main(["check", str(tmp_path / "t.json")]) == 2
    assert "give the server command" in capsys.readouterr().err
