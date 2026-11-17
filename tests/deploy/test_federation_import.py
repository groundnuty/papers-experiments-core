"""A space whose owner holds it on an imported POSIX storage (deploy/compose-federation, #184): the layout key and its
refusals, the source a variable names, the bind it adds, the copy a writable import gets, the storage and the support
body the bootstrap sends, the scan it waits for, what the content writer and `reset --clean` leave alone, the facts,
the layout of the live slot and the script that makes a dataset to import. Nothing here starts a container: the panel
and the providers are fakes that record their calls."""

import os
import re
import shutil
from pathlib import Path

import pytest
from federation_support import MINIMAL, WITH_IMPORT, WITH_WRITABLE_IMPORT, import_layout


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "federation.toml"
    path.write_text(text, encoding="utf-8")
    return path


# -- the layout key ---------------------------------------------------------------------------------------------------


def test_a_space_declares_an_import_with_its_mode_and_the_variable_that_names_its_source(fed, tmp_path):
    spec = fed.spec.load_spec(write(tmp_path, WITH_WRITABLE_IMPORT))
    assert spec.space("alpha-space").imported is None
    assert spec.space("fern-space").imported == fed.spec.Import(readonly=True, source_env="EXPCORE_FED_IMPORT_FERN")
    assert spec.space("moss-space").imported == fed.spec.Import(readonly=False, source_env="EXPCORE_FED_IMPORT_MOSS")


def test_the_default_and_the_example_layout_declare_no_import(fed):
    for path in (None, fed.spec.HERE / "layouts" / "five-providers.toml"):
        assert all(space.imported is None for space in fed.spec.load_spec(path).spaces)


IMPORT_LINE = 'import = { readonly = true, source_env = "EXPCORE_FED_IMPORT_FERN" }'

IMPORT_REFUSED = [
    (
        "the baseline left on",
        ("baseline = false\nimport", "import"),
        "spaces.fern-space.import: a space with an import must declare baseline = false",
    ),
    (
        "the baseline declared on",
        ("baseline = false\nimport", "baseline = true\nimport"),
        "spaces.fern-space.import: a space with an import must declare baseline = false",
    ),
    (
        "no mode",
        ('{ readonly = true, source_env = "EXPCORE_FED_IMPORT_FERN" }', '{ source_env = "EXPCORE_FED_IMPORT_FERN" }'),
        "spaces.fern-space.import.readonly: is required",
    ),
    (
        "no variable",
        ('{ readonly = true, source_env = "EXPCORE_FED_IMPORT_FERN" }', "{ readonly = true }"),
        "spaces.fern-space.import.source_env: is required",
    ),
    (
        "a mode that is not a boolean",
        ("readonly = true", 'readonly = "yes"'),
        "spaces.fern-space.import.readonly: must be true or false",
    ),
    (
        "a path in place of a variable name",
        ("EXPCORE_FED_IMPORT_FERN", "/srv/data/fern"),
        "spaces.fern-space.import.source_env: must be the name of an environment variable",
    ),
    (
        "a variable outside the prefix",
        ("EXPCORE_FED_IMPORT_FERN", "HOME"),
        "spaces.fern-space.import.source_env",
    ),
    (
        "a lower-case variable",
        ("EXPCORE_FED_IMPORT_FERN", "EXPCORE_FED_IMPORT_fern"),
        "spaces.fern-space.import.source_env",
    ),
    (
        "an empty suffix",
        ("EXPCORE_FED_IMPORT_FERN", "EXPCORE_FED_IMPORT_"),
        "spaces.fern-space.import.source_env",
    ),
    (
        "a suffix of 49 characters",
        ("EXPCORE_FED_IMPORT_FERN", "EXPCORE_FED_IMPORT_" + "A" * 49),
        "spaces.fern-space.import.source_env",
    ),
    (
        "a key that is not one",
        ("readonly = true,", 'readonly = true, path = "/srv",'),
        "spaces.fern-space.import.path: is not a key of a layout here",
    ),
    ("not a table", (IMPORT_LINE, 'import = "yes"'), "spaces.fern-space.import: must be a table"),
]


@pytest.mark.parametrize(("change", "words"), [c[1:] for c in IMPORT_REFUSED], ids=[c[0] for c in IMPORT_REFUSED])
def test_an_import_the_layout_cannot_hold_is_refused_naming_the_file_and_the_key(fed, tmp_path, change, words):
    old, new = change
    assert WITH_IMPORT.count(old) == 1
    path = write(tmp_path, WITH_IMPORT.replace(old, new))
    with pytest.raises(fed.spec.LayoutError, match=re.escape(f"{path}: {words}")):
        fed.spec.load_spec(path)


def test_a_variable_of_forty_eight_characters_after_the_prefix_is_taken(fed, tmp_path):
    name = "EXPCORE_FED_IMPORT_" + "A9_" * 16
    spec = fed.spec.load_spec(write(tmp_path, WITH_IMPORT.replace("EXPCORE_FED_IMPORT_FERN", name)))
    assert spec.space("fern-space").imported.source_env == name


def test_two_writable_imports_may_not_name_the_same_variable(fed, tmp_path):
    text = WITH_WRITABLE_IMPORT.replace("EXPCORE_FED_IMPORT_FERN", "EXPCORE_FED_IMPORT_MOSS").replace(
        "readonly = true", "readonly = false"
    )
    path = write(tmp_path, text)
    with pytest.raises(fed.spec.LayoutError, match=re.escape(f"{path}: spaces.moss-space.import.source_env")) as found:
        fed.spec.load_spec(path)
    assert "fern-space" in str(found.value)  # the one that already holds the variable


def test_a_writable_import_and_a_read_only_one_may_share_a_source(fed, tmp_path):
    """The writable one copies the source and writes into its copy; the read-only one mounts the source itself."""
    text = WITH_WRITABLE_IMPORT.replace("EXPCORE_FED_IMPORT_MOSS", "EXPCORE_FED_IMPORT_FERN")
    spec = fed.spec.load_spec(write(tmp_path, text))
    assert spec.space("moss-space").imported.source_env == spec.space("fern-space").imported.source_env


def test_read_only_imports_may_share_one_source(fed, tmp_path):
    """One read-only copy of a dataset serves any number of spaces: nothing writes to it."""
    extra = WITH_IMPORT + (
        '\n[spaces.reed-space]\nowner = "oak"\nbaseline = false\n'
        'import = { readonly = true, source_env = "EXPCORE_FED_IMPORT_FERN" }\n'
    )
    spec = fed.spec.load_spec(write(tmp_path, extra))
    assert spec.space("reed-space").imported == spec.space("fern-space").imported


def test_a_layout_with_no_import_still_loads_with_every_space_unimported(fed, tmp_path):
    spec = fed.spec.load_spec(write(tmp_path, MINIMAL))
    assert [space.imported for space in spec.spaces] == [None]


# -- where an import's directories are ---------------------------------------------------------------------------------


def test_an_imported_space_has_a_storage_named_after_it_mounted_under_the_import_directory(fed):
    assert fed.layout.IMPORT_MOUNT == "/volumes/import"
    assert fed.layout.import_storage_name("fern-space") == "import-fern-space"


def test_a_writable_copy_lies_in_the_owners_directory_of_the_root(fed, tmp_path):
    layout, _ = import_layout(fed, tmp_path)
    assert layout.import_dir("moss-space") == layout.root / "oak" / "import" / "moss-space"
    assert layout.import_dir("fern-space") == layout.root / "elm" / "import" / "fern-space"


def test_the_source_is_the_directory_the_variable_names(fed, tmp_path):
    layout, data = import_layout(fed, tmp_path)
    assert layout.import_source("fern-space", {"EXPCORE_FED_IMPORT_FERN": str(data)}) == data


def test_a_trailing_slash_on_the_source_changes_nothing(fed, tmp_path):
    layout, data = import_layout(fed, tmp_path)
    assert layout.import_source("fern-space", {"EXPCORE_FED_IMPORT_FERN": f"{data}/"}) == data


def source_problem(fed, tmp_path: Path, value: str | None, *, make=None) -> str:
    """The message the layout refuses a source with; `make(base)` builds what `value` names, `{base}` standing for the
    resolved temp directory."""
    layout, _ = import_layout(fed, tmp_path)
    base = tmp_path.resolve()
    if make is not None:
        make(base)
    given = None if value is None else value.replace("{base}", str(base))
    with pytest.raises(ValueError, match="EXPCORE_FED_IMPORT_FERN") as found:
        layout.import_source("fern-space", {} if given is None else {"EXPCORE_FED_IMPORT_FERN": given})
    assert not given or given not in str(found.value)  # never the value itself
    return str(found.value)


def test_a_source_variable_that_is_not_set_is_refused(fed, tmp_path):
    assert "is not set" in source_problem(fed, tmp_path, None)


def test_a_source_variable_that_is_empty_is_refused(fed, tmp_path):
    assert "must be an absolute path" in source_problem(fed, tmp_path, "")


def test_a_relative_source_is_refused_without_echoing_it(fed, tmp_path):
    assert "must be an absolute path" in source_problem(fed, tmp_path, "relative/data")


def test_a_source_with_a_parent_step_is_refused(fed, tmp_path):
    """`a/link/../b` is not `a/b` when `link` is a symbolic link, so the check never normalises a path away."""
    assert "'..'" in source_problem(fed, tmp_path, "{base}/data/../data")


def test_a_source_that_does_not_exist_is_refused(fed, tmp_path):
    assert "must be an existing directory" in source_problem(fed, tmp_path, "{base}/absent")


def test_a_source_that_is_a_file_is_refused(fed, tmp_path):
    def make(base):
        (base / "file").write_text("x", encoding="utf-8")

    assert "must be an existing directory" in source_problem(fed, tmp_path, "{base}/file", make=make)


def test_a_source_that_is_a_symbolic_link_is_refused(fed, tmp_path):
    def make(base):
        (base / "link").symlink_to(base / "data")

    assert "symbolic link" in source_problem(fed, tmp_path, "{base}/link", make=make)


def test_a_source_below_a_symbolic_link_is_refused(fed, tmp_path):
    def make(base):
        (base / "link").symlink_to(base / "data")
        (base / "data" / "inner").mkdir()

    assert "symbolic link" in source_problem(fed, tmp_path, "{base}/link/inner", make=make)


def test_a_source_that_is_the_root_is_refused(fed, tmp_path):
    def make(base):
        (base / "fed").mkdir()

    assert "is the root" in source_problem(fed, tmp_path, "{base}/fed", make=make)


def test_a_source_inside_the_root_is_refused(fed, tmp_path):
    """Down empties the root, so a dataset inside it would go with it."""

    def make(base):
        (base / "fed" / "oak").mkdir(parents=True)

    assert "inside the root" in source_problem(fed, tmp_path, "{base}/fed/oak", make=make)


def test_a_source_that_holds_the_root_is_refused(fed, tmp_path):
    def make(base):
        (base / "fed").mkdir()

    assert "holds the root" in source_problem(fed, tmp_path, "{base}", make=make)


def test_the_filesystem_root_as_a_source_is_refused(fed, tmp_path):
    assert "holds the root" in source_problem(fed, tmp_path, "/")


def test_a_space_that_is_not_imported_has_no_source_and_no_copy(fed, tmp_path):
    layout, _ = import_layout(fed, tmp_path)
    with pytest.raises(ValueError, match="alpha-space"):
        layout.import_source("alpha-space", {})
    with pytest.raises(ValueError, match="alpha-space"):
        layout.import_dir("alpha-space")


# -- the bind an import adds ------------------------------------------------------------------------------------------


def volumes(fed, spec, service: str) -> dict[str, dict]:
    """A service's binds by target."""
    return {v["target"]: v for v in fed.compose.model(spec)["services"][service]["volumes"]}


def test_a_read_only_import_binds_its_source_by_the_variable_read_only_into_the_owners_service(fed, tmp_path):
    spec = fed.spec.load_spec(write(tmp_path, WITH_WRITABLE_IMPORT))
    mount = volumes(fed, spec, "elm")["/volumes/import/fern-space"]
    assert mount["source"] == "${EXPCORE_FED_IMPORT_FERN:?set by federation.py}"
    assert mount["read_only"] is True
    assert mount["bind"] == {"create_host_path": False}


def test_a_writable_import_binds_its_copy_in_the_root_read_write_into_the_owners_service(fed, tmp_path):
    spec = fed.spec.load_spec(write(tmp_path, WITH_WRITABLE_IMPORT))
    mount = volumes(fed, spec, "oak")["/volumes/import/moss-space"]
    assert mount["source"] == f"{fed.compose.ROOT}/oak/import/moss-space"
    assert "read_only" not in mount
    assert mount["bind"] == {"create_host_path": False}


def test_an_import_is_bound_into_its_owners_service_only(fed, tmp_path):
    """A mirror keeps its regular storage: it never sees the dataset's directory."""
    spec = fed.spec.load_spec(write(tmp_path, WITH_WRITABLE_IMPORT))
    for service, space in (("oak", "fern-space"), ("elm", "moss-space")):
        assert f"/volumes/import/{space}" not in volumes(fed, spec, service)
    assert "/volumes/import/fern-space" not in volumes(fed, spec, "zone")
    assert "/volumes/import/moss-space" not in volumes(fed, spec, "zone")


def test_a_service_with_no_imported_space_keeps_the_binds_it_always_had(fed, tmp_path):
    spec = fed.spec.load_spec(write(tmp_path, MINIMAL))
    assert [v["target"] for v in fed.compose.model(spec)["services"]["oak"]["volumes"]] == [
        "/volumes/persistence",
        "/volumes/storage",
        "/expcore/registration-token",
        "/expcore/federation-ca.pem",
        "/expcore/start.sh",
    ]


def test_an_owner_of_two_imported_spaces_gets_one_bind_each(fed, tmp_path):
    text = WITH_IMPORT + '\n[spaces.reed-space]\nowner = "elm"\nbaseline = false\nimport = { readonly = false'
    text += ', source_env = "EXPCORE_FED_IMPORT_REED" }\n'
    spec = fed.spec.load_spec(write(tmp_path, text))
    targets = [t for t in volumes(fed, spec, "elm") if t.startswith("/volumes/import/")]
    assert targets == ["/volumes/import/fern-space", "/volumes/import/reed-space"]


class FakeDocker:
    """subprocess.run as docker compose is run through it: every command and the environment it got."""

    def __init__(self) -> None:
        self.runs: list[tuple[list[str], dict[str, str]]] = []

    def run(self, command, *, env, **options):
        self.runs.append((command, env))
        return type("Done", (), {"returncode": 0, "stdout": ""})()


@pytest.fixture
def docker(fed, monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(fed.docker.subprocess, "run", fake.run)
    for name in ("EXPCORE_FED_IMPORT_FERN", "EXPCORE_FED_IMPORT_MOSS"):
        monkeypatch.delenv(name, raising=False)
    return fake


def test_compose_gets_the_checked_source_of_a_read_only_import_in_its_environment(fed, tmp_path, docker, monkeypatch):
    layout, data = import_layout(fed, tmp_path)
    monkeypatch.setenv("EXPCORE_FED_IMPORT_FERN", str(data))
    fed.docker.compose(layout, "up", "--detach")
    ((_, env),) = docker.runs
    assert env["EXPCORE_FED_IMPORT_FERN"] == str(data)
    assert env["EXPCORE_FED_ROOT"] == str(layout.root)


def test_compose_refuses_a_read_only_import_whose_variable_is_not_set_before_it_runs_docker(fed, tmp_path, docker):
    layout, _ = import_layout(fed, tmp_path)
    with pytest.raises(RuntimeError, match="EXPCORE_FED_IMPORT_FERN is not set"):
        fed.docker.compose(layout, "down")
    assert docker.runs == []


def test_compose_refuses_a_read_only_source_the_layout_would_not_take(fed, tmp_path, docker, monkeypatch):
    layout, _ = import_layout(fed, tmp_path)
    monkeypatch.setenv("EXPCORE_FED_IMPORT_FERN", "relative/data")
    with pytest.raises(RuntimeError, match="EXPCORE_FED_IMPORT_FERN must be an absolute path"):
        fed.docker.compose(layout, "ps")
    assert docker.runs == []


def test_compose_needs_no_source_for_a_writable_import_whose_copy_is_in_the_root(fed, tmp_path, docker, monkeypatch):
    """The source of a writable import is read once, when up makes the copy; down and the rest never need it again."""
    layout, data = import_layout(fed, tmp_path)
    monkeypatch.setenv("EXPCORE_FED_IMPORT_FERN", str(data))
    fed.docker.compose(layout, "down")
    ((_, env),) = docker.runs
    assert "EXPCORE_FED_IMPORT_MOSS" not in env


def test_compose_of_a_layout_without_an_import_needs_no_variable(fed, tmp_path, docker):
    layout = fed.layout.Layout(tmp_path / "fed", spec=fed.spec.load_spec(write(tmp_path, MINIMAL)))
    fed.docker.compose(layout, "ps")
    ((_, env),) = docker.runs
    assert not [name for name in env if name.startswith("EXPCORE_FED_IMPORT_")]


# -- the copy a writable import gets ----------------------------------------------------------------------------------


def dataset(root: Path, files: dict[str, bytes]) -> None:
    """A directory tree of the given files, whose names may hold directories."""
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def writable_layout(fed, tmp_path: Path, files: dict[str, bytes] | None = None):
    """A layout whose moss-space is a writable import of a source holding `files`; return it with the source and the
    environment that names it."""
    layout, data = import_layout(fed, tmp_path)
    dataset(data, {"a.txt": b"alpha", "d00/b.bin": b"\x00\x01\x02", "d00/deep/c.txt": b"c"} if files is None else files)
    return layout, data, {"EXPCORE_FED_IMPORT_MOSS": str(data), "EXPCORE_FED_IMPORT_FERN": str(data)}


def modes(root: Path) -> dict[str, int]:
    return {str(p.relative_to(root)): p.stat().st_mode & 0o777 for p in sorted(root.rglob("*"))}


def test_a_writable_import_is_copied_into_its_directory_with_the_modes_an_upload_has(fed, tmp_path):
    layout, data, environ = writable_layout(fed, tmp_path)
    data.chmod(0o700)
    (data / "a.txt").chmod(0o600)
    copies = fed.importing.pending_copies(layout, environ)
    assert copies == [("moss-space", data)]
    fed.importing.copy_imports(layout, copies)
    target = layout.import_dir("moss-space")
    assert {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob("*") if p.is_file()} == {
        "a.txt": b"alpha",
        "d00/b.bin": b"\x00\x01\x02",
        "d00/deep/c.txt": b"c",
    }
    assert {name: mode for name, mode in modes(target).items() if "." in name} == {
        "a.txt": 0o664,
        "d00/b.bin": 0o664,
        "d00/deep/c.txt": 0o664,
    }
    assert {name: mode for name, mode in modes(target).items() if "." not in name} == {"d00": 0o775, "d00/deep": 0o775}
    assert target.stat().st_mode & 0o777 == 0o775
    assert (data / "a.txt").stat().st_mode & 0o777 == 0o600  # the source is read, never changed


def test_a_copy_keeps_neither_times_nor_the_sources_modes(fed, tmp_path):
    layout, data, environ = writable_layout(fed, tmp_path, {"old.txt": b"x"})
    os.utime(data / "old.txt", (1_000_000_000, 1_000_000_000))
    (data / "old.txt").chmod(0o755)
    fed.importing.copy_imports(layout, fed.importing.pending_copies(layout, environ))
    copied = layout.import_dir("moss-space") / "old.txt"
    assert copied.stat().st_mtime > 1_000_000_000
    assert copied.stat().st_mode & 0o777 == 0o664


def test_an_empty_source_gives_an_empty_directory_of_the_upload_mode(fed, tmp_path):
    layout, _, environ = writable_layout(fed, tmp_path, {})
    fed.importing.copy_imports(layout, fed.importing.pending_copies(layout, environ))
    assert list(layout.import_dir("moss-space").iterdir()) == []
    assert layout.import_dir("moss-space").stat().st_mode & 0o777 == 0o775


def test_a_copy_that_exists_is_never_made_again_and_its_source_is_no_longer_read(fed, tmp_path):
    layout, data, environ = writable_layout(fed, tmp_path)
    fed.importing.copy_imports(layout, fed.importing.pending_copies(layout, environ))
    changed = layout.import_dir("moss-space") / "a.txt"
    changed.write_bytes(b"written by the platform")
    (data / "late.txt").write_bytes(b"added to the source afterwards")
    # the source may even be gone: nothing reads it once the copy exists
    assert fed.importing.pending_copies(layout, {"EXPCORE_FED_IMPORT_FERN": str(data)}) == []
    assert changed.read_bytes() == b"written by the platform"
    assert not (layout.import_dir("moss-space") / "late.txt").exists()


@pytest.mark.parametrize("linked", ["import directory", "copy"])
def test_o41_a_symbolic_link_where_the_copy_goes_is_refused_and_nothing_outside_the_root_is_touched(
    fed, tmp_path, linked
):
    """O41: the copy and its mount lie below the root, which down empties; a link at the owner's import directory, or
    at the copy itself, would lead them, the copy's chmod and the removal of an unfinished copy outside it."""
    layout, _, environ = writable_layout(fed, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "kept.txt").write_bytes(b"not the federation's")
    outside.chmod(0o700)
    target = layout.import_dir("moss-space")
    link = target.parent if linked == "import directory" else target
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symbolic link"):
        fed.importing.copy_imports(layout, fed.importing.pending_copies(layout, environ))
    assert sorted(p.name for p in outside.iterdir()) == ["kept.txt"]
    assert outside.stat().st_mode & 0o777 == 0o700


def test_o41_no_compose_command_mounts_a_writable_copy_that_is_a_symbolic_link(fed, tmp_path, monkeypatch):
    """O41: every Compose command binds the writable copy read-write from the root; one that is a link is refused
    before Compose runs, whatever made it after up."""
    layout, data, environ = writable_layout(fed, tmp_path)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    target = layout.import_dir("moss-space")
    target.parent.mkdir(parents=True)
    target.symlink_to(data, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symbolic link"):
        fed.docker.import_sources(layout)


def test_a_read_only_import_is_never_copied_but_its_source_is_checked(fed, tmp_path):
    layout, data, environ = writable_layout(fed, tmp_path)
    assert [space for space, _ in fed.importing.pending_copies(layout, environ)] == ["moss-space"]
    assert not layout.import_dir("fern-space").exists()
    with pytest.raises(RuntimeError, match="EXPCORE_FED_IMPORT_FERN is not set"):
        fed.importing.pending_copies(layout, {"EXPCORE_FED_IMPORT_MOSS": str(data)})


def test_a_writable_import_without_a_copy_needs_its_variable(fed, tmp_path):
    layout, data, _ = writable_layout(fed, tmp_path)
    with pytest.raises(RuntimeError, match="EXPCORE_FED_IMPORT_MOSS is not set"):
        fed.importing.pending_copies(layout, {"EXPCORE_FED_IMPORT_FERN": str(data)})


def odd_tree(base: Path) -> None:
    """A source with a symbolic link to a file, one to a directory, a dangling one and a named pipe among regular
    entries."""
    dataset(base, {"a.txt": b"a", "d00/b.txt": b"b"})
    (base / "d00" / "link-to-file").symlink_to(base / "a.txt")
    (base / "link-to-dir").symlink_to(base / "d00")
    (base / "dangling").symlink_to(base / "absent")
    os.mkfifo(base / "pipe")


def test_a_source_with_anything_but_files_and_directories_is_refused_with_a_count_and_nothing_copied(fed, tmp_path):
    """op-worker's import skips a symbolic link without a word, so a copy that kept or followed one would give a space
    that is quietly short of files."""
    layout, data, environ = writable_layout(fed, tmp_path, {})
    odd_tree(data)
    with pytest.raises(RuntimeError, match=r"4 entries of the dataset of moss-space") as found:
        fed.importing.pending_copies(layout, environ)
    assert str(data) not in str(found.value)
    assert "link-to-file" not in str(found.value)  # a count, never a name
    assert not layout.import_dir("moss-space").exists()
    assert not layout.import_dir("moss-space").parent.exists()


def test_a_single_odd_entry_is_counted_in_the_singular(fed, tmp_path):
    layout, data, environ = writable_layout(fed, tmp_path)
    (data / "link").symlink_to(data / "a.txt")
    with pytest.raises(RuntimeError, match=r"1 entry of the dataset of moss-space"):
        fed.importing.pending_copies(layout, environ)


def test_a_copy_that_stops_midway_leaves_no_directory_to_be_taken_for_a_finished_one(fed, tmp_path, monkeypatch):
    layout, data, environ = writable_layout(fed, tmp_path)
    copied = []
    real = fed.importing.shutil.copyfile

    def stops(source, target):
        if copied:
            raise OSError(5, "input/output error", str(source))
        copied.append(target)
        return real(source, target)

    monkeypatch.setattr(fed.importing.shutil, "copyfile", stops)
    with pytest.raises(RuntimeError, match=r"moss-space could not be copied \(OSError") as found:
        fed.importing.copy_imports(layout, fed.importing.pending_copies(layout, environ))
    assert str(data) not in str(found.value)
    assert not layout.import_dir("moss-space").exists()  # the next up copies it again, from nothing
    monkeypatch.undo()
    fed.importing.copy_imports(layout, fed.importing.pending_copies(layout, environ))
    assert (layout.import_dir("moss-space") / "d00" / "deep" / "c.txt").read_bytes() == b"c"
    assert [p.name for p in layout.import_dir("moss-space").parent.iterdir()] == ["moss-space"]


def test_a_symbolic_link_that_appears_after_the_check_is_refused_by_the_copy_too(fed, tmp_path):
    layout, data, environ = writable_layout(fed, tmp_path)
    copies = fed.importing.pending_copies(layout, environ)
    (data / "late-link").symlink_to(data / "a.txt")
    with pytest.raises(RuntimeError, match="1 entry"):
        fed.importing.copy_imports(layout, copies)
    assert not layout.import_dir("moss-space").exists()


def test_a_bad_source_stops_prepare_before_it_touches_the_root(fed, tmp_path, monkeypatch):
    layout, _, _ = writable_layout(fed, tmp_path)
    monkeypatch.delenv("EXPCORE_FED_IMPORT_FERN", raising=False)
    monkeypatch.delenv("EXPCORE_FED_IMPORT_MOSS", raising=False)
    with pytest.raises(RuntimeError, match="EXPCORE_FED_IMPORT_FERN is not set"):
        fed.cli.prepare(layout)
    assert not layout.root.exists()


needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None, reason="the openssl command makes the CA")


@needs_openssl
def test_prepare_copies_a_writable_import_once_and_leaves_a_read_only_one_alone(fed, tmp_path, monkeypatch):
    layout, data, environ = writable_layout(fed, tmp_path)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    fed.cli.prepare(layout)
    target = layout.import_dir("moss-space")
    assert (target / "d00" / "b.bin").read_bytes() == b"\x00\x01\x02"
    assert not layout.import_dir("fern-space").exists()
    assert not list((layout.root / "elm").glob("import*"))  # nothing of a read-only import in its owner's directory
    (target / "a.txt").write_bytes(b"the platform wrote here")
    (data / "late.txt").write_bytes(b"x")
    fed.cli.prepare(layout)  # up again: the copy is not made over
    assert (target / "a.txt").read_bytes() == b"the platform wrote here"
    assert not (target / "late.txt").exists()
