"""A space whose owner holds it on an imported POSIX storage (deploy/compose-federation, #184): the layout key and its
refusals, the source a variable names, the bind it adds, the copy a writable import gets, the storage and the support
body the bootstrap sends, the scan it waits for, what the content writer and `reset --clean` leave alone, the facts,
the layout of the live slot and the script that makes a dataset to import. Nothing here starts a container: the panel
and the providers are fakes that record their calls."""

import re
from pathlib import Path

import pytest
from federation_support import MINIMAL, WITH_IMPORT, WITH_WRITABLE_IMPORT


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


def import_layout(fed, tmp_path: Path, text: str = WITH_WRITABLE_IMPORT):
    """A layout over a root and a source directory of its own, both below a resolved temp directory."""
    base = tmp_path.resolve()
    (base / "data").mkdir()
    spec = fed.spec.load_spec(write(base, text))
    return fed.layout.Layout(base / "fed", spec=spec), base / "data"


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
