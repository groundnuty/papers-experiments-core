"""The layout of the storage-import slot (deploy/compose-federation/layouts/storage-import.toml, #184), its platform
template, and the script that makes a dataset to import (make_import_tree.py)."""

import ast
import hashlib
import os
import sys
import tomllib
from pathlib import Path

import pytest
from federation_support import DEPLOY, STORAGE_IMPORT

TEMPLATE = DEPLOY / "layouts" / "storage-import.platform.toml.template"


def test_the_slot_has_its_own_project_domain_and_ports_beside_the_other_layouts(fed):
    spec = fed.spec.load_spec(STORAGE_IMPORT)
    assert (spec.project, spec.domain) == ("expcore-fedimp", "expcore-fedimp.test")
    assert [(s.name, s.port) for s in spec.services] == [
        ("zone", 29643),
        ("pine", 29644),
        ("birch", 29645),
        ("cedar", 29646),
    ]
    others = [fed.spec.load_spec(path) for path in (None, DEPLOY / "layouts" / "five-providers.toml")]
    assert all(spec.project != other.project and spec.domain != other.domain for other in others)
    assert not {s.port for s in spec.services} & {s.port for other in others for s in other.services}


def test_the_slot_declares_an_imported_read_only_space_and_an_uploaded_one_with_the_same_owner_and_mirrors(fed):
    spec = fed.spec.load_spec(STORAGE_IMPORT)
    imported, uploaded = spec.space("alpha-import"), spec.space("alpha-upload")
    assert imported.imported == fed.spec.Import(readonly=True, source_env="EXPCORE_FED_IMPORT_ALPHA")
    assert uploaded.imported is None
    for space in (imported, uploaded):
        assert (space.owner, space.mirrors, space.baseline) == ("pine", ("birch", "cedar"), False)
    assert spec.study_space == "alpha-upload"
    assert spec.holder == "pine"


def test_the_slots_three_users_read_both_spaces_and_nothing_else(fed):
    spec = fed.spec.load_spec(STORAGE_IMPORT)
    assert [(p.name, p.privileges, p.spaces, p.agent) for p in spec.principals] == [
        (user, ("space_view", "space_read_data"), ("alpha-import", "alpha-upload"), True)
        for user in ("alpha-reader", "alpha-tools", "alpha-health")
    ]


def test_the_slots_layout_holds_no_path_and_names_the_variable_only(fed):
    text = STORAGE_IMPORT.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert 'source_env = "EXPCORE_FED_IMPORT_ALPHA"' in code
    assert "/" not in code.replace("deploy/", "")  # no path of any kind outside the comments


def test_only_the_owner_of_the_slot_binds_the_dataset_and_only_read_only(fed):
    model = fed.compose.model(fed.spec.load_spec(STORAGE_IMPORT))["services"]
    binds = {name: [v for v in s["volumes"] if v["target"].startswith("/volumes/import/")] for name, s in model.items()}
    assert binds["pine"] == [
        {
            "type": "bind",
            "source": "${EXPCORE_FED_IMPORT_ALPHA:?set by federation.py}",
            "target": "/volumes/import/alpha-import",
            "read_only": True,
            "bind": {"create_host_path": False},
        }
    ]
    assert not [name for name, found in binds.items() if found and name != "pine"]


# -- the platform template --------------------------------------------------------------------------------------------


def rendered(fed, tmp_path) -> str:
    """The template filled from facts, as up fills it, over made-up ids."""
    import fedboot

    spec = fed.spec.load_spec(STORAGE_IMPORT)
    layout = fed.layout.Layout(tmp_path, spec=spec)
    boot = fedboot.Bootstrap.__new__(fedboot.Bootstrap)
    boot.layout = layout
    boot.ids = fedboot.Ids(
        users={p.name: f"user-{p.name}" for p in spec.principals},
        providers={p.name: f"provider-{p.name}" for p in spec.providers},
        spaces={s.name: f"space-{s.name}" for s in spec.spaces},
    )
    facts = fed.facts.facts_of(boot)
    return fed.facts.render(TEMPLATE.read_text(encoding="utf-8"), fed.facts.placeholders(facts))


def test_the_template_fills_every_placeholder_and_is_a_toml_file_with_two_spaces(fed, tmp_path):
    text = rendered(fed, tmp_path)
    body = "\n".join(line for line in text.splitlines() if not line.startswith("#"))
    assert fed.facts.PLACEHOLDER.search(body) is None
    spaces = tomllib.loads(text)["spaces"]
    assert [(s["label"], s["name"], s["id"]) for s in spaces] == [
        ("imported", "alpha-import", "space-alpha-import"),
        ("uploaded", "alpha-upload", "space-alpha-upload"),
    ]
    for entry in spaces:
        assert (entry["prefixes"], entry["providers"], entry["local"], entry["holder"]) == (
            ["alpha-"],
            ["pine", "birch", "cedar"],
            "pine",
            "pine",
        )


def test_the_template_marks_the_imported_entry_read_only_as_the_layout_declares_it(fed, tmp_path):
    spec = fed.spec.load_spec(STORAGE_IMPORT)
    spaces = {s["name"]: s for s in tomllib.loads(rendered(fed, tmp_path))["spaces"]}
    assert spaces["alpha-import"]["import"] == {"readonly": spec.space("alpha-import").imported.readonly}
    assert "import" not in spaces["alpha-upload"]


def test_the_templates_ids_and_users_are_those_the_layout_declares(fed, tmp_path):
    spec = fed.spec.load_spec(STORAGE_IMPORT)
    data = tomllib.loads(rendered(fed, tmp_path))
    assert set(data["providers"]) == {p.name for p in spec.providers}
    assert {p["username"] for p in data["principals"].values()} == {p.name for p in spec.principals}
    assert {"space", "support"}.isdisjoint(data)  # the one-space form is never beside [[spaces]]


def test_o41_the_template_is_a_platform_file_the_core_loads_with_its_placement_as_the_live_tests_read_it(fed, tmp_path):
    """O41: the rendered template loads in the platform layer as tests/live/test_storage_import.py loads it: the two
    spaces by their labels, the imported one read-only, the other regular, and a `[placement]` table whose data
    provider is the owner, which a provisioner needs."""
    from expcore_onedata.config import load_platform
    from expcore_onedata.rangemap import load_placement
    from expcore_onedata.spaces import ImportedStorage

    platform = tmp_path / "platform.toml"
    platform.write_text(rendered(fed, tmp_path), encoding="utf-8")
    (tmp_path / "ca").mkdir()
    fed.pki.make_ca(tmp_path / "ca" / "federation-ca.pem", tmp_path / "ca.key", "test CA")
    config = load_platform(platform, require_secrets=False)
    assert config.space_named("imported").imported == ImportedStorage(readonly=True)
    assert config.space_named("uploaded").imported is None
    assert load_placement(platform, config).settings.holder == "pine"


def test_the_template_names_no_variable_and_no_path(fed):
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "EXPCORE_FED_IMPORT" not in text
    for host_path in ("/srv", "/mnt", "/home"):
        assert host_path not in text


# -- the script that makes a dataset to import ------------------------------------------------------------------------


def tree_of(root: Path) -> dict[str, tuple[str, int]]:
    """Every file below `root` by its relative path: the SHA-256 of its bytes and its mode."""
    return {
        str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode & 0o777)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def make(fed, out: Path, *args: str) -> int:
    return fed.tree.main([str(out), *args])


def test_the_script_imports_nothing_of_the_core_or_the_deploy_scripts(fed):
    """It runs on its own, on any machine that has Python, to make the dataset a layout's variable names."""
    source = ast.parse(Path(fed.tree.__file__).read_text(encoding="utf-8"))
    imported = {
        (node.module if isinstance(node, ast.ImportFrom) else alias.name).split(".")[0]
        for node in ast.walk(source)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (node.names if isinstance(node, ast.Import) else [None])
    }
    assert imported <= set(sys.stdlib_module_names)


def test_the_stream_is_the_sha256_counter_stream_of_the_baseline_files_whatever_the_piece_size(fed):
    wanted = fed.layout.BaselineFile("d00/file-000000.bin", 300).content()
    for piece in (1, 7, 32, 100, 300, 1 << 20):
        assert b"".join(fed.tree.stream("d00/file-000000.bin", 300, piece)) == wanted


def test_the_stream_never_holds_more_than_a_piece(fed):
    pieces = list(fed.tree.stream("large.bin", 1000, 64))
    assert [len(p) for p in pieces] == [64] * 15 + [40]


def test_two_runs_make_the_same_tree_with_the_same_modes(fed, tmp_path, capsys):
    assert make(fed, tmp_path / "one", "--small", "25", "--dirs", "4", "--large-bytes", "500") == 0
    assert make(fed, tmp_path / "two", "--small", "25", "--dirs", "4", "--large-bytes", "500") == 0
    first = tree_of(tmp_path / "one")
    assert first == tree_of(tmp_path / "two")
    assert len(first) == 26


def test_the_files_are_spread_over_numbered_directories_and_named_in_order(fed, tmp_path):
    assert make(fed, tmp_path / "out", "--small", "7", "--dirs", "3") == 0
    names = sorted(str(p.relative_to(tmp_path / "out")) for p in (tmp_path / "out").rglob("*") if p.is_file())
    assert names == [
        "d00/file-000000.bin",
        "d00/file-000003.bin",
        "d00/file-000006.bin",
        "d01/file-000001.bin",
        "d01/file-000004.bin",
        "d02/file-000002.bin",
        "d02/file-000005.bin",
    ]


def test_the_default_is_a_thousand_small_files_in_ten_directories_and_no_large_one(fed, tmp_path, capsys):
    assert make(fed, tmp_path / "out") == 0
    listed = [p for p in (tmp_path / "out").rglob("*") if p.is_file()]
    assert len(listed) == 1000
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [f"d{i:02d}" for i in range(10)]
    assert not (tmp_path / "out" / "large.bin").exists()


def test_the_sizes_lie_between_one_and_65536_bytes_and_differ_from_file_to_file(fed, tmp_path):
    assert make(fed, tmp_path / "out", "--small", "60", "--dirs", "5") == 0
    sizes = [p.stat().st_size for p in (tmp_path / "out").rglob("*") if p.is_file()]
    assert min(sizes) >= 1
    assert max(sizes) <= 65536
    assert len(set(sizes)) > 30


def test_the_files_are_0664_and_the_directories_0775_whatever_the_umask(fed, tmp_path):
    old = os.umask(0o077)
    try:
        assert make(fed, tmp_path / "out", "--small", "6", "--dirs", "2", "--large-bytes", "10") == 0
    finally:
        os.umask(old)
    out = tmp_path / "out"
    assert {p.stat().st_mode & 0o777 for p in out.rglob("*") if p.is_file()} == {0o664}
    assert {p.stat().st_mode & 0o777 for p in [out, *out.rglob("*")] if p.is_dir()} == {0o775}


def test_the_large_file_is_written_in_pieces_and_holds_the_streams_bytes(fed, tmp_path, monkeypatch):
    monkeypatch.setattr(fed.tree, "PIECE", 128)
    written = []
    real = fed.tree.stream

    def watching(path, size, piece=None):
        for chunk in real(path, size, piece):
            written.append((path, len(chunk)))
            yield chunk

    monkeypatch.setattr(fed.tree, "stream", watching)
    assert make(fed, tmp_path / "out", "--small", "0", "--large-bytes", "300") == 0
    assert written == [("large.bin", 128), ("large.bin", 128), ("large.bin", 44)]
    assert (tmp_path / "out" / "large.bin").read_bytes() == fed.layout.BaselineFile("large.bin", 300).content()
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["large.bin"]  # no small file, so no directory


def test_it_prints_the_file_count_and_the_total_bytes(fed, tmp_path, capsys):
    assert make(fed, tmp_path / "out", "--small", "9", "--dirs", "2", "--large-bytes", "400") == 0
    on_disk = [p.stat().st_size for p in (tmp_path / "out").rglob("*") if p.is_file()]
    assert capsys.readouterr().out.strip() == f"{len(on_disk)} files, {sum(on_disk)} bytes"
    assert len(on_disk) == 10


def test_an_empty_existing_output_directory_is_taken(fed, tmp_path):
    (tmp_path / "out").mkdir()
    assert make(fed, tmp_path / "out", "--small", "3") == 0
    assert len(tree_of(tmp_path / "out")) == 3


def test_a_non_empty_output_is_refused_and_left_as_it_was(fed, tmp_path, capsys):
    out = tmp_path / "out"
    out.mkdir()
    (out / "mine.txt").write_text("keep", encoding="utf-8")
    assert make(fed, out, "--small", "3") == 2
    assert "is not empty" in capsys.readouterr().err
    assert [p.name for p in out.iterdir()] == ["mine.txt"]
    assert (out / "mine.txt").read_text(encoding="utf-8") == "keep"


def test_an_output_that_is_a_file_or_a_symbolic_link_is_refused(fed, tmp_path, capsys):
    (tmp_path / "file").write_text("x", encoding="utf-8")
    (tmp_path / "empty").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "empty")
    assert make(fed, tmp_path / "file") == 2
    assert make(fed, tmp_path / "link") == 2
    assert list((tmp_path / "empty").iterdir()) == []
    assert capsys.readouterr().err.count("error:") == 2


@pytest.mark.parametrize("args", [["--small", "-1"], ["--dirs", "0"], ["--large-bytes", "-5"], ["--small", "many"]])
def test_a_count_that_makes_no_sense_is_refused_before_anything_is_written(fed, tmp_path, args, capsys):
    with pytest.raises(SystemExit) as stopped:
        make(fed, tmp_path / "out", *args)
    assert stopped.value.code == 2
    assert not (tmp_path / "out").exists()
