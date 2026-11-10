"""The fault injector's kinds come from the platform layer's one module of kinds, the module a study lists in
`[study] platforms`, so a study declares `provider_outage`, and `check` knows `injector_failure`, by importing it
alone."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from expcore.config.loading import ConfigError
from expcore.study.config import load_study

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
KINDS_MODULE = "expcore_onedata.sandbox.kinds"
TREATMENT = 'infrastructure_is_treatment = ["knowledge"]'


def study_declaring(tmp_path: Path, treatment: str, *, platforms: bool = True) -> Path:
    """The example study, its relay-down condition declaring `treatment`, listing the Onedata kinds module or not."""
    study = tmp_path / "study"
    shutil.copytree(EXAMPLE, study, ignore=shutil.ignore_patterns("out", "__pycache__"))
    path = study / "study.toml"
    text = path.read_text(encoding="utf-8").replace(TREATMENT, f"infrastructure_is_treatment = {treatment}", 1)
    if platforms:
        text = text.replace('plugin = "plugin.py"\n', f'plugin = "plugin.py"\nplatforms = ["{KINDS_MODULE}"]\n', 1)
    path.write_text(text, encoding="utf-8")
    return study


def test_c02_importing_the_one_kinds_module_alone_registers_the_fault_injectors_kinds():
    """C02: in a fresh process, the module a study lists in [study] platforms is all it takes: `provider_outage` is a
    treatable kind and `injector_failure` a known one that can never be a treatment; neither is known before."""
    code = (
        "from expcore.errors import infra_kinds, is_treatable\n"
        "before = {'provider_outage', 'injector_failure'} & set(infra_kinds())\n"
        f"import {KINDS_MODULE}\n"
        "import sys\n"
        "assert not [m for m in sys.modules if m.startswith('expcore_onedata.fault')], 'a fault module was imported'\n"
        "print(sorted(before), is_treatable('provider_outage'), is_treatable('injector_failure'))\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[] True False"


def test_c02_a_study_that_lists_the_kinds_module_declares_a_provider_outage_with_its_target(tmp_path):
    """C02: with the module listed, a condition declares `provider_outage` with the target it takes down."""
    study = load_study(study_declaring(tmp_path, '[{ kind = "provider_outage", target = "providers[1]" }]'))
    assert study.schema.treatment_kinds["relay-down"] == ("provider_outage",)
    assert study.schema.treatment_targets["relay-down"] == {"provider_outage": "providers[1]"}


def test_c02_the_injectors_own_failure_is_refused_as_a_treatment_as_one_that_never_can_be_not_as_unknown(tmp_path):
    """C02: `injector_failure` is known once the module is listed, and refused because a run whose fault was never
    verified says nothing about the outage, with the reason its registration gives."""
    with pytest.raises(ConfigError) as caught:
        load_study(study_declaring(tmp_path, '["injector_failure"]'))
    message = str(caught.value)
    assert "names infrastructure kind 'injector_failure', which can never be a treatment" in message
    assert "could not inject, clear or verify its fault" in message
    assert "unknown" not in message


def test_c02_without_the_module_listed_a_fresh_process_does_not_know_a_provider_outage(tmp_path):
    """C02: study.toml is validated before the plugin is imported, so a study that declares `provider_outage` without
    listing the module is refused, in a process that has not imported the platform layer, as naming an unknown kind."""
    study = study_declaring(tmp_path, '[{ kind = "provider_outage", target = "providers[1]" }]', platforms=False)
    code = (
        "from expcore.config.loading import ConfigError\n"
        "from expcore.study.config import load_study\n"
        "try:\n"
        f"    load_study({str(study)!r})\n"
        "except ConfigError as error:\n"
        "    print(error)\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert "unknown infrastructure kind 'provider_outage'" in done.stdout
    assert "[study] platforms" in done.stdout
