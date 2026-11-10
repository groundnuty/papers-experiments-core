"""Importing a plugin by location: the study's own code is held to its identity, the environment's code never is.

The core's own packages and every installed distribution, editable or not, are covered by the recorded environment,
so a study directory that holds them (a repository root with the core checked out, an editable dependency vendored
inside the study) neither refuses them nor drops them from the process.
"""

import importlib
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from package_support import installed_distribution

import expcore
from expcore.config.loading import ConfigError
from expcore.provenance.codestamp import CodeScope
from expcore.study import plugin_import
from expcore.study.config import load_study
from expcore.study.plugin import StudyContext, import_plugin_module, load_plugin

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "tiny_study"
FACTS_LINE = 'FACTS = {"lake": "deep", "river": "long", "hill": "green", "dune": "moving"}\n'
HOLDER = Path(expcore.__file__).resolve().parents[1]  # the directory that holds the core: src/ of a checkout


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def example_copy(base: Path, name: str = "study") -> Path:
    copy = base / name
    shutil.copytree(EXAMPLE, copy, ignore=shutil.ignore_patterns("out", "__pycache__"))
    return copy


def context(study_dir: Path) -> StudyContext:
    return StudyContext(load_study(study_dir), {"commit": "a" * 40})


def core_modules() -> dict[str, object]:
    return {name: module for name, module in sys.modules.items() if name.split(".", 1)[0] == "expcore"}


def load_beside_the_core(tmp_path: Path) -> None:
    """Import a plugin of a study whose directory holds the core's own code, as a repository root with the core
    checked out and installed editable does."""
    plugin = tmp_path / "beside-core" / "plugin.py"
    plugin.parent.mkdir(exist_ok=True)
    plugin.write_text("import expcore.stats.pairing  # the core, imported from inside the study directory\n", "utf-8")
    study = SimpleNamespace(root=HOLDER, plugin_file=plugin, scope=CodeScope.of(["pyproject.toml"]))
    import_plugin_module(study)


def test_p02_a_study_directory_that_holds_the_core_loads_and_keeps_the_core_in_the_process(tmp_path):
    """P02: adds that the core's own modules are the environment's code wherever they lie: a study whose directory
    holds them loads, and none of them is refused as uncovered or dropped, the plugin importer above all."""
    before, importer = core_modules(), sys.modules["expcore.study.plugin_import"]
    load_beside_the_core(tmp_path)
    assert all(sys.modules.get(name) is module for name, module in before.items())
    assert sys.modules["expcore.study.plugin_import"] is importer


def test_p02_an_editable_dependency_inside_the_study_is_the_environment_s_code(tmp_path, monkeypatch):
    """P02: adds that a distribution installed editable from a directory inside the study is covered by the recorded
    environment, not by `[study] code`: the plugin that imports it loads, and the module stays."""
    study_dir = example_copy(tmp_path)
    vendor = study_dir / "vendor"
    (vendor / "toykit").mkdir(parents=True)
    (vendor / "toykit" / "__init__.py").write_text('ANSWER = "deep"\n', encoding="utf-8")
    url = json.dumps({"url": vendor.as_uri(), "dir_info": {"editable": True}})
    installed_distribution(tmp_path / "site", "toykit", "1.0", {}, direct_url=url, checkout=vendor)
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    monkeypatch.syspath_prepend(str(vendor))  # what the install's .pth does at start-up
    monkeypatch.delitem(sys.modules, "toykit", raising=False)
    edit(study_dir / "plugin.py", '"""The plugin of the tiny study', 'import toykit\n\n"""The plugin of the tiny study')
    try:
        load_plugin(context(study_dir))
        assert sys.modules["toykit"].ANSWER == "deep"
    finally:
        sys.modules.pop("toykit", None)


def test_p02_one_audit_hook_is_installed_in_a_process_however_many_studies_load(tmp_path, monkeypatch):
    """P02: adds that the importer is never dropped, so loading studies, one beside the core among them, never
    installs a second audit hook: hooks cannot be removed, and each would watch nothing but its own copy's state."""
    plugin_import.install()
    added: list[object] = []
    monkeypatch.setattr(sys, "addaudithook", added.append)
    study_dir = example_copy(tmp_path)
    for _ in range(3):
        load_beside_the_core(tmp_path)
        importlib.import_module("expcore.study.plugin").load_plugin(context(study_dir))
    assert added == []
    assert sys.modules["expcore.study.plugin_import"] is plugin_import


def test_p02_overlapping_watches_each_keep_their_own_record(tmp_path):
    """P02: adds that a watch is registered by identity, so one that ends inside another, empty as both may be,
    does not end the other's record."""
    script = tmp_path / "ran.py"
    script.write_text("ran = True\n", encoding="utf-8")
    code = compile(script.read_text(encoding="utf-8"), str(script), "exec")
    with plugin_import.watching() as outer:
        with plugin_import.watching() as inner:
            assert inner == outer == set()
        exec(code, {})
    assert str(script) in outer


def test_p02_a_plugin_listed_through_a_symlink_inside_the_study_is_covered(tmp_path):
    """P02: adds that coverage compares resolved paths on both sides: a plugin that `[study] plugin` and
    `[study] code` name by a link inside the study is the file the link resolves to, and loads."""
    study_dir = example_copy(tmp_path)
    real = study_dir / "real"
    real.mkdir()
    (study_dir / "plugin.py").rename(real / "plugin.py")
    shutil.copy(study_dir / "prompt.txt", real / "prompt.txt")  # the plugin reads its prompt from beside its file
    (study_dir / "plugin.py").symlink_to(Path("real") / "plugin.py")
    plugin = load_plugin(context(study_dir))
    assert Path(sys.modules[type(plugin.executor).__module__].__file__) == real / "plugin.py"


def test_p02_the_fix_for_a_plugin_in_a_subdirectory_names_the_module_from_the_plugin_s_own_directory(
    tmp_path, monkeypatch
):
    """P02: adds that the refusal of a module found through the search path suggests the relative import as the
    plugin would write it, from its own directory (`from .facts import ...`), not from the study's."""
    study_dir = example_copy(tmp_path)
    plugins = study_dir / "plugins"
    plugins.mkdir()
    (study_dir / "plugin.py").rename(plugins / "plugin.py")
    shutil.copy(study_dir / "prompt.txt", plugins / "prompt.txt")
    (plugins / "facts.py").write_text(FACTS_LINE, encoding="utf-8")
    edit(plugins / "plugin.py", FACTS_LINE, "from facts import FACTS\n")
    edit(study_dir / "study.toml", 'plugin = "plugin.py"', 'plugin = "plugins/plugin.py"')
    edit(study_dir / "study.toml", '"plugin.py", "prompt.txt"]', '"plugins", "prompt.txt"]')
    monkeypatch.syspath_prepend(str(plugins))
    with pytest.raises(ConfigError) as caught:
        load_plugin(context(study_dir))
    assert "'plugins/facts.py' runs from the study directory outside the plugin's package" in str(caught.value)
    assert "(from .facts import ...)" in str(caught.value)
    assert "facts" not in sys.modules
