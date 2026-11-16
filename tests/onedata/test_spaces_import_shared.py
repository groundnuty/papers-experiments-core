"""A space whose imported tree is shared with other federations: `import = { readonly = true, shared = true }` (O56).

A shared tree is read by several federations at once, so no one of them may be allowed to write to it: the declaration
is refused at load unless the storage is read-only, and it enters the fingerprint only when it is made."""

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


def test_o56_a_shared_read_only_import_loads_in_both_forms(tmp_path):
    """O56: `shared = true` with `readonly = true` is the declaration of a tree several federations import."""
    declared = ImportedStorage(readonly=True, shared=True)
    assert load(tmp_path / "a", one_space({"readonly": True, "shared": True})).spaces[0].imported == declared
    several = load(tmp_path / "b", second_entry({"readonly": True, "shared": True}))
    assert [entry.imported for entry in several.spaces] == [None, declared]


def test_o56_an_import_that_does_not_say_shared_is_not_shared(tmp_path):
    """O56: the key is optional, and without it an import is what it was: not shared, whatever its read-only."""
    assert load(tmp_path / "a", one_space({"readonly": True})).spaces[0].imported == ImportedStorage(readonly=True)
    assert load(tmp_path / "b", one_space({"readonly": True, "shared": False})).spaces[0].imported.shared is False
    assert load(tmp_path / "c", one_space({"readonly": False})).spaces[0].imported.shared is False


@pytest.mark.parametrize("form", ["one space", "several"])
def test_o56_a_shared_import_that_is_not_read_only_is_refused_at_load(form, tmp_path):
    """O56: other federations read this tree; a federation that could write to it could change theirs. The refusal
    names the file, the table and the key, and echoes no value."""
    value = {"readonly": False, "shared": True}
    edit = one_space(value) if form == "one space" else second_entry(value)
    with pytest.raises(ConfigError, match="key 'readonly' must be true when 'shared' is true") as caught:
        load(tmp_path, edit)
    assert leaked_values(str(caught.value), tmp_path) == []
    assert ("spaces[1]" in str(caught.value)) == (form == "several")


@pytest.mark.parametrize("shared", ["yes", 1, "true"])
def test_o56_shared_must_be_a_boolean(shared, tmp_path):
    """O56: a word or a number is not a declaration; it is refused rather than read as true or false."""
    with pytest.raises(ConfigError, match="key 'shared' must be true or false"):
        load(tmp_path, one_space({"readonly": True, "shared": shared}))


def test_o56_a_shared_import_enters_the_fingerprint_and_a_plain_one_keeps_its_old_one(tmp_path):
    """O56, C06: a resume after the tree stopped being shared is refused, so the declaration is in the fingerprint,
    and only when made: every earlier import's fingerprint is what it was."""
    plain = load(tmp_path / "a", one_space({"readonly": True})).fingerprint()
    assert plain == {"tls": {"mode": "verify"}, "storage_import": {"readonly": True}}
    shared = load(tmp_path / "b", one_space({"readonly": True, "shared": True})).fingerprint()
    assert shared == {"tls": {"mode": "verify"}, "storage_import": {"readonly": True, "shared": True}}
    unshared = load(tmp_path / "c", one_space({"readonly": True, "shared": False})).fingerprint()
    assert unshared == plain
    base = load(tmp_path / "d", second_entry({"readonly": True})).fingerprint()["spaces"]
    marked = load(tmp_path / "e", second_entry({"readonly": True, "shared": True})).fingerprint()["spaces"]
    assert base[0] == marked[0]
    assert base[1] != marked[1]
    assert leaked_values(json.dumps(marked), tmp_path / "e") == []
