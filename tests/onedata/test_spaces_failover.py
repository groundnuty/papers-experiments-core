"""A space's failover holders in `[[spaces]]` (`failover`, O98): the providers holding its whole replica, which its
reads fail over to when its data provider is taken down. Loading, the refusals, and the fingerprint.

The declaration is in the platform file, so the campaign fingerprint holds it; an entry without the key has the digest
it always had, so no existing campaign's fingerprint moves."""

import dataclasses
import hashlib
import json

import pytest
from platform_support import leaked_values

from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.spaces import ImportedStorage, Space, StudySpace, Support
from expcore_onedata.testing import ADMIN_ENV, write_platform
from expcore_onedata.testing.world import with_spaces


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")


def load(tmp_path, edit=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return load_platform(write_platform(tmp_path, edit))


def entries(count=2, **first):
    """`count` entries of `[[spaces]]`, supported by alpha (local, the holder) and beta, `first` updating the first."""

    def edit(data):
        with_spaces(data, count)
        data["providers"]["gamma"] = {"url": "https://gamma.test", "id": "id-gamma"}
        data["spaces"][0].update(first)

    return edit


def test_o98_a_failover_holder_is_loaded_into_its_entry_and_the_other_entries_declare_none(tmp_path):
    """O98: `failover = ["beta"]` is the entry's holders; an entry without the key has none: the empty tuple."""
    config = load(tmp_path, entries(failover=["beta"]))
    assert [entry.failover for entry in config.spaces] == [("beta",), ()]


def test_o98_the_holders_are_other_supporters_than_the_data_provider_and_the_local_provider_in_the_order_declared(
    tmp_path,
):
    """O98: any supporter of the entry but its data provider and its local provider may be a holder, and the holders are
    kept as they are declared. With beta the holder and alpha local, gamma alone qualifies; with alpha both the holder
    and the local provider, gamma and beta both do."""
    only = load(
        tmp_path / "only",
        entries(providers=["alpha", "beta", "gamma"], local="alpha", holder="beta", failover=["gamma"]),
    )
    assert only.spaces[0].failover == ("gamma",)
    wide = load(
        tmp_path / "wide",
        entries(providers=["alpha", "beta", "gamma"], local="alpha", holder="alpha", failover=["gamma", "beta"]),
    )
    assert wide.spaces[0].failover == ("gamma", "beta")


REFUSED = {
    "empty": ({"failover": []}, r"spaces\[0\]: key 'failover' must list at least one provider, each once"),
    "a name twice": ({"failover": ["beta", "beta"]}, r"spaces\[0\]: key 'failover' must list at least one provider"),
    "not a list": ({"failover": "beta"}, r"spaces\[0\]: key 'failover' must be an array of non-empty strings"),
    "not a supporter": ({"failover": ["gamma"]}, r"spaces\[0\]: key 'failover' must name providers that support"),
    "the holder": ({"failover": ["alpha"], "local": "beta"}, r"spaces\[0\]: key 'failover' must not name the space's"),
    "the local provider": (
        {"failover": ["alpha"], "holder": "beta"},
        r"spaces\[0\]: key 'failover' must not name the local provider: it is evicted at every reset",
    ),
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_o98_a_bad_failover_is_refused_at_load_naming_the_entry_by_position_and_echoing_no_value(case, tmp_path):
    """O98: an empty list, a name twice, a value that is no list, a provider that does not support the entry, its data
    provider and the agent's local provider are each refused, naming the file, the entry's position and the key, never a
    label, a name, an id or a provider."""
    change, pattern = REFUSED[case]
    with pytest.raises(ConfigError, match=pattern) as caught:
        load(tmp_path, entries(**change))
    assert leaked_values(str(caught.value), tmp_path) == []
    for provider in ("alpha", "beta", "gamma"):
        assert f"'{provider}'" not in str(caught.value)


def test_o98_the_one_space_form_cannot_declare_failover_holders(tmp_path):
    """O98: the key belongs to an entry of `[[spaces]]`; `[space]` refuses it as the unknown key it is there."""

    def edit(data):
        data["space"]["failover"] = ["beta"]
        data["support"] = {"providers": ["alpha", "beta"], "local": "alpha"}

    with pytest.raises(ConfigError, match=r"platform\.toml\.space: key 'failover' is not a known key"):
        load(tmp_path, edit)


# -- the fingerprint ---------------------------------------------------------------------------------------------------

# an entry as it was loaded before the key existed, built by hand: `digest()` and `legacy_digest()` are pinned from
# e5d12f52 (without it), so that no existing campaign's fingerprint moves
PLAIN = StudySpace(
    "alpha", "spaces[0]", Space("alpha-space", "sp-1", ("alpha-",)), Support(("one", "two", "three"), "two"), "one"
)
PLAIN_DIGEST = "41f5257f9b7568be7330211f2af0ed96b18a809b6c70f2e2cda29060821a65e6"
FULL = dataclasses.replace(PLAIN, tokens=("data_path",), imported=ImportedStorage(True), data="abc123")
FULL_DIGEST = "133698448087aadf75c9820cb481d411c5f4e0ba0d4f252d0c77737bdf73c542"
FULL_LEGACY = "ed1374640132e76d7f9990f81e9eeba00b13b3957d7d128711696315c8552817"


def test_o98_an_entry_without_the_key_has_the_digest_it_always_had(tmp_path):
    """O98: the digest and the legacy digest of an entry that declares no `failover` are byte for byte what they were
    before the key existed, with and without a data identity, so a campaign started earlier resumes."""
    assert PLAIN.failover == ()
    assert PLAIN.digest() == PLAIN.legacy_digest() == PLAIN_DIGEST
    assert FULL.digest() == FULL_DIGEST
    assert FULL.legacy_digest() == FULL_LEGACY


def old_digest(entry):
    """The digest of an entry as `StudySpace.legacy_digest` computed it before `failover` existed, from its fields."""
    facts = {
        "label": entry.label,
        "space": [entry.space.name, entry.space.id, list(entry.space.prefixes)],
        "support": [list(entry.support.providers), entry.support.local],
        "holder": entry.holder,
        "tokens": list(entry.tokens),
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def test_o98_a_loaded_entry_without_the_key_has_the_old_digest_and_the_platform_fingerprint_is_the_same(tmp_path):
    """O98: loaded from a file that declares none, an entry's digest is the old computation's, and the platform's
    fingerprint holds exactly those digests: it is what a file without the key always gave."""
    config = load(tmp_path, entries())
    assert [entry.digest() for entry in config.spaces] == [old_digest(entry) for entry in config.spaces]
    assert [entry.legacy_digest() for entry in config.spaces] == [old_digest(entry) for entry in config.spaces]
    assert config.fingerprint()["spaces"] == [old_digest(entry) for entry in config.spaces]


def test_o98_a_declared_failover_changes_the_digest_and_the_fingerprint_and_only_of_its_entry(tmp_path):
    """O98: the declaration is part of the experiment: with the key an entry's digest and legacy digest differ from
    without it, the platform fingerprint moves, and the entry that declares nothing keeps its digest. Another
    declaration is another digest."""
    plain = load(tmp_path / "plain", entries())
    declared = load(tmp_path / "declared", entries(failover=["beta"]))
    other = load(tmp_path / "other", entries(providers=["alpha", "beta", "gamma"], failover=["gamma"]))
    assert declared.spaces[0].digest() != plain.spaces[0].digest()
    assert declared.spaces[0].legacy_digest() != plain.spaces[0].legacy_digest()
    assert declared.spaces[1].digest() == plain.spaces[1].digest()
    assert declared.fingerprint() != plain.fingerprint()
    assert other.spaces[0].digest() != declared.spaces[0].digest()
    assert "failover" not in json.dumps(declared.fingerprint())  # a hash, never the providers named
    with_data = dataclasses.replace(FULL, failover=("one",))
    assert with_data.digest() != FULL_DIGEST
    assert with_data.legacy_digest() != FULL_LEGACY
