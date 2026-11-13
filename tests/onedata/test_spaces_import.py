"""A space whose data provider holds it on an imported storage: the `import` key of `[space]` and `[[spaces]]` (O37)."""

import json

import pytest
from platform_support import leaked_values

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.spaces import ImportedStorage
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.testing.world import with_spaces


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")


def load(tmp_path, edit=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return load_platform(write_platform(tmp_path, edit))


def one_space(value):
    return lambda data: data["space"].update({"import": value})


def second_entry(value):
    def edit(data):
        with_spaces(data, 2)
        data["spaces"][1]["import"] = value

    return edit


def test_o37_a_space_without_the_key_is_held_on_a_regular_storage(tmp_path):
    """O37: without `import` nothing changes: the space is ingested, as every space was."""
    assert load(tmp_path).spaces[0].imported is None


def test_o37_the_one_space_form_declares_a_read_only_imported_storage(tmp_path):
    """O37: `[space] import = { readonly = true }` says the data provider holds the space on an imported storage that
    it never writes."""
    assert load(tmp_path, one_space({"readonly": True})).spaces[0].imported == ImportedStorage(readonly=True)


def test_o37_an_entry_of_several_spaces_declares_a_writable_imported_storage_and_the_others_stay_regular(tmp_path):
    """O37: the key is per entry; an entry without it is a regular space."""
    config = load(tmp_path, second_entry({"readonly": False}))
    assert [entry.imported for entry in config.spaces] == [None, ImportedStorage(readonly=False)]


BAD = {
    "not a table": (True, "key 'import' must be a table"),
    "readonly missing": ({}, "key 'readonly' is required"),
    "readonly not a boolean": ({"readonly": "yes"}, "key 'readonly' must be true or false"),
    "unknown key": ({"readonly": True, "mode": "manual"}, "key 'mode' is not a known key"),
}


@pytest.mark.parametrize("case", list(BAD))
@pytest.mark.parametrize("form", ["one space", "several"])
def test_o37_a_bad_import_table_is_refused_at_load_naming_the_key_and_echoing_no_value(case, form, tmp_path):
    """O37, O12: each mistake fails at load, naming the file, the table and the key, never a name or an id."""
    value, words = BAD[case]
    edit = one_space(value) if form == "one space" else second_entry(value)
    with pytest.raises(ConfigError, match=words) as caught:
        load(tmp_path, edit)
    assert leaked_values(str(caught.value), tmp_path) == []
    assert ("spaces[1]" in str(caught.value)) == (form == "several")


def test_c06_an_imported_storage_enters_the_fingerprint_and_its_absence_leaves_the_old_one(tmp_path):
    """C06, O37: a resume after the data provider's storage changed kind is refused, so the declaration enters the
    fingerprint: an entry's digest with several spaces, and a `storage_import` key in the one-space form, which keeps
    its fingerprint unchanged without the key."""
    plain = load(tmp_path / "a").fingerprint()
    assert plain == {"tls": {"mode": "verify"}}
    imported = load(tmp_path / "b", one_space({"readonly": True})).fingerprint()
    assert imported == {"tls": {"mode": "verify"}, "storage_import": {"readonly": True}}
    writable = load(tmp_path / "c", one_space({"readonly": False})).fingerprint()
    assert writable["storage_import"] == {"readonly": False}
    several = load(tmp_path / "d", lambda d: with_spaces(d, 2)).fingerprint()["spaces"]
    marked = load(tmp_path / "e", second_entry({"readonly": True})).fingerprint()["spaces"]
    assert several[0] == marked[0] and several[1] != marked[1]
    assert leaked_values(json.dumps(marked), tmp_path / "e") == []
