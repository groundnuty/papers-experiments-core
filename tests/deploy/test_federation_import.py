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
