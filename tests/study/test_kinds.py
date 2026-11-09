"""Infrastructure kinds a study can declare as treatment: the platforms study.toml names, and the study's own kinds.

Both are known before study.toml's conditions are validated: `[study] platforms` names modules imported first, so their
`register_infra_kind` calls have run, and `[study.kinds]` declares the study's own.
"""

import shutil
import uuid
from pathlib import Path

import pytest

from expcore import errors
from expcore.config.loading import ConfigError
from expcore.study.config import load_study

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
PLUGIN_LINE = 'plugin = "plugin.py"\n'  # inside [study]
CODE_END = '"plugin.py", "prompt.txt"]\n'  # the end of [study]
TREATMENT = 'infrastructure_is_treatment = ["knowledge"]'


@pytest.fixture
def registry(monkeypatch):
    """Start with only the core's infrastructure kinds and leave no platform or study kind behind."""
    monkeypatch.setattr(errors, "_platform_kinds", {})
    monkeypatch.setattr(errors, "_platform_untreatable", {})
    monkeypatch.setattr(errors, "_study_kinds", {})


@pytest.fixture
def study_dir(tmp_path) -> Path:
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    return copy


def edit(study: Path, old: str, new: str) -> Path:
    path = study / "study.toml"
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return path


def declare(study: Path, kind: str) -> None:
    """Make the relay-down condition declare `kind` as its treatment."""
    edit(study, TREATMENT, f'infrastructure_is_treatment = ["{kind}"]')


def list_platforms(study: Path, *modules: str) -> Path:
    listed = ", ".join(f'"{m}"' for m in modules)
    return edit(study, PLUGIN_LINE, f"{PLUGIN_LINE}platforms = [{listed}]\n")


def add_kinds(study: Path, body: str) -> Path:
    return edit(study, CODE_END, f"{CODE_END}\n[study.kinds]\n{body}\n")


def toy_platform(tmp_path: Path, monkeypatch, *registrations: str) -> str:
    """Write a platform module that registers kinds when imported, importable by a name no other test uses."""
    name = f"toy_platform_{uuid.uuid4().hex[:12]}"
    folder = tmp_path / "site"
    folder.mkdir(exist_ok=True)
    lines = ["from expcore.errors import register_infra_kind", *registrations]
    (folder / f"{name}.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(folder))
    return name


def refused(study: Path) -> str:
    with pytest.raises(ConfigError) as caught:
        load_study(study)
    return str(caught.value)


def test_c02_a_platform_module_named_in_platforms_makes_its_treatable_kind_declarable(
    study_dir, tmp_path, monkeypatch, registry
):
    """C02: study.toml is validated after the platforms it names are imported, so a kind a platform registers at
    import can be a condition's treatment; without the platform listed it is unknown."""
    module = toy_platform(tmp_path, monkeypatch, 'register_infra_kind("relay_flap", "the relay restarted")')
    declare(study_dir, "relay_flap")
    assert "unknown infrastructure kind 'relay_flap'" in refused(study_dir)
    list_platforms(study_dir, module)
    study = load_study(study_dir)
    assert study.schema.treatment_kinds["relay-down"] == ("relay_flap",)
    assert "relay_flap" in errors.infra_kinds()
    assert load_study(study_dir).schema.digest == study.schema.digest  # loading again changes nothing


def test_c02_a_study_declares_its_own_kind_in_study_kinds_and_may_load_again(study_dir, registry):
    """C02: a kind the study declares is registered before its conditions are validated, so it can be a treatment;
    loading the same study again in one process is not a clash with itself."""
    declare(study_dir, "relay_flap")
    add_kinds(study_dir, 'relay_flap = { description = "the relay restarted", treatable = true }')
    first = load_study(study_dir)
    assert first.schema.treatment_kinds["relay-down"] == ("relay_flap",)
    assert errors.is_treatable("relay_flap")
    assert load_study(study_dir).schema.digest == first.schema.digest


@pytest.mark.parametrize(
    ("declared", "fragment"),
    [
        (
            'endpoint = { description = "x" }',
            "key 'endpoint' cannot be declared: infrastructure kind 'endpoint' is one",
        ),
        (
            'relay_flap = { description = "x" }',
            "key 'relay_flap' cannot be declared: infrastructure kind 'relay_flap' is a platform's kind",
        ),
        ('Bad-Kind = { description = "x" }', "key 'Bad-Kind' cannot be declared: infrastructure kind 'Bad-Kind' must"),
        ("relay_gone = { treatable = true }", "[study.kinds.relay_gone]: key 'description' is required"),
        ('relay_gone = { description = "x", treatable = "yes" }', "key 'treatable' must be true or false"),
        ('relay_gone = { description = "x", scored = true }', "key 'scored' is not a known key"),
    ],
)
def test_c02_a_study_kind_that_clashes_with_a_core_or_platform_kind_or_is_malformed_is_refused(
    study_dir, tmp_path, monkeypatch, registry, declared, fragment
):
    """C02: a study's kind is its own; one that is a core kind or a platform's kind is refused, naming the file and the
    key, as is a declaration that cannot be read."""
    module = toy_platform(tmp_path, monkeypatch, 'register_infra_kind("relay_flap", "the relay restarted")')
    list_platforms(study_dir, module)
    toml = add_kinds(study_dir, declared)
    message = refused(study_dir)
    assert message.startswith(f"{toml}: [study.kinds"), message
    assert fragment in message, message


def test_c02_a_known_kind_that_is_not_treatable_can_never_be_a_treatment_and_is_not_called_unknown(
    study_dir, tmp_path, monkeypatch, registry
):
    """C02: a platform's non-treatable kind, made known by listing the platform, is refused as a kind that can never
    be a treatment, not as an unknown one; so is a study's own kind declared not treatable."""
    module = toy_platform(
        tmp_path,
        monkeypatch,
        'register_infra_kind("gate_tamper", "the gate refused an answer it could not pass", treatable=False)',
    )
    list_platforms(study_dir, module)
    declare(study_dir, "gate_tamper")
    message = refused(study_dir)
    assert "key 'infrastructure_is_treatment' names infrastructure kind 'gate_tamper', which can never be" in message
    assert "unknown" not in message
    edit(study_dir, '["gate_tamper"]', '["relay_audit"]')
    add_kinds(study_dir, 'relay_audit = { description = "the relay failed its audit", treatable = false }')
    assert "'relay_audit', which can never be a treatment: the relay failed its audit" in refused(study_dir)


def test_c02_a_platform_module_that_cannot_be_imported_is_refused_naming_the_file_and_the_key(study_dir, registry):
    """C02: a platform that is not installed, or fails on import, is refused before anything is validated against
    it, naming study.toml, the key and the module."""
    toml = list_platforms(study_dir, "no_such_platform_module")
    message = refused(study_dir)
    assert message.startswith(f"{toml}: [study]: key 'platforms' names module 'no_such_platform_module'"), message
    assert "cannot be imported (ModuleNotFoundError" in message
    edit(study_dir, 'platforms = ["no_such_platform_module"]', "platforms = [3]")
    assert "key 'platforms' must be an array of non-empty strings" in refused(study_dir)


def test_c06_a_study_that_lists_platforms_and_kinds_but_declares_none_keeps_its_schema_digest(study_dir, registry):
    """C06: the two keys add to the vocabulary and are not part of the factor schema, so a study that uses neither,
    or lists them without declaring one of their kinds, has the schema digest it had."""
    before = load_study(study_dir).schema.digest
    list_platforms(study_dir, "json")
    add_kinds(study_dir, 'relay_flap = { description = "the relay restarted" }')
    assert load_study(study_dir).schema.digest == before
