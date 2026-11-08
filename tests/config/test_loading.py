"""TOML loading: every failure names the file, and a typed read names the key."""

import pytest

from expcore.config.loading import ConfigError, Section, load_toml


def test_a_missing_file_is_named():
    with pytest.raises(ConfigError, match=r"nowhere\.toml: file not found"):
        load_toml("nowhere.toml")


def test_malformed_toml_names_the_file_and_the_position(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text("[models\nkey = 1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"broken\.toml: not valid TOML: .*line 1"):
        load_toml(path)


def test_a_file_that_is_not_text_is_named(tmp_path):
    path = tmp_path / "binary.toml"
    path.write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ConfigError, match=r"binary\.toml: cannot be read"):
        load_toml(path)


def test_a_valid_file_loads(tmp_path):
    path = tmp_path / "ok.toml"
    path.write_text('[models.alpha]\nbackend = "stub"\n', encoding="utf-8")
    assert load_toml(path) == {"models": {"alpha": {"backend": "stub"}}}


def section(data):
    return Section(data, "cfg.toml", "[models.alpha]")


def test_a_required_string_that_is_absent_names_file_table_and_key():
    with pytest.raises(ConfigError, match=r"cfg\.toml: \[models\.alpha\]: key 'api_id' is required"):
        section({}).string("api_id")


@pytest.mark.parametrize("value", [3, True, "", "  ", ["a"]])
def test_a_string_of_the_wrong_type_is_refused(value):
    with pytest.raises(ConfigError, match="key 'api_id' must be a non-empty string"):
        section({"api_id": value}).string("api_id")


def test_a_refusal_names_the_type_of_the_value_never_the_value():
    with pytest.raises(ConfigError, match="got int") as caught:
        section({"key_env": 424242}).string("key_env")
    assert "424242" not in str(caught.value)


@pytest.mark.parametrize("value", ["8", 1.5, True])
def test_an_integer_of_the_wrong_type_is_refused(value):
    with pytest.raises(ConfigError, match="key 'max_output_tokens' must be an integer"):
        section({"max_output_tokens": value}).optional_integer("max_output_tokens")


def test_an_absent_optional_key_is_none_or_empty():
    s = section({})
    assert s.optional_string("x") is None
    assert s.optional_integer("x") is None
    assert s.strings("x") == ()
    assert s.table("x") == {}


def test_an_array_of_strings_must_hold_only_strings():
    assert section({"aliases": ["a", "b"]}).strings("aliases") == ("a", "b")
    with pytest.raises(ConfigError, match="key 'aliases' must be an array of non-empty strings"):
        section({"aliases": ["a", 2]}).strings("aliases")


def test_a_table_key_must_hold_a_table():
    with pytest.raises(ConfigError, match="key 'sampling' must be a table"):
        section({"sampling": 1}).table("sampling")


def test_an_unknown_key_is_refused_and_the_known_ones_are_listed():
    with pytest.raises(ConfigError, match=r"key 'temprature' is not a known key; known keys: .*temperature"):
        section({"temprature": 1}).reject_unknown(["temperature", "top_p"])


def test_a_sub_section_is_named_below_its_parent():
    parent = Section({"models": {"alpha": {"backend": "stub"}}}, "cfg.toml", "(file)")
    child = parent.section("models").section("alpha")
    assert child.where == "(file).models.alpha"
    models = Section({"alpha": {}}, "cfg.toml", "[models]")
    assert models.section("alpha").where == "[models.alpha]"
    with pytest.raises(ConfigError, match=r"\[models\]: key 'beta' is required"):
        models.section("beta")
