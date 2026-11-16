"""C11: a study cannot build a platform it did not list, whatever its plugin wraps.

The scheduler refuses a deferral that a run's admission or health check declares from a module `[study] platforms` does
not list, and the core reads a listed platform's declaration from its own configuration. A plugin that wraps both
objects, hides both declarations and lists nothing escapes both. It cannot build a platform without the core's own
loader running, though: `load_platform` and `load_faults` record the platform module and the file in a registry scoped
to the study's build (`expcore.config.platform_loads`), and the scheduler refuses, when the campaign starts, a record of
a module the study does not list, naming the module. A platform layer other than the Onedata one records through
`record_platform_load`.
"""

import shutil
import uuid
from pathlib import Path

import pytest
from study_support import EXAMPLE, HEAD

from expcore.campaign.healthgate import PlatformDeferral
from expcore.config.loading import ConfigError
from expcore.config.platform_loads import record_platform_load
from expcore.provenance.codestamp import write_stamp
from expcore.study.config import load_study
from expcore.study.pipeline import run_study
from expcore.study.platform_deferral import PLATFORM_HOOK, platform_deferrals
from expcore_onedata.config import load_platform
from expcore_onedata.testing import write_platform

ONEDATA = "expcore_onedata.sandbox.kinds"  # the module a study lists for the Onedata layer
ORIGINAL = "toy_original_platform.kinds"  # a platform module of another layer, which a study's own module may re-export
PLUGIN_LINE = 'plugin = "plugin.py"\n'
# What a study's plugin adds to build the platform through the core's own loader and to hide everything it declares: a
# closure around the health check and an admission of its own, so neither carries a `max_deferral_s` or a
# `declared_deferral_s`.
HIDING = """

import contextlib as _contextlib
import dataclasses as _dataclasses

_build_before_hiding = build


def build(context):
    plugin = _build_before_hiding(context)
    {load}
    inner = plugin.health

    def hidden():  # a closure: nothing of the platform's health check is visible through it
        return inner()

    def admission(treatment_kinds, treatment_targets, cancel, exclusive):  # no declared_deferral_s either
        return _contextlib.nullcontext("shared")

    return _dataclasses.replace(plugin, health=hidden, admission=admission)
"""
ONEDATA_LOAD = (
    "from expcore_onedata.config import load_platform\n"
    '    load_platform(context.study.root / "platform.toml", require_secrets=False)'
)


def toy_load(module: str) -> str:
    """A plugin's line that loads a platform of another layer, which records through `record_platform_load`."""
    return (
        "from expcore.config.platform_loads import record_platform_load\n"
        f'    record_platform_load({module!r}, context.study.root / "platform.toml")'
    )


def hiding(load: str = "pass") -> str:
    return HIDING.replace("{load}", load)


def study_with(tmp_path: Path, *modules: str, onedata_file: bool = True) -> Path:
    """A copy of the example listing `modules`, with a loadable Onedata platform.toml beside study.toml, stamped as a
    snapshot made without .git is."""
    copy = tmp_path / "study"
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    toml = copy / "study.toml"
    listed = ", ".join(f'"{m}"' for m in modules)
    extra = f"platforms = [{listed}]\n" if modules else ""
    toml.write_text(toml.read_text(encoding="utf-8").replace(PLUGIN_LINE, PLUGIN_LINE + extra, 1), encoding="utf-8")
    if onedata_file:
        write_platform(copy)
    return copy


def with_plugin(study: Path, addition: str) -> Path:
    plugin = study / "plugin.py"
    plugin.write_text(plugin.read_text(encoding="utf-8") + addition, encoding="utf-8")
    write_stamp(study, HEAD, load_study(study).scope)
    return study


def platform_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str) -> str:
    """Write a platform module of a study's own, importable by a name no other test uses."""
    name = f"toy_loader_platform_{uuid.uuid4().hex[:12]}"
    folder = tmp_path / "site"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{name}.py").write_text(source, encoding="utf-8")
    monkeypatch.syspath_prepend(str(folder))
    return name


def refused_unstarted(study: Path) -> str:
    """Run the study, expect the scheduler to refuse it before it opens its store, and return the refusal."""
    with pytest.raises(ConfigError) as caught:
        run_study(study)
    assert not (study / "out" / "state.sqlite").exists()
    return str(caught.value)


def test_c11_a_plugin_that_hides_both_declarations_and_lists_nothing_is_refused_naming_the_module(tmp_path):
    """C11: the study's plugin builds the Onedata platform through the core's loader, wraps its health check and its
    admission so neither declares anything, and lists no platform: nothing visible names the module, yet the loader
    recorded it, so the run is refused when the scheduler is built, before its store, naming the module and saying to
    list it."""
    study = with_plugin(study_with(tmp_path), hiding(ONEDATA_LOAD))
    text = refused_unstarted(study)
    for part in (repr(ONEDATA), "[study] platforms", "list it", "platform.toml"):
        assert part in text, text
    assert str(tmp_path) not in text, text  # the file's name, never its directory


def test_c11_the_same_plugin_with_the_module_listed_is_accepted(tmp_path):
    """C11: listing the module is the whole fix: the core reads the platform's own configuration through it (a
    platform.toml with no `[faults]` declares no deferral), and the campaign runs."""
    study = with_plugin(study_with(tmp_path, ONEDATA), hiding(ONEDATA_LOAD))
    assert set(run_study(study).statuses) == {"done"}


def test_c11_a_study_that_uses_no_platform_is_unaffected(tmp_path):
    """C11: no loader runs in the build of a study with no platform, so there is nothing to list: it runs with the same
    wrappers, with or without a platform.toml beside it."""
    for name, file in (("with", True), ("without", False)):
        study = with_plugin(study_with(tmp_path / name, onedata_file=file), hiding())
        assert set(run_study(study).statuses) == {"done"}, name


def test_c11_two_study_builds_in_one_process_do_not_inherit_each_others_entries(tmp_path):
    """C11: a study refused for an unlisted module leaves nothing for the next one in the process, which uses no
    platform and starts; and one that lists the module and runs leaves nothing for one that does not list it."""
    refused = with_plugin(study_with(tmp_path / "refused"), hiding(ONEDATA_LOAD))
    refused_unstarted(refused)
    plain = with_plugin(study_with(tmp_path / "plain"), hiding())
    assert set(run_study(plain).statuses) == {"done"}
    listed = with_plugin(study_with(tmp_path / "listed", ONEDATA), hiding(ONEDATA_LOAD))
    assert set(run_study(listed).statuses) == {"done"}
    again = with_plugin(study_with(tmp_path / "again"), hiding())
    assert set(run_study(again).statuses) == {"done"}


def test_c11_a_loader_run_outside_a_study_build_is_not_checked(tmp_path):
    """C11: a platform's own tooling (the day-0 tools, a federation script) loads the platform with no study build
    open, in the process that later starts a study that lists nothing: the call is no part of the study."""
    study = with_plugin(study_with(tmp_path), hiding())
    load_platform(study / "platform.toml", require_secrets=False)
    record_platform_load(ONEDATA, study / "platform.toml")
    assert set(run_study(study).statuses) == {"done"}


def test_c11_a_platform_of_another_layer_records_through_the_same_registry(tmp_path):
    """C11: a platform layer other than the Onedata one calls `record_platform_load` from its loader, and a study that
    builds it without listing its module is refused, naming that module."""
    study = with_plugin(study_with(tmp_path, onedata_file=False), hiding(toy_load(ORIGINAL)))
    text = refused_unstarted(study)
    assert repr(ORIGINAL) in text, text
    assert ONEDATA not in text, text


def test_c11_a_listed_module_that_does_not_speak_for_the_platform_built_covers_nothing(tmp_path, monkeypatch):
    """C11: a study lists a platform module of its own whose function loads nothing, and builds another platform: the
    listed module is no cover for it, and the refusal names the module the build loaded, not the listed one."""
    listed = platform_module(tmp_path / "a", monkeypatch, f"def {PLATFORM_HOOK}(path):\n    return None\n")
    study = study_with(tmp_path / "s", listed, onedata_file=False)
    (study / "platform.toml").write_text("# the toy platform's own settings\n", encoding="utf-8")
    text = refused_unstarted(with_plugin(study, hiding(toy_load(ORIGINAL))))
    assert repr(ORIGINAL) in text, text
    assert listed not in text, text


def test_c11_a_listed_module_that_loaded_the_platform_for_its_answer_speaks_for_it_even_when_it_answers_none(
    tmp_path, monkeypatch
):
    """C11: the study lists a module of its own that re-exports a platform's function and loads that platform's
    configuration to answer (a heal off answers None, which names no module). What the module's function loaded is the
    platform it speaks for, so building that platform is covered and the study is not refused."""
    speaking = platform_module(
        tmp_path / "a",
        monkeypatch,
        "from expcore.config.platform_loads import record_platform_load\n"
        f"def {PLATFORM_HOOK}(path):\n    record_platform_load({ORIGINAL!r}, path)\n    return None\n",
    )
    study = with_plugin(study_with(tmp_path / "s", speaking, onedata_file=False), hiding(toy_load(ORIGINAL)))
    (study / "platform.toml").write_text("# the toy platform's own settings\n", encoding="utf-8")
    assert set(run_study(study).statuses) == {"done"}


def test_c11_the_core_names_what_each_platforms_function_loaded_beside_its_answer(tmp_path, monkeypatch):
    """C11: `platform_deferrals` says, for each listed platform, which modules its function ran a loader for (empty
    for a function that loads nothing), so the scheduler knows what a listed module speaks for."""
    loading = platform_module(
        tmp_path / "a",
        monkeypatch,
        "from expcore.config.platform_loads import record_platform_load\n"
        f"def {PLATFORM_HOOK}(path):\n    record_platform_load({ORIGINAL!r}, path)\n    return None\n",
    )
    silent = platform_module(tmp_path / "b", monkeypatch, f"def {PLATFORM_HOOK}(path):\n    return None\n")
    study = study_with(tmp_path / "s", loading, silent, onedata_file=False)
    (study / "platform.toml").write_text("# the toy platform's own settings\n", encoding="utf-8")
    declared = platform_deferrals(load_study(study))
    assert declared == (PlatformDeferral(loading, None, (ORIGINAL,)), PlatformDeferral(silent, None, ()))
