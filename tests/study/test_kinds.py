"""Infrastructure kinds a study can declare as treatment: the platforms study.toml names, and the study's own kinds.

Both are known before study.toml's conditions are validated: `[study] platforms` names modules imported first, so their
`register_infra_kind` calls have run, and `[study.kinds]` declares the study's own.
"""

import json
import re
import shutil
import uuid
from pathlib import Path

import pytest
from study_support import HEAD, clean_environment, cli

from expcore import errors
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import write_stamp
from expcore.replay.check import CHECK_FILE
from expcore.study.config import load_study
from expcore.study.pipeline import analyse_study, run_study

__all__ = ["clean_environment"]  # a fixture the tests below use, imported from the support module

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
    """C02: a kind the study declares is known while its conditions are validated, so it can be a treatment; loading
    the same study again in one process is not a clash with itself, and the loaded study carries what it declared."""
    declare(study_dir, "relay_flap")
    add_kinds(study_dir, 'relay_flap = { description = "the relay restarted", treatable = true }')
    first = load_study(study_dir)
    assert first.schema.treatment_kinds["relay-down"] == ("relay_flap",)
    assert first.kinds == {"relay_flap": errors.StudyKind("the relay restarted", True)}
    assert load_study(study_dir).schema.digest == first.schema.digest


def another_study(tmp_path: Path, name: str) -> Path:
    return Path(shutil.copytree(EXAMPLE, tmp_path / name, ignore=shutil.ignore_patterns("out", "__pycache__")))


def test_c02_a_study_s_own_kind_is_unknown_to_another_study_in_the_same_process(study_dir, tmp_path, registry):
    """C02: a study's kinds are its own: when it has loaded they are gone, so a second study in the process that names
    one without declaring it is refused as it would be alone."""
    declare(study_dir, "relay_flap")
    add_kinds(study_dir, 'relay_flap = { description = "the relay restarted" }')
    load_study(study_dir)
    assert "relay_flap" not in errors.infra_kinds()
    other = another_study(tmp_path, "other")
    declare(other, "relay_flap")
    assert "unknown infrastructure kind 'relay_flap'" in refused(other)


def test_c02_two_studies_in_a_process_may_declare_one_name_each_with_its_own_meaning(study_dir, tmp_path, registry):
    """C02: nothing of one study's declaration binds another: the same name may have another description and another
    answer, and each study is refused or loaded by its own."""
    declare(study_dir, "relay_flap")
    add_kinds(study_dir, 'relay_flap = { description = "the relay restarted", treatable = true }')
    assert load_study(study_dir).schema.treatment_kinds["relay-down"] == ("relay_flap",)
    other = another_study(tmp_path, "other")
    declare(other, "relay_flap")
    add_kinds(other, 'relay_flap = { description = "the relay failed its audit", treatable = false }')
    assert "'relay_flap', which can never be a treatment: the relay failed its audit" in refused(other)
    third = another_study(tmp_path, "third")
    declare(third, "relay_flap")
    add_kinds(third, 'relay_flap = { description = "the relay was rebuilt", treatable = true }')
    assert load_study(third).kinds["relay_flap"].description == "the relay was rebuilt"


@pytest.mark.parametrize("failure", ["condition", "models"])
def test_c02_a_failed_load_leaves_none_of_the_study_s_kinds_behind(study_dir, registry, failure):
    """C02: whichever file refuses the study, what it declared is gone with the failed load."""
    add_kinds(study_dir, 'relay_flap = { description = "the relay restarted" }')
    if failure == "condition":
        declare(study_dir, "wifi")
    else:
        (study_dir / "models.toml").write_text("[models]\n", encoding="utf-8")
    refused(study_dir)
    assert errors.infra_kinds() == errors.CORE_KINDS


def test_c02_a_study_s_own_kind_is_a_treatment_through_run_check_and_analyze_and_is_gone_after(
    study_dir, registry, clean_environment
):
    """C02: every step of the pipeline holds the study's kinds for as long as it works: a run ends in the kind, the
    replay check in a fresh process accepts the record, the analysis scores it, and none of them leaves the kind."""
    plugin = study_dir / "plugin.py"
    text = plugin.read_text(encoding="utf-8")
    assert 'InfraError("knowledge"' in text
    plugin.write_text(text.replace('InfraError("knowledge"', 'InfraError("relay_flap"'), encoding="utf-8")
    declare(study_dir, "relay_flap")
    add_kinds(study_dir, 'relay_flap = { description = "the relay restarted", treatable = true }')
    write_stamp(study_dir, HEAD, load_study(study_dir).scope)
    summary = run_study(study_dir)
    assert summary.statuses == {"done": len(load_study(study_dir).manifest.rows)}, summary.statuses
    assert errors.infra_kinds() == errors.CORE_KINDS
    code, stderr = cli(clean_environment, "check", str(study_dir / "out" / "runs"), process=True)
    assert code == 0, stderr
    assert analyse_study(study_dir).rows
    assert errors.infra_kinds() == errors.CORE_KINDS


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


def set_scheduler(study: Path, key: str, value: str) -> Path:
    """Set one key of `[scheduler]` in the copy's campaign.toml, replacing the line if it has one."""
    path = study / "campaign.toml"
    text = re.sub(rf"^{key} = .*\n", "", path.read_text(encoding="utf-8"), flags=re.M)
    assert "[scheduler]\n" in text
    path.write_text(text.replace("[scheduler]\n", f'[scheduler]\n{key} = "{value}"\n', 1), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("key", "wrong", "meant"), [("health_kind", "sandbxo", "sandbox"), ("identity_kind", "endpiont", "endpoint")]
)
def test_c02_a_scheduler_kind_that_names_no_known_kind_is_refused_at_load_naming_the_file_and_the_key(
    study_dir, registry, key, wrong, meant
):
    """C02: a misspelt `health_kind` or `identity_kind` used to pass the load and crash `run` with a traceback that
    named neither file nor key; it is refused when the study is loaded, with the kinds that are known."""
    toml = set_scheduler(study_dir, key, wrong)
    message = refused(study_dir)
    assert message.startswith(f"{toml}: [scheduler]: key '{key}' names '{wrong}' (did you mean '{meant}'?)"), message
    assert "which is not an infrastructure kind" in message
    assert f"the kinds known here are: {', '.join(errors.CORE_KINDS)}" in message
    assert "[study] platforms" in message
    assert "[study.kinds]" in message


def test_c02_a_platform_kind_is_a_scheduler_kind_once_its_module_is_listed_and_the_choices_then_hold_it(
    study_dir, tmp_path, monkeypatch, registry
):
    """C02: the check follows `[study] platforms`: a kind only a platform registers is unknown until the module is
    listed, and once it is listed it is accepted and is among the choices an unknown kind is refused with."""
    module = toy_platform(tmp_path, monkeypatch, REGISTER_FLAP)
    set_scheduler(study_dir, "health_kind", "relay_flap")
    alone = refused(study_dir)
    assert "key 'health_kind' names 'relay_flap'" in alone
    assert "relay_flap" not in alone.split("the kinds known here are:")[1].split(". ")[0]
    list_platforms(study_dir, module)
    assert load_study(study_dir).campaign.scheduler.health_kind == "relay_flap"
    set_scheduler(study_dir, "identity_kind", "relay_flop")
    message = refused(study_dir)
    assert "key 'identity_kind' names 'relay_flop' (did you mean 'relay_flap'?)" in message
    assert "relay_flap" in message.split("the kinds known here are:")[1].split(". ")[0]


def test_c02_a_kind_the_study_declares_is_a_scheduler_kind_while_the_study_loads_and_is_gone_after(study_dir, registry):
    """C02: `[study.kinds]` is in the vocabulary while campaign.toml is validated, so the study's own kind can be the
    health kind, and it leaves with the load like any of the study's kinds."""
    add_kinds(study_dir, 'relay_flap = { description = "the relay restarted", treatable = true }')
    set_scheduler(study_dir, "health_kind", "relay_flop")
    assert "key 'health_kind' names 'relay_flop' (did you mean 'relay_flap'?)" in refused(study_dir)
    set_scheduler(study_dir, "health_kind", "relay_flap")
    assert load_study(study_dir).campaign.scheduler.health_kind == "relay_flap"
    assert errors.infra_kinds() == errors.CORE_KINDS


def test_c02_a_platform_module_that_cannot_be_imported_is_refused_naming_the_file_and_the_key(study_dir, registry):
    """C02: a platform that is not installed, or fails on import, is refused before anything is validated against
    it, naming study.toml, the key and the module."""
    toml = list_platforms(study_dir, "no_such_platform_module")
    message = refused(study_dir)
    assert message.startswith(f"{toml}: [study]: key 'platforms' names module 'no_such_platform_module'"), message
    assert "cannot be imported (ModuleNotFoundError" in message
    edit(study_dir, 'platforms = ["no_such_platform_module"]', "platforms = [3]")
    assert "key 'platforms' must be an array of non-empty strings" in refused(study_dir)


def test_r12_a_platform_import_failure_reports_the_module_and_the_exception_type_and_never_its_message(
    study_dir, tmp_path, monkeypatch, registry
):
    """R12: the text of an exception raised while a platform is imported can echo a credential, and a refusal is printed
    and logged, so it names the module as study.toml declares it and the exception's type, and keeps no cause."""
    credential = "toy-platform-credential-0123456789"
    module = toy_platform(tmp_path, monkeypatch, f'raise RuntimeError("login refused for {credential}")')
    list_platforms(study_dir, module)
    with pytest.raises(ConfigError) as caught:
        load_study(study_dir)
    message = str(caught.value)
    assert f"key 'platforms' names module {module!r}, which cannot be imported (RuntimeError)" in message
    assert credential not in message
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


def test_c06_a_study_that_lists_platforms_and_kinds_but_declares_none_keeps_its_schema_digest(study_dir, registry):
    """C06: the two keys add to the vocabulary and are not part of the factor schema, so a study that uses neither,
    or lists them without declaring one of their kinds, has the schema digest it had."""
    before = load_study(study_dir).schema.digest
    list_platforms(study_dir, "json")
    add_kinds(study_dir, 'relay_flap = { description = "the relay restarted" }')
    assert load_study(study_dir).schema.digest == before


GATE_VARIABLE = "TOY_REFUSE_IMPORT"
GATE_CREDENTIAL = "toy-gate-credential-0123456789"
RAISE_WHEN_GATED = (
    "import os",
    f'if os.environ.get("{GATE_VARIABLE}"):',
    f'    raise RuntimeError("login refused for {GATE_CREDENTIAL}")',
)
REGISTER_FLAP = 'register_infra_kind("relay_flap", "the relay restarted")'
PLATFORMS_NOTE = "importing the study's platforms ("


def ran_ending_in(study: Path, kind: str) -> None:
    """Make the relay-down condition's failure the kind, gate the plugin's import, and run the study to its end."""
    plugin = study / "plugin.py"
    text = plugin.read_text(encoding="utf-8")
    assert 'InfraError("knowledge"' in text
    gate = "\n".join(RAISE_WHEN_GATED)
    swapped = text.replace('InfraError("knowledge"', f'InfraError("{kind}"')
    plugin.write_text(f"{swapped}\n{gate}\n", encoding="utf-8")
    declare(study, kind)
    write_stamp(study, HEAD, load_study(study).scope)
    summary = run_study(study)
    assert summary.statuses == {"done": len(load_study(study).manifest.rows)}, summary.statuses


def gated_check(study: Path, tmp_path: Path, environment: dict[str, str]) -> tuple[int, str, dict]:
    """Run `check` as a fresh process in which a gated module raises at import; return its status, stderr and check."""
    gated = {**environment, GATE_VARIABLE: "1", "PYTHONPATH": str(tmp_path / "site")}
    code, stderr = cli(gated, "check", str(study / "out" / "runs"), process=True)
    written = study / "out" / "runs" / CHECK_FILE
    return code, stderr, json.loads(written.read_text(encoding="utf-8")) if written.is_file() else {}


def test_r03_a_platform_s_kind_on_core_backends_passes_a_check_that_imports_the_platform_and_never_the_plugin(
    study_dir, tmp_path, monkeypatch, registry, clean_environment
):
    """R03: a record names a kind only a platform registers, so a fresh `check` imports the modules study.toml names and
    says so in one line; it does not import the plugin, which raises here, because no backend needs it."""
    module = toy_platform(tmp_path, monkeypatch, REGISTER_FLAP)
    list_platforms(study_dir, module)
    ran_ending_in(study_dir, "relay_flap")
    code, stderr, check = gated_check(study_dir, tmp_path, clean_environment)
    assert code == 0, stderr
    assert check["failures"] == []
    assert check["n_ok"] == check["n_records"] > 0
    notes = [line for line in stderr.splitlines() if PLATFORMS_NOTE in line]
    assert len(notes) == 1, stderr
    assert module in notes[0]
    assert "'relay_flap'" in notes[0]


def test_r03_a_kind_the_study_declares_passes_a_check_that_imports_nothing(
    study_dir, tmp_path, monkeypatch, registry, clean_environment
):
    """R03: `[study.kinds]` is read as data, so a batch whose kinds are declared there imports neither the listed
    platform nor the plugin, both of which raise here."""
    module = toy_platform(tmp_path, monkeypatch, *RAISE_WHEN_GATED)
    list_platforms(study_dir, module)
    add_kinds(study_dir, 'relay_flap = { description = "the relay restarted", treatable = true }')
    ran_ending_in(study_dir, "relay_flap")
    code, stderr, check = gated_check(study_dir, tmp_path, clean_environment)
    assert code == 0, stderr
    assert check["failures"] == []
    assert PLATFORMS_NOTE not in stderr


def test_r03_a_platform_needed_for_a_kind_that_cannot_be_imported_is_refused_by_module_and_exception_type_only(
    study_dir, tmp_path, monkeypatch, registry, clean_environment
):
    """R03: the records need the platform and it will not import, so `check` refuses them; the text of the exception
    can hold a credential, so the refusal names only the module and the exception's type."""
    module = toy_platform(tmp_path, monkeypatch, *RAISE_WHEN_GATED, REGISTER_FLAP)
    list_platforms(study_dir, module)
    ran_ending_in(study_dir, "relay_flap")
    code, stderr, _ = gated_check(study_dir, tmp_path, clean_environment)
    assert code != 0
    assert PLATFORMS_NOTE in stderr
    assert f"key 'platforms' names module {module!r}, which cannot be imported (RuntimeError)" in stderr
    assert GATE_CREDENTIAL not in stderr
    assert "Traceback" not in stderr


def test_r03_a_kind_still_unknown_after_the_platforms_are_imported_fails_its_records(
    study_dir, tmp_path, monkeypatch, registry, clean_environment
):
    """R03: importing the listed platforms is all `check` does for a kind; one none of them registers fails the records
    that name it, in the check's own list of failures."""
    module = toy_platform(
        tmp_path, monkeypatch, "import os", f'if not os.environ.get("{GATE_VARIABLE}"):', f"    {REGISTER_FLAP}"
    )
    list_platforms(study_dir, module)
    ran_ending_in(study_dir, "relay_flap")
    code, stderr, check = gated_check(study_dir, tmp_path, clean_environment)
    assert code != 0
    assert PLATFORMS_NOTE in stderr
    assert check["failures"]
    assert all("unknown infrastructure kind 'relay_flap'" in failure["error"] for failure in check["failures"])
