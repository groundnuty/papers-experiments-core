"""The data provider of each space, as the sandbox's attribution of a blocked read needs it (O97): the space's name as
the mount shows it, mapped to its holder's label."""

import pytest

from expcore_onedata.config import PlatformConfig, load_platform
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.testing.world import SPACE_NAMES, with_placement, with_spaces


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")


def load(tmp_path, edit=None):
    return load_platform(write_platform(tmp_path, edit))


def test_o97_each_space_maps_to_the_label_of_its_data_provider(tmp_path):
    """O97: the name the mount shows maps to the holder's position label, never the holder's name."""

    def edit(data):
        with_spaces(data, 2)
        data["spaces"][1].update(holder="beta")

    holders = load(tmp_path, edit).space_holders()
    assert holders == {SPACE_NAMES[0]: "providers[0]", SPACE_NAMES[1]: "providers[1]"}
    assert "alpha" not in repr(holders)
    assert "beta" not in repr(holders)


def test_o97_the_one_space_form_without_a_placement_table_has_no_holder_so_its_map_is_empty(tmp_path):
    """O97: nothing names the single space's data provider: its map is empty, so no block on it is the agent's."""
    assert load(tmp_path).space_holders() == {}


def test_o97_the_one_space_forms_data_provider_is_the_one_placement_names(tmp_path):
    """O97: the single space's holder is `[placement]`'s data provider, by its label."""
    config = load(tmp_path, lambda data: with_placement(data, holder="beta"))
    assert config.space_holders() == {config.space.name: "providers[1]"}


def test_o97_an_entry_of_spaces_never_takes_placements_holder(tmp_path):
    """O97, O09: `[[spaces]]` entries name their own holder; `[placement]`'s is never borrowed for one."""

    def edit(data):
        with_spaces(data, 2)
        with_placement(data, holder="beta")

    holders = load(tmp_path, edit).space_holders()
    assert holders == {SPACE_NAMES[0]: "providers[0]", SPACE_NAMES[1]: "providers[0]"}


def test_o97_a_placement_holder_that_names_no_declared_provider_gives_no_holder(tmp_path):
    """O97: a holder that is not a declared provider is not mapped (the table's own loading refuses it)."""
    config = load(tmp_path, lambda data: with_placement(data, holder="nowhere"))
    assert config.space_holders() == {}


def test_o97_the_labels_are_those_the_fault_injector_is_given(tmp_path):
    """O97: the holder's label is `provider_label`, the label an injector takes as its target."""
    config = load(tmp_path, lambda data: with_spaces(data, 2))
    for entry in config.spaces:
        assert config.space_holders()[entry.space.name] == config.provider_label(entry.holder)


def test_o97_the_map_is_a_plain_copy_each_time(tmp_path):
    """O97: a caller that changes the map it was given changes nothing of the platform's."""
    config = load(tmp_path, lambda data: with_spaces(data, 2))
    config.space_holders().clear()
    assert len(config.space_holders()) == 2
    assert isinstance(config, PlatformConfig)
