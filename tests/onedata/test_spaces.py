"""Several spaces in one platform configuration (`[[spaces]]`): loading, labels and the fingerprint."""

import json

import pytest
from campaign_support import make_fingerprint, make_manifest, make_schema
from platform_support import leaked_values

from expcore.campaign.resume import FingerprintMismatch, open_campaign
from expcore.config.loading import ConfigError
from expcore_onedata.config import load_platform
from expcore_onedata.testing import ADMIN_ENV, NOW, SPACE_NAME, write_platform
from expcore_onedata.testing.world import SPACE_IDS, SPACE_LABELS, with_spaces


@pytest.fixture(autouse=True)
def admin(monkeypatch):
    monkeypatch.setenv(ADMIN_ENV, "admin-credential-value")


def load(tmp_path, edit=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return load_platform(write_platform(tmp_path, edit))


def several(count=2, **override):
    return lambda data: with_spaces(data, count, **override)


def test_o12_the_one_space_form_is_the_single_space_labelled_space_with_its_fingerprint_unchanged(tmp_path):
    """O12, C06: [space] and [support] read as one space, `spaces[0]` labelled `space`, and the fingerprint holds the
    TLS setting alone, as it did before several spaces could be declared."""
    config = load(tmp_path)
    assert [(e.label, e.position) for e in config.spaces] == [("space", "spaces[0]")]
    assert config.space_named() is config.spaces[0]
    assert config.space_named("space").space is config.space
    assert config.space.name == SPACE_NAME
    assert config.support.providers == ("alpha",)
    assert config.space_ids("data_path") == config.space_ids("tool_server") == ("spmock0001",)
    assert config.prefixes == ("study-a-",)
    assert config.fingerprint() == {"tls": {"mode": "verify"}}


def test_o12_several_spaces_load_each_named_by_its_label_with_its_own_support_and_planes(tmp_path):
    """O12, O04: each [[spaces]] entry has its label, its supporting providers, its data provider and the planes its
    token reaches; the prefixes of the gate are every entry's, each once."""

    def edit(data):
        with_spaces(data, 3)
        data["spaces"][1].update(holder="beta", tokens=["data_path"])
        data["spaces"][2].update(providers=["alpha"], tokens=["tool_server"])

    config = load(tmp_path, edit)
    assert [e.label for e in config.spaces] == list(SPACE_LABELS)
    assert [e.position for e in config.spaces] == ["spaces[0]", "spaces[1]", "spaces[2]"]
    assert config.space_named(SPACE_LABELS[1]).holder == "beta"
    assert config.space_named(SPACE_LABELS[2]).support.providers == ("alpha",)
    assert config.space_ids("data_path") == SPACE_IDS[:2]
    assert config.space_ids("tool_server") == (SPACE_IDS[0], SPACE_IDS[2])
    assert config.prefixes == ("study-a-", "study-b-")
    assert config.local_provider.host == "alpha.test"


def test_o12_with_several_spaces_the_one_space_accessors_refuse_without_naming_a_space(tmp_path):
    """O12: there is no one space to give, so `space`, `support` and an unnamed lookup refuse, as does an undeclared
    label; no message holds a label, a name or an id."""
    config = load(tmp_path, several())
    for read in (
        lambda: config.space,
        lambda: config.support,
        config.space_named,
        lambda: config.space_named("nowhere"),
    ):
        with pytest.raises(ConfigError) as caught:
            read()
        assert leaked_values(str(caught.value), tmp_path) == []


def edit_spaces(change):
    """An edit into the several-space form, followed by `change` of the document."""

    def edit(data):
        with_spaces(data, 2)
        change(data)

    return edit


BAD_SPACES = {
    "mixed": (lambda d: d.update(space={"name": SPACE_NAME, "id": "x", "prefixes": ["study-a-"]}), r"beside \[space\]"),
    "empty": (lambda d: d.update(spaces=[]), r"key 'spaces' must be a non-empty array of tables"),
    "label twice": (lambda d: d["spaces"][1].update(label=SPACE_LABELS[0]), "its own label"),
    "id twice": (lambda d: d["spaces"][1].update(id=SPACE_IDS[0]), "its own id"),
    "bad label": (lambda d: d["spaces"][1].update(label="9-starts-with-a-digit"), r"spaces\[1\]: key 'label'"),
    "two locals": (lambda d: d["spaces"][1].update(local="beta"), "same local provider"),
    "holder elsewhere": (
        lambda d: d["spaces"][1].update(providers=["alpha"], holder="beta"),
        r"spaces\[1\]: key 'holder' must be one of the providers",
    ),
    "unknown plane": (lambda d: d["spaces"][0].update(tokens=["mounting"]), r"spaces\[0\]: key 'tokens'"),
    "plane nowhere": (
        lambda d: [entry.update(tokens=["data_path"]) for entry in d["spaces"]],
        "tool_server plane's token to at least one space",
    ),
    "name off prefix": (lambda d: d["spaces"][1].update(name="unrelated-space"), r"spaces\[1\]: key 'name'"),
    "unknown provider": (lambda d: d["spaces"][0].update(providers=["gamma"], local="gamma"), r"spaces\[0\]"),
    "unknown key": (lambda d: d["spaces"][1].update(colour="blue"), r"spaces\[1\]: key 'colour'"),
}


@pytest.mark.parametrize("case", list(BAD_SPACES))
def test_o12_a_bad_spaces_table_names_the_entry_by_position_and_the_key_and_echoes_no_value(case, tmp_path):
    """O12: each mistake in [[spaces]] fails at load, naming the file, the entry's position and the key, never a
    label, a name, an id or a provider."""
    change, pattern = BAD_SPACES[case]
    with pytest.raises(ConfigError, match=pattern) as caught:
        load(tmp_path, edit_spaces(change))
    assert leaked_values(str(caught.value), tmp_path) == []


def test_c06_several_spaces_enter_the_fingerprint_by_hash_and_a_resume_under_another_set_is_refused(tmp_path):
    """C06, O12: with several spaces the fingerprint holds each entry's SHA-256 in order, and nothing of a label, a
    name or an id; a campaign started under one set is refused a resume under another set, another order, another
    support set or one space, and resumes under the same set."""
    configs = {
        "two": load(tmp_path / "a", several(2)),
        "three": load(tmp_path / "b", several(3)),
        "reordered": load(tmp_path / "c", lambda d: (with_spaces(d, 2), d["spaces"].reverse())),
        "other support": load(tmp_path / "d", several(2, providers=["alpha"])),
        "one space": load(tmp_path / "e"),
    }
    fingerprint = configs["two"].fingerprint()
    assert set(fingerprint) == {"tls", "spaces"}
    assert len(fingerprint["spaces"]) == 2
    assert leaked_values(json.dumps(fingerprint), tmp_path / "a") == []
    schema, manifest = make_schema(), make_manifest(make_schema())

    def resume(config):
        store, _ = open_campaign(
            tmp_path / "campaign.db",
            schema,
            manifest,
            make_fingerprint(schema, platform=config.fingerprint()),
            now=NOW,
            max_attempts=3,
        )
        store.close()

    resume(configs["two"])
    for label in ("three", "reordered", "other support", "one space"):
        with pytest.raises(FingerprintMismatch) as caught:
            resume(configs[label])
        assert all(line.startswith("platform.spaces") for line in caught.value.differences), label
    resume(configs["two"])
